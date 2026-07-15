"""git-monitor collector.

Reads config.yaml, scans each target, and writes results to SQLite.

- Local target (`ssh: local`) runs scan.py as a subprocess against the
  collector host's own mounts (e.g. /mnt/git, /home/user in the container).
- Remote targets pipe scan.py to the host's own interpreter over SSH:
      ssh <opts> host <remote_python> - <b64config>   (scan.py on stdin)
  so nothing needs to be installed on the remote and there is no shell quoting
  to get wrong. Unreachable hosts fail fast and keep their last snapshot.

Run standalone:  python collector.py --config config.yaml --db data.db --once
"""

import argparse
import base64
import json
import os
import subprocess
import sys

import storage

HERE = os.path.dirname(os.path.abspath(__file__))
SCAN_PY = os.path.join(HERE, "scan.py")


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if path.lower().endswith(".json"):
        return json.loads(text)
    try:
        import yaml
    except ImportError:
        raise SystemExit(
            "PyYAML is required for YAML config (or pass a .json file). "
            "pip install pyyaml"
        )
    return yaml.safe_load(text)


def build_scan_config(target, defaults):
    return {
        "machine": target["name"],
        "since_days": target.get("since_days", defaults.get("since_days", 365)),
        "authors": target.get("authors", defaults.get("authors", [])),
        "roots": target.get("roots", []),
        "extra": target.get("extra", []),
        "exclude": target.get("exclude", []),
    }


def _b64(cfg):
    return base64.b64encode(json.dumps(cfg).encode("utf-8")).decode("ascii")


def run_local(scan_cfg, timeout):
    proc = subprocess.run(
        [sys.executable, SCAN_PY, _b64(scan_cfg)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "local scan failed: " + proc.stderr.decode("utf-8", "replace")[:400])
    return json.loads(proc.stdout.decode("utf-8", "replace"))


def run_remote(target, scan_cfg, defaults):
    host = target["ssh"]
    remote_python = target.get("remote_python", defaults.get("remote_python", "python3"))
    connect_timeout = int(target.get("connect_timeout", defaults.get("connect_timeout", 8)))
    overall_timeout = int(target.get("timeout", defaults.get("timeout", 90)))

    ssh_cmd = ["ssh",
               "-o", "BatchMode=yes",
               "-o", "ConnectTimeout=%d" % connect_timeout,
               "-o", "StrictHostKeyChecking=accept-new"]
    identity = target.get("ssh_identity", defaults.get("ssh_identity"))
    if identity:
        ssh_cmd += ["-i", identity]
    for opt in defaults.get("ssh_options", []) + target.get("ssh_options", []):
        ssh_cmd += ["-o", opt]
    ssh_cmd += [host, remote_python, "-", _b64(scan_cfg)]

    with open(SCAN_PY, "rb") as fh:
        scan_bytes = fh.read()

    proc = subprocess.run(
        ssh_cmd, input=scan_bytes,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=overall_timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "ssh scan failed (rc=%d): %s"
            % (proc.returncode, proc.stderr.decode("utf-8", "replace")[:400]))
    out = proc.stdout.decode("utf-8", "replace").strip()
    # A remote login shell can emit a banner/MOTD before our JSON; take the
    # last JSON object on stdout to be safe.
    start = out.rfind("\n{")
    if start != -1:
        out = out[start + 1:]
    return json.loads(out)


def scan_target(target, defaults):
    """Scan one target WITHOUT touching the DB. Returns (ok, result_or_error)."""
    ssh = target.get("ssh", "local")
    scan_cfg = build_scan_config(target, defaults)
    try:
        if ssh == "local":
            return True, run_local(scan_cfg, int(defaults.get("timeout", 90)))
        return True, run_remote(target, scan_cfg, defaults)
    except Exception as exc:
        return False, str(exc)[:400]


def collect_one(conn, target, defaults):
    name = target["name"]
    ssh = target.get("ssh", "local")
    remote_python = target.get("remote_python", defaults.get("remote_python", "python3"))
    ok, result = scan_target(target, defaults)
    if ok:
        storage.save_scan(conn, name, ssh, remote_python, result)
        return True, len(result.get("repos", []))
    storage.mark_unreachable(conn, name, ssh, remote_python, result)
    return False, result


def collect_all(conn, config):
    defaults = {k: v for k, v in config.items() if k != "targets"}
    targets = config.get("targets", [])
    results = []
    for target in targets:
        ok, info = collect_one(conn, target, defaults)
        results.append((target["name"], ok, info))
    storage.prune_machines(conn, {t["name"] for t in targets})
    return results


CONFIG_HEADER = (
    "# git-monitor configuration.\n"
    "# Edit here in the browser (/config) or by hand; changes take effect on the\n"
    "# next scan. Each target is scanned by piping scan.py to that host's Python\n"
    "# over SSH (or run locally for `ssh: local`).\n\n"
)


def validate_config(cfg):
    """Raise ValueError if the config is structurally unusable."""
    if not isinstance(cfg, dict):
        raise ValueError("config must be a mapping")
    targets = cfg.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ValueError("`targets` must be a non-empty list")
    seen = set()
    for t in targets:
        if not isinstance(t, dict):
            raise ValueError("each target must be a mapping")
        name = t.get("name")
        if not name:
            raise ValueError("a target is missing `name`")
        if name in seen:
            raise ValueError("duplicate target name: %s" % name)
        seen.add(name)
        if not t.get("ssh"):
            raise ValueError("target %s is missing `ssh` (use 'local' or user@host)" % name)
        roots = t.get("roots", []) or []
        if not isinstance(roots, list):
            raise ValueError("target %s: `roots` must be a list" % name)
        for r in roots:
            if not isinstance(r, dict) or not r.get("path"):
                raise ValueError("target %s: each root needs a `path`" % name)
        if not roots and not t.get("extra"):
            raise ValueError("target %s: needs at least one root or extra path" % name)
    return True


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def save_config_dict(path, cfg):
    """Validate a config dict and write it as YAML (atomic)."""
    import yaml
    validate_config(cfg)
    _atomic_write(path, CONFIG_HEADER + yaml.safe_dump(cfg, sort_keys=False))


def save_config_raw(path, text):
    """Validate raw YAML text and write it verbatim (atomic)."""
    import yaml
    cfg = yaml.safe_load(text)
    validate_config(cfg)
    _atomic_write(path, text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.yaml"))
    ap.add_argument("--db", default=os.path.join(HERE, "data.db"))
    ap.add_argument("--once", action="store_true", help="scan once and exit")
    args = ap.parse_args()

    config = load_config(args.config)
    conn = storage.connect(args.db)
    results = collect_all(conn, config)
    for name, ok, info in results:
        if ok:
            print("[ok]   %-12s %s repos" % (name, info))
        else:
            print("[FAIL] %-12s %s" % (name, info))


if __name__ == "__main__":
    main()
