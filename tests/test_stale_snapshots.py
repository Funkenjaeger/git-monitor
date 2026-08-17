"""The desktop that was gone for a fortnight, Aug 2026.

The desktop spent about two weeks off wired ethernet. Every nightly digest in
that window quoted its repos -- branches, unpushed counts, dirty files -- as
current fact, and no line anywhere said the numbers were a fortnight old.

Nothing was broken. Each piece behaved as designed: an unreachable host keeps
its last snapshot (save_scan/mark_unreachable), so the repo rows stayed; the
host declares `expected_online: "07:00-23:00"` and the digest runs at 01:07, so
the failed scans were correctly judged off-hours; and `offline_machines`
deliberately excludes an off-hours machine, so the one counter that could have
raised a hand was structurally incapable of it for exactly this host. The
result is a dashboard reporting a machine it has not seen in two weeks in the
same voice it uses for one scanned a minute ago.

`stale` is the missing sentence: how old is the data on the screen. It is a
question about the CLOCK, not about health -- which is why nothing here is
gated on `reachable` or on `off_hours`. Gating it on either would rebuild the
blind spot: off-hours is precisely the state the desktop was in.

WHAT THIS FILE IS FOR. A staleness check that cannot go red is worse than
none, because it also says "this data is current". So every case below is a
PAIR over one fixture: same machine, same config, one mutation, opposite
verdicts. Proving the warning fires proves nothing alone (a flag hard-wired to
True is trivially red); proving it stays quiet proves nothing alone either (a
flag hard-wired to False is quiet forever). The off-hours pair is the one the
change exists for, so it is a pair twice over -- an off-hours machine that IS
stale and an off-hours machine that is NOT, both at the same 01:07 clock.
"""

import datetime as dt
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage

#: The nightly consumer's clock: 01:07 America/New_York, in UTC as the
#: container sees it. Chosen because it is outside the desktop's declared
#: 07:00-23:00 window -- the whole point.
NIGHTLY = dt.datetime(2026, 8, 17, 5, 7, tzinfo=dt.timezone.utc)

#: The same day, inside the window, for the pairs that need the machine online.
MIDDAY = dt.datetime(2026, 8, 17, 16, 0, tzinfo=dt.timezone.utc)

WINDOW = "07:00-23:00"


def scan_result(*names):
    return {"repos": [{"path": "C:/projects/" + n, "name": n} for n in names],
            "roots": [{"path": "C:/projects", "exists": True,
                       "found": len(names)}]}


def config_for(expected_online=None, **extra):
    target = {"name": "desktop", "ssh": "evand@192.168.1.20",
              "roots": [{"path": "C:/projects", "depth": 2}]}
    if expected_online:
        target["expected_online"] = expected_online
    cfg = {"timezone": "America/New_York", "targets": [target]}
    cfg.update(extra)
    return cfg


def iso(when):
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


class Base(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.conn = storage.connect(os.path.join(self.dir, "t.db"))

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def scan(self, machine="desktop", *names):
        storage.save_scan(self.conn, machine, "evand@192.168.1.20", "python",
                          scan_result(*(names or ("git-monitor", "digest-agent"))))

    def fail_scan(self, machine="desktop", expected_offline=False):
        storage.mark_unreachable(self.conn, machine, "evand@192.168.1.20",
                                 "python", "ssh: connect timed out",
                                 expected_offline=expected_offline)

    def age_snapshot(self, days, machine="desktop", now=NIGHTLY):
        """Move this machine's last SUCCESSFUL scan `days` into the past.

        The DB is written directly because there is no other way to express
        the fixture: save_scan stamps `now`, and the whole subject here is a
        snapshot that has been sitting untouched while the clock ran on.
        """
        self.set_last_success(iso(now - dt.timedelta(days=days)), machine)

    def set_last_success(self, value, machine="desktop"):
        with self.conn:
            self.conn.execute("UPDATE machines SET last_success=? WHERE name=?",
                              (value, machine))

    def machine(self, cfg=None, now=NIGHTLY, name="desktop"):
        rows = storage.get_machines(self.conn, cfg or config_for(), now=now)
        return [m for m in rows if m["name"] == name][0]

    def summary(self, cfg=None, now=NIGHTLY):
        return storage.get_summary(self.conn, cfg or config_for(), now=now)


# ---------------------------------------------------------------------------
# (a)/(b) The pair. One fixture, one mutation, opposite verdicts.
# ---------------------------------------------------------------------------

class FreshAndAged(Base):

    def test_a_machine_scanned_an_hour_ago_is_not_stale(self):
        self.scan()
        self.age_snapshot(1.0 / 24)
        m = self.machine()
        self.assertFalse(m["stale"])
        self.assertIsNone(m["stale_reason"])
        self.assertAlmostEqual(m["snapshot_age_days"], 1.0 / 24, places=4)
        self.assertEqual(self.summary()["stale_machines"], 0)

    def test_the_same_fixture_aged_four_days_is_stale(self):
        """The only difference from the case above is the clock."""
        self.scan()
        self.age_snapshot(4)
        m = self.machine()
        self.assertTrue(m["stale"])
        self.assertEqual(m["stale_reason"], "aged")
        self.assertAlmostEqual(m["snapshot_age_days"], 4.0, places=4)
        self.assertEqual(self.summary()["stale_machines"], 1)

    def test_the_fortnight_that_started_this(self):
        self.scan()
        self.age_snapshot(14)
        m = self.machine()
        self.assertTrue(m["stale"])
        self.assertAlmostEqual(m["snapshot_age_days"], 14.0, places=4)

    def test_a_stale_machine_keeps_every_repo_it_reported(self):
        """Labelling is the fix. Hiding the rows would swap one wrong reading
        ("fresh") for another ("this machine has no repos")."""
        self.scan("desktop", "git-monitor", "digest-agent", "sketchtocut")
        self.age_snapshot(14)
        repos = storage.get_repos(self.conn, config_for())
        self.assertEqual(sorted(r["name"] for r in repos),
                         ["digest-agent", "git-monitor", "sketchtocut"])
        self.assertEqual(self.summary()["total_repos"], 3)
        self.assertTrue(self.machine()["stale"])


# ---------------------------------------------------------------------------
# (c) The boundary, from both sides.
# ---------------------------------------------------------------------------

class Boundary(Base):

    def test_just_under_three_days_is_not_stale(self):
        self.scan()
        self.age_snapshot(3 - 1.0 / 24)
        self.assertFalse(self.machine()["stale"])
        self.assertEqual(self.summary()["stale_machines"], 0)

    def test_just_over_three_days_is_stale(self):
        self.scan()
        self.age_snapshot(3 + 1.0 / 24)
        self.assertTrue(self.machine()["stale"])
        self.assertEqual(self.summary()["stale_machines"], 1)

    def test_exactly_the_threshold_is_not_yet_stale(self):
        """`>` and not `>=`: the threshold is the age that is still acceptable,
        so the alarm belongs to the first moment past it."""
        self.scan()
        self.age_snapshot(3)
        self.assertFalse(self.machine()["stale"])


# ---------------------------------------------------------------------------
# (d) THE CASE THIS EXISTS FOR. Off-hours is not a defence against staleness.
# Both machines below are off-hours at the same clock; only one is stale.
# ---------------------------------------------------------------------------

class OffHoursAndStale(Base):

    def setUp(self):
        Base.setUp(self)
        self.cfg = config_for(expected_online=WINDOW)

    def test_off_hours_and_freshly_scanned_says_nothing(self):
        """The pair's quiet half. If this went red too, the loud half below
        would only be proving that everything is stale."""
        self.scan()
        self.age_snapshot(2.0 / 24)          # scanned two hours ago, at 23:07
        self.fail_scan(expected_offline=True)
        m = self.machine(self.cfg)
        self.assertEqual(m["status"], "off-hours")   # the fixture really is
        self.assertTrue(m["off_hours"])              # the one being claimed
        self.assertFalse(m["stale"])
        self.assertEqual(self.summary(self.cfg)["stale_machines"], 0)

    def test_off_hours_and_a_week_old_is_stale_and_says_so(self):
        """The desktop, mid-fortnight, as the 01:07 digest saw it: correctly
        off-hours, correctly excluded from offline_machines, and carrying repo
        data a week out of date that nothing was measuring."""
        self.scan()
        self.age_snapshot(7)
        self.fail_scan(expected_offline=True)
        m = self.machine(self.cfg)
        self.assertEqual(m["status"], "off-hours")
        self.assertTrue(m["off_hours"])
        self.assertTrue(m["stale"])
        self.assertEqual(m["stale_reason"], "aged")
        summary = self.summary(self.cfg)
        self.assertEqual(summary["stale_machines"], 1)
        # The counter that could not see it, still cannot -- which is the
        # reason a second one had to exist. If this ever starts reporting 1,
        # the two counters have been quietly merged and the comment in
        # get_summary about them being independent is no longer true.
        self.assertEqual(summary["offline_machines"], 0)

    def test_the_verdict_does_not_change_when_the_window_opens(self):
        """Same DB, same age, a clock inside the window. Staleness is measured
        against time alone, so it must read identically at midday -- if it
        moves, something is gating it on the window after all."""
        self.scan()
        self.age_snapshot(7, now=MIDDAY)
        self.fail_scan(expected_offline=True)
        m = self.machine(self.cfg, now=MIDDAY)
        self.assertTrue(m["stale"])
        self.assertNotEqual(m["status"], "off-hours")

    def test_an_online_reachable_machine_can_be_stale_too(self):
        """Not gated on `reachable` either. A machine can be up, answering
        pings, and still not have completed a successful SCAN in a week --
        an expired key, a wedged sshd, a wrong remote_python."""
        self.scan()
        self.age_snapshot(9)
        m = self.machine(config_for(), now=MIDDAY)
        self.assertEqual(m["status"], "online")
        self.assertTrue(m["reachable"])
        self.assertTrue(m["stale"])

    def test_an_offline_machine_scanned_yesterday_is_not_stale(self):
        """The other direction of the same independence: down is not old.
        Something has to be reported here -- offline -- but not this."""
        self.scan()
        self.age_snapshot(1)
        self.fail_scan()
        self.fail_scan()                     # OFFLINE_AFTER_FAILURES
        m = self.machine(config_for(), now=MIDDAY)
        self.assertEqual(m["status"], "offline")
        self.assertFalse(m["stale"])
        summary = self.summary(config_for(), now=MIDDAY)
        self.assertEqual(summary["offline_machines"], 1)
        self.assertEqual(summary["stale_machines"], 0)


# ---------------------------------------------------------------------------
# (e)/(f) No data, and unreadable data. Neither is fresh; neither is an age.
# ---------------------------------------------------------------------------

class NoUsableTimestamp(Base):

    def test_a_machine_never_scanned_successfully_is_stale(self):
        self.fail_scan()
        m = self.machine()
        self.assertTrue(m["stale"])
        self.assertEqual(m["stale_reason"], "never")
        self.assertIsNone(m["snapshot_age_days"])
        self.assertEqual(self.summary()["stale_machines"], 1)

    def test_never_scanned_is_distinguishable_from_aged(self):
        """Absent data is not old data. Both are stale; a reader has to be
        able to tell 'we have not looked in nine days' from 'we have never
        once looked', because only one of those has a snapshot behind it."""
        self.fail_scan("desktop")
        storage.save_scan(self.conn, "elspi", "root@elspi.lan", "python3",
                          scan_result("reflex-fw"))
        self.age_snapshot(9, machine="elspi")

        never = self.machine(name="desktop")
        aged = self.machine(name="elspi")
        self.assertTrue(never["stale"] and aged["stale"])
        self.assertNotEqual(never["stale_reason"], aged["stale_reason"])
        self.assertEqual((never["stale_reason"], never["snapshot_age_days"]),
                         ("never", None))
        self.assertEqual(aged["stale_reason"], "aged")
        self.assertAlmostEqual(aged["snapshot_age_days"], 9.0, places=4)
        self.assertEqual(self.summary()["stale_machines"], 2)

    def test_an_unparseable_timestamp_is_stale_with_an_unknown_age(self):
        """Failing open here would be the worst of the lot: a garbled column
        would read as a machine scanned this instant."""
        self.scan()
        self.set_last_success("last Tuesday-ish")
        m = self.machine()
        self.assertTrue(m["stale"])
        self.assertEqual(m["stale_reason"], "unreadable")
        self.assertIsNone(m["snapshot_age_days"])
        self.assertEqual(self.summary()["stale_machines"], 1)

    def test_the_same_column_written_properly_is_fresh(self):
        """The paired half: the parse path works, so the case above is failing
        on the garbling and not on everything."""
        self.scan()
        self.set_last_success(iso(NIGHTLY - dt.timedelta(hours=2)))
        m = self.machine()
        self.assertFalse(m["stale"])
        self.assertAlmostEqual(m["snapshot_age_days"], 2.0 / 24, places=4)

    def test_a_timestamp_with_an_offset_instead_of_a_z_is_read_correctly(self):
        """storage writes ...Z, but the column is TEXT and an offset form is a
        legal ISO timestamp. Two hours ago is two hours ago in either spelling
        -- reading -04:00 as UTC would invent a four-hour drift."""
        self.scan()
        local = (NIGHTLY - dt.timedelta(hours=2)).astimezone(
            dt.timezone(dt.timedelta(hours=-4)))
        self.set_last_success(local.strftime("%Y-%m-%dT%H:%M:%S%z"))
        self.assertAlmostEqual(self.machine()["snapshot_age_days"],
                               2.0 / 24, places=4)


# ---------------------------------------------------------------------------
# The threshold itself.
# ---------------------------------------------------------------------------

class Threshold(Base):

    def test_the_default_is_three_days(self):
        self.assertEqual(storage.stale_after_days(), 3)
        self.assertEqual(storage.stale_after_days({}), 3)

    def test_config_can_widen_it(self):
        self.scan()
        self.age_snapshot(4)
        self.assertTrue(self.machine()["stale"])
        self.assertFalse(self.machine(config_for(stale_after_days=10))["stale"])

    def test_config_can_tighten_it(self):
        self.scan()
        self.age_snapshot(1)
        self.assertFalse(self.machine()["stale"])
        self.assertTrue(self.machine(config_for(stale_after_days=0.5))["stale"])

    def test_a_string_from_hand_edited_yaml_still_counts(self):
        self.scan()
        self.age_snapshot(4)
        self.assertFalse(self.machine(config_for(stale_after_days="10"))["stale"])

    def test_an_unusable_threshold_falls_back_to_the_default(self):
        """Not to "never stale". A typo must not silently disable the check."""
        self.scan()
        self.age_snapshot(4)
        for bad in ("soon", None, 0, -1, [3]):
            self.assertEqual(storage.stale_after_days({"stale_after_days": bad}), 3,
                             "threshold %r should fall back to 3" % (bad,))
            self.assertTrue(self.machine(config_for(stale_after_days=bad))["stale"],
                            "threshold %r must not disable the check" % (bad,))


# ---------------------------------------------------------------------------
# Callers that have no config, and the render path.
# ---------------------------------------------------------------------------

class WithoutConfig(Base):

    def test_staleness_is_reported_with_no_config_at_all(self):
        """app.py degrades to an empty config when config.yaml will not parse
        -- the moment when "how old is this data" matters most."""
        self.scan()
        self.age_snapshot(9)
        rows = storage.get_machines(self.conn, now=NIGHTLY)
        self.assertTrue(rows[0]["stale"])
        self.assertAlmostEqual(rows[0]["snapshot_age_days"], 9.0, places=4)
        self.assertEqual(
            storage.get_summary(self.conn, now=NIGHTLY)["stale_machines"], 1)

    def test_the_default_clock_is_now(self):
        """Every other test pins `now`. If the default argument were wrong,
        production would be the only caller that ever saw it."""
        self.scan()                          # stamped at the real current time
        self.assertFalse(storage.get_machines(self.conn)[0]["stale"])
        self.set_last_success(iso(dt.datetime.now(dt.timezone.utc)
                                  - dt.timedelta(days=30)))
        self.assertTrue(storage.get_machines(self.conn)[0]["stale"])


class MachineCard(Base):
    """The label has to survive as far as the thing a human actually reads.
    A flag that only /api/data carries is a flag nobody opens the dashboard
    and sees, which is the reason this went into git-monitor at all."""

    def card(self, cfg=None, now=NIGHTLY):
        import render
        machines = storage.get_machines(self.conn, cfg or config_for(), now=now)
        repos = storage.get_repos(self.conn, cfg or config_for())
        return render.render_machines(machines, repos)

    def test_a_fresh_machine_prints_no_stale_warning(self):
        self.scan()
        self.age_snapshot(2.0 / 24)
        self.assertNotIn("stale", self.card())

    def test_an_aged_machine_prints_the_age_on_its_card(self):
        self.scan()
        self.age_snapshot(4.2)
        html = self.card()
        self.assertIn("stale -- last successful scan 4.2 days ago", html)
        self.assertIn("mwarn", html)

    def test_an_off_hours_card_still_prints_it(self):
        """The card whose absence started this. `off-hours` is a calm status
        with no error line; the stale warning has to appear anyway."""
        self.scan()
        self.age_snapshot(14)
        self.fail_scan(expected_offline=True)
        html = self.card(config_for(expected_online=WINDOW))
        self.assertIn("off-hours", html)
        self.assertIn("stale -- last successful scan 14 days ago", html)

    def test_a_never_scanned_card_says_never_not_a_number(self):
        self.fail_scan()
        html = self.card()
        self.assertIn("never scanned successfully", html)
        self.assertNotIn("days ago", html)

    def test_an_unreadable_timestamp_card_says_so(self):
        self.scan()
        self.set_last_success("last Tuesday-ish")
        html = self.card()
        self.assertIn("unreadable", html)
        self.assertNotIn("days ago", html)

    def test_the_stale_card_still_lists_its_repos(self):
        self.scan("desktop", "git-monitor", "sketchtocut")
        self.age_snapshot(14)
        self.assertIn("2 repos", self.card())


if __name__ == "__main__":
    unittest.main()
