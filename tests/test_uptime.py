"""The permanently-red desktop, 2026-08-13.

Every night at 01:07 the dashboard carried two alerts:

    ALERT  machines OFFLINE: 1
    ALERT  scan failed on desktop

with `error: "RuntimeError: ssh scan failed (rc=255): ssh: connect to host
192.168.1.243 port 22: No route to host"` on the desktop row. "No route to
host" on a permanent DHCP reservation is not a lease drift and not a broken
key: the machine is simply switched off. Since the nightly job moved to dserver
on 2026-08-10, nothing keeps it awake, so those two alerts were guaranteed red
every night forever -- and an alert that is always on costs the whole panel its
meaning, not just its own line.

The debounce could not fix it. `OFFLINE_AFTER_FAILURES = 2` guards against a
BLIP -- one slow night, a host mid-reboot -- and a machine that is off for nine
hours fails every consecutive scan and trips a two-in-a-row rule on the second
one, precisely as designed. What was missing was not a bigger number but a
statement of INTENT: when is this machine supposed to be up at all?

So a target may declare `expected_online` (see uptime.py), and outside that
window an unreachable machine is not debounced, not counted in
`offline_machines`, and not reported as a failed scan.

Three properties this file pins down, in that order of importance:

    (a) a failure INSIDE the window still alerts, exactly as before;
    (b) a failure OUTSIDE the window does not;
    (c) a machine with NO window configured behaves identically to today.

(c) is the one worth being fussy about. The whole change is a suppression, and
a suppression that reaches machines nobody exempted is strictly worse than the
noise it removed -- so every off-hours assertion below is paired with the same
scenario on an unconfigured machine, which must still go red.

    python -m unittest discover -s tests
"""

import datetime as dt
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import collector
import render
import storage
import uptime

UTC = dt.timezone.utc

#: A fixed Thursday, so weekday assertions read as dates rather than arithmetic.
#: 2026-08-13 is a Thursday.
THU_NOON = dt.datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
THU_0200 = dt.datetime(2026, 8, 13, 2, 0, tzinfo=UTC)
FRI_0200 = dt.datetime(2026, 8, 14, 2, 0, tzinfo=UTC)
SAT_NOON = dt.datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def _cfg(*targets, **kw):
    cfg = {"timezone": kw.pop("timezone", "UTC"), "targets": list(targets)}
    cfg.update(kw)
    return cfg


def _target(name="desktop", window="07:00-23:00", **kw):
    t = {"name": name, "ssh": "evand@192.168.1.243",
         "roots": [{"path": "C:/projects", "depth": 2}]}
    if window is not None:
        t["expected_online"] = window
    t.update(kw)
    return t


def _hhmm(when):
    return "%02d:%02d" % (when.hour, when.minute)


def _window_around_now(hours=2):
    """A window that certainly contains the real current moment.

    collect_one() asks uptime for the answer at the real wall clock, with no
    seam to inject a fake one -- so the collector-level tests move the WINDOW
    instead of the clock. Whole hours either side keeps it clear of any
    minute-rounding boundary, and a window that crosses midnight is exercised
    for free whenever the suite happens to run late.
    """
    now = dt.datetime.now(UTC)
    return "%s-%s" % (_hhmm(now - dt.timedelta(hours=hours)),
                      _hhmm(now + dt.timedelta(hours=hours)))


def _window_excluding_now(gap=2, hours=2):
    """A window that certainly does NOT contain the real current moment."""
    now = dt.datetime.now(UTC)
    return "%s-%s" % (_hhmm(now + dt.timedelta(hours=gap)),
                      _hhmm(now + dt.timedelta(hours=gap + hours)))


try:
    uptime._zone("America/New_York")
    HAVE_TZDB = True
except uptime.WindowError:
    # Python ships no timezone database on Windows and slim images often carry
    # none either -- hence tzdata in requirements.txt. Everything else here
    # uses UTC (which needs no database) or builds a Window directly, so only
    # the named-zone tests skip.
    HAVE_TZDB = False


# ---------------------------------------------------------------------------
# Parsing: what a window spec may say
# ---------------------------------------------------------------------------

class WindowParsing(unittest.TestCase):

    def test_shorthand_string_is_hours_only(self):
        w = uptime.parse("07:00-23:00", default_tz="UTC")
        self.assertEqual(w.start, dt.time(7, 0))
        self.assertEqual(w.end, dt.time(23, 0))
        self.assertIsNone(w.days)

    def test_full_mapping_with_days_and_its_own_timezone(self):
        w = uptime.parse({"hours": "8:30-17:00", "days": ["mon", "Friday"],
                          "timezone": "UTC"})
        self.assertEqual(w.start, dt.time(8, 30))
        self.assertEqual(w.end, dt.time(17, 0))
        self.assertEqual(w.days, {0, 4})

    def test_target_timezone_beats_the_file_default(self):
        w = uptime.parse({"hours": "07:00-23:00", "timezone": "GMT"},
                         default_tz="Definitely/NotAZone")
        self.assertIs(w.tz, UTC)

    def test_end_24_00_is_midnight_at_the_end_of_the_day(self):
        w = uptime.parse("07:00-24:00", default_tz="UTC")
        self.assertEqual(w.end, dt.time(0, 0))
        self.assertTrue(w.contains(dt.datetime(2026, 8, 13, 23, 59, tzinfo=UTC)))
        self.assertFalse(w.contains(dt.datetime(2026, 8, 13, 6, 59, tzinfo=UTC)))

    def test_str_names_the_hours_the_days_and_the_zone(self):
        w = uptime.parse({"hours": "07:00-23:00", "days": ["fri", "mon"],
                          "timezone": "UTC"})
        self.assertEqual(str(w), "07:00-23:00 mon,fri UTC")

    def _bad(self, spec, default_tz="UTC"):
        with self.assertRaises(uptime.WindowError) as cm:
            uptime.parse(spec, default_tz=default_tz)
        return str(cm.exception)

    def test_rejects_a_missing_timezone_rather_than_guessing_utc(self):
        """The collector's own clock is UTC and the hours in the config are
        wall-clock hours somewhere else. Guessing would shift every window by
        the UTC offset and still look like a working feature."""
        msg = self._bad("07:00-23:00", default_tz=None)
        self.assertIn("timezone", msg)

    def test_rejects_a_timezone_with_no_database_behind_it(self):
        self.assertIn("unknown timezone", self._bad("07:00-23:00",
                                                    default_tz="Not/AZone"))

    def test_rejects_malformed_hours(self):
        for spec in ("07:00", "07:00-", "7-23", "07:00-23:00-01:00", "",
                     "25:00-26:00", "07:60-08:00"):
            with self.subTest(spec=spec):
                self._bad(spec)

    def test_rejects_a_zero_length_window_as_ambiguous(self):
        self.assertIn("always or never", self._bad("09:00-09:00"))

    def test_rejects_24_00_as_a_start(self):
        self.assertIn("only meaningful as the end", self._bad("24:00-07:00"))

    def test_rejects_an_unknown_day_name(self):
        self.assertIn("unknown day",
                      self._bad({"hours": "07:00-23:00", "days": ["mondy"]}))

    def test_rejects_an_empty_day_list_instead_of_meaning_never(self):
        self.assertIn("never expected online",
                      self._bad({"hours": "07:00-23:00", "days": []}))

    def test_rejects_a_bare_string_for_days(self):
        self._bad({"hours": "07:00-23:00", "days": "mon"})

    def test_rejects_a_spec_that_is_not_a_string_or_mapping(self):
        self._bad(["07:00-23:00"])


# ---------------------------------------------------------------------------
# Evaluation: is `now` inside the window
# ---------------------------------------------------------------------------

class WindowContains(unittest.TestCase):

    def _w(self, hours, **kw):
        kw.setdefault("timezone", "UTC")
        return uptime.parse(dict(hours=hours, **kw))

    def test_inside_and_outside_a_daytime_window(self):
        w = self._w("07:00-23:00")
        self.assertTrue(w.contains(THU_NOON))
        self.assertFalse(w.contains(THU_0200))

    def test_start_is_inclusive_and_end_is_exclusive(self):
        w = self._w("07:00-23:00")
        self.assertTrue(w.contains(dt.datetime(2026, 8, 13, 7, 0, tzinfo=UTC)))
        self.assertFalse(w.contains(dt.datetime(2026, 8, 13, 6, 59, tzinfo=UTC)))
        self.assertFalse(w.contains(dt.datetime(2026, 8, 13, 23, 0, tzinfo=UTC)))
        self.assertTrue(w.contains(dt.datetime(2026, 8, 13, 22, 59, tzinfo=UTC)))

    def test_a_window_may_cross_midnight(self):
        w = self._w("22:00-06:00")
        self.assertTrue(w.contains(dt.datetime(2026, 8, 13, 23, 30, tzinfo=UTC)))
        self.assertTrue(w.contains(dt.datetime(2026, 8, 13, 1, 0, tzinfo=UTC)))
        self.assertFalse(w.contains(THU_NOON))

    def test_days_restrict_the_window(self):
        w = self._w("07:00-23:00", days=["mon", "tue", "wed", "thu", "fri"])
        self.assertTrue(w.contains(THU_NOON))
        self.assertFalse(w.contains(SAT_NOON))

    def test_an_overnight_window_is_anchored_to_the_day_it_STARTED(self):
        """"thu 22:00-06:00" covers Friday 02:00 -- that hour belongs to
        Thursday's window. Anchoring on the calendar day instead would make
        every overnight window silently one day wrong at its tail."""
        w = self._w("22:00-06:00", days=["thu"])
        self.assertTrue(w.contains(FRI_0200))
        self.assertFalse(w.contains(THU_0200))          # Wednesday's window

    def test_now_is_converted_into_the_configured_zone_first(self):
        """Built directly rather than parsed so this runs without a timezone
        database. 12:00 UTC is 08:00 in a UTC-4 zone: inside 07:00-23:00
        there, and the same instant is 02:00 UTC-10, which is not."""
        for offset, expected in ((-4, True), (-10, False)):
            with self.subTest(offset=offset):
                w = uptime.Window(dt.time(7), dt.time(23), None,
                                  dt.timezone(dt.timedelta(hours=offset)),
                                  "UTC%+d" % offset, "07:00-23:00")
                self.assertEqual(w.contains(THU_NOON), expected)

    @unittest.skipUnless(HAVE_TZDB, "no timezone database on this interpreter")
    def test_a_named_zone_resolves_and_shifts_the_window(self):
        w = self._w("07:00-23:00", timezone="America/New_York")
        # 10:00 UTC is 06:00 in New York in August (EDT) -- before the window
        # opens, though it looks like mid-morning to a UTC clock.
        self.assertFalse(w.contains(dt.datetime(2026, 8, 13, 10, 0, tzinfo=UTC)))
        self.assertTrue(w.contains(dt.datetime(2026, 8, 13, 12, 0, tzinfo=UTC)))

    def test_a_naive_now_is_refused_rather_than_assumed(self):
        with self.assertRaises(uptime.WindowError):
            self._w("07:00-23:00").contains(dt.datetime(2026, 8, 13, 12, 0))


# ---------------------------------------------------------------------------
# Failing open: every unknown resolves toward alerting
# ---------------------------------------------------------------------------

class UnusableWindowsFailOpen(unittest.TestCase):
    """A monitor that quietly stops monitoring is worse than one that cries
    wolf, so a window that cannot be resolved suppresses nothing at all -- and
    says why."""

    def _off(self, target, defaults=None):
        # Not `defaults or {...}`: an EMPTY defaults dict is a case under test
        # (a window with no timezone anywhere), and falsiness would quietly
        # swap in the UTC one and assert nothing.
        if defaults is None:
            defaults = {"timezone": "UTC"}
        return uptime.off_hours(target, defaults, now=THU_0200)

    def test_no_window_is_never_off_hours(self):
        self.assertFalse(self._off(_target(window=None)))

    def test_a_malformed_window_is_never_off_hours(self):
        self.assertFalse(self._off(_target(window="nonsense")))

    def test_a_window_with_no_resolvable_timezone_is_never_off_hours(self):
        self.assertFalse(self._off(_target(), defaults={}))

    def test_the_reason_is_reported_not_swallowed(self):
        window, err = uptime.window_for(_target(window="nonsense"),
                                        {"timezone": "UTC"})
        self.assertIsNone(window)
        self.assertTrue(err)

    def test_a_good_window_reports_no_error(self):
        window, err = uptime.window_for(_target(), {"timezone": "UTC"})
        self.assertIsNone(err)
        self.assertEqual(str(window), "07:00-23:00 UTC")

    def test_a_window_and_an_error_are_mutually_exclusive(self):
        """The invariant callers rely on to test the window alone. Written
        down as a test because the alternative -- every caller also checking
        the error -- is a branch no input can reach, and an unreachable branch
        is where a real bug hides undisturbed."""
        for spec in ("07:00-23:00", "nonsense", None,
                     {"hours": "07:00-23:00", "days": ["nope"]},
                     {"hours": "07:00-23:00", "timezone": "Not/AZone"}):
            with self.subTest(spec=spec):
                window, err = uptime.window_for(_target(window=spec),
                                                {"timezone": "UTC"})
                self.assertFalse(window is not None and err)

    def test_a_naive_now_suppresses_nothing(self):
        """contains() refuses a naive datetime rather than assuming a zone;
        the callers turn that refusal into "no opinion", not "off-hours"."""
        self.assertFalse(uptime.off_hours(_target(), {"timezone": "UTC"},
                                          now=dt.datetime(2026, 8, 13, 2, 0)))

    def test_a_naive_now_is_reported_through_annotate(self):
        rows = [{"name": "desktop", "reachable": 0, "error": "boom"}]
        uptime.annotate(rows, _cfg(_target("desktop")),
                        now=dt.datetime(2026, 8, 13, 2, 0))
        self.assertFalse(rows[0]["off_hours"])
        self.assertTrue(rows[0]["window_error"])
        self.assertEqual(rows[0]["status"], "offline")

    def test_a_file_level_window_applies_to_a_target_without_one(self):
        """Consistent with remote_python/timeout/remote_script, which all fall
        back to a file-level default."""
        self.assertTrue(uptime.off_hours(
            _target(window=None),
            {"timezone": "UTC", "expected_online": "07:00-23:00"},
            now=THU_0200))

    def test_a_per_target_window_overrides_the_file_level_one(self):
        self.assertFalse(uptime.off_hours(
            _target(window="00:00-06:00"),
            {"timezone": "UTC", "expected_online": "07:00-23:00"},
            now=THU_0200))


# ---------------------------------------------------------------------------
# (a)/(b)/(c) at the storage layer: the debounce is not spent off-hours
# ---------------------------------------------------------------------------

class DebounceIsNotSpentOffHours(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.conn = storage.connect(os.path.join(self.dir, "t.db"))
        storage.save_scan(self.conn, "desktop", "ssh", "python", {"repos": []})

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _machine(self, name="desktop"):
        return dict(self.conn.execute(
            "SELECT * FROM machines WHERE name=?", (name,)).fetchone())

    def _fail(self, n=1, expected_offline=False, name="desktop"):
        for i in range(n):
            storage.mark_unreachable(self.conn, name, "ssh", "python",
                                     "ssh scan failed (rc=255): No route to "
                                     "host #%d" % i,
                                     expected_offline=expected_offline)

    # (b)
    def test_a_night_of_off_hours_failures_never_marks_the_machine_offline(self):
        self._fail(18, expected_offline=True)       # 9 hours at 30-minute scans
        m = self._machine()
        self.assertEqual(m["reachable"], 1)
        self.assertEqual(m["fail_streak"], 0,
                         "an expected absence must not accumulate toward the "
                         "OFFLINE debounce -- that is the whole fix")

    def test_the_error_and_the_timestamp_are_still_recorded_off_hours(self):
        """Suppressing the ALARM is not the same as hiding the evidence."""
        self._fail(1, expected_offline=True)
        m = self._machine()
        self.assertIn("No route to host", m["error"])
        self.assertTrue(m["last_scanned"])

    # (a)
    def test_two_failures_inside_the_window_still_go_offline(self):
        self._fail(2)
        m = self._machine()
        self.assertEqual(m["reachable"], 0)
        self.assertEqual(m["fail_streak"], 2)

    def test_an_off_hours_failure_does_not_clear_an_offline_verdict(self):
        """A machine that died DURING its window stays offline in the DB; only
        a successful scan is evidence that it came back."""
        self._fail(2)
        self._fail(4, expected_offline=True)
        m = self._machine()
        self.assertEqual(m["reachable"], 0)
        self.assertEqual(m["fail_streak"], 2)

    def test_an_off_hours_failure_does_not_reset_a_streak_either(self):
        """One failure inside the window, then the window closes: the machine
        is still one failure into the debounce when it reopens."""
        self._fail(1)
        self._fail(3, expected_offline=True)
        self.assertEqual(self._machine()["fail_streak"], 1)
        self._fail(1)
        m = self._machine()
        self.assertEqual(m["fail_streak"], 2)
        self.assertEqual(m["reachable"], 0)

    def test_a_success_after_an_off_hours_night_clears_everything(self):
        self._fail(6, expected_offline=True)
        storage.save_scan(self.conn, "desktop", "ssh", "python", {"repos": []})
        m = self._machine()
        self.assertEqual(m["reachable"], 1)
        self.assertEqual(m["fail_streak"], 0)
        self.assertIsNone(m["error"])

    def test_a_brand_new_machine_seen_only_off_hours_is_not_claimed_online(self):
        self._fail(1, expected_offline=True, name="newbox")
        m = self._machine("newbox")
        self.assertEqual(m["reachable"], 0)
        self.assertEqual(m["fail_streak"], 0)

    # (c)
    def test_the_default_call_is_byte_for_byte_todays_behaviour(self):
        """No `expected_offline` argument at all -- every existing caller."""
        storage.mark_unreachable(self.conn, "desktop", "ssh", "python", "err1")
        self.assertEqual(self._machine()["fail_streak"], 1)
        self.assertEqual(self._machine()["reachable"], 1)
        storage.mark_unreachable(self.conn, "desktop", "ssh", "python", "err2")
        self.assertEqual(self._machine()["reachable"], 0)


# ---------------------------------------------------------------------------
# The read side: what the dashboard and /api/data say
# ---------------------------------------------------------------------------

class SummaryAndStatus(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.conn = storage.connect(os.path.join(self.dir, "t.db"))
        self.cfg = _cfg(_target("desktop"), _target("homelab", window=None))
        for name in ("desktop", "homelab"):
            storage.save_scan(self.conn, name, "ssh", "python", {"repos": []})

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _machines(self, now):
        return {m["name"]: m
                for m in storage.get_machines(self.conn, self.cfg, now=now)}

    def _down(self, name, n=2, expected_offline=False):
        for _ in range(n):
            storage.mark_unreachable(self.conn, name, "ssh", "python",
                                     "No route to host",
                                     expected_offline=expected_offline)

    # (b)
    def test_an_unreachable_machine_outside_its_window_is_not_counted(self):
        self._down("desktop", expected_offline=True)
        s = storage.get_summary(self.conn, self.cfg, now=THU_0200)
        self.assertEqual(s["offline_machines"], 0)

    def test_an_offline_verdict_from_inside_the_window_stops_alarming_at_night(self):
        """The 22:45 case: two real failures while the machine WAS expected
        up, so reachable=0 honestly. By 02:00 nobody expects it up, so the
        count must not still be shouting -- it resumes when the window does."""
        self._down("desktop")
        self.assertEqual(
            storage.get_summary(self.conn, self.cfg, now=THU_NOON)["offline_machines"], 1)
        self.assertEqual(
            storage.get_summary(self.conn, self.cfg, now=THU_0200)["offline_machines"], 0)

    # (a)
    def test_an_unreachable_machine_inside_its_window_is_counted(self):
        self._down("desktop")
        s = storage.get_summary(self.conn, self.cfg, now=THU_NOON)
        self.assertEqual(s["offline_machines"], 1)

    # (c)
    def test_a_machine_with_no_window_is_counted_at_any_hour(self):
        self._down("homelab")
        for now in (THU_NOON, THU_0200):
            with self.subTest(now=now):
                self.assertEqual(
                    storage.get_summary(self.conn, self.cfg, now=now)["offline_machines"],
                    1)

    def test_get_machines_without_a_config_is_unannotated_as_before(self):
        rows = storage.get_machines(self.conn)
        self.assertTrue(rows)
        for r in rows:
            self.assertNotIn("off_hours", r)
            self.assertNotIn("status", r)

    def test_get_summary_without_a_config_counts_plainly(self):
        self._down("desktop", expected_offline=True)
        self._down("homelab")
        self.assertEqual(storage.get_summary(self.conn)["offline_machines"], 1)

    def test_status_words(self):
        self._down("desktop", expected_offline=True)
        night = self._machines(THU_0200)
        self.assertEqual(night["desktop"]["status"], "off-hours")
        self.assertEqual(night["desktop"]["expected_online"], "07:00-23:00 UTC")
        self.assertTrue(night["desktop"]["off_hours"])
        self.assertEqual(night["homelab"]["status"], "online")
        self.assertFalse(night["homelab"]["off_hours"])
        self.assertIsNone(night["homelab"]["expected_online"])

    def test_a_healthy_machine_outside_its_window_still_reads_online(self):
        """Scanned successfully at 02:00 because it happened to be up: that is
        a fact, and "off-hours" would be a worse description of it."""
        m = self._machines(THU_0200)["desktop"]
        self.assertEqual(m["status"], "online")
        self.assertTrue(m["off_hours"])

    def test_a_single_debounced_failure_inside_the_window_still_reads_online(self):
        self._down("desktop", n=1)
        self.assertEqual(self._machines(THU_NOON)["desktop"]["status"], "online")

    def test_an_unparseable_window_is_reported_and_suppresses_nothing(self):
        self.cfg = _cfg(_target("desktop", window="nonsense"))
        self._down("desktop")
        m = self._machines(THU_0200)["desktop"]
        self.assertEqual(m["status"], "offline")
        self.assertFalse(m["off_hours"])
        self.assertTrue(m["window_error"])
        self.assertEqual(
            storage.get_summary(self.conn, self.cfg, now=THU_0200)["offline_machines"], 1)


class DashboardRendering(unittest.TestCase):

    def _card(self, machine):
        m = {"name": "desktop", "reachable": 1, "error": None,
             "last_scanned": "2026-08-13T02:00:00Z"}
        m.update(machine)
        return render.render_machines([m], [])

    def test_an_off_hours_machine_says_so_instead_of_showing_a_green_dot(self):
        html = self._card({"status": "off-hours", "off_hours": True,
                           "expected_online": "07:00-23:00 America/New_York",
                           "error": "No route to host"})
        self.assertIn("off-hours", html)
        self.assertNotIn('class="dot on"', html)
        self.assertIn("expected online 07:00-23:00 America/New_York", html)
        self.assertNotIn('class="merr"', html)      # not an error line

    def test_an_offline_machine_still_shows_its_error(self):
        html = self._card({"status": "offline", "reachable": 0,
                           "error": "No route to host"})
        self.assertIn("offline", html)
        self.assertIn('class="merr"', html)
        self.assertIn("No route to host", html)

    def test_an_unannotated_row_renders_exactly_as_before(self):
        self.assertIn('class="dot on"', self._card({}))
        self.assertIn("offline", self._card({"reachable": 0}))

    def test_an_ignored_window_is_visible_on_the_card(self):
        html = self._card({"status": "online", "window_error": "nonsense"})
        self.assertIn("expected-online window ignored", html)


# ---------------------------------------------------------------------------
# The collector: which failures are judged
# ---------------------------------------------------------------------------

class CollectOneClassifiesTheFailure(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.conn = storage.connect(os.path.join(self.dir, "t.db"))

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _collect(self, target, ok=False, result="ssh scan failed (rc=255)"):
        defaults = {"timezone": "UTC"}
        with mock.patch("collector.scan_target", return_value=(ok, result)):
            return collector.collect_one(self.conn, target, defaults)

    def _machine(self, name="desktop"):
        return dict(self.conn.execute(
            "SELECT * FROM machines WHERE name=?", (name,)).fetchone())

    # (b)
    def test_a_failure_outside_the_window_is_not_a_fault(self):
        ok, _info, status = self._collect(_target(window=_window_excluding_now()))
        self.assertTrue(ok, "`ok` is the flag alerting consumers gate on")
        self.assertEqual(status, "off_hours")
        self.assertEqual(self._machine()["fail_streak"], 0)

    def test_repeated_off_hours_failures_never_trip_the_debounce(self):
        target = _target(window=_window_excluding_now())
        for _ in range(6):
            self._collect(target)
        m = self._machine()
        self.assertEqual(m["fail_streak"], 0)
        self.assertEqual(m["reachable"], 0)          # never succeeded, not a verdict

    # (a)
    def test_a_failure_inside_the_window_is_reported(self):
        ok, info, status = self._collect(_target(window=_window_around_now()))
        self.assertFalse(ok)
        self.assertEqual(status, "failed")
        self.assertIn("rc=255", info)
        self.assertEqual(self._machine()["fail_streak"], 1)

    # (c)
    def test_a_target_with_no_window_is_reported_exactly_as_today(self):
        ok, _info, status = self._collect(_target(window=None))
        self.assertFalse(ok)
        self.assertEqual(status, "failed")
        self.assertEqual(self._machine()["fail_streak"], 1)

    def test_a_target_whose_window_is_broken_is_reported_too(self):
        ok, _info, status = self._collect(_target(window="07:00"))
        self.assertFalse(ok)
        self.assertEqual(status, "failed")

    def test_a_successful_scan_off_hours_is_still_saved(self):
        """The scan is attempted regardless of the window, so a machine that
        happens to be up at 02:00 still refreshes its repos."""
        ok, info, status = self._collect(
            _target(window=_window_excluding_now()),
            ok=True, result={"repos": [{"path": "/a", "name": "a"}]})
        self.assertTrue(ok)
        self.assertEqual(status, "ok")
        self.assertEqual(info, 1)
        m = self._machine()
        self.assertEqual(m["reachable"], 1)
        self.assertIsNone(m["error"])

    def test_collect_all_carries_the_status_through(self):
        cfg = _cfg(_target("desktop", window=_window_excluding_now()),
                   _target("cncpc", window=None))
        with mock.patch("collector.scan_target", return_value=(False, "boom")):
            results = collector.collect_all(self.conn, cfg)
        self.assertEqual([(n, ok, st) for n, ok, _i, st in results],
                         [("desktop", True, "off_hours"),
                          ("cncpc", False, "failed")])


class TheIncidentItself(unittest.TestCase):
    """End to end, in the shape the 01:07 digest actually consumes: a night of
    scans against a machine that is off, then the two numbers that were red.

    Both alerts are checked WITH and WITHOUT the window, in one place. A
    suppression test that only ever runs the suppressed case cannot tell the
    difference between "correctly quiet" and "broken and quiet"."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.conn = storage.connect(os.path.join(self.dir, "t.db"))

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _night(self, window):
        """18 failed scans (9 hours at the 30-minute cadence), then the two
        fields the digest alarms on."""
        target = _target("desktop", window=window)
        cfg = _cfg(target)
        err = ("RuntimeError: ssh scan failed (rc=255): ssh: connect to host "
               "192.168.1.243 port 22: No route to host")
        storage.save_scan(self.conn, "desktop", "ssh", "python", {"repos": []})
        results = []
        with mock.patch("collector.scan_target", return_value=(False, err)):
            for _ in range(18):
                results = collector.collect_all(self.conn, cfg)
        summary = storage.get_summary(self.conn, cfg)
        return {
            # "ALERT machines OFFLINE: %s"
            "offline_machines": summary["offline_machines"],
            # "ALERT scan failed on %s"  <- `if not r.get("ok")`
            "failed_scans": [n for n, ok, _i, _st in results if not ok],
        }

    def test_with_a_window_the_night_is_quiet(self):
        got = self._night(_window_excluding_now())
        self.assertEqual(got, {"offline_machines": 0, "failed_scans": []})

    def test_without_a_window_both_alerts_still_fire(self):
        got = self._night(None)
        self.assertEqual(got, {"offline_machines": 1,
                               "failed_scans": ["desktop"]})

    def test_inside_the_window_both_alerts_still_fire(self):
        got = self._night(_window_around_now())
        self.assertEqual(got, {"offline_machines": 1,
                               "failed_scans": ["desktop"]})


# ---------------------------------------------------------------------------
# Config validation: a typo is refused where someone is looking
# ---------------------------------------------------------------------------

class ConfigValidation(unittest.TestCase):

    def _cfg(self, window, **kw):
        return _cfg(_target("desktop", window=window), **kw)

    def test_a_good_window_validates(self):
        self.assertTrue(collector.validate_config(self._cfg("07:00-23:00")))

    def test_a_good_full_window_validates(self):
        self.assertTrue(collector.validate_config(self._cfg(
            {"hours": "07:00-23:00", "days": ["mon", "fri"],
             "timezone": "UTC"}, timezone=None)))

    def test_no_window_validates_as_before(self):
        self.assertTrue(collector.validate_config(self._cfg(None)))

    def _rejects(self, cfg):
        with self.assertRaises(ValueError) as cm:
            collector.validate_config(cfg)
        self.assertIn("expected_online", str(cm.exception))

    def test_a_malformed_window_is_refused(self):
        self._rejects(self._cfg("7 to 11"))

    def test_a_window_with_no_timezone_anywhere_is_refused(self):
        cfg = self._cfg("07:00-23:00")
        del cfg["timezone"]
        self._rejects(cfg)

    def test_the_error_names_the_target(self):
        with self.assertRaises(ValueError) as cm:
            collector.validate_config(self._cfg("7 to 11"))
        self.assertIn("desktop", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
