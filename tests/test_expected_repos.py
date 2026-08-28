"""The reflex-fw silence, 2026-08-17.

elspi's only scan root was `{path: /, depth: 1}`. Its repos live at /reflex-ui
and /rotary-controller-python, which that root reaches, and at
/home/default/projects/reflex-fw, which is three levels down and which it never
reached. Both reflex-ui and reflex-fw were carrying a single-copy branch that
night. The nightly digest alarmed about reflex-ui and said nothing whatsoever
about reflex-fw.

The root has been fixed. That is not the interesting half. The interesting half
is that NOTHING WENT RED while the gap existed, and nothing could have: every
check in this app reports on a repo that is present, so a repo which is absent
raises no signal, logs no error, and renders as no row -- which is byte for byte
what a machine with nothing to report looks like. A scan root can be wrong for
as long as nobody happens to look.

`expected_repos` is the declaration that closes that: `roots` says where to
look, this says what has to come back. storage.get_missing_repos compares the
two after each read, and anything declared and not found is reported as
`missing:<repo>` on the machine card, in /api/data, and inside that machine's
`root_warnings` -- the last because the nightly digest already prints an ALERT
per root warning, and a finding only a future consumer can see is a finding
nobody sees.

WHAT THIS FILE IS FOR. A missing-repo check that cannot go red is worth less
than none, because it also reports "all declared repos present". So every case
here is a PAIR over one fixture: the same machine, the same declaration, one
mutation, opposite verdicts. Proving the alarm fires proves nothing on its own
(a check that always fires is trivially red); proving it stays quiet proves
nothing on its own either (a check wired to nothing is quiet forever). The
suppressions are paired the same way -- an offline machine and an off-hours
machine are each shown a state that WAS alarming a line earlier, so a gate that
suppressed everything, or nothing, fails here.
"""

import datetime as dt
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import collector
import storage

#: elspi as config.yaml declares it since 2026-08-17.
ELSPI = ("reflex-ui", "rotary-controller-python", "reflex-fw")

#: Where each actually lives -- and the depth that made reflex-fw invisible.
PATHS = {"reflex-ui": "/reflex-ui",
         "rotary-controller-python": "/rotary-controller-python",
         "reflex-fw": "/home/default/projects/reflex-fw"}

#: 02:00 and 12:00 New York, as the collector sees them (its clock is UTC).
NIGHT = dt.datetime(2026, 8, 17, 6, 0, tzinfo=dt.timezone.utc)
DAY = dt.datetime(2026, 8, 17, 16, 0, tzinfo=dt.timezone.utc)


def scan_of(*names):
    """A scan result carrying exactly these repos."""
    return {
        "repos": [{"path": PATHS.get(n, "/" + n), "name": n} for n in names],
        "roots": [{"path": "/", "exists": True, "found": len(names)}],
    }


def config_for(expected=ELSPI, **target_extra):
    cfg = {"timezone": "America/New_York",
           "targets": [{"name": "elspi", "ssh": "root@elspi.lan",
                        "roots": [{"path": "/", "depth": 1}]}]}
    if expected is not None:
        # Passed through UNWRAPPED when it is not already a sequence of names:
        # `expected_repos: reflex-fw` in YAML is a bare string, and a helper
        # that quietly did list() on it turned that into a list of nine
        # single-character repo names which validate_config was happy to
        # accept. The fixture has to be able to express the malformed config,
        # or the test for it passes against nothing.
        cfg["targets"][0]["expected_repos"] = (
            list(expected) if isinstance(expected, (list, tuple)) else expected)
    cfg["targets"][0].update(target_extra)
    return cfg


def missing_alarms(warnings, machine="elspi"):
    """Just the missing-repo entries out of a root_warnings payload."""
    return [w for w in warnings.get(machine, []) if w.get("kind") == "missing_repo"]


# The format string the nightly digest uses for a root warning. It lives in
# ol-control/collect.sh, which is NOT part of this repo, so it has to be
# transcribed here -- but transcribing it and then asserting against the
# transcription is a test that cannot fail, which is exactly the defect the
# 2026-08-27 probe audit named (and exactly how houston sat 1-of-26 red for
# five days). So the transcription is checked against the real file by
# test_the_transcribed_digest_format_still_matches_collect_sh below.
DIGEST_FMT = "   ALERT  root warning %s:%s -- %s"

# Where the real one lives. Overridable so this is runnable off dserver.
COLLECT_SH = os.environ.get(
    "COLLECT_SH", os.path.expanduser("~/ol-control/collect.sh"))


def real_digest_fmt(path=None):
    """Extract the root-warning format string from the live collect.sh.

    Returns None if the file cannot be read -- the caller must treat that as
    UNKNOWN and say so, never as agreement.
    """
    p = path or COLLECT_SH
    try:
        src = open(p, encoding="utf-8").read()
    except (IOError, OSError):
        return None
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("print(") and "root warning" in s:
            first = s.find('"')
            last = s.find('"', first + 1)
            if first != -1 and last != -1:
                return s[first + 1:last]
    return ""


def digest_line(machine, w):
    """The line the nightly digest would print for this warning."""
    return DIGEST_FMT % (machine, w.get("path"), w.get("reason"))


class Base(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.conn = storage.connect(os.path.join(self.dir, "t.db"))

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def scan(self, *names):
        storage.save_scan(self.conn, "elspi", "root@elspi.lan", "python3",
                          scan_of(*names))

    def fail_scan(self, expected_offline=False, error="ssh scan failed"):
        storage.mark_unreachable(self.conn, "elspi", "root@elspi.lan", "python3",
                                 error, expected_offline=expected_offline)

    def missing(self, cfg=None, now=None):
        return storage.get_missing_repos(self.conn, cfg or config_for(), now=now)

    def warnings(self, cfg=None, now=None):
        return storage.get_root_warnings(self.conn, cfg or config_for(), now=now)


# ---------------------------------------------------------------------------
# The pair. One fixture, one mutation, opposite verdicts.
# ---------------------------------------------------------------------------

class PresentAndAbsent(Base):

    def test_all_three_declared_repos_present_is_silent(self):
        self.scan(*ELSPI)
        self.assertEqual(self.missing(), {})
        self.assertEqual(missing_alarms(self.warnings()), [])

    def test_the_same_fixture_without_reflex_fw_fires_exactly_one_alarm(self):
        """The 2026-08-17 shape: the depth-1 root finds the two repos at / and
        never reaches the one under /home/default/projects."""
        self.scan("reflex-ui", "rotary-controller-python")
        self.assertEqual(self.missing(), {"elspi": ["reflex-fw"]})
        alarms = missing_alarms(self.warnings())
        self.assertEqual(len(alarms), 1)
        self.assertEqual(alarms[0]["path"], "reflex-fw")
        self.assertIn("missing:reflex-fw", alarms[0]["reason"])

    def test_the_digest_line_names_the_missing_repo(self):
        """The alarm has to survive as far as the channel that is actually
        read. Rendering it into the digest's own format is the last link."""
        self.scan("reflex-ui", "rotary-controller-python")
        line = digest_line("elspi", missing_alarms(self.warnings())[0])
        self.assertIn("ALERT", line)
        self.assertIn("missing:reflex-fw", line)
        self.assertIn("elspi", line)

    def test_two_absent_repos_produce_two_alarms_not_one(self):
        self.scan("reflex-ui")
        self.assertEqual(self.missing(),
                         {"elspi": ["rotary-controller-python", "reflex-fw"]})
        self.assertEqual(len(missing_alarms(self.warnings())), 2)

    def test_a_scan_that_found_nothing_at_all_alarms_on_every_declared_repo(self):
        """The unmounted-share / broken-root case. `found: 0` already raises a
        root warning; that says a ROOT is empty, not which repos went with it."""
        self.scan()
        self.assertEqual(self.missing(), {"elspi": list(ELSPI)})
        self.assertEqual(len(missing_alarms(self.warnings())), 3)

    def test_an_undeclared_repo_being_present_is_not_an_alarm(self):
        """expected_repos is a floor, not an inventory."""
        self.scan("something-else", *ELSPI)
        self.assertEqual(self.missing(), {})

    def test_letter_case_is_not_a_miss(self):
        self.scan("Reflex-UI", "rotary-controller-python", "REFLEX-FW")
        self.assertEqual(self.missing(), {})

    def test_a_repo_that_is_present_but_unreadable_is_still_present(self):
        """git refusing a repo ("dubious ownership") is repo_errors' alarm.
        Reporting it as MISSING as well would send someone looking for a repo
        that is sitting right there."""
        result = scan_of(*ELSPI)
        result["repos"][2]["error"] = "detected dubious ownership"
        storage.save_scan(self.conn, "elspi", "root@elspi.lan", "python3", result)
        self.assertEqual(self.missing(), {})


# ---------------------------------------------------------------------------
# Declaring nothing must behave exactly as before.
# ---------------------------------------------------------------------------

class UndeclaredTargets(Base):

    def test_a_target_with_no_expected_repos_never_alarms(self):
        self.scan()                                   # found nothing at all
        self.assertEqual(self.missing(config_for(expected=None)), {})
        self.assertEqual(missing_alarms(self.warnings(config_for(expected=None))), [])

    def test_an_empty_declaration_is_the_same_as_none(self):
        self.scan()
        self.assertEqual(self.missing(config_for(expected=[])), {})

    def test_with_no_config_at_all_only_real_root_warnings_come_back(self):
        """The read side must keep working for a caller that has no config to
        give it -- app.py degrades to that when config.yaml will not parse."""
        self.scan()
        warns = storage.get_root_warnings(self.conn)
        self.assertEqual([w["reason"] for w in warns["elspi"]], ["no repos found"])
        self.assertEqual(missing_alarms(warns), [])

    def test_a_declared_target_the_db_has_never_heard_of_does_not_crash(self):
        cfg = config_for()
        cfg["targets"].append({"name": "ghost", "ssh": "user@nowhere",
                               "roots": [{"path": "/", "depth": 1}],
                               "expected_repos": ["never-scanned"]})
        self.scan(*ELSPI)
        self.assertEqual(self.missing(cfg), {})


# ---------------------------------------------------------------------------
# Suppression. Each case is shown a state that WAS alarming one line earlier.
# ---------------------------------------------------------------------------

class UnreachableMachinesDoNotAlarm(Base):

    def test_an_offline_machine_stops_alarming_about_repos_it_cannot_look_for(self):
        self.scan(*ELSPI)
        self.scan()                       # a real, successful, empty scan
        self.assertEqual(self.missing(), {"elspi": list(ELSPI)})   # red...

        self.fail_scan()
        self.fail_scan()                  # OFFLINE_AFTER_FAILURES
        self.assertEqual(
            [m["status"] for m in storage.get_machines(self.conn, config_for())],
            ["offline"])
        self.assertEqual(self.missing(), {})                       # ...and quiet
        self.assertEqual(missing_alarms(self.warnings()), [])

    def test_an_off_hours_machine_is_quiet_at_night_and_loud_in_the_day(self):
        """Same DB state, two clocks. A desktop that is off overnight would
        otherwise emit one false alarm per declared repo, every night, which is
        the failure `expected_online` was added to stop in the first place."""
        cfg = config_for(expected_online="07:00-23:00")
        self.scan(*ELSPI)
        self.scan()
        self.fail_scan(expected_offline=True)
        self.assertEqual(self.missing(cfg, now=NIGHT), {})
        self.assertEqual(self.missing(cfg, now=DAY), {"elspi": list(ELSPI)})

    def test_one_debounced_failure_keeps_checking_the_snapshot_it_still_has(self):
        """A single failure deliberately does NOT flip the machine offline, and
        it keeps the previous repo rows. Both halves still have to work: a real
        gap seen just before a blip must not be silenced by the blip."""
        self.scan(*ELSPI)
        self.fail_scan()
        self.assertEqual(self.missing(), {})          # snapshot is complete

        self.scan()                                   # gap, then a blip
        self.fail_scan()
        self.assertEqual(self.missing(), {"elspi": list(ELSPI)})

    def test_a_machine_that_has_never_scanned_successfully_is_not_alarmed(self):
        """Belt to the status gate's braces: nothing in normal operation leaves
        a row reachable with last_success NULL, so this hand-builds one. "scan
        failed on X" is that machine's alarm; inventing three more about repos
        nobody has ever managed to look for is noise on top of a known fault."""
        self.fail_scan()
        with self.conn:
            self.conn.execute("UPDATE machines SET reachable=1 WHERE name='elspi'")
        row = storage.get_machines(self.conn, config_for())[0]
        self.assertEqual(row["status"], "online")
        self.assertIsNone(row["last_success"])
        self.assertEqual(self.missing(), {})


# ---------------------------------------------------------------------------
# The two warning kinds share a channel and must stay tellable apart.
# ---------------------------------------------------------------------------

class RootWarningsAndMissingRepos(Base):

    def test_both_kinds_are_reported_and_tagged(self):
        storage.save_scan(self.conn, "elspi", "root@elspi.lan", "python3", {
            "repos": [{"path": "/reflex-ui", "name": "reflex-ui"}],
            "roots": [{"path": "/", "exists": True, "found": 1},
                      {"path": "/mnt/gone", "exists": False, "found": 0}],
        })
        warns = self.warnings()["elspi"]
        kinds = sorted(w["kind"] for w in warns)
        self.assertEqual(kinds, ["missing_repo", "missing_repo", "root"])
        root = [w for w in warns if w["kind"] == "root"][0]
        self.assertEqual((root["path"], root["reason"]), ("/mnt/gone", "missing"))

    def test_a_root_warning_keeps_the_wording_its_consumers_already_parse(self):
        self.scan()
        warns = storage.get_root_warnings(self.conn)["elspi"]
        self.assertEqual(warns[0]["path"], "/")
        self.assertEqual(warns[0]["reason"], "no repos found")


# ---------------------------------------------------------------------------
# A declaration that will not do anything must be refused when it is written,
# not ignored for weeks (the reasoning uptime's window validation uses).
# ---------------------------------------------------------------------------

class ConfigValidation(unittest.TestCase):

    def _cfg(self, expected):
        cfg = config_for(expected=expected)
        cfg["targets"][0]["precious_coverage"] = []
        return cfg

    def test_a_good_declaration_validates(self):
        self.assertTrue(collector.validate_config(self._cfg(ELSPI)))

    def test_no_declaration_validates(self):
        self.assertTrue(collector.validate_config(self._cfg(None)))

    def test_a_bare_string_is_refused_rather_than_read_as_characters(self):
        with self.assertRaises(ValueError) as cm:
            collector.validate_config(self._cfg("reflex-fw"))
        self.assertIn("expected_repos", str(cm.exception))

    def test_an_empty_name_is_refused(self):
        with self.assertRaises(ValueError):
            collector.validate_config(self._cfg(["reflex-fw", "  "]))

    def test_a_non_string_entry_is_refused(self):
        with self.assertRaises(ValueError):
            collector.validate_config(self._cfg([{"path": "/reflex-fw"}]))


if __name__ == "__main__":
    unittest.main()


class TranscribedFormatStillMatchesReality(unittest.TestCase):
    """The anti-encoding guard for this file.

    `DIGEST_FMT` is a copy of a string that lives in another repo. A copy that
    nothing compares against is not evidence -- it is a second opinion that
    always agrees with itself. This is the only test here that can notice the
    original moved.

    Added 2026-08-28 after the probe audit classified this file ENCODES-RISKY.
    Verified at the time: the transcription was IDENTICAL to collect.sh:578,
    so this went green on a real comparison, not on a vacuous one.
    """

    def test_the_transcribed_digest_format_still_matches_collect_sh(self):
        real = real_digest_fmt()
        if real is None:
            self.skipTest(
                "UNKNOWN: cannot read %s from here, so the transcribed "
                "DIGEST_FMT was NOT verified against the original. This is a "
                "wrong-vantage-point skip, not a pass -- re-run on dserver, or "
                "set COLLECT_SH." % COLLECT_SH)
        self.assertNotEqual(
            real, "",
            "found %s but no root-warning print() in it -- either the block "
            "was removed or its shape changed; DIGEST_FMT is now unanchored"
            % COLLECT_SH)
        self.assertEqual(
            real, DIGEST_FMT,
            "collect.sh's root-warning format has been reworded.\n"
            "  collect.sh: %r\n  this file:  %r\n"
            "Update DIGEST_FMT to match, and re-check any assertion that "
            "depends on the wording." % (real, DIGEST_FMT))

    def test_the_extractor_itself_can_fail(self):
        """Positive control for the guard above.

        A guard whose extractor silently returns the expected answer on any
        input would make the real test vacuous. Point it at a file with no
        such line and at a file that does not exist, and require it to say so
        differently in each case.
        """
        import tempfile as _tf
        d = _tf.mkdtemp()
        try:
            empty = os.path.join(d, "no-such-block.sh")
            open(empty, "w", encoding="utf-8").write("#!/bin/sh\necho hi\n")
            self.assertEqual(real_digest_fmt(empty), "",
                             "a file with no root-warning line must yield ''")
            self.assertIsNone(real_digest_fmt(os.path.join(d, "absent.sh")),
                              "an unreadable file must yield None, not ''")
            # And it must actually FIND one when present.
            good = os.path.join(d, "good.sh")
            open(good, "w", encoding="utf-8").write(
                '        print("   ALERT  root warning %s:%s -- %s" % (a, b, c))\n')
            self.assertEqual(real_digest_fmt(good), DIGEST_FMT)
        finally:
            shutil.rmtree(d, ignore_errors=True)
