"""Per-target expected-online windows.

A machine that is deliberately powered off for part of the day is not a fault,
and reporting it as one costs the dashboard its meaning. The desktop target has
carried `ALERT machines OFFLINE: 1` + `ALERT scan failed on desktop` every
single night since 2026-08-10, when the nightly job moved to dserver and
nothing kept the desktop awake any more: "No route to host" at 01:07 on a
machine whose owner is asleep. A light that is guaranteed red is not a signal,
and the cost is not the light -- it is that every OTHER alert on that panel now
arrives next to a known-false one.

`OFFLINE_AFTER_FAILURES` (storage.py) cannot help here. Two consecutive
failures is a debounce against a BLIP -- one slow night, a host mid-reboot. A
machine that is off for nine hours fails every scan in a row and trips it on
the second one, exactly as designed.

So a target may declare when it is expected to be up:

    expected_online:
      hours: "07:00-23:00"          # local time in `timezone`; end exclusive
      days: [mon, tue, wed, thu, fri]   # optional; default: every day
      timezone: America/New_York    # required -- see _zone() below

    expected_online: "07:00-23:00"  # shorthand; timezone then comes from the
                                    # target's or the file's `timezone:` key

Outside that window an unreachable target is not debounced, not counted and not
alarmed -- it is simply not expected. The accepted cost, chosen deliberately:
a genuinely dead machine goes unreported until its window opens.

TWO THINGS THIS MODULE IS CAREFUL ABOUT
---------------------------------------
1. It fails OPEN. A window that will not parse, or a timezone this Python has
   no database for, yields "in window" plus a visible error string -- that is,
   exactly today's alerting behaviour, loudly, rather than a silent suppression
   nobody can see. A monitor that quietly stops monitoring is worse than one
   that cries wolf, so every unknown resolves toward alerting.
2. It never guesses a timezone. The collector runs in a python:3.12-slim
   container with no TZ set, so "now" there is UTC while the hours in this
   config are the ones on the wall in front of Evan -- four or five hours out.
   A window silently evaluated in the wrong zone is the worst outcome available
   here: it suppresses real alerts during the day and fires false ones at
   night, and looks like a working feature the whole time. An absent timezone
   is therefore an error, not a default.
"""

import datetime as dt
import re

#: Names accepted in `days`. Both the three-letter and the full form, because
#: which one a person writes is a coin flip and being wrong costs a silent
#: never-in-window.
_DAY_NAMES = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}

_HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")

#: Spellings of UTC that need no timezone database at all. Worth special-casing:
#: it is the one zone we can always honour, so a test (or a deployment with no
#: tzdata) is never forced through the failure path just to get a window.
_UTC_NAMES = {"UTC", "ETC/UTC", "GMT", "ETC/GMT", "Z"}


class WindowError(ValueError):
    """An `expected_online` spec that cannot be used as written."""


def _zone(name):
    """Resolve a timezone NAME to a tzinfo, or raise WindowError.

    There is no fallback to the host's local time on purpose. The collector
    runs in a container whose local time is UTC, so falling back would quietly
    shift every window by the UTC offset and produce a feature that looks like
    it works. Refusing is recoverable (the window is ignored, alerts behave as
    they do today, and the reason is on the dashboard); a four-hour skew is
    not, because nothing about it is visible.
    """
    if name is None or not str(name).strip():
        raise WindowError(
            "`expected_online` needs a `timezone` (e.g. America/New_York). "
            "The collector's own clock is UTC, so an omitted zone would "
            "silently shift the window by the UTC offset")
    text = str(name).strip()
    if text.upper() in _UTC_NAMES:
        return dt.timezone.utc
    try:
        from zoneinfo import ZoneInfo
    except ImportError as exc:                          # pragma: no cover
        raise WindowError("no zoneinfo module available (%s)" % exc)
    try:
        return ZoneInfo(text)
    except Exception as exc:
        # Most likely cause by far: no tz database on this interpreter. Python
        # ships none on Windows and slim container images often carry none
        # either -- hence `tzdata` in requirements.txt.
        raise WindowError(
            "unknown timezone %r (%s: %s). Install the `tzdata` package if "
            "this interpreter has no timezone database"
            % (text, type(exc).__name__, exc))


def _parse_time(text, field):
    m = _HHMM.match(str(text).strip())
    if not m:
        raise WindowError("%s time %r is not HH:MM" % (field, text))
    h, mi = int(m.group(1)), int(m.group(2))
    if h == 24 and mi == 0:
        if field != "end":
            raise WindowError("24:00 is only meaningful as the end of a window")
        return dt.time(0, 0)        # midnight at the END of the day
    if h > 23 or mi > 59:
        raise WindowError("%s time %r is not a real time of day" % (field, text))
    return dt.time(h, mi)


def _parse_days(value):
    if value is None:
        return None
    if isinstance(value, str) or not hasattr(value, "__iter__"):
        raise WindowError("`days` must be a list of day names, e.g. [mon, tue]")
    days = set()
    for d in value:
        key = str(d).strip().lower()
        if key not in _DAY_NAMES:
            raise WindowError("unknown day %r (use mon/tue/.../sun)" % d)
        days.add(_DAY_NAMES[key])
    if not days:
        raise WindowError(
            "`days` is empty, which would mean the machine is never expected "
            "online. Omit `expected_online` entirely instead")
    return days


class Window(object):
    """One target's expected-online window, resolved and ready to test."""

    __slots__ = ("start", "end", "days", "tz", "tzname", "hours")

    def __init__(self, start, end, days, tz, tzname, hours):
        self.start, self.end, self.days = start, end, days
        self.tz, self.tzname, self.hours = tz, tzname, hours

    def __str__(self):
        parts = [self.hours]
        if self.days is not None:
            order = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
            parts.append(",".join(order[i] for i in sorted(self.days)))
        parts.append(self.tzname)
        return " ".join(parts)

    def contains(self, now=None):
        """Is `now` (an AWARE datetime; default: real now) inside the window?"""
        if now is None:
            now = dt.datetime.now(dt.timezone.utc)
        if now.tzinfo is None:
            raise WindowError("`now` must be timezone-aware")
        local = now.astimezone(self.tz)
        t = local.time()
        if self.start == self.end:
            # Ambiguous as written -- 24 hours or none at all. Resolve toward
            # never suppressing anything, like every other unknown here.
            inside, anchor = True, local.date()
        elif self.start < self.end:
            inside, anchor = (self.start <= t < self.end), local.date()
        elif t >= self.start:                  # window crosses midnight
            inside, anchor = True, local.date()
        elif t < self.end:
            # Still inside YESTERDAY's window, so that is the day `days` has
            # an opinion about: "sat 22:00-02:00" covers Sunday 01:00.
            inside, anchor = True, local.date() - dt.timedelta(days=1)
        else:
            inside, anchor = False, local.date()
        if inside and self.days is not None:
            inside = anchor.weekday() in self.days
        return inside


def parse(spec, default_tz=None):
    """Build a Window from a raw `expected_online` spec. Raises WindowError."""
    if isinstance(spec, str):
        spec = {"hours": spec}
    if not isinstance(spec, dict):
        raise WindowError(
            "`expected_online` must be \"HH:MM-HH:MM\" or a mapping with "
            "`hours` (and optional `days`, `timezone`)")
    hours = spec.get("hours")
    if not hours or not isinstance(hours, str):
        raise WindowError("`expected_online` needs `hours`, e.g. \"07:00-23:00\"")
    if hours.count("-") != 1:
        raise WindowError("`hours` must be HH:MM-HH:MM, got %r" % hours)
    raw_start, raw_end = hours.split("-")
    start = _parse_time(raw_start, "start")
    end = _parse_time(raw_end, "end")
    if start == end:
        raise WindowError(
            "`hours` %r starts and ends at the same time, which could mean "
            "always or never" % hours)
    tzname = spec.get("timezone") or default_tz
    return Window(start, end, _parse_days(spec.get("days")), _zone(tzname),
                  str(tzname).strip(), hours.strip())


def spec_for(target, defaults=None):
    """The raw spec for a target, or None. A file-level `expected_online:`
    applies to every target, like `remote_python` and `timeout` do."""
    defaults = defaults or {}
    spec = target.get("expected_online")
    if spec is None:
        spec = defaults.get("expected_online")
    return spec


def window_for(target, defaults=None):
    """(Window|None, error|None). Never raises: an unusable spec comes back as
    a message for the caller to SHOW, and no window -- see the module docstring
    on failing open.

    The two are mutually exclusive by construction: an error ALWAYS comes with
    window=None, so callers need only test the window. (An earlier version had
    every caller check both, which reads as caution but is a branch no input
    can reach -- and an untestable branch is where a real bug hides.)
    """
    spec = spec_for(target, defaults)
    if spec is None:
        return None, None
    defaults = defaults or {}
    default_tz = target.get("timezone") or defaults.get("timezone")
    try:
        return parse(spec, default_tz), None
    except WindowError as exc:
        return None, str(exc)
    except Exception as exc:            # a spec is user input; nothing here
        return None, "%s: %s" % (type(exc).__name__, exc)   # may kill a scan


def off_hours(target, defaults=None, now=None):
    """Is this target OUTSIDE its expected-online window right now?

    False whenever we are not certain -- no window configured, or a window we
    could not resolve. Only a window that parsed cleanly and genuinely excludes
    `now` suppresses anything.
    """
    window, _err = window_for(target, defaults)
    if window is None:          # not declared, or not usable -- either way we
        return False            # know nothing, so we suppress nothing
    try:
        return not window.contains(now)
    except WindowError:         # a caller handed us a naive `now`; same rule
        return False


def targets_by_name(config):
    out = {}
    for t in (config or {}).get("targets") or []:
        if isinstance(t, dict) and t.get("name"):
            out[t["name"]] = t
    return out


def annotate(machines, config, now=None):
    """Tag machine rows with their window state, in place.

    Adds, on every row:
        expected_online  the window as text, or None
        off_hours        True only if outside a window that parsed cleanly
        window_error     why a configured window was ignored, or None
        status           "online" | "offline" | "off-hours"

    `status` is derived here rather than in the renderer so that the dashboard,
    /api/data and the offline COUNT cannot disagree about what a machine is.
    A machine with no window gets off_hours=False and the same online/offline
    status the `reachable` column has always produced.
    """
    if not machines:
        return machines
    by_name = targets_by_name(config)
    defaults = {k: v for k, v in (config or {}).items() if k != "targets"}
    for m in machines:
        target = by_name.get(m.get("name"))
        window, err = window_for(target, defaults) if target else (None, None)
        outside = False
        if window is not None:
            try:
                outside = not window.contains(now)
            except WindowError as exc:
                err = str(exc)
        m["expected_online"] = str(window) if window is not None else None
        m["window_error"] = err
        m["off_hours"] = outside
        reachable = bool(m.get("reachable"))
        # A machine inside its window that merely failed one scan is still
        # "online" (the debounce's whole point). Outside the window, a failed
        # scan is what off-hours MEANS -- so say so, instead of showing a green
        # dot for a machine that is unplugged.
        if outside and (not reachable or m.get("error")):
            m["status"] = "off-hours"
        else:
            m["status"] = "online" if reachable else "offline"
    return machines
