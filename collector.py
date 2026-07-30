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
        "precious_patterns": target.get("precious_patterns",
                                        defaults.get("precious_patterns", [])),
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
        # Declared backup coverage for precious files (see coverage.py). Absent
        # means "unknown"; an empty list means "nothing here is backed up" and
        # is a legitimate declaration, so only the shape is checked.
        cov = t.get("precious_coverage")
        if cov is not None:
            if not isinstance(cov, list):
                raise ValueError(
                    "target %s: `precious_coverage` must be a list (omit it "
                    "entirely for 'unknown', or use [] for 'nothing here is "
                    "backed up')" % name)
            for c in cov:
                if isinstance(c, dict):
                    if not c.get("path"):
                        raise ValueError(
                            "target %s: each precious_coverage entry needs a "
                            "`path`" % name)
                elif not isinstance(c, str) or not c.strip():
                    raise ValueError(
                        "target %s: precious_coverage entries must be a path "
                        "string or a {path, by} mapping" % name)
        if not roots and not t.get("extra"):
            raise ValueError("target %s: needs at least one root or extra path" % name)
    return True


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _count_comment_lines(text):
    """Lines that are purely a comment. A crude metric on purpose: it is used
    only to detect that comments went MISSING, and for that a lower bound on a
    simple count is enough."""
    return sum(1 for ln in text.splitlines() if ln.lstrip().startswith("#"))


def _merge_value(cur, new):
    """Merge `new` (plain data) into `cur` (a round-trip node) IN PLACE where the
    shapes line up, and return what should be stored.

    Assigning wholesale is what loses comments, and not only the obvious way. A
    comment block sitting *inside* a mapping or sequence belongs to that node, so
    `target["precious_coverage"] = [...]` replaces a CommentedSeq with a plain
    list and takes the reasoning written above and inside it with it. That is how
    48 of this config's 67 comment lines disappeared on the first attempt --
    caught by the post-write check in save_config_dict, not by inspection.

    The short-circuit on equality matters most: a form Save resubmits almost
    everything unchanged, and an untouched node keeps its comments for free.
    """
    if cur == new:
        return cur
    if isinstance(cur, dict) and isinstance(new, dict):
        for k, v in new.items():
            cur[k] = _merge_value(cur[k], v) if k in cur else v
        for k in [k for k in cur if k not in new]:
            del cur[k]
        return cur
    if isinstance(cur, list) and isinstance(new, list):
        # Trim from the tail: deleting by index shifts every later index, and
        # ruamel tracks a sequence's comments BY INDEX.
        for i in range(len(cur) - 1, len(new) - 1, -1):
            del cur[i]
        for i, v in enumerate(new):
            if i < len(cur):
                cur[i] = _merge_value(cur[i], v)
            else:
                cur.append(v)
        return cur
    return new


def _merge_preserving_comments(doc, cfg):
    """Update the round-trip document `doc` in place from plain dict `cfg`.

    Targets are matched by NAME rather than position, so editing the third
    machine cannot move the comments belonging to the first two.
    """
    for k, v in cfg.items():
        if k != "targets":
            doc[k] = _merge_value(doc[k], v) if k in doc else v
    for k in [k for k in doc if k not in cfg]:
        del doc[k]

    wanted = cfg.get("targets", [])
    old = doc.get("targets")
    if old is None:
        doc["targets"] = wanted
        return

    keep = [t.get("name") for t in wanted]
    for i in range(len(old) - 1, -1, -1):
        t = old[i]
        if not isinstance(t, dict) or t.get("name") not in keep:
            del old[i]
    by_name = {t.get("name"): t for t in old if isinstance(t, dict)}
    for spec in wanted:
        cur = by_name.get(spec.get("name"))
        if cur is None:
            old.append(spec)          # appending cannot shift anyone else
        else:
            _merge_value(cur, spec)
    # Only reorder if the order genuinely differs. The form has no reorder
    # control -- cards are collected in DOM order, which is config order -- so
    # this is a safety net, and sorting a CommentedSeq scrambles index-attached
    # comments, which the post-write check will then refuse.
    if [t.get("name") for t in old] != keep:
        order = {n: i for i, n in enumerate(keep)}
        old.sort(key=lambda t: order.get(t.get("name"), len(order)))


def save_config_dict(path, cfg):
    """Validate a config dict and write it as YAML (atomic), keeping comments.

    The /config form posts a whole config dict, and the old implementation ran
    it through `yaml.safe_dump`, which regenerates the document and DROPS EVERY
    COMMENT with no error. That was survivable when the file was mostly values;
    it stopped being survivable once config.yaml started carrying the reasoning
    behind `precious_coverage` and `precious_patterns` -- the values would
    survive a Save and the explanation for them would not.

    Returns a warning string (or None). The caller surfaces it rather than
    swallowing it: this function refuses to write a document it can't verify,
    but a *reduced* comment count after a legitimate target removal is expected
    and only worth mentioning.
    """
    import yaml
    validate_config(cfg)

    try:
        from ruamel.yaml import YAML
    except ImportError:
        # Deliberately fatal rather than falling back to safe_dump. A silent
        # fallback here would reintroduce exactly the data loss this exists to
        # prevent, and the raw-YAML editor is a working alternative.
        raise RuntimeError(
            "ruamel.yaml is not installed, so saving from the form would strip "
            "every comment from config.yaml. Install it (see requirements.txt) "
            "or edit the raw YAML instead.")

    try:
        with open(path, "r", encoding="utf-8") as fh:
            original = fh.read()
    except OSError:
        original = ""

    ry = YAML()                     # round-trip mode is the default
    ry.preserve_quotes = True
    ry.width = 4096                 # never re-wrap a long line into a new one

    if original.strip():
        doc = ry.load(original)
        _merge_preserving_comments(doc, cfg)
    else:
        doc = cfg

    import io
    buf = io.StringIO()
    ry.dump(doc, buf)
    text = buf.getvalue()
    if not original.strip():
        text = CONFIG_HEADER + text

    # Verify the result rather than trusting the round-trip. Two independent
    # checks, because the failure modes are different: a merge bug changes the
    # VALUES, and a ruamel-version quirk drops the COMMENTS.
    reparsed = yaml.safe_load(text)
    if reparsed != cfg:
        raise RuntimeError(
            "refusing to write: the round-trip result does not match the "
            "submitted config. Nothing was changed on disk.")

    before, after = _count_comment_lines(original), _count_comment_lines(text)
    warning = None
    if after < before:
        old_names = {t.get("name") for t in (yaml.safe_load(original) or {}).get("targets") or []}
        removed = old_names - {t.get("name") for t in cfg.get("targets") or []}
        if not removed:
            raise RuntimeError(
                "refusing to write: %d comment line(s) would be lost and no "
                "target was removed to explain it. Nothing was changed on disk; "
                "use the raw YAML editor." % (before - after))
        warning = ("%d comment line(s) went with the removed target(s): %s"
                   % (before - after, ", ".join(sorted(n for n in removed if n))))

    _atomic_write(path, text)
    return warning


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
