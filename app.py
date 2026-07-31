"""git-monitor Flask app.

Serves the dashboard (heatmap + top project list + machine status), the
Homepage summary widget endpoint, and full JSON. A background scheduler runs
the collector every N minutes; a Refresh button triggers an on-demand scan.

Env:
    GITMON_CONFIG  path to config.yaml   (default ./config.yaml)
    GITMON_DB      path to sqlite db     (default ./data.db)
    GITMON_PORT    listen port           (default 8083)
"""

import functools
import hmac
import os
import threading
import time

from flask import Flask, abort, jsonify, redirect, request, url_for

import collector
import storage
from render import render_config_page, render_page

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("GITMON_CONFIG", os.path.join(HERE, "config.yaml"))
DB_PATH = os.environ.get("GITMON_DB", os.path.join(HERE, "data.db"))
PORT = int(os.environ.get("GITMON_PORT", "8083"))

app = Flask(__name__)
_scan_lock = threading.Lock()
_last_scan = {"running": False, "at": None, "results": None}


def get_conn():
    return storage.connect(DB_PATH)


def run_scan():
    """Run one collection pass. Config is re-read each time so edits to
    config.yaml (add/remove targets) take effect without a restart."""
    if not _scan_lock.acquire(blocking=False):
        return {"skipped": "a scan is already running"}
    _last_scan["running"] = True
    try:
        config = collector.load_config(CONFIG_PATH)
        conn = get_conn()
        results = collector.collect_all(conn, config)
        conn.close()
        _last_scan["at"] = storage.now_iso()
        _last_scan["results"] = [
            {"machine": n, "ok": ok, "info": info} for n, ok, info in results
        ]
        return {"results": _last_scan["results"], "at": _last_scan["at"]}
    finally:
        _last_scan["running"] = False
        _scan_lock.release()


def scheduler_loop():
    # First pass shortly after startup, then on the configured interval.
    time.sleep(2)
    while True:
        try:
            config = collector.load_config(CONFIG_PATH)
            interval = int(config.get("scan_interval_minutes", 30))
        except Exception:
            interval = 30
        try:
            run_scan()
        except Exception as exc:  # never let the loop die
            app.logger.warning("scan failed: %s", exc)
        time.sleep(max(60, interval * 60))


def _config_or_empty():
    """Config for the read side. Backup coverage for precious files is declared
    there (see coverage.py), so a page render needs it -- but an unparseable
    config must degrade to "coverage unknown", not to a blank dashboard."""
    try:
        return collector.load_config(CONFIG_PATH)
    except Exception:
        return {}


# The control plane -- the config editor, the config API and the scan triggers --
# is reachable two ways, and only one of them is authenticated.
#
#   through lanauth:  Pocket ID login, then Caddy stamps GATE_HEADER
#   direct:           http://192.168.1.211:8083, which any LAN host can reach
#
# Source IP cannot tell them apart: docker SNATs published-port traffic to the
# bridge gateway, so a gated request and a direct one arrive from the same
# address. The header is the only distinguishing signal. This mirrors houston,
# which does exactly this with X-Houston-Gate.
#
# What is NOT gated is deliberate: the dashboard and the read APIs stay open.
# Reading which repos have uncommitted work is low-risk and there is no
# attribution to preserve. What can CHANGE something is what needs the gate --
# /api/config rewrites the collector's ssh targets and remote_python, and
# /api/config/test opens an SSH connection to a target supplied in the request
# body.
GATE_HEADER = "X-Gitmonitor-Gate"
GATE_SECRET = os.environ.get("GITMON_GATE_SECRET", "")


def through_gate():
    """Did this request arrive through lanauth? Used to decide whether to render
    control-plane affordances, not to authorise anything -- `gated` does that."""
    return bool(GATE_SECRET) and hmac.compare_digest(
        request.headers.get(GATE_HEADER, ""), GATE_SECRET)


def gated(fn):
    """Refuse a control-plane request that did not come through lanauth.

    Fails CLOSED when no secret is configured. An unset secret means the gate
    was never wired up, and the wrong response to that is not "let everyone in"
    -- the same reasoning that put webedge on oauth2-proxy, which refuses to
    start without an explicit allowlist, rather than tinyauth, whose policy
    defaulted to allow.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not GATE_SECRET:
            abort(503, "control plane disabled: GITMON_GATE_SECRET is not set. "
                       "Set it in the compose env and give lanauth the same value.")
        # compare_digest, not ==: a shared secret compared byte-by-byte leaks
        # its prefix through timing.
        if not hmac.compare_digest(request.headers.get(GATE_HEADER, ""), GATE_SECRET):
            abort(403, "the control plane is reachable only through lanauth "
                       "(https://gitmonitor.funkenjaeger.net)")
        return fn(*args, **kwargs)
    return wrapper


@app.route("/")
def index():
    cfg = _config_or_empty()
    conn = get_conn()
    try:
        summary = storage.get_summary(conn, cfg)
        machines = storage.get_machines(conn)
        repos = storage.get_repos(conn, cfg)
        commit_days = storage.get_commit_days(conn)
        root_warnings = storage.get_root_warnings(conn)
        repo_errors = storage.get_repo_errors(conn)
        project_tree = storage.get_projects(conn, cfg)
    finally:
        conn.close()
    try:
        top_n = int(cfg.get("top_n", 12))
    except Exception:
        top_n = 12
    return render_page(summary, machines, repos, commit_days,
                       top_n=top_n, last_scan=_last_scan,
                       root_warnings=root_warnings, repo_errors=repo_errors,
                       projects=project_tree, gated=through_gate())


@app.route("/api/summary")
def api_summary():
    cfg = _config_or_empty()
    conn = get_conn()
    try:
        return jsonify(storage.get_summary(conn, cfg))
    finally:
        conn.close()


@app.route("/api/data")
def api_data():
    cfg = _config_or_empty()
    conn = get_conn()
    try:
        return jsonify({
            "summary": storage.get_summary(conn, cfg),
            "machines": storage.get_machines(conn),
            "repos": storage.get_repos(conn, cfg),
            "commit_days": storage.get_commit_days(conn),
            "root_warnings": storage.get_root_warnings(conn),
            "repo_errors": storage.get_repo_errors(conn),
            "projects": storage.get_projects(conn, cfg),
            "last_scan": _last_scan,
        })
    finally:
        conn.close()


# POST only. This used to accept GET, which made a scan fire on a bare URL
# fetch -- a link, a prefetch, a crawler. A control-plane route that answers
# GET defeats any "protect the writes" approach.
@app.route("/api/refresh", methods=["POST"])
@gated
def api_refresh():
    result = run_scan()
    return jsonify(result)


@app.route("/refresh", methods=["POST"])
@gated
def refresh_and_redirect():
    run_scan()
    return redirect(url_for("index"))


def _read_config_text():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


@app.route("/config")
@gated
def config_page():
    return render_config_page(_read_config_text(), collector.load_config(CONFIG_PATH))


@app.route("/api/config", methods=["GET"])
@gated
def api_config_get():
    return jsonify({"raw": _read_config_text(),
                    "config": collector.load_config(CONFIG_PATH)})


@app.route("/api/config", methods=["POST"])
@gated
def api_config_post():
    body = request.get_json(force=True, silent=True) or {}
    warning = None
    try:
        if "raw" in body:
            collector.save_config_raw(CONFIG_PATH, body["raw"])
        elif "config" in body:
            # Returns a note when a legitimate edit still cost comment lines
            # (a removed target takes its own comments with it). Surfaced rather
            # than dropped -- a save that quietly loses documentation is the bug
            # this path was written to fix.
            warning = collector.save_config_dict(CONFIG_PATH, body["config"])
        else:
            return jsonify({"ok": False, "error": "no 'config' or 'raw' in request"}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    # Reflect the new config soon without blocking the save response.
    def _bg_rescan():
        try:
            run_scan()
        except Exception as exc:  # never surface a background failure as noise
            app.logger.warning("post-save scan failed: %s", exc)
    threading.Thread(target=_bg_rescan, name="gitmon-postsave", daemon=True).start()
    return jsonify({"ok": True, "warning": warning} if warning else {"ok": True})


@app.route("/api/config/test", methods=["POST"])
@gated
def api_config_test():
    body = request.get_json(force=True, silent=True) or {}
    target = body.get("target")
    if not isinstance(target, dict) or not target.get("ssh"):
        return jsonify({"ok": False, "error": "target with an 'ssh' field required"}), 400
    config = collector.load_config(CONFIG_PATH)
    defaults = {k: v for k, v in config.items() if k != "targets"}
    ok, result = collector.scan_target(target, defaults)
    if not ok:
        return jsonify({"ok": False, "error": result})
    repos = result.get("repos", [])
    return jsonify({"ok": True, "count": len(repos),
                    "repos": [r["name"] for r in repos[:12]]})


def start_scheduler():
    t = threading.Thread(target=scheduler_loop, name="gitmon-scheduler", daemon=True)
    t.start()


# Start the scheduler once, whether run directly or under a WSGI server.
start_scheduler()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)
