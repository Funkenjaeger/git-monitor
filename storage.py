"""SQLite storage for git-monitor.

Shared by the collector (writes) and the Flask app (reads). One small file DB.
On a successful scan we replace a machine's repos + commit history atomically;
on an unreachable machine we keep the last snapshot and just flag it offline.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

import signals
import uptime

# The repos table deliberately declares only the columns that are NOT signals
# (see signals.py). Every signal column is added by _migrate below, on fresh and
# existing databases alike, so its type is written down in exactly one place.
# Declaring a column in CREATE TABLE and again in an ALTER is what broke
# the stashes/untracked migration -- the two spellings disagreed on affinity, so
# a fresh DB stored ints and an upgraded one handed back strings. With one code
# path that mismatch has nowhere to come from.
SCHEMA = """
CREATE TABLE IF NOT EXISTS machines (
    name          TEXT PRIMARY KEY,
    ssh           TEXT,
    remote_python TEXT,
    reachable     INTEGER DEFAULT 0,
    last_scanned  TEXT,
    last_success  TEXT,
    error         TEXT
);
CREATE TABLE IF NOT EXISTS repos (
    machine     TEXT,
    path        TEXT,
    name        TEXT,
    branch      TEXT,
    ahead       INTEGER,
    behind      INTEGER,
    last_commit TEXT,
    updated_at  TEXT,
    PRIMARY KEY (machine, path)
);
CREATE TABLE IF NOT EXISTS commit_days (
    machine   TEXT,
    repo_path TEXT,
    day       TEXT,
    count     INTEGER,
    PRIMARY KEY (machine, repo_path, day)
);
CREATE TABLE IF NOT EXISTS repo_lineage (
    machine TEXT,
    path    TEXT,
    branch  TEXT,
    shas    TEXT,
    PRIMARY KEY (machine, path, branch)
);
CREATE TABLE IF NOT EXISTS machine_roots (
    machine TEXT,
    path    TEXT,
    exists_ INTEGER,
    found   INTEGER,
    PRIMARY KEY (machine, path)
);
CREATE INDEX IF NOT EXISTS idx_commit_days_day ON commit_days(day);
CREATE INDEX IF NOT EXISTS idx_repos_machine ON repos(machine);
"""


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path):
    d = os.path.dirname(db_path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


# Non-signal payload columns. JSON blobs and identity fields the renderer never
# chips: they can't go missing the way a signal can, because something visibly
# breaks when they do.
_PAYLOAD_COLS = (
    ("head_sha", "TEXT"), ("root_key", "TEXT"),
    ("branch_tips", "TEXT"), ("branch_dates", "TEXT"),
    ("remotes", "TEXT"), ("unpushed_by_remote", "TEXT"),
    # A JSON-encoded list of matched paths, not a count.
    ("precious_files", "TEXT"),
)
_JSON_COLS = ("branch_tips", "branch_dates", "remotes", "unpushed_by_remote")


def _migrate(conn):
    """Bring an existing DB up to the current column set.

    Signal columns come from signals.STORED rather than a list kept here, so
    registering a signal is all it takes to persist it. `worktrees` was scanned
    and rendered for weeks with no column to land in -- the chip simply never
    appeared, on any machine, for anyone."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(repos)")}
    with conn:
        for col, typ in _PAYLOAD_COLS + signals.STORED:
            if col not in have:
                conn.execute("ALTER TABLE repos ADD COLUMN %s %s" % (col, typ))
        have_m = {r["name"] for r in conn.execute("PRAGMA table_info(machines)")}
        if "fail_streak" not in have_m:
            # Consecutive-failure counter backing the OFFLINE debounce in
            # mark_unreachable/save_scan below. Additive and defaulted, like the
            # repos columns above, so a live DB with rows in it loads with no
            # manual migration.
            conn.execute(
                "ALTER TABLE machines ADD COLUMN fail_streak INTEGER DEFAULT 0")


def _repo_row(machine, ts, r):
    """One repos row as (columns, values).

    Signal columns are appended from the registry, so a signal that scan.py
    produces reaches the database without anyone remembering to widen an INSERT
    here -- the stage where `worktrees` was silently dropped on the floor."""
    cols = ["machine", "path", "name", "branch", "ahead", "behind",
            "last_commit", "updated_at"]
    vals = [machine, r.get("path"), r.get("name"), r.get("branch"),
            r.get("ahead"), r.get("behind"), r.get("last_commit"), ts]
    for col, _typ in _PAYLOAD_COLS:
        if col in _JSON_COLS:
            vals.append(json.dumps(r.get(col) or {}))
        elif col == "precious_files":
            # None (not configured for this scan) stays NULL, distinct from
            # "[]" (configured, nothing matched) -- see get_repos().
            vals.append(json.dumps(r[col]) if r.get(col) is not None else None)
        else:
            vals.append(r.get(col))
        cols.append(col)
    for s in signals.SIGNALS:
        if s.stored:
            cols.append(s.key)
            vals.append(s.to_db(r))
    return cols, vals


def save_scan(conn, machine, ssh, remote_python, result):
    """Persist a successful scan, replacing this machine's rows."""
    ts = now_iso()
    with conn:
        conn.execute(
            """INSERT INTO machines (name, ssh, remote_python, reachable, last_scanned, last_success, error, fail_streak)
               VALUES (?, ?, ?, 1, ?, ?, NULL, 0)
               ON CONFLICT(name) DO UPDATE SET
                   ssh=excluded.ssh, remote_python=excluded.remote_python,
                   reachable=1, last_scanned=excluded.last_scanned,
                   last_success=excluded.last_success, error=NULL, fail_streak=0""",
            (machine, ssh, remote_python, ts, ts),
        )
        conn.execute("DELETE FROM repos WHERE machine=?", (machine,))
        conn.execute("DELETE FROM commit_days WHERE machine=?", (machine,))
        conn.execute("DELETE FROM machine_roots WHERE machine=?", (machine,))
        conn.execute("DELETE FROM repo_lineage WHERE machine=?", (machine,))
        for rt in result.get("roots", []):
            conn.execute(
                """INSERT OR REPLACE INTO machine_roots (machine, path, exists_, found)
                   VALUES (?,?,?,?)""",
                (machine, rt.get("path"), 1 if rt.get("exists") else 0, rt.get("found", 0)),
            )
        for r in result.get("repos", []):
            cols, vals = _repo_row(machine, ts, r)
            conn.execute(
                "INSERT INTO repos (%s) VALUES (%s)"
                % (", ".join(cols), ", ".join("?" * len(cols))),
                vals,
            )
            for branch, shas in (r.get("lineage") or {}).items():
                conn.execute(
                    """INSERT OR REPLACE INTO repo_lineage (machine, path, branch, shas)
                       VALUES (?,?,?,?)""",
                    (machine, r.get("path"), branch, "\n".join(shas)),
                )
            for day, count in (r.get("commit_days") or {}).items():
                conn.execute(
                    """INSERT OR REPLACE INTO commit_days (machine, repo_path, day, count)
                       VALUES (?,?,?,?)""",
                    (machine, r.get("path"), day, count),
                )


# Consecutive scan failures required before a machine flips to OFFLINE. A
# lone timeout is routine (a slow network blip, a host mid-reboot); flapping
# the dashboard to OFFLINE and back for one bad cycle trains everyone to
# ignore the label. Two in a row is the signal.
OFFLINE_AFTER_FAILURES = 2


def mark_unreachable(conn, machine, ssh, remote_python, error,
                     expected_offline=False):
    """Record a failed scan; flip the machine OFFLINE only on the Nth
    consecutive failure (see OFFLINE_AFTER_FAILURES).

    The error and last_scanned time are recorded on every failure regardless,
    so a debounced first failure is not silent -- it just doesn't (yet) claim
    the machine is down. A success anywhere in between resets the streak (see
    save_scan), so this counts CONSECUTIVE failures, not failures overall.

    `expected_offline` is for a target that declared an expected-online window
    and is outside it right now (see uptime.py): the attempt and its error are
    still recorded, but nothing is CONCLUDED from them. The debounce above is
    the wrong instrument for a machine that is off every night by design -- a
    nine-hour absence fails every consecutive scan and trips a
    two-in-a-row rule on the second one, which is what put a permanent red
    OFFLINE on a desktop that was working exactly as intended.
    """
    ts = now_iso()
    if expected_offline:
        # No fail_streak increment and no `reachable` change: this failure is
        # not evidence about the machine's health, so it must not accumulate
        # into a verdict, and it must not clear one either (a machine that
        # went offline DURING its window keeps that state until it succeeds).
        with conn:
            conn.execute(
                """INSERT INTO machines (name, ssh, remote_python, reachable, last_scanned, error, fail_streak)
                   VALUES (?, ?, ?, 0, ?, ?, 0)
                   ON CONFLICT(name) DO UPDATE SET
                       ssh=excluded.ssh, remote_python=excluded.remote_python,
                       last_scanned=excluded.last_scanned, error=excluded.error""",
                (machine, ssh, remote_python, ts, error),
            )
        return
    with conn:
        conn.execute(
            """INSERT INTO machines (name, ssh, remote_python, reachable, last_scanned, error, fail_streak)
               VALUES (?, ?, ?, 0, ?, ?, 1)
               ON CONFLICT(name) DO UPDATE SET
                   ssh=excluded.ssh, remote_python=excluded.remote_python,
                   last_scanned=excluded.last_scanned, error=excluded.error,
                   fail_streak=machines.fail_streak + 1,
                   reachable=CASE WHEN machines.fail_streak + 1 >= %d
                                  THEN 0 ELSE machines.reachable END"""
            % OFFLINE_AFTER_FAILURES,
            (machine, ssh, remote_python, ts, error),
        )


def prune_machines(conn, keep_names):
    """Drop machines (and their rows) no longer present in config."""
    with conn:
        rows = conn.execute("SELECT name FROM machines").fetchall()
        for row in rows:
            if row["name"] not in keep_names:
                conn.execute("DELETE FROM repos WHERE machine=?", (row["name"],))
                conn.execute("DELETE FROM commit_days WHERE machine=?", (row["name"],))
                conn.execute("DELETE FROM machines WHERE name=?", (row["name"],))


# ---- snapshot age ----------------------------------------------------------

# How old a machine's last SUCCESSFUL scan may get before its snapshot stops
# being presented as current fact.
#
# Why three days. The number has to clear the longest ordinary silence -- a
# weekend away, a long holiday Monday, a machine left off for a trip -- or the
# warning fires on healthy setups and gets ignored, which is how the OFFLINE
# label on a nightly-off desktop became worthless before `expected_online`
# existed. It also has to be short enough that a real absence cannot run its
# course unnoticed: the incident this exists for is a desktop that sat about
# two weeks off wired ethernet in Aug 2026 while every nightly digest reported
# its git state as current fact. Three days is the widest gap that still
# catches that inside the first week.
# THIS IS THE SHARED ATTENTION HORIZON, NOT A LOCAL CHOICE. The same 3 days is
# houston IMAGE_PIN_DRIFT_WARN_D, and the rationale is documented once in the
# homelab wiki at docs/conventions.md, "The attention horizon: 3 days". Change
# it there and follow the citations rather than editing one number in isolation.
# That page also records the honest provenance: both constants were set to 3 on
# 2026-08-17 hours apart by the same reasoning, so their agreement is NOT
# independent confirmation -- it is one judgement applied twice, never
# cross-checked. The page also draws the boundary this number keeps being
# mistaken for: it is not an urgency threshold. A republished version-pinned
# tag or a failed backup is a finding at zero days. This applies only where
# being behind is normal and only being behind A WHILE is not.
DEFAULT_STALE_AFTER_DAYS = 3


def stale_after_days(config=None):
    """The staleness threshold in days, read the same way app.py reads
    `scan_interval_minutes` and collector.py reads `since_days`: a plain
    top-level key with a default, coerced here so a hand-edited string
    ("3") behaves like the number.

    An unusable value falls back to the default rather than disabling the
    check -- a threshold of 0 or less is not a threshold, it is an alarm that
    fires on a machine scanned one second ago, and an alarm that always fires
    is worth no more than one that never does.
    """
    try:
        days = float((config or {}).get("stale_after_days",
                                        DEFAULT_STALE_AFTER_DAYS))
    except (TypeError, ValueError):
        return float(DEFAULT_STALE_AFTER_DAYS)
    if days <= 0:
        return float(DEFAULT_STALE_AFTER_DAYS)
    return days


def _snapshot_age(last_success, now):
    """(age_in_days, reason) for one machine's last successful scan.

    reason is None when the timestamp parsed and the age is a real number;
    otherwise the age is None and the reason says why:

        "never"       no successful scan has ever been recorded
        "unreadable"  a timestamp is stored and it does not parse

    Both are stale, and neither is an age -- absent data is not old data, and
    the card has to be able to say which it is looking at.
    """
    if not last_success:
        return None, "never"
    text = str(last_success)
    try:
        # storage.now_iso() writes UTC with a trailing Z, which fromisoformat
        # rejects before 3.11; same swap render.rel_time makes.
        t = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith("Z") else text)
    except (TypeError, ValueError):
        return None, "unreadable"
    if t.tzinfo is None:
        # No offset at all: UTC is the only reading that doesn't invent one.
        t = t.replace(tzinfo=timezone.utc)
    return (now - t).total_seconds() / 86400.0, None


def annotate_staleness(machines, config=None, now=None):
    """Tag machine rows with the age of their snapshot, in place.

    Adds, on every row:
        snapshot_age_days  days since the last SUCCESSFUL scan (float), or
                           None when there is no usable timestamp
        stale              True if that snapshot is older than the threshold
        stale_reason       "aged" | "never" | "unreadable", or None when fresh

    WHY THIS IS NOT A HEALTH CHECK. An unreachable machine keeps its last
    snapshot (see save_scan / mark_unreachable), so every repo row it ever
    produced keeps being served as though it had been observed just now.
    `offline_machines` cannot cover that: it is deliberately blind to a
    machine outside its expected-online window, and the desktop's window is
    07:00-23:00 while the consumer that reads this runs at 01:07 -- so the one
    host most likely to be unplugged for a fortnight is the one host that
    counter can never report. In Aug 2026 that desktop sat about two weeks off
    wired ethernet and nothing anywhere said so.

    So staleness is measured against the CLOCK and nothing else. It is not
    gated on `reachable`, and it is not gated on `off_hours`: a machine that
    is off-hours is behaving exactly as declared AND its data can still be a
    week old, and those are two different sentences. Gating this on either one
    would rebuild the blind spot it closes.

    Evaluated at READ time for the same reason uptime.annotate is: "how old is
    this snapshot" has a different answer every hour, and the answer stored at
    scan time is the one answer guaranteed to be wrong.

    Nothing here hides or drops the stale machine's repos. The snapshot is
    still the best information there is about that host; the defect was that
    it was unlabelled, and a label is the whole fix.
    """
    if not machines:
        return machines
    limit = stale_after_days(config)
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    for m in machines:
        age, reason = _snapshot_age(m.get("last_success"), now)
        m["snapshot_age_days"] = age
        if reason is not None:          # never scanned, or an unreadable time
            m["stale"] = True
            m["stale_reason"] = reason
        else:
            m["stale"] = age > limit
            m["stale_reason"] = "aged" if m["stale"] else None
    return machines


# ---- read side (used by the Flask app) -------------------------------------

def get_machines(conn, config=None, now=None):
    """Machine rows. Pass `config` to have each row annotated with its
    expected-online state (see uptime.annotate) -- which window it declared,
    whether it is outside that window right now, and the status word the
    dashboard should print.

    Evaluated at READ time, against the current clock, because that is the
    question being asked: "is this machine expected to be up NOW?". Storing the
    answer at scan time instead would freeze a judgement that goes stale within
    the hour -- a machine that failed twice at 22:45 (inside its window, so
    genuinely OFFLINE) would still be shouting OFFLINE at 03:00, when nobody
    expects it to be up at all.

    Snapshot age is annotated whether or not a config was passed (see
    annotate_staleness): the config only carries the threshold, which has a
    default, and "how old is this data" is a question a caller with no usable
    config.yaml needs answered more than most.
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM machines ORDER BY name").fetchall()]
    if config is not None:
        uptime.annotate(rows, config, now=now)
    annotate_staleness(rows, config, now=now)
    return rows


def get_repos(conn, config=None):
    """Repo rows. Pass `config` to have declared-precious files classified into
    covered / orphaned / unknown -- that classification is a fact about the
    host's backup arrangement, which lives in the config and not in the DB."""
    rows = []
    for r in conn.execute("SELECT * FROM repos ORDER BY last_commit DESC"):
        d = dict(r)
        for col in ("branch_tips", "branch_dates", "remotes", "unpushed_by_remote"):
            try:
                d[col] = json.loads(d.get(col) or "{}")
            except (ValueError, TypeError):
                d[col] = {}
        # NULL means precious_patterns wasn't configured for this scan (no
        # opinion); "[]" means it was configured and nothing matched. Keep
        # that distinction instead of collapsing both to the same default.
        raw = d.get("precious_files")
        if raw is None:
            d["precious_files"] = None
        else:
            try:
                d["precious_files"] = json.loads(raw)
            except (ValueError, TypeError):
                d["precious_files"] = None
        rows.append(d)
    if config is not None:
        import coverage
        coverage.annotate(rows, config)
    return rows


def get_lineages(conn):
    """(machine, path) -> {branch: [sha, ...]} for cross-copy comparison."""
    out = {}
    for r in conn.execute("SELECT machine, path, branch, shas FROM repo_lineage"):
        out.setdefault((r["machine"], r["path"]), {})[r["branch"]] = \
            (r["shas"] or "").split("\n")
    return out


def get_projects(conn, config=None):
    import projects
    return projects.build_projects(get_repos(conn, config), get_lineages(conn))


def _repo_key(name):
    """Repo names are compared case-insensitively: `roots` on the desktop
    target are NTFS paths, where Reflex-UI and reflex-ui are the same
    directory, and an alarm that fires on letter case is noise."""
    return (name or "").strip().lower()


def get_missing_repos(conn, config=None, now=None):
    """Repos a target DECLARES it should be carrying that the last scan did
    NOT find, keyed by machine.

    Every other check in this file reports on a repo that IS there. Nothing
    reported on one that stopped being there. A repo that falls out of scope --
    a path that moved, a `depth` set one level too shallow, a root that
    silently stopped matching -- simply produces no rows, and no rows is also
    exactly what a machine with nothing to say looks like. There is no signal
    to fire, because there is no repo to fire it on.

    2026-08-17 is what that costs. elspi's only root was {path: /, depth: 1},
    so /home/default/projects/reflex-fw (three levels down) was never walked.
    Both reflex-ui and reflex-fw were carrying single-copy work that night; the
    nightly digest alarmed about reflex-ui and said nothing at all about
    reflex-fw -- and its silence was indistinguishable from reflex-fw being
    fine. `roots` says where to look; `expected_repos` says what has to come
    back.

    DECLARED, not inferred from what was found last time. A "we saw it
    yesterday and not today" rule cannot catch the case it is most needed for
    -- a scan root that was wrong from the day it was written, which is the
    08-17 shape exactly -- and it would turn every intentional repo deletion
    into an alarm that has to be cleared by hand.

    Evaluated at READ time against the current config, like precious coverage
    (coverage.py) and expected-online (uptime.py): the declaration lives in
    config.yaml, so editing it takes effect on the next page load rather than
    on the next successful scan of a machine that may well be down.

    A machine is only checked when the DB holds a scan worth checking:

      * status must be "online". An OFFLINE or off-hours host already explains
        the absence, and alarming there would make every host that is merely
        powered off emit one false alarm per declared repo, every cycle -- the
        same way an unwindowed desktop used to emit a nightly OFFLINE. (A
        first, debounced failure keeps `reachable` AND the previous snapshot,
        so it stays checked, against real data.)
      * last_success must be set. A target that has never once been scanned
        successfully has no repo list to compare against, and "scan failed on
        X" is already the alarm for that.
    """
    if not config:
        return {}
    by_name = uptime.targets_by_name(config)
    found = {}
    for r in conn.execute("SELECT machine, name FROM repos"):
        found.setdefault(r["machine"], set()).add(_repo_key(r["name"]))
    out = {}
    for m in get_machines(conn, config, now=now):
        declared = (by_name.get(m.get("name")) or {}).get("expected_repos") or []
        if not declared:
            continue
        if m.get("status") != "online" or not m.get("last_success"):
            continue
        here = found.get(m["name"], set())
        gone = [str(d) for d in declared if _repo_key(d) not in here]
        if gone:
            out[m["name"]] = gone
    return out


def get_root_warnings(conn, config=None, now=None):
    """Roots that are missing or yielded no repos, keyed by machine.
    Catches e.g. an unmounted NFS share silently dropping repos.

    Pass `config` and this also carries declared repos that did not come back
    (see get_missing_repos), tagged `kind: "missing_repo"`. They ride THIS
    channel rather than one of their own because this is the channel that is
    already being read: the nightly digest's dashboard-health block loops over
    /api/data's `root_warnings` and prints one ALERT line per entry, so a
    finding that only a new consumer could see is a finding nobody would see
    for as long as it took to teach that consumer about it. The reason text
    leads with `missing:<repo>` so the digest line names the repo it is about.
    /api/data also exposes them on their own under `missing_repos`.
    """
    rows = conn.execute(
        "SELECT machine, path, exists_, found FROM machine_roots "
        "WHERE exists_=0 OR found=0 ORDER BY machine, path").fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["machine"], []).append(
            {"path": r["path"], "kind": "root",
             "reason": "missing" if not r["exists_"] else "no repos found"})
    for machine, names in get_missing_repos(conn, config, now=now).items():
        for nm in names:
            out.setdefault(machine, []).append(
                {"path": nm, "kind": "missing_repo",
                 "reason": "missing:%s -- declared in expected_repos, "
                           "not found by this scan" % nm})
    return out


def get_repo_errors(conn):
    """Repos the scanner reached but couldn't read, keyed by machine.
    Catches e.g. git refusing a repo for 'dubious ownership', which otherwise
    reports as a repo with every field null and no visible complaint."""
    rows = conn.execute(
        "SELECT machine, path, name, error FROM repos "
        "WHERE error IS NOT NULL AND error != '' ORDER BY machine, path").fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["machine"], []).append(dict(r))
    return out


def get_commit_days(conn):
    """Aggregate commit counts per day across all machines/repos."""
    rows = conn.execute(
        "SELECT day, SUM(count) AS c FROM commit_days GROUP BY day").fetchall()
    return {r["day"]: r["c"] for r in rows}


def get_summary(conn, config=None, now=None):
    repos = get_repos(conn, config)
    machines = get_machines(conn, config, now=now)
    # Every registered signal, counted both ways, so a signal added later is in
    # /api/summary without a line being written here. The named keys below are
    # the curated ones the dashboard's stat tiles and the Homepage widget read;
    # they are derived from the same totals rather than recounted.
    by_signal = {
        s.key: {"repos": sum(1 for r in repos if s.fires(r)),
                "total": sum(s.count(r) for r in repos)}
        for s in signals.SIGNALS
    }
    n = lambda key, unit: by_signal[key][unit]
    precious_repos = sum(1 for r in repos if r.get("precious_files"))
    precious_files_total = sum(len(r.get("precious_files") or []) for r in repos)
    # A machine outside its declared expected-online window is not counted:
    # `offline_machines` is what downstream monitors alarm on, and an alarm
    # that fires every night on a desktop that is SUPPOSED to be off is a
    # false positive that devalues every other number on the panel. With no
    # config passed, or no window declared, off_hours is False everywhere and
    # this is the plain `not reachable` count it has always been.
    offline = sum(1 for m in machines
                  if not m["reachable"] and not m.get("off_hours"))
    # Counted INDEPENDENTLY of the line above, not as a subset of it. Offline
    # asks "is this machine up right now"; stale asks "how old is the data on
    # the screen", and the second question is the one nobody was asking. A
    # machine can be off-hours (and so, correctly, offline=0) while its
    # snapshot is a fortnight old, or be down since this morning with data
    # from an hour ago. A host may land in both counters, either, or neither;
    # what it must not do is fall between them, which is what it did.
    stale = sum(1 for m in machines if m.get("stale"))
    return {
        "total_repos": len(repos),
        "dirty_repos": n("dirty", "repos"),
        "unpushed_repos": n("unpushed", "repos"),
        "stash_repos": n("stashes", "repos"),
        "untracked_repos": n("untracked", "repos"),
        "precious_repos": precious_repos,
        "precious_files_total": precious_files_total,
        # Split by backup coverage (see coverage.py). The headline number is the
        # ORPHANED count -- the only one that can reach zero, and so the only
        # one worth an alarm colour. Covered/unknown stay available for the
        # tooltip and for /api/summary consumers. All three are 0 when no config
        # was passed, in which case precious_files_total is still the honest
        # total.
        "precious_orphaned_files": n("precious_orphaned", "total"),
        "precious_orphaned_repos": n("precious_orphaned", "repos"),
        "precious_covered_files": n("precious_covered", "total"),
        "precious_unknown_files": n("precious_unknown", "total"),
        "unpushed_commits": n("unpushed", "total"),
        "machines": len(machines),
        "offline_machines": offline,
        "stale_machines": stale,
        "signals": by_signal,
    }
