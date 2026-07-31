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
            """INSERT INTO machines (name, ssh, remote_python, reachable, last_scanned, last_success, error)
               VALUES (?, ?, ?, 1, ?, ?, NULL)
               ON CONFLICT(name) DO UPDATE SET
                   ssh=excluded.ssh, remote_python=excluded.remote_python,
                   reachable=1, last_scanned=excluded.last_scanned,
                   last_success=excluded.last_success, error=NULL""",
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


def mark_unreachable(conn, machine, ssh, remote_python, error):
    """Flag a machine offline but keep its last snapshot intact."""
    ts = now_iso()
    with conn:
        conn.execute(
            """INSERT INTO machines (name, ssh, remote_python, reachable, last_scanned, error)
               VALUES (?, ?, ?, 0, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   ssh=excluded.ssh, remote_python=excluded.remote_python,
                   reachable=0, last_scanned=excluded.last_scanned, error=excluded.error""",
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


# ---- read side (used by the Flask app) -------------------------------------

def get_machines(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM machines ORDER BY name").fetchall()]


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


def get_root_warnings(conn):
    """Roots that are missing or yielded no repos, keyed by machine.
    Catches e.g. an unmounted NFS share silently dropping repos."""
    rows = conn.execute(
        "SELECT machine, path, exists_, found FROM machine_roots "
        "WHERE exists_=0 OR found=0 ORDER BY machine, path").fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["machine"], []).append(
            {"path": r["path"],
             "reason": "missing" if not r["exists_"] else "no repos found"})
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


def get_summary(conn, config=None):
    repos = get_repos(conn, config)
    machines = get_machines(conn)
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
    offline = sum(1 for m in machines if not m["reachable"])
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
        "signals": by_signal,
    }
