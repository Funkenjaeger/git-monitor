"""The desktop-timeout incident, 2026-08-03.

collector.run_remote() always piped scan.py's bytes to the remote over stdin.
That is correct for a normal remote (nothing to install, no shell quoting to
get wrong) and silently wrong for a host whose sshd runs a forced-command
wrapper that execs its OWN installed copy of scan.py and never reads stdin:
Windows sshd's stdin pipe buffer fills and the ssh session hangs until the
whole-scan timeout. The desktop target sat in that exact deadlock for ~24
consecutive cycles -- confirmed by running the identical ssh command with
stdin closed, which completed in 15.7s with 32 repos of valid JSON.

Two things made a 24-hour outage look like a mystery instead of an obvious
"desktop's sshd doesn't read stdin" bug:

1. `scan_target`'s `except Exception as exc: return False, str(exc)[:400]`
   sliced a `subprocess.TimeoutExpired`'s message from the head. That message
   opens with the full ssh argv repr (`Command '[...]'`), including the
   base64-encoded scan config, and `str()` on the exception never mentions its
   own class name. A real timeout's first 400 chars land entirely inside the
   base64 blob -- no type, no "timed out", nothing actionable stored anywhere.
2. One failed cycle flipped `reachable` straight to 0. A machine that is
   merely having a slow night looks identical, in the DB and on the
   dashboard, to one that is genuinely down.

This file covers the three fixes: a per-target `remote_script: installed`
option that skips piping the script (collector.run_remote), error shaping
that keeps the exception type and the TAIL of the message through truncation
(collector._shape_error / _tail_truncate / _middle_truncate), and a
2-consecutive-failure debounce before a machine is marked OFFLINE
(storage.mark_unreachable / save_scan).
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import collector
import storage


# ---------------------------------------------------------------------------
# Fix 2: error shaping survives truncation
# ---------------------------------------------------------------------------

class ErrorShapingSurvivesTruncation(unittest.TestCase):

    def _timeout_expired(self, blob_len=300, timeout=120):
        """A TimeoutExpired shaped like the one run_remote actually raises:
        an ssh argv list whose last element is the base64 scan config."""
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
               "-o", "StrictHostKeyChecking=accept-new", "-i", "/data/id_ed25519",
               "-o", "UserKnownHostsFile=/data/known_hosts",
               "evand@192.168.1.222", "python", "-", "A" * blob_len]
        return subprocess.TimeoutExpired(cmd, timeout)

    def test_naive_slice_loses_the_message(self):
        """Prove the bug this file exists for actually reproduces, so a
        regression that reintroduces it fails loudly here rather than in a
        24-cycle outage again."""
        exc = self._timeout_expired()
        naive = str(exc)[:400]
        self.assertNotIn("timed out", naive)
        self.assertNotIn("TimeoutExpired", naive)

    def test_type_name_and_tail_survive(self):
        exc = self._timeout_expired()
        shaped = collector._shape_error(exc)
        self.assertLessEqual(len(shaped), 400)
        self.assertTrue(shaped.startswith("TimeoutExpired: "))
        self.assertIn("timed out after 120 seconds", shaped)

    def test_survives_even_with_a_much_bigger_blob(self):
        exc = self._timeout_expired(blob_len=5000)
        shaped = collector._shape_error(exc)
        self.assertLessEqual(len(shaped), 400)
        self.assertTrue(shaped.startswith("TimeoutExpired: "))
        self.assertIn("timed out after 120 seconds", shaped)

    def test_short_message_is_untouched(self):
        shaped = collector._shape_error(ValueError("short"))
        self.assertEqual(shaped, "ValueError: short")

    def test_calledprocesserror_style_stderr_keeps_prefix_and_tail(self):
        """run_remote's rc!=0 branch: a Python traceback on the remote puts
        the real error on its LAST line, so the tail -- not the head -- has
        to survive."""
        stderr = ("Traceback (most recent call last):\n"
                   + "  File \"scan.py\", line 42, in <module>\n" * 30
                   + "ValueError: dubious ownership in repository at "
                     "/home/evand/projects/thing\n")
        prefix = "ssh scan failed (rc=1): "
        shaped = prefix + collector._tail_truncate(stderr, 400 - len(prefix))
        self.assertLessEqual(len(shaped), 400)
        self.assertTrue(shaped.startswith(prefix))
        self.assertIn(
            "ValueError: dubious ownership in repository at "
            "/home/evand/projects/thing", shaped)

    def test_run_remote_rc_failure_message_keeps_tail(self):
        """End-to-end through run_remote itself, not just the helper."""
        stderr = ("noise " * 200 + "ValueError: the actual remote error").encode()
        proc = subprocess.CompletedProcess(
            args=["ssh"], returncode=1, stdout=b"", stderr=stderr)
        with mock.patch("collector.subprocess.run", return_value=proc):
            with self.assertRaises(RuntimeError) as cm:
                collector.run_remote(
                    {"ssh": "user@host", "name": "t"},
                    {"machine": "t"}, {})
        msg = str(cm.exception)
        self.assertLessEqual(len(msg), 400)
        self.assertTrue(msg.startswith("ssh scan failed (rc=1):"))
        self.assertIn("ValueError: the actual remote error", msg)

    def test_scan_target_stores_a_diagnosable_error(self):
        """The path actually used by collect_one/scan_target: a raised
        TimeoutExpired must come back identifiable, not just truncated."""
        with mock.patch("collector.subprocess.run",
                         side_effect=self._timeout_expired()):
            ok, err = collector.scan_target(
                {"ssh": "user@host", "name": "t"}, {})
        self.assertFalse(ok)
        self.assertLessEqual(len(err), 400)
        self.assertTrue(err.startswith("TimeoutExpired: "))
        self.assertIn("timed out after 120 seconds", err)


# ---------------------------------------------------------------------------
# Fix 1: remote_script: installed skips piping scan.py over stdin
# ---------------------------------------------------------------------------

class RemoteScriptOption(unittest.TestCase):

    def _run(self, target, defaults=None):
        proc = subprocess.CompletedProcess(
            args=["ssh"], returncode=0, stdout=b'{"repos": []}', stderr=b"")
        with mock.patch("collector.subprocess.run", return_value=proc) as run:
            collector.run_remote(target, {"machine": target["name"]},
                                 defaults or {})
        return run.call_args

    def test_default_still_pipes_the_script(self):
        """Every existing target (cncpc, elspi, ...) relies on this path --
        it must not change just because the option now exists."""
        with open(collector.SCAN_PY, "rb") as fh:
            scan_bytes = fh.read()
        call = self._run({"ssh": "user@host", "name": "t"})
        self.assertEqual(call.kwargs["input"], scan_bytes)
        self.assertGreater(len(call.kwargs["input"]), 0)

    def test_installed_sends_no_stdin(self):
        call = self._run({"ssh": "user@host", "name": "t",
                           "remote_script": "installed"})
        self.assertEqual(call.kwargs["input"], b"")

    def test_installed_as_a_default_applies_without_a_per_target_override(self):
        call = self._run({"ssh": "user@host", "name": "t"},
                          defaults={"remote_script": "installed"})
        self.assertEqual(call.kwargs["input"], b"")

    def test_per_target_pipe_overrides_an_installed_default(self):
        call = self._run({"ssh": "user@host", "name": "t",
                           "remote_script": "piped"},
                          defaults={"remote_script": "installed"})
        self.assertGreater(len(call.kwargs["input"]), 0)


# ---------------------------------------------------------------------------
# Fix 3: two consecutive failures before OFFLINE
# ---------------------------------------------------------------------------

class OfflineDebounce(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.conn = storage.connect(os.path.join(self.dir, "t.db"))

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _machine(self, name="desktop"):
        row = self.conn.execute(
            "SELECT * FROM machines WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def test_a_single_failure_after_success_stays_reachable(self):
        storage.save_scan(self.conn, "desktop", "ssh", "python", {"repos": []})
        storage.mark_unreachable(self.conn, "desktop", "ssh", "python",
                                 "TimeoutExpired: ...")
        m = self._machine()
        self.assertEqual(m["reachable"], 1,
                         "one transient timeout must not mark a machine "
                         "OFFLINE -- that is the exact bug this fix is for")
        self.assertEqual(m["fail_streak"], 1)
        self.assertIn("TimeoutExpired", m["error"],
                     "the error must still be recorded even while debounced")

    def test_two_consecutive_failures_go_offline(self):
        storage.save_scan(self.conn, "desktop", "ssh", "python", {"repos": []})
        storage.mark_unreachable(self.conn, "desktop", "ssh", "python", "err1")
        storage.mark_unreachable(self.conn, "desktop", "ssh", "python", "err2")
        m = self._machine()
        self.assertEqual(m["reachable"], 0)
        self.assertEqual(m["fail_streak"], 2)
        self.assertEqual(m["error"], "err2")

    def test_a_success_between_failures_resets_the_streak(self):
        storage.save_scan(self.conn, "desktop", "ssh", "python", {"repos": []})
        storage.mark_unreachable(self.conn, "desktop", "ssh", "python", "err1")
        storage.save_scan(self.conn, "desktop", "ssh", "python", {"repos": []})
        m = self._machine()
        self.assertEqual(m["fail_streak"], 0)
        self.assertEqual(m["reachable"], 1)
        # Now a single failure again should NOT go offline -- the streak was
        # genuinely reset, not just decremented.
        storage.mark_unreachable(self.conn, "desktop", "ssh", "python", "err2")
        m = self._machine()
        self.assertEqual(m["reachable"], 1)
        self.assertEqual(m["fail_streak"], 1)

    def test_three_or_more_consecutive_failures_stay_offline(self):
        storage.save_scan(self.conn, "desktop", "ssh", "python", {"repos": []})
        for i in range(4):
            storage.mark_unreachable(self.conn, "desktop", "ssh", "python",
                                     "err%d" % i)
        m = self._machine()
        self.assertEqual(m["reachable"], 0)
        self.assertEqual(m["fail_streak"], 4)

    def test_a_machine_that_has_never_succeeded_is_offline_on_first_failure(self):
        """No prior state to debounce against -- unlike a flapping known-good
        host, a brand-new target that fails immediately has nothing to
        preserve."""
        storage.mark_unreachable(self.conn, "newbox", "ssh", "python", "err1")
        m = self._machine("newbox")
        self.assertEqual(m["reachable"], 0)
        self.assertEqual(m["fail_streak"], 1)

    def test_old_db_migrates_fail_streak_additively(self):
        """The deployed DB has live rows; this must load with no manual
        migration and without disturbing existing data."""
        self.conn.execute(
            "CREATE TABLE machines_old AS SELECT name, ssh, remote_python, "
            "reachable, last_scanned, last_success, error FROM machines")
        self.conn.execute("DROP TABLE machines")
        self.conn.execute(
            "ALTER TABLE machines_old RENAME TO machines")
        self.conn.execute(
            "INSERT INTO machines (name, ssh, remote_python, reachable, "
            "last_scanned, last_success, error) VALUES "
            "('legacy', 'ssh', 'python3', 1, 't', 't', NULL)")
        self.conn.commit()
        cols_before = {r["name"] for r in
                       self.conn.execute("PRAGMA table_info(machines)")}
        self.assertNotIn("fail_streak", cols_before)

        storage._migrate(self.conn)

        cols_after = {r["name"] for r in
                      self.conn.execute("PRAGMA table_info(machines)")}
        self.assertIn("fail_streak", cols_after)
        row = self.conn.execute(
            "SELECT * FROM machines WHERE name='legacy'").fetchone()
        self.assertEqual(row["fail_streak"], 0)
        self.assertEqual(row["reachable"], 1)


if __name__ == "__main__":
    unittest.main()
