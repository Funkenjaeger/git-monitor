"""git-monitor Flask app.

Serves the dashboard (heatmap + top project list + machine status), the
Homepage summary widget endpoint, and full JSON. A background scheduler runs
the collector every N minutes; a Refresh button triggers an on-demand scan.

Env:
    GITMON_CONFIG  path to config.yaml   (default ./config.yaml)
    GITMON_DB      path to sqlite db     (default ./data.db)
    GITMON_PORT    listen port           (default 8083)
"""

import os
import threading
import time

from flask import Flask, jsonify, redirect, request, url_for

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


@app.route("/")
def index():
    conn = get_conn()
    try:
        summary = storage.get_summary(conn)
        machines = storage.get_machines(conn)
        repos = storage.get_repos(conn)
        commit_days = storage.get_commit_days(conn)
        root_warnings = storage.get_root_warnings(conn)
    finally:
        conn.close()
    try:
        top_n = int(collector.load_config(CONFIG_PATH).get("top_n", 12))
    except Exception:
        top_n = 12
    return render_page(summary, machines, repos, commit_days,
                       top_n=top_n, last_scan=_last_scan,
                       root_warnings=root_warnings)


@app.route("/api/summary")
def api_summary():
    conn = get_conn()
    try:
        return jsonify(storage.get_summary(conn))
    finally:
        conn.close()


@app.route("/api/data")
def api_data():
    conn = get_conn()
    try:
        return jsonify({
            "summary": storage.get_summary(conn),
            "machines": storage.get_machines(conn),
            "repos": storage.get_repos(conn),
            "commit_days": storage.get_commit_days(conn),
            "root_warnings": storage.get_root_warnings(conn),
            "last_scan": _last_scan,
        })
    finally:
        conn.close()


@app.route("/api/refresh", methods=["POST", "GET"])
def api_refresh():
    result = run_scan()
    return jsonify(result)


@app.route("/refresh", methods=["POST"])
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
def config_page():
    return render_config_page(_read_config_text(), collector.load_config(CONFIG_PATH))


@app.route("/api/config", methods=["GET"])
def api_config_get():
    return jsonify({"raw": _read_config_text(),
                    "config": collector.load_config(CONFIG_PATH)})


@app.route("/api/config", methods=["POST"])
def api_config_post():
    body = request.get_json(force=True, silent=True) or {}
    try:
        if "raw" in body:
            collector.save_config_raw(CONFIG_PATH, body["raw"])
        elif "config" in body:
            collector.save_config_dict(CONFIG_PATH, body["config"])
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
    return jsonify({"ok": True})


@app.route("/api/config/test", methods=["POST"])
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
