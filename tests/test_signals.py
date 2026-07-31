"""The dashboard's one recurring bug, tested instead of re-fixed.

Every signal git-monitor reports has to survive four hand-offs -- scan.py to the
database, the database to projects.py, projects.py into the dict a row is drawn
from, and that dict to a chip in render.py. Drop it at any of them and nothing
fails, nothing logs, nothing renders empty: the repo renders as "clean". That
has now happened four times (see signals.py), each time found by eye weeks
later.

These tests walk each registered signal through the whole pipeline and insist it
comes out the other end visible -- including the case that keeps recurring, a
signal firing on a copy the collapsed project row is not the one displaying.
They iterate signals.SIGNALS rather than naming anything, so a signal added
later is covered the day it is added.

    python -m unittest discover -s tests
"""

import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import coverage
import projects
import render
import scan
import signals
import storage


NEWER = "2026-07-30T12:00:00Z"
OLDER = "2026-06-01T12:00:00Z"


def quiet_value(s):
    """The value of `s` on a repo where it is NOT firing."""
    if isinstance(s, signals.ListSignal):
        return []
    if isinstance(s, signals.NoRemote):
        return 1                       # has a remote
    if isinstance(s, signals.Bare):
        return False
    if isinstance(s, signals.Unreadable):
        return None
    return 0


def firing_value(s):
    """The value of `s` on a repo where it IS firing."""
    if isinstance(s, signals.ListSignal):
        return [{"path": ".env", "by": None}]
    if isinstance(s, signals.NoRemote):
        return 0                       # no remote at all
    if isinstance(s, signals.Bare):
        return True
    if isinstance(s, signals.Unreadable):
        return "fatal: detected dubious ownership"
    return 3


def repo(machine="desktop", path="C:/projects/thing", last_commit=NEWER, **over):
    """A repo row with nothing wrong with it, plus whatever `over` says."""
    r = {
        "machine": machine, "path": path, "name": "thing", "branch": "main",
        "ahead": 0, "behind": 0, "last_commit": last_commit,
        "head_sha": "aaa", "root_key": "rootsha", "precious_files": [],
        "branch_tips": {"main": "aaa"}, "branch_dates": {"main": last_commit},
        "remotes": {}, "unpushed_by_remote": {}, "lineage": {}, "commit_days": {},
    }
    for s in signals.SIGNALS:
        r[s.key] = quiet_value(s)
    r.update(over)
    return r


def collapsed_row(html):
    """Just the top-level project row out of render_projects' output -- the one
    thing you see before expanding anything."""
    m = re.search(r'<div class="trow prow lvl0".*?</div>\s*(?=<div |$)', html, re.S)
    return m.group(0) if m else ""


class ScanFieldsAreClassified(unittest.TestCase):
    """scan.py collects a field; signals.py decides whether anyone hears about
    it. A field in neither list is a detector whose output goes nowhere."""

    # Fields that are deliberately not signals: identity, timing, and the raw
    # inputs other things are derived from. Adding to this list is a claim that
    # nothing on the page needs to chip the field -- make it consciously.
    NOT_SIGNALS = {
        "path", "name", "branch",           # which copy this is
        "last_commit", "commit_days",       # timing, drawn as the heatmap
        "ahead", "behind",                  # vs upstream; superseded by unpushed
        "head_sha", "root_key",             # identity, for grouping copies
        "branch_tips", "branch_dates", "lineage",   # cross-copy comparison
        "remotes", "unpushed_by_remote",    # backup-target labelling
        "precious_files",                   # raw input to the coverage buckets
    }

    def test_every_scanned_field_is_a_signal_or_declared_not_to_be(self):
        keys = set(signals.CARRY)
        for field in scan.REPO_FIELDS:
            self.assertTrue(
                field in keys or field in self.NOT_SIGNALS,
                "scan.py collects %r but signals.py doesn't know about it, so "
                "nothing on the dashboard can ever show it. Register a Signal, "
                "or add it to NOT_SIGNALS here to say it isn't one." % field)

    def test_every_stored_signal_is_actually_scanned(self):
        for key, _typ in signals.STORED:
            self.assertIn(
                key, scan.REPO_FIELDS,
                "signal %r has a DB column but scan.py never produces it" % key)

    def test_derived_signals_are_the_coverage_buckets(self):
        derived = {s.key for s in signals.SIGNALS if not s.stored}
        self.assertEqual(derived, set(coverage.FIELDS))


class StoredSignalsSurviveTheDatabase(unittest.TestCase):
    """`worktrees` was scanned and drawn for weeks with no column to land in."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.conn = storage.connect(os.path.join(self.dir, "t.db"))

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_each_stored_signal_round_trips(self):
        stored = [signals.by_key(k) for k, _ in signals.STORED]
        rows = [repo(path="/r/%s" % s.key, **{s.key: firing_value(s)})
                for s in stored]
        storage.save_scan(self.conn, "desktop", "ssh", "python3", {"repos": rows})
        back = {r["path"]: r for r in storage.get_repos(self.conn)}
        for s in stored:
            r = back["/r/%s" % s.key]
            self.assertTrue(s.fires(r),
                            "%r fires before the DB and not after it" % s.key)
            self.assertEqual(s.count(r), s.count(rows[stored.index(s)]),
                             "%r changed magnitude across the DB" % s.key)

    def test_a_fresh_db_has_a_column_for_every_stored_signal(self):
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(repos)")}
        for key, _typ in signals.STORED:
            self.assertIn(key, cols)

    def test_an_old_db_gains_columns_added_later(self):
        # Simulate a database created before a signal existed.
        self.conn.execute("DROP TABLE repos")
        self.conn.execute("CREATE TABLE repos (machine TEXT, path TEXT, "
                          "PRIMARY KEY (machine, path))")
        self.conn.commit()
        storage._migrate(self.conn)
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(repos)")}
        for key, _typ in signals.STORED:
            self.assertIn(key, cols)


class ProjectViewsCarryEverySignal(unittest.TestCase):
    """`has_remote` was drawn by the renderer but never copied into any of the
    three dicts the renderer is handed, so the chip could not appear at all."""

    def test_view_forwards_every_signal(self):
        r = repo(**{s.key: firing_value(s) for s in signals.SIGNALS})
        v = projects._view(r)
        for s in signals.SIGNALS:
            self.assertEqual(v.get(s.key), r[s.key],
                             "_view dropped %r" % s.key)

    def test_surfaced_and_instance_dicts_agree(self):
        r = repo(**{s.key: firing_value(s) for s in signals.SIGNALS
                    if not isinstance(s, signals.Bare)})
        p = projects.build_projects([r], {})[0]
        entry = p["branches"][0]["entries"][0]
        for s in signals.SIGNALS:
            self.assertEqual(p["surfaced"].get(s.key), entry.get(s.key),
                             "surfaced row and instance row disagree on %r" % s.key)


class SignalsReachTheCollapsedRow(unittest.TestCase):
    """The bug that keeps coming back.

    A project row shows ONE copy -- the most recently touched one, which is
    routinely the healthy one. Anything true of the other copies has to be
    rolled up onto that row or it stays invisible behind the very row hiding
    it."""

    def _two_copies(self, sig):
        """A project whose surfaced copy is clean and whose other copy fires
        `sig`. Same root_key and name groups them; identical branch tips keep
        them in sync, so nothing but `sig` can explain a chip."""
        clean = repo(machine="desktop", path="C:/projects/thing",
                     last_commit=NEWER)
        other = repo(machine="dserver", path="/home/evand/projects/thing",
                     last_commit=OLDER, **{sig.key: firing_value(sig)})
        return projects.build_projects([clean, other], {})

    def test_every_problem_signal_bubbles_up(self):
        for sig in signals.PROBLEMS:
            with self.subTest(signal=sig.key):
                projs = self._two_copies(sig)
                self.assertEqual(len(projs), 1, "the two copies didn't group")
                p = projs[0]
                self.assertEqual(p["surfaced"]["machine"], "desktop",
                                 "test assumes the clean copy is surfaced")
                row = collapsed_row(render.render_projects(projs))
                self.assertIn(
                    'badge %s elsewhere' % sig.cls, row,
                    "%r fires on a copy the collapsed row isn't showing, and "
                    "the row says nothing about it. That is the bug this file "
                    "exists for: roll it up in signals.hidden_from / "
                    "render._hidden_badges rather than patching one signal."
                    % sig.key)

    def test_a_row_hiding_a_problem_never_claims_clean(self):
        for sig in signals.PROBLEMS:
            with self.subTest(signal=sig.key):
                row = collapsed_row(render.render_projects(self._two_copies(sig)))
                self.assertNotIn(
                    'badge clean', row,
                    "the collapsed row reports 'clean' while %r fires on a copy "
                    "underneath it" % sig.key)

    def test_a_genuinely_clean_project_still_says_clean(self):
        projs = projects.build_projects(
            [repo(machine="desktop", path="C:/projects/thing", last_commit=NEWER),
             repo(machine="dserver", path="/srv/thing", last_commit=OLDER)], {})
        row = collapsed_row(render.render_projects(projs))
        self.assertIn('badge clean', row)
        self.assertNotIn('elsewhere', row)

    def test_the_surfaced_copys_own_problem_is_not_double_reported(self):
        for sig in signals.PROBLEMS:
            with self.subTest(signal=sig.key):
                one = repo(machine="desktop", path="C:/projects/thing",
                           last_commit=NEWER, **{sig.key: firing_value(sig)})
                two = repo(machine="dserver", path="/srv/thing",
                           last_commit=OLDER)
                row = collapsed_row(
                    render.render_projects(projects.build_projects([one, two], {})))
                self.assertIn('badge %s"' % sig.cls, row)
                self.assertNotIn('badge %s elsewhere' % sig.cls, row)


class EverySignalCanDrawItself(unittest.TestCase):
    """A signal with no class or no wording renders an unstyled, unreadable
    chip -- cheap to catch here rather than on the page."""

    def test_each_signal_has_a_chip_and_a_class(self):
        for s in signals.SIGNALS:
            with self.subTest(signal=s.key):
                r = repo(**{s.key: firing_value(s)})
                self.assertTrue(s.fires(r))
                chip = s.chip(r)
                self.assertTrue(s.cls, "%r has no CSS class" % s.key)
                self.assertIn('class="badge %s"' % s.cls, chip)
                self.assertTrue(s.word, "%r has no wording" % s.key)

    def test_each_signal_has_css(self):
        for s in signals.SIGNALS:
            with self.subTest(signal=s.key):
                # `,` as well as `{`: several chips share one rule.
                self.assertTrue(
                    re.search(r"\.badge\.%s\s*[,{]" % re.escape(s.cls), render.PAGE),
                    "%r renders a .badge.%s chip with no style rule in PAGE"
                    % (s.key, s.cls))

    def test_machine_cards_count_every_card_signal(self):
        r = repo(**{s.key: firing_value(s) for s in signals.ON_CARD
                    if not isinstance(s, signals.NoRemote)})
        totals = render._machine_totals([r])["desktop"]
        for s in signals.ON_CARD:
            if isinstance(s, signals.NoRemote):
                continue
            self.assertEqual(totals[s.key],
                             s.count(r) if s.card_unit == "items" else 1,
                             "machine cards don't count %r" % s.key)


if __name__ == "__main__":
    unittest.main()
