"""SQLite storage for git-monitor.

Shared by the collector (writes) and the Flask app (reads). One small file DB.
On a successful scan we replace a machine's repos + commit history atomically;
on an unreachable machine we keep the last snapshot and just flag it offline.
"""

import os
import sqlite3
from datetime import datetime, timezone

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
    dirty       INTEGER,
    ahead       INTEGER,
    behind      INTEGER,
    unpushed    INTEGER,
    has_remote  INTEGER,
    is_bare     INTEGER,
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
    return conn


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
        for r in result.get("repos", []):
            conn.execute(
                """INSERT INTO repos
                   (machine, path, name, branch, dirty, ahead, behind, unpushed,
                    has_remote, is_bare, last_commit, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    machine, r.get("path"), r.get("name"), r.get("branch"),
                    r.get("dirty"), r.get("ahead"), r.get("behind"),
                    r.get("unpushed"),
                    1 if r.get("has_remote") else 0,
                    1 if r.get("is_bare") else 0,
                    r.get("last_commit"), ts,
                ),
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


def get_repos(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM repos ORDER BY last_commit DESC").fetchall()]


def get_commit_days(conn):
    """Aggregate commit counts per day across all machines/repos."""
    rows = conn.execute(
        "SELECT day, SUM(count) AS c FROM commit_days GROUP BY day").fetchall()
    return {r["day"]: r["c"] for r in rows}


def get_summary(conn):
    repos = get_repos(conn)
    machines = get_machines(conn)
    dirty_repos = sum(1 for r in repos if (r["dirty"] or 0) > 0)
    unpushed_repos = sum(1 for r in repos if (r["unpushed"] or 0) > 0)
    unpushed_commits = sum(r["unpushed"] or 0 for r in repos)
    offline = sum(1 for m in machines if not m["reachable"])
    return {
        "total_repos": len(repos),
        "dirty_repos": dirty_repos,
        "unpushed_repos": unpushed_repos,
        "unpushed_commits": unpushed_commits,
        "machines": len(machines),
        "offline_machines": offline,
    }
