"""The desktop scanner-skew gap, 2026-09-09.

e7f8a75 (the worktree-dedupe fix) landed on dserver and reached every `piped`
target immediately -- scan.py's bytes go over stdin on every scan, so a patch
to the collector's own copy is live everywhere on the next cycle. The desktop
target is the one exception: `remote_script: installed` (see config.example.yaml
and collector.run_remote's comment above `remote_script`), because its sshd
runs a ForceCommand wrapper that execs its OWN installed copy at
C:\\ProgramData\\ssh\\git-monitor-scan.py and never reads stdin -- piping the
script there just fills the pipe buffer (see test_reliability.py, the
desktop-timeout incident this option exists for). The desktop kept running a
2026-08-02 copy of scan.py for over a month after e7f8a75 landed, hand-refreshed
only once someone happened to notice. Nothing scanned for the skew itself, so
nothing would have caught the next one either.

The fix: scan.py reports a sha256 of its own running source (`scan_py_sha256`,
read off `__file__` -- see scan._self_sha256) in the payload it already
returns. collector.py compares that against its own copy of scan.py
(collector.local_scan_py_sha256) for `installed` targets only
(collector.scan_py_version_signal), and collect_one prints a warning to
stderr when the comparison finds a mismatch OR an absent version field.

Two properties this file exists to pin down, in the order the build order
gave them:

    (1) a `piped` target must NEVER raise this signal -- the collector
        supplies scan.py's own bytes for those, so the hashes agree by
        construction and comparing anyway is circular at best.
    (2) an ABSENT `scan_py_sha256` is UNKNOWN, not a pass -- an old scanner
        (the exact thing that went silently stale above) predates the field
        and so cannot report it; treating silence as agreement would make
        this check blind to its own motivating case.

    python -m unittest discover -s tests
"""

import io
import json
import os
import subprocess
import sys
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import collector
import scan
import storage


def _target(name="desktop", **kw):
    t = {"name": name, "ssh": "user@host", "roots": [{"path": "/x"}]}
    t.update(kw)
    return t


class ScanPyVersionSignal(unittest.TestCase):
    """Direct tests of collector.scan_py_version_signal -- the comparison
    itself, decoupled from collect_one's stderr side effect."""

    def setUp(self):
        self.local_hash = collector.local_scan_py_sha256()

    # (a) an `installed` target whose reported hash differs raises the signal
    def test_installed_target_with_differing_hash_raises_mismatch(self):
        target = _target(remote_script="installed")
        result = {"scan_py_sha256": "0" * 64}  # cannot collide with a real sha256
        self.assertNotEqual(result["scan_py_sha256"], self.local_hash)
        sig = collector.scan_py_version_signal(target, {}, result)
        self.assertEqual(sig, "mismatch")

    # (b) an `installed` target whose hash MATCHES raises nothing
    def test_installed_target_with_matching_hash_raises_nothing(self):
        target = _target(remote_script="installed")
        result = {"scan_py_sha256": self.local_hash}
        sig = collector.scan_py_version_signal(target, {}, result)
        self.assertIsNone(sig)

    # (c) a `piped` target with a differing hash raises NOTHING -- property 1
    def test_piped_target_with_differing_hash_raises_nothing(self):
        target = _target(remote_script="piped")
        result = {"scan_py_sha256": "0" * 64}
        self.assertNotEqual(result["scan_py_sha256"], self.local_hash)
        sig = collector.scan_py_version_signal(target, {}, result)
        self.assertIsNone(sig, "a piped target must never raise this signal "
                              "-- the collector supplied the bytes, so the "
                              "hashes agree by construction")

    def test_default_remote_script_is_piped_and_never_raises(self):
        """No `remote_script` key at all -- the common case for every target
        except the desktop -- must behave exactly like an explicit `piped`."""
        target = _target()
        result = {"scan_py_sha256": "0" * 64}
        sig = collector.scan_py_version_signal(target, {}, result)
        self.assertIsNone(sig)

    def test_local_target_never_raises(self):
        target = {"name": "collector-host", "ssh": "local", "roots": [{"path": "/x"}]}
        result = {"scan_py_sha256": "0" * 64}
        sig = collector.scan_py_version_signal(target, {}, result)
        self.assertIsNone(sig)

    # (d) an `installed` target reporting NO version field yields UNKNOWN,
    # never a pass
    def test_installed_target_with_no_version_field_is_unknown_not_a_pass(self):
        target = _target(remote_script="installed")
        result = {"repos": []}  # an old scanner: no scan_py_sha256 key at all
        sig = collector.scan_py_version_signal(target, {}, result)
        self.assertEqual(sig, "unknown")
        self.assertNotEqual(sig, None,
                             "an absent version field must not read as a "
                             "match -- that is precisely the case an old, "
                             "silently-stale scanner produces")

    def test_installed_target_with_empty_string_version_is_also_unknown(self):
        """Falsy-but-present is still 'nothing to compare', not a match."""
        target = _target(remote_script="installed")
        result = {"scan_py_sha256": ""}
        sig = collector.scan_py_version_signal(target, {}, result)
        self.assertEqual(sig, "unknown")

    def test_installed_as_a_default_applies_without_a_per_target_override(self):
        """Mirrors RemoteScriptOption's own coverage of this default in
        test_reliability.py -- the comparison must respect the same
        target/defaults precedence as run_remote's stdin-piping decision."""
        target = _target()
        result = {"scan_py_sha256": "0" * 64}
        sig = collector.scan_py_version_signal(
            target, {"remote_script": "installed"}, result)
        self.assertEqual(sig, "mismatch")


class CollectOneRaisesTheSignal(unittest.TestCase):
    """End to end through collect_one: the warning actually gets printed
    (or doesn't), and collect_one's return contract -- (ok, info, status),
    unchanged, relied on by app.py's run_scan and by CollectOneClassifiesTheFailure
    in test_uptime.py -- is undisturbed by any of it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.conn = storage.connect(os.path.join(self.dir, "t.db"))
        self.local_hash = collector.local_scan_py_sha256()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _collect(self, target, result):
        buf = io.StringIO()
        with mock.patch("collector.scan_target", return_value=(True, result)):
            with redirect_stderr(buf):
                outcome = collector.collect_one(self.conn, target, {})
        return outcome, buf.getvalue()

    def test_installed_mismatch_prints_a_warning(self):
        target = _target(remote_script="installed")
        outcome, err = self._collect(target, {"repos": [], "scan_py_sha256": "0" * 64})
        self.assertEqual(outcome, (True, 0, "ok"),
                          "the version signal must not change collect_one's "
                          "return contract")
        self.assertIn("desktop", err)
        self.assertIn("mismatch", err.lower())

    def test_installed_match_prints_nothing(self):
        target = _target(remote_script="installed")
        outcome, err = self._collect(
            target, {"repos": [], "scan_py_sha256": self.local_hash})
        self.assertEqual(outcome, (True, 0, "ok"))
        self.assertEqual(err, "")

    def test_piped_mismatch_prints_nothing(self):
        """Property 1, end to end: even a wildly wrong hash from a `piped`
        target must produce no warning at all."""
        target = _target(remote_script="piped")
        outcome, err = self._collect(target, {"repos": [], "scan_py_sha256": "0" * 64})
        self.assertEqual(outcome, (True, 0, "ok"))
        self.assertEqual(err, "")

    def test_installed_no_version_field_prints_an_unknown_warning(self):
        target = _target(remote_script="installed")
        outcome, err = self._collect(target, {"repos": []})
        self.assertEqual(outcome, (True, 0, "ok"))
        self.assertIn("unknown", err.lower())


class ScanStandaloneStillWellFormed(unittest.TestCase):
    """(e) scan.py still returns a well-formed payload when run standalone --
    both in-process (scan.scan(), as the rest of the suite calls it) and as
    an actual subprocess invoked the way collector.run_local invokes it, so
    the __file__ codepath _self_sha256 depends on is exercised for real
    rather than through the test runner's own import machinery."""

    def test_scan_dict_carries_a_real_sha256_alongside_the_existing_fields(self):
        result = scan.scan({"roots": [], "extra": []})
        for key in ("machine", "host", "scanned_at", "repos", "roots", "errors"):
            self.assertIn(key, result)
        self.assertIn("scan_py_sha256", result)
        self.assertEqual(len(result["scan_py_sha256"]), 64)
        int(result["scan_py_sha256"], 16)  # raises ValueError if not hex

    def test_subprocess_invocation_matches_run_local_and_reports_its_own_hash(self):
        proc = subprocess.run(
            [sys.executable, collector.SCAN_PY, "--root", "/nonexistent-xyz"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        payload = json.loads(proc.stdout.decode("utf-8"))
        self.assertIn("scan_py_sha256", payload)
        self.assertEqual(payload["scan_py_sha256"],
                         collector.local_scan_py_sha256(),
                         "scan.py run as a real file must report the hash "
                         "of that exact file, __file__ and all")

    def test_pretty_output_is_still_valid_json(self):
        proc = subprocess.run(
            [sys.executable, collector.SCAN_PY, "--pretty",
             "--root", "/nonexistent-xyz"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        payload = json.loads(proc.stdout.decode("utf-8"))
        self.assertIn("scan_py_sha256", payload)


if __name__ == "__main__":
    unittest.main()
