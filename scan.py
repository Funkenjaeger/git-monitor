#!/usr/bin/env python3
"""git-monitor scanner.

Stdlib-only. Runs identically as a local subprocess or piped to a remote host's
interpreter over SSH:  ssh host <python> - <b64config> < scan.py

Reads a JSON config describing where to look for git repos, walks for them,
collects cheap per-repo status via git plumbing, and prints one JSON blob to
stdout. No third-party imports, so it works under any Python 3.6+ (the Windows
desktop's `python` 3.14 and the homelab's `python3` 3.12 alike).

Config schema (all keys optional except roots/extra — supply at least one):
    {
      "machine": "desktop",              # echoed back; collector may override
      "since_days": 365,                 # heatmap window
      "roots": [
        {"path": "C:/projects", "depth": 2, "bare": false},
        {"path": "/mnt/git",    "depth": 1, "bare": true}
      ],
      "extra":   ["D:/oneoff/weird-repo"],   # explicit repo paths, not walked
      "exclude": ["C:/projects/linuxcnc"]    # path prefixes to skip
    }

Config may be provided as:
    - a base64-encoded JSON string as the first CLI argument (SSH/subprocess use)
    - --config PATH               (a JSON file)
    - --root PATH [--depth N] [--bare]   (ad-hoc, repeatable --root)
    - JSON on stdin, when stdin is not a TTY and no other source is given
Add --pretty for indented output when testing by hand.
"""

import base64
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

GIT_TIMEOUT = 20  # seconds per git invocation
# Directory names we never descend into while hunting for repos.
PRUNE = {
    "node_modules", ".venv", "venv", "env", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".tox", "build", "dist", ".next", ".cache", "vendor",
    "target", ".idea", ".vscode", "bin", "obj",
}


def run_git(git_args, cwd=None, git_dir=None):
    """Run a git command, returning (ok, stdout_text). Never raises."""
    cmd = ["git"]
    if git_dir is not None:
        cmd += ["--git-dir", git_dir]
    elif cwd is not None:
        cmd += ["-C", cwd]
    cmd += git_args
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, ""
    if proc.returncode != 0:
        return False, proc.stdout.decode("utf-8", "replace")
    return True, proc.stdout.decode("utf-8", "replace")


def norm(p):
    return os.path.normpath(p).replace("\\", "/")


def is_worktree_repo(d):
    # A working checkout has a `.git` dir (or file, for submodules/worktrees).
    return os.path.exists(os.path.join(d, ".git"))


def is_bare_repo(d):
    # A bare repo is itself the git dir: HEAD + objects/ + refs/ at the top.
    return all(os.path.exists(os.path.join(d, x)) for x in ("HEAD", "objects", "refs"))


def find_repos(root_path, depth, bare, exclude):
    """Yield (repo_path, is_bare) under root_path, searching up to `depth`
    levels below it, without descending into a repo once found."""
    root_path = norm(root_path)
    if not os.path.isdir(root_path):
        return
    detector = is_bare_repo if bare else is_worktree_repo

    # BFS with an explicit depth budget so we can bound how deep we look.
    stack = [(root_path, 0)]
    while stack:
        d, level = stack.pop()
        nd = norm(d)
        if any(nd == ex or nd.startswith(ex + "/") for ex in exclude):
            continue
        if detector(d):
            yield d, bare
            continue  # don't descend into a repo
        if level >= depth:
            continue
        try:
            entries = os.scandir(d)
        except OSError:
            continue
        with entries:
            for e in entries:
                try:
                    if not e.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if e.name in PRUNE:
                    continue
                stack.append((e.path, level + 1))


def collect_repo(path, bare, since_days):
    """Gather cheap status for one repo. Returns a dict; never raises."""
    path = norm(path)
    name = os.path.basename(path)
    if bare and name.endswith(".git"):
        name = name[:-4]
    # For bare repos address the git dir directly; for worktrees use -C.
    gd = path if bare else None
    cwd = None if bare else path

    info = {
        "path": path,
        "name": name,
        "is_bare": bool(bare),
        "branch": None,
        "dirty": None,
        "ahead": None,
        "behind": None,
        "unpushed": None,
        "has_remote": None,
        "last_commit": None,
        "commit_days": {},
        "error": None,
    }

    ok, out = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd, gd)
    if ok:
        info["branch"] = out.strip() or None

    if not bare:
        ok, out = run_git(["status", "--porcelain"], cwd, gd)
        if ok:
            info["dirty"] = sum(1 for ln in out.splitlines() if ln.strip())
        # ahead/behind vs upstream; guarded — many repos have no upstream.
        ok, out = run_git(
            ["rev-list", "--left-right", "--count", "@{u}...HEAD"], cwd, gd
        )
        if ok:
            parts = out.split()
            if len(parts) == 2:
                info["behind"] = int(parts[0])
                info["ahead"] = int(parts[1])
        # "unpushed": commits on HEAD not reachable from ANY remote branch.
        # Works even when the current branch has no configured upstream, which
        # is the exact case (a never-pushed feature branch) we care about.
        ok, remotes = run_git(["remote"], cwd, gd)
        info["has_remote"] = bool(ok and remotes.strip())
        if info["has_remote"]:
            ok, out = run_git(
                ["rev-list", "--count", "HEAD", "--not", "--remotes"], cwd, gd
            )
            if ok and out.strip().isdigit():
                info["unpushed"] = int(out.strip())

    ok, out = run_git(["log", "-1", "--format=%cI"], cwd, gd)
    if ok and out.strip():
        info["last_commit"] = out.strip()

    # Commit-day histogram across all refs within the window.
    ok, out = run_git(
        [
            "log", "--all", "--no-merges",
            "--since=%d.days.ago" % int(since_days),
            "--date=short", "--format=%cd",
        ],
        cwd, gd,
    )
    if ok:
        days = {}
        for ln in out.splitlines():
            ln = ln.strip()
            if ln:
                days[ln] = days.get(ln, 0) + 1
        info["commit_days"] = days

    if info["branch"] is None and info["last_commit"] is None:
        info["error"] = "not a readable git repository"
    return info


def load_config(argv):
    cfg = None
    args = argv[1:]
    roots, extra, exclude = [], [], []
    i = 0
    pretty = False
    pending_depth = 2
    pending_bare = False
    while i < len(args):
        a = args[i]
        if a == "--config":
            i += 1
            with open(args[i], "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
        elif a == "--root":
            i += 1
            roots.append({"path": args[i], "depth": pending_depth, "bare": pending_bare})
        elif a == "--depth":
            i += 1
            pending_depth = int(args[i])
            if roots:
                roots[-1]["depth"] = pending_depth
        elif a == "--bare":
            pending_bare = True
            if roots:
                roots[-1]["bare"] = True
        elif a == "--extra":
            i += 1
            extra.append(args[i])
        elif a == "--exclude":
            i += 1
            exclude.append(args[i])
        elif a == "--pretty":
            pretty = True
        elif not a.startswith("--"):
            # Positional: base64-encoded JSON config (SSH/subprocess path).
            cfg = json.loads(base64.b64decode(a).decode("utf-8"))
        i += 1

    if cfg is None and roots:
        cfg = {"roots": roots, "extra": extra, "exclude": exclude}
    if cfg is None and not sys.stdin.isatty():
        data = sys.stdin.read().strip()
        if data:
            cfg = json.loads(data)
    if cfg is None:
        cfg = {"roots": [], "extra": [], "exclude": []}
    return cfg, pretty


def main():
    cfg, pretty = load_config(sys.argv)
    since_days = int(cfg.get("since_days", 365))
    exclude = [norm(p) for p in cfg.get("exclude", [])]

    result = {
        "machine": cfg.get("machine"),
        "host": _hostname(),
        "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repos": [],
        "errors": [],
    }

    seen = set()
    for root in cfg.get("roots", []):
        path = root.get("path")
        if not path:
            continue
        depth = int(root.get("depth", 2))
        bare = bool(root.get("bare", False))
        try:
            for repo_path, is_bare in find_repos(path, depth, bare, exclude):
                np = norm(repo_path)
                if np in seen:
                    continue
                seen.add(np)
                result["repos"].append(collect_repo(repo_path, is_bare, since_days))
        except Exception as exc:  # never let one root sink the whole scan
            result["errors"].append("root %s: %s" % (path, exc))

    for path in cfg.get("extra", []):
        np = norm(path)
        if np in seen or not os.path.isdir(path):
            if not os.path.isdir(path):
                result["errors"].append("extra path missing: %s" % path)
            continue
        seen.add(np)
        bare = is_bare_repo(path) and not is_worktree_repo(path)
        result["repos"].append(collect_repo(path, bare, since_days))

    text = json.dumps(result, indent=2 if pretty else None, sort_keys=pretty)
    sys.stdout.write(text + "\n")


def _hostname():
    try:
        import platform
        return platform.node()
    except Exception:
        return ""


if __name__ == "__main__":
    main()
