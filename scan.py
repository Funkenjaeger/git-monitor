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
      "exclude": ["C:/projects/linuxcnc"],   # path prefixes to skip
      "precious_patterns": ["credentials.json", "token.json", "*.env", "secrets/*"]
      # ^ declared, not inferred -- a regenerable __pycache__ and an
      # irreplaceable OAuth token look identical to git (both just "ignored").
      # A pattern with no "/" matches a filename at any depth (like a
      # .gitignore rule without one); a pattern with a "/" is anchored to the
      # repo root. Checked against `git status --ignored`, so only files git
      # is actually ignoring are ever considered -- a merely-untracked file is
      # untracked's problem, not this one's.
    }

Config may be provided as:
    - a base64-encoded JSON string as the first CLI argument (SSH/subprocess use)
    - --config PATH               (a JSON file)
    - --root PATH [--depth N] [--bare]   (ad-hoc, repeatable --root)
    - JSON on stdin, when stdin is not a TTY and no other source is given
Add --pretty for indented output when testing by hand.
"""

import base64
import fnmatch
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

GIT_TIMEOUT = 20  # seconds per git invocation
# Per-branch history we ship back so the collector can compare copies of the
# same repo across machines without any host having to fetch. Capped to keep
# the JSON payload sane: a copy more than this far behind reports "far behind"
# rather than an exact count.
LINEAGE_BRANCHES = 5
LINEAGE_DEPTH = 80
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
        # On failure prefer git's complaint over its (usually empty) stdout, so
        # callers can report the real reason (e.g. "dubious ownership").
        err = proc.stderr.decode("utf-8", "replace").strip()
        return False, err or proc.stdout.decode("utf-8", "replace")
    return True, proc.stdout.decode("utf-8", "replace")


# Every field probe_repo reports about one repo. Named here rather than written
# out as a literal inside probe_repo so that tests can check each one against
# the signal registry (signals.py): a detector added here but never registered
# there produces a number nobody will ever see on the dashboard, and says
# nothing about it -- the repo just renders "clean". This module deliberately
# imports nothing from the rest of the app; it is shipped whole to each target
# and run there under whatever python that host has.
REPO_FIELDS = (
    "path", "name", "is_bare", "branch",
    "dirty", "ahead", "behind", "unpushed", "stashes", "untracked",
    "precious_files", "worktrees", "has_remote", "error",
    "last_commit", "commit_days",
    "head_sha", "root_key", "branch_tips", "branch_dates", "lineage",
    "remotes", "unpushed_by_remote",
    "worktree_note",
)
#: Of those, the ones that default to an empty dict rather than None.
DICT_FIELDS = ("commit_days", "branch_tips", "branch_dates", "lineage",
               "remotes", "unpushed_by_remote")


def norm(p):
    return os.path.normpath(p).replace("\\", "/")


def is_worktree_repo(d):
    # A working checkout has a `.git` dir (or file, for submodules/worktrees).
    return os.path.exists(os.path.join(d, ".git"))


def is_bare_repo(d):
    # A bare repo is itself the git dir: HEAD + objects/ + refs/ at the top.
    return all(os.path.exists(os.path.join(d, x)) for x in ("HEAD", "objects", "refs"))


def is_primary_worktree(path):
    """The worktree whose `.git` is a real directory, not the small
    gitdir-pointer FILE `git worktree add` leaves in a linked one (the same
    distinction is_worktree_repo already reads, since it deliberately accepts
    either). Picking this one as the row that carries shared-store signals
    keeps the dashboard's naming stable across scans: whichever checkout
    started life as `git clone`/`git init` keeps the name, instead of it
    flipping between "reflex" and "reflex-bl" depending on directory walk
    order.
    """
    return os.path.isdir(os.path.join(path, ".git"))


# Signals whose value lives in the shared object store (refs, refs/stash, the
# object database) rather than in one checkout's index or working tree.
# Verified against a real `git worktree add` fixture (git 2.43): `git
# rev-list --branches HEAD --not --remotes` (unpushed / unpushed_by_remote)
# and `git stash list` (stashes) read refs that live in the *common* git dir,
# so every worktree sharing that common dir reports the identical number --
# not a coincidence, the same ref read from two directories. `dirty`,
# `untracked` and `precious_files` are deliberately NOT here: those come from
# `git status`/`ls-files` against the worktree's own index and working tree,
# which genuinely differs checkout to checkout (that's the whole point of a
# linked worktree). `ahead`/`behind` and `head_sha`/`branch`/`last_commit`
# aren't here either -- they describe the branch currently checked out in
# THIS worktree, which a linked worktree is free to have pointed somewhere
# else entirely.
SHARED_STORE_SIGNALS = ("unpushed", "unpushed_by_remote", "stashes")
#: Of those, the ones worth naming in a linked worktree's folded-in note.
#: (unpushed_by_remote is a per-remote breakdown of `unpushed`, already
#: implied by it, so it is zeroed but not separately narrated.)
_NOTEWORTHY_SHARED_SIGNALS = ("unpushed", "stashes")


def git_common_dir(path):
    """Absolute, normalized path to the git dir this checkout's objects and
    refs actually live in -- identical for every worktree sharing one object
    store. None if git can't answer (not a repo, unreadable, etc).

    `git rev-parse --git-common-dir` prints a path relative to `path` itself
    when that checkout's own .git IS the common dir (the primary worktree),
    and an absolute path otherwise (a linked worktree, pointed elsewhere) --
    both observed directly against git 2.43. Normalizing here means callers
    never have to care which case they got.
    """
    ok, out = run_git(["rev-parse", "--git-common-dir"], cwd=path)
    if not ok:
        return None
    gd = out.strip().splitlines()[0] if out.strip() else ""
    if not gd:
        return None
    if not os.path.isabs(gd):
        gd = os.path.join(path, gd)
    return norm(gd)


def dedupe_shared_worktrees(repos):
    """Collapse shared-object-store signals of linked worktrees onto their
    primary worktree, in place.

    scan.py already understands worktrees: is_worktree_repo() accepts a
    `.git` FILE as well as a directory, and collect_repo() already asks `git
    worktree list` how many other checkouts share this one's store. What
    nothing here noticed until now is when TWO of the scan's OWN top-level
    results are two names for that one store: a depth-2 walk over
    C:/projects enumerates a repo's main checkout and each linked worktree as
    separate directories, so a signal that actually lives in the shared
    object store -- not in either checkout's working tree -- gets reported
    once under each name. On 2026-09-08 that was reflex (main worktree,
    branch integration) and reflex-bl (linked worktree): byte-identical
    `unpushed` detail under two rows.

    Repos with no shared common-dir -- independent clones, bare mirrors, a
    repo git could not read -- are left alone, including when two
    independent repos each happen to carry unpushed work: they group under
    two different common-dirs and never touch each other.
    """
    groups = {}
    for r in repos:
        if r.get("is_bare") or r.get("error"):
            continue
        cd = git_common_dir(r["path"])
        if not cd:
            continue
        groups.setdefault(cd, []).append(r)

    for members in groups.values():
        if len(members) < 2:
            continue
        primaries = [m for m in members if is_primary_worktree(m["path"])]
        # Deterministic even in the pathological case of none or several
        # "primaries": sort by path so a repeated scan of the same layout
        # never flips which row the dashboard names.
        primary = primaries[0] if primaries else sorted(members, key=lambda m: m["path"])[0]

        folded = []
        for m in members:
            if m is primary:
                continue
            phrase_parts = []
            for key in SHARED_STORE_SIGNALS:
                val = m.get(key)
                if not val:
                    continue
                m[key] = {} if key in DICT_FIELDS else 0
                if key in _NOTEWORTHY_SHARED_SIGNALS:
                    phrase_parts.append("%d %s" % (val, key))
            if phrase_parts:
                folded.append("%s: %s" % (m["name"], ", ".join(phrase_parts)))

        if folded:
            primary["worktree_note"] = "; ".join(folded) + " (shared object store)"


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


def _matches_precious(rel_path, patterns):
    """True if rel_path (posix-style, repo-root-relative) matches a declared
    precious pattern. A pattern with no "/" matches the basename at any depth
    (same convention as a slash-less .gitignore rule); one with a "/" is
    anchored to the repo root -- so "secrets/*" means the top-level secrets
    dir, not any directory named secrets anywhere in the tree."""
    base = rel_path.rsplit("/", 1)[-1]
    for pat in patterns:
        if "/" in pat:
            if fnmatch.fnmatch(rel_path, pat):
                return True
        elif fnmatch.fnmatch(base, pat):
            return True
    return False


def collect_repo(path, bare, since_days, authors=None, precious_patterns=None):
    """Gather cheap status for one repo. Returns a dict; never raises."""
    path = norm(path)
    name = os.path.basename(path)
    if bare and name.endswith(".git"):
        name = name[:-4]
    # For bare repos address the git dir directly; for worktrees use -C.
    gd = path if bare else None
    cwd = None if bare else path

    info = {f: ({} if f in DICT_FIELDS else None) for f in REPO_FIELDS}
    info["path"] = path
    info["name"] = name
    info["is_bare"] = bool(bare)

    ok, out = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd, gd)
    if ok:
        info["branch"] = out.strip() or None
    else:
        # Keep git's reason; a repo that fails here yields all-null fields and
        # would otherwise look like a quiet repo instead of a broken one.
        first = (out or "").strip().splitlines()
        info["error"] = first[0][:200] if first else None

    if not bare:
        ok, out = run_git(["status", "--porcelain"], cwd, gd)
        if ok:
            info["dirty"] = sum(1 for ln in out.splitlines() if ln.strip())
        # Untracked-but-not-ignored files are already folded into "dirty" above
        # (status --porcelain reports them as "?? path"), but that hides them
        # inside a generic count indistinguishable from modified tracked files.
        # Surface them separately so "N untracked" can be its own signal (e.g.
        # a whole new module that was never `git add`ed) distinct from "N dirty"
        # (changes to files git already knows about).
        ok, out = run_git(["ls-files", "--others", "--exclude-standard"], cwd, gd)
        if ok:
            info["untracked"] = sum(1 for ln in out.splitlines() if ln.strip())
        # Stashes are invisible to `status --porcelain` and everything else
        # collected here -- a `git stash` tucks changes onto refs/stash where no
        # ahead/behind or dirty check will ever see them. `stash list` reads
        # that ref's reflog directly; each line is one stash entry.
        ok, out = run_git(["stash", "list"], cwd, gd)
        if ok:
            info["stashes"] = sum(1 for ln in out.splitlines() if ln.strip())
        # Declared-precious files that are gitignored. Heuristics can't tell a
        # regenerable __pycache__ from an irreplaceable OAuth token -- both are
        # just "ignored" to git -- so this checks a declared list of patterns
        # instead of guessing. Plain `--ignored` collapses a whole ignored
        # directory to one line (e.g. "!! secrets/"), which a "secrets/*"
        # pattern can't match against; `--untracked-files=all` is what makes
        # git list each individual file inside it instead.
        if precious_patterns:
            ok, out = run_git(
                ["status", "--porcelain", "--ignored", "--untracked-files=all"],
                cwd, gd,
            )
            if ok:
                hits = []
                for ln in out.splitlines():
                    if not ln.startswith("!! "):
                        continue
                    rel = ln[3:].strip().strip('"')
                    if _matches_precious(rel, precious_patterns):
                        hits.append(rel)
                info["precious_files"] = hits
        # Linked worktrees: `git worktree list` enumerates every checkout
        # sharing this repo's object store, including this one. A worktree
        # with real uncommitted work would otherwise just be another
        # unlabeled directory to the walk in find_repos() (or invisible to
        # it entirely, if pruned by hand and left as an empty directory).
        # First entry is always this checkout itself, so subtract one.
        ok, out = run_git(["worktree", "list", "--porcelain"], cwd, gd)
        if ok:
            info["worktrees"] = max(
                sum(1 for ln in out.splitlines() if ln.startswith("worktree ")) - 1,
                0,
            )
        # ahead/behind vs upstream; guarded — many repos have no upstream.
        ok, out = run_git(
            ["rev-list", "--left-right", "--count", "@{u}...HEAD"], cwd, gd
        )
        if ok:
            parts = out.split()
            if len(parts) == 2:
                info["behind"] = int(parts[0])
                info["ahead"] = int(parts[1])
        # "unpushed": commits on ANY local branch not reachable from ANY remote.
        #
        # Scoped to HEAD until 2026-07-30, which under-reported badly: a repo
        # whose current branch is pushed read as clean no matter how much sat on
        # other branches. rotary-controller-f4 reported 0 while holding 14
        # commits across add-emulator, automation-comparison and els-shoulder --
        # and the whole point of this tool is that work on one disk is visible.
        #
        # `--branches HEAD` rather than bare `--branches`: --branches covers
        # refs/heads only, so a detached HEAD (where commits are unreachable and
        # will eventually be GC'd) would otherwise be missed entirely.
        ok, remotes = run_git(["remote"], cwd, gd)
        remote_names = remotes.split() if ok else []
        info["has_remote"] = bool(remote_names)
        if info["has_remote"]:
            ok, out = run_git(
                ["rev-list", "--count", "--branches", "HEAD", "--not", "--remotes"],
                cwd, gd,
            )
            if ok and out.strip().isdigit():
                info["unpushed"] = int(out.strip())
        # Remote URLs identify the project across machines (same origin =>
        # same repo, whatever the checkout is named); per-remote unpushed lets
        # us tell "on the NAS but not on GitHub" apart from the union count.
        for rn in remote_names:
            ok, url = run_git(["remote", "get-url", rn], cwd, gd)
            if ok and url.strip():
                info["remotes"][rn] = url.strip().splitlines()[0]
            ok, out = run_git(
                ["rev-list", "--count", "--branches", "HEAD",
                 "--not", "--remotes=" + rn], cwd, gd,
            )
            if ok and out.strip().isdigit():
                info["unpushed_by_remote"][rn] = int(out.strip())

    ok, out = run_git(["log", "-1", "--format=%cI"], cwd, gd)
    if ok and out.strip():
        info["last_commit"] = out.strip()

    # --- replica identity -------------------------------------------------
    # Root commit(s) survive cloning, so every copy of a project shares them.
    # That's what lets the collector recognise the desktop checkout, the
    # homelab checkout and the bare mirror as one project.
    ok, out = run_git(["rev-parse", "HEAD"], cwd, gd)
    if ok:
        info["head_sha"] = out.strip() or None
    ok, out = run_git(["rev-list", "--max-parents=0", "--all"], cwd, gd)
    if ok:
        # All root commits, comma-joined. The collector keys on each one
        # individually, so two clones match if they share *any* root even when
        # their branch sets (and thus the full list) differ.
        roots = sorted(x.strip() for x in out.splitlines() if x.strip())
        info["root_key"] = ",".join(roots) or None

    # Branch tip + last-commit date per local branch. Names can't contain
    # spaces, and iso-strict has no space, so a 3-field split is safe.
    ok, out = run_git(
        ["for-each-ref", "--sort=-committerdate",
         "--format=%(refname:short) %(objectname) %(committerdate:iso-strict)",
         "refs/heads"], cwd, gd)
    if ok:
        for ln in out.splitlines():
            parts = ln.split()
            if len(parts) >= 2:
                info["branch_tips"][parts[0]] = parts[1]
                if len(parts) >= 3:
                    info["branch_dates"][parts[0]] = parts[2]
    # Ordered history per branch: position of another copy's tip in this list
    # *is* the number of commits that copy is behind. Cover the busiest branches
    # plus, always, the checked-out one -- that's the branch you're comparing.
    lineage_for = list(info["branch_tips"])[:LINEAGE_BRANCHES]
    if info["branch"] in info["branch_tips"] and info["branch"] not in lineage_for:
        lineage_for.append(info["branch"])
    for b in lineage_for:
        ok, out = run_git(
            ["rev-list", "-n", str(LINEAGE_DEPTH), info["branch_tips"][b]], cwd, gd)
        if ok:
            info["lineage"][b] = [x.strip() for x in out.splitlines() if x.strip()]

    # Commit-day histogram across all refs within the window. When `authors` is
    # given, count only commits whose author matches one of the patterns
    # (case-insensitive regex over "Name <email>"), so the heatmap reflects your
    # own activity, not upstream contributors on cloned/public repos.
    log_args = [
        "log", "--all", "--no-merges",
        "--since=%d.days.ago" % int(since_days),
        "--date=short", "--format=%cd",
    ]
    if authors:
        log_args.append("--regexp-ignore-case")
        for pat in authors:
            log_args.append("--author=%s" % pat)
    ok, out = run_git(log_args, cwd, gd)
    if ok:
        days = {}
        for ln in out.splitlines():
            ln = ln.strip()
            if ln:
                days[ln] = days.get(ln, 0) + 1
        info["commit_days"] = days

    if info["branch"] is None and info["last_commit"] is None and not info["error"]:
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


def scan(cfg):
    """Run one scan for the given config dict and return the result dict
    (pre-serialization). Factored out of main() so tests can drive a real
    fixture through find_repos/collect_repo/dedupe_shared_worktrees without
    going through argv/stdin parsing or stdout.
    """
    since_days = int(cfg.get("since_days", 365))
    authors = cfg.get("authors") or []
    precious_patterns = cfg.get("precious_patterns") or []
    exclude = [norm(p) for p in cfg.get("exclude", [])]

    result = {
        "machine": cfg.get("machine"),
        "host": _hostname(),
        "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        # Self-identifying version of the copy of scan.py that actually
        # produced this payload -- see _self_sha256(). None on the piped
        # path (nothing on disk to hash there); collector.py only ever
        # compares this for an `installed` target, where it names a real
        # file. A top-level payload key, not a REPO_FIELDS entry, so it
        # carries no signals.py/storage.py obligation.
        "scan_py_sha256": _self_sha256(),
        "repos": [],
        "roots": [],
        "errors": [],
    }

    seen = set()
    for root in cfg.get("roots", []):
        path = root.get("path")
        if not path:
            continue
        depth = int(root.get("depth", 2))
        bare = bool(root.get("bare", False))
        # Track whether the root is actually there and how much it yielded, so
        # an unmounted share shows up as a warning instead of silently fewer repos.
        found = 0
        try:
            for repo_path, is_bare in find_repos(path, depth, bare, exclude):
                np = norm(repo_path)
                if np in seen:
                    continue
                seen.add(np)
                result["repos"].append(collect_repo(
                    repo_path, is_bare, since_days, authors, precious_patterns))
                found += 1
        except Exception as exc:  # never let one root sink the whole scan
            result["errors"].append("root %s: %s" % (path, exc))
        result["roots"].append({
            "path": norm(path),
            "exists": os.path.isdir(path),
            "found": found,
        })

    for path in cfg.get("extra", []):
        np = norm(path)
        if np in seen or not os.path.isdir(path):
            if not os.path.isdir(path):
                result["errors"].append("extra path missing: %s" % path)
            continue
        seen.add(np)
        bare = is_bare_repo(path) and not is_worktree_repo(path)
        result["repos"].append(collect_repo(
            path, bare, since_days, authors, precious_patterns))

    dedupe_shared_worktrees(result["repos"])
    return result


def main():
    cfg, pretty = load_config(sys.argv)
    result = scan(cfg)
    text = json.dumps(result, indent=2 if pretty else None, sort_keys=pretty)
    sys.stdout.write(text + "\n")


def _hostname():
    try:
        import platform
        return platform.node()
    except Exception:
        return ""


def _self_sha256():
    """sha256 of this script's own source, read off __file__.

    This is the version-skew fix: an `installed` target (see collector.py's
    run_remote / the `remote_script` comment there) execs some OTHER copy of
    scan.py than the one the collector would have piped, and nothing on the
    piped path notices when the two drift -- a patch reaches every piped
    target silently and the installed one just keeps running whatever was
    last hand-deployed. Putting a hash of the running copy's own bytes in the
    payload it already returns lets collector.py catch that without a new
    channel through the ForceCommand wrapper (see collector.py's comparison).

    None -- not raised, not "" -- when __file__ doesn't name a real file on
    disk. That covers the piped path deliberately: there, scan.py is fed to
    `<python> -` over stdin (see run_remote), and CPython sets
    `__file__ == "<stdin>"` for a script read that way (confirmed by hand:
    `cat scan.py | python3 -` prints "<stdin>", not a path) -- there is
    nothing on disk to hash. Collector-side, a piped target is never compared
    at all (its hash agrees by construction, since the collector supplies the
    bytes), so this returning None there is inert, not load-bearing; the
    `installed` desktop case is what depends on __file__ being real, and it
    is: the ForceCommand wrapper execs a real file path
    (C:\\ProgramData\\ssh\\git-monitor-scan.py), not stdin.
    """
    try:
        with open(__file__, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


if __name__ == "__main__":
    main()
