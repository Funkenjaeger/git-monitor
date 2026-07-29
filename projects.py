"""Collapse repo instances into projects, then into a Project -> Branch ->
Instance tree.

The unit of work is a *project*, not a checkout. `digestif` lives in three
places -- a desktop checkout, the server checkout that runs it, and a bare
mirror on the NAS acting as the local remote -- but any change to digestif
touches all three in service of one logical update. So the dashboard collapses
those instances into a single row showing the most-recently-touched one, and
lets you expand to see how the copies relate.

Branches complicate this: different instances can sit on different branches
(the desktop on a feature branch, the lathe on `dev`). So the expansion is two
levels -- branch, then instance -- and it degenerates gracefully:

    one instance             -> a plain row, nothing to expand
    many instances, 1 branch -> expand straight to instances (skip the branch level)
    many instances, N branches -> expand to branches; the newest opens to its instances

All cross-instance comparison is done here from data every copy already
reports (branch tips + a capped ordered history per branch, see scan.py):
finding one copy's tip inside another's history gives the exact commits-behind,
with no fetch and no network -- so it works even for a machine that can't reach
the remote.
"""

# Matches scan.py's LINEAGE_DEPTH: past this we can't tell "far behind" from a
# genuine fork, so we say "far" rather than invent a number.
LINEAGE_DEPTH = 80


def _norm_name(name):
    n = (name or "").lower()
    return n[:-4] if n.endswith(".git") else n


HOSTS = ("github.com", "gitlab.com", "bitbucket.org")


def _norm_url(u):
    """Collapse the many spellings of one remote to a single key.

    git@github.com:Org/Repo.git, https://github.com/Org/Repo.git and
    https://github.com/Org/Repo all become github.com/org/repo, so the same
    repo groups no matter how a given checkout addressed it."""
    u = (u or "").strip()
    if not u:
        return ""
    # scp-like  user@host:path  ->  host/path
    if "://" not in u and "@" in u and ":" in u.split("@", 1)[1]:
        u = u.split("@", 1)[1].replace(":", "/", 1)
    else:
        if "://" in u:
            u = u.split("://", 1)[1]
        head = u.split("/", 1)[0]
        if "@" in head:
            u = u.split("@", 1)[1]
    u = u.replace("\\", "/").rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    return u.lower()


def _is_hosting(url):
    return any(h in (url or "").lower() for h in HOSTS)


def _repo_name_from_url(u):
    """Repo name from a remote URL, preserving its original casing.
    _norm_url lowercases so keys match across spellings -- display names must
    not go through it, or SketchToCut renders as sketchtocut."""
    u = (u or "").strip().replace("\\", "/").rstrip("/")
    if not u:
        return ""
    seg = u.split("/")[-1]
    if ":" in seg:                       # scp-like host:repo with no path part
        seg = seg.split(":")[-1]
    return seg[:-4] if seg.endswith(".git") else seg


def _tail2(p):
    """Last two path components, lowercased, .git stripped -- a mount-agnostic
    id for a local bare repo. dserver:/mnt/git/digestif.git, z:\\git\\digestif.git
    and /mnt/git/digestif.git all reduce to git/digestif, so a checkout's local
    remote lines up with the bare mirror it points at across machines."""
    parts = [x for x in (p or "").replace("\\", "/").split("/") if x]
    if parts and parts[-1].endswith(".git"):
        parts[-1] = parts[-1][:-4]
    return "/".join(parts[-2:]).lower()


def _origin_hosting(r):
    o = (r.get("remotes") or {}).get("origin")
    return o if o and _is_hosting(o) else None


def _keys_for(r):
    """Identity keys linking copies of one project; sharing *any* key groups two
    instances. The origin URL ties the same hosted repo together across differing
    directory names; a local (non-hosting) remote's path-tail ties a checkout to
    the bare mirror it pushes to; and root+name bridges plain clones. Only the
    *origin* contributes a hosting key -- an `upstream` remote must not fuse every
    fork of one project together."""
    keys = set()
    origin = (r.get("remotes") or {}).get("origin")
    if origin:
        # Only origin defines identity. A hosting origin ties the same repo
        # together across dir names; a local origin's path-tail ties a checkout
        # to the bare it clones from. Secondary remotes (a stale `NAS` copied in
        # from another project, an `upstream` fork) are deliberately ignored.
        keys.add(("url:" + _norm_url(origin)) if _is_hosting(origin)
                 else ("local:" + _tail2(origin)))
    if r.get("is_bare"):
        keys.add("local:" + _tail2(r.get("path")))
    if r.get("root_key"):
        # One key per root commit (not the whole list): clones that differ only
        # in which branches they carry still share their original root, so they
        # group -- while the name guards against distinct projects that merely
        # branched off a shared root (air_dryer cloned from the valve project).
        nm = _norm_name(r.get("name"))
        for root in r["root_key"].split(","):
            if root:
                keys.add("root:%s|%s" % (root, nm))
    if not keys:
        keys.add("solo:%s|%s" % (r.get("machine"), r.get("path")))
    return keys


def _neg(iso):
    # Newest-first sort key without parsing the timestamp.
    return tuple(-ord(c) for c in (iso or ""))


def _relation(tip, leader_tip, leader_lin, own_lin):
    """Where a copy's tip sits relative to the leader. (state, count)."""
    if tip and tip == leader_tip:
        return ("sync", 0)
    if tip and leader_lin:
        try:
            return ("behind", leader_lin.index(tip))
        except ValueError:
            pass
    if leader_tip and own_lin:
        try:
            return ("ahead", own_lin.index(leader_tip))
        except ValueError:
            pass
    return ("diverged", None)


def build_projects(repos, lineages):
    """Group instances into projects and rank everything newest-first.

    `repos` are storage rows (machine, path, name, is_bare, dirty, unpushed,
    error, last_commit, branch, root_key, branch_tips, branch_dates).
    `lineages` maps (machine, path) -> {branch: [sha, ...]}.
    Returns a list of project dicts, newest activity first.
    """
    # Union-find over the identity keys: any shared key merges two instances
    # into one project. This groups the same GitHub repo across differing
    # directory names (via origin URL) yet keeps distinct repos that merely
    # share a root commit apart (different URLs), while local checkouts and
    # their bare mirror still join through root+name.
    parent = list(range(len(repos)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    seen = {}
    for i, r in enumerate(repos):
        for k in _keys_for(r):
            if k in seen:
                union(i, seen[k])
            else:
                seen[k] = i

    comps = {}
    for i in range(len(repos)):
        comps.setdefault(find(i), []).append(repos[i])

    projects = [_build_one(m, lineages) for m in comps.values()]
    projects.sort(key=lambda p: _neg(p["last_commit"]))
    return projects


def _build_one(members, lineages):
    def lin(m, b):
        return (lineages.get((m["machine"], m["path"])) or {}).get(b) or []

    def tips(m):
        return m.get("branch_tips") or {}

    def dates(m):
        return m.get("branch_dates") or {}

    def working(m):
        return not m.get("is_bare")

    # Branches "in play": the current HEAD of some working copy, or any branch
    # two+ copies share (so the bare remote's branches that back a working
    # branch show up too). Private one-off branches on a single copy stay out.
    head_branches = {m.get("branch") for m in members
                     if working(m) and m.get("branch")}
    tip_counts = {}
    for m in members:
        for b in tips(m):
            tip_counts[b] = tip_counts.get(b, 0) + 1
    inplay = {b for b in (head_branches | {b for b, c in tip_counts.items() if c >= 2})
              if b}
    if not inplay:  # bare-only mirror etc. -- fall back to a sensible default
        for pref in ("main", "master"):
            if pref in tip_counts:
                inplay = {pref}
                break
        else:
            if tip_counts:
                inplay = {max(tip_counts, key=lambda b: tip_counts[b])}

    candidates = []
    for b in inplay:
        participants = [m for m in members if b in tips(m)]
        if not participants:
            continue

        # Leader = the copy furthest ahead (its history contains the most other
        # copies' tips); ties break to the most recent commit on the branch.
        def reach(m):
            L = lin(m, b)
            return sum(1 for o in participants
                       if o is not m and tips(o).get(b) in L)

        leader = max(participants, key=lambda m: (reach(m), dates(m).get(b) or ""))
        ltip = tips(leader).get(b)
        llin = lin(leader, b)

        entries, stale = [], 0
        for m in participants:
            tip = tips(m).get(b)
            if m is leader:
                state, count = "leader", 0
            else:
                state, count = _relation(tip, ltip, llin, lin(m, b))
                if state == "diverged" and tip and ltip and len(llin) >= LINEAGE_DEPTH:
                    state = "far"
            if state in ("behind", "far"):
                stale += 1
            entries.append({
                "machine": m["machine"], "path": m["path"],
                "is_bare": bool(m.get("is_bare")), "dirty": m.get("dirty"),
                "unpushed": m.get("unpushed"), "error": m.get("error"),
                "stashes": m.get("stashes"), "untracked": m.get("untracked"),
                "precious_files": m.get("precious_files"),
                "worktrees": m.get("worktrees"),
                "last_commit": dates(m).get(b) or m.get("last_commit"),
                "branch": b, "state": state, "count": count,
            })
        entries.sort(key=lambda e: (e["state"] != "leader", _neg(e["last_commit"])))
        candidates.append({
            "name": b,
            "last_commit": max((dates(m).get(b) or "") for m in participants),
            "copies": len(participants),
            "stale": stale,
            "in_sync": stale == 0 and all(
                e["state"] in ("leader", "sync") for e in entries),
            "entries": entries,
        })
    candidates.sort(key=lambda x: _neg(x["last_commit"]))

    # A branch earns a row only if it's the checked-out branch of some working
    # copy, or the copies actually disagree on it. Branches that are identical
    # everywhere and checked out nowhere are just quiet shared history -- a repo
    # with many such branches (dinterest's docker/master/…) would otherwise
    # bury the one branch you're on. But never drop a branch that's the only
    # place a given copy appears, so the copy count stays honest.
    branches = [b for b in candidates
                if b["name"] in head_branches or not b["in_sync"]]
    covered = {(e["machine"], e["path"]) for b in branches for e in b["entries"]}
    if len(covered) < len(members):
        for b in candidates:
            if b in branches:
                continue
            here = {(e["machine"], e["path"]) for e in b["entries"]}
            if here - covered:
                branches.append(b)
                covered |= here
                if len(covered) >= len(members):
                    break
        branches.sort(key=lambda x: _neg(x["last_commit"]))
    if not branches and candidates:
        branches = [candidates[0]]

    # Surface the most recently touched working copy (fall back to any copy).
    pool = [m for m in members if working(m)] or members
    surf = max(pool, key=lambda m: (m.get("last_commit") or ""))
    surfaced = {
        "branch": surf.get("branch"),
        "machine": surf.get("machine"), "path": surf.get("path"),
        "dirty": surf.get("dirty"), "unpushed": surf.get("unpushed"),
        "stashes": surf.get("stashes"), "untracked": surf.get("untracked"),
        "precious_files": surf.get("precious_files"),
        "worktrees": surf.get("worktrees"),
        "is_bare": bool(surf.get("is_bare")), "error": surf.get("error"),
        "last_commit": surf.get("last_commit"),
    }
    # ...unless the copies disagree somewhere. Then show the branch they
    # disagree on, from the copy that's furthest ahead. A collapsed row is
    # triage: it should point at what needs attention, not at whatever was
    # touched last -- otherwise the row reads "clean" while its own copies
    # chip warns, which is exactly the branch you can't see.
    problem = next((b for b in branches if not b["in_sync"] and b["entries"]), None)
    if problem:
        e = next((x for x in problem["entries"] if x["state"] == "leader"),
                 problem["entries"][0])
        lags = [x["count"] for x in problem["entries"]
                if x["state"] == "behind" and x["count"]]
        surfaced = {
            "branch": problem["name"],
            "machine": e["machine"], "path": e["path"],
            "dirty": e["dirty"], "unpushed": e["unpushed"],
            "stashes": e.get("stashes"), "untracked": e.get("untracked"),
            "precious_files": e.get("precious_files"),
            "worktrees": e.get("worktrees"),
            "is_bare": e["is_bare"], "error": e["error"],
            "last_commit": e["last_commit"],
            # How far the worst-off copy trails, so the collapsed row can say
            # what's wrong instead of reporting the leader as "clean".
            "lag": max(lags) if lags else None,
        }
    # Prefer the repo name from a hosting origin -- it's stable and canonical,
    # so a checkout that happens to sit in a differently-named directory (cncpc's
    # ~/linuxcnc for fj-lcnc-cfg) doesn't mislabel the whole project.
    name = None
    for m in members:
        o = _origin_hosting(m)
        if o:
            name = _repo_name_from_url(o)
            break
    if not name:
        name = next((m.get("name") for m in members if working(m)),
                    members[0].get("name"))

    # Where this project is backed up, as a neutral label (not a nag): a bare
    # mirror or a non-hosting remote counts as "local"; github/gitlab/bitbucket
    # as a hosting backup. A repo with both is genuinely covered on both.
    urls = [u for m in members for u in (m.get("remotes") or {}).values()]
    has_host = any(_is_hosting(u) for u in urls)
    has_local = any(not _is_hosting(u) for u in urls) or any(m.get("is_bare") for m in members)
    backup = ("host+local" if has_host and has_local else
              "host" if has_host else "local" if has_local else "none")
    host_label = ""
    for u in urls:
        lu = u.lower()
        if "github.com" in lu:
            host_label = "GitHub"; break
        if "gitlab" in lu:
            host_label = "GitLab"; break
        if "bitbucket" in lu:
            host_label = "Bitbucket"; break
    if has_host and not host_label:
        host_label = "hosted"
    # For a repo backed up in both places, how far the newest copy is ahead of
    # its hosting remote -- i.e. committed locally/to the NAS but not pushed up.
    host_unpushed = 0
    for m in members:
        for rn, url in (m.get("remotes") or {}).items():
            if _is_hosting(url):
                host_unpushed = max(host_unpushed,
                                    (m.get("unpushed_by_remote") or {}).get(rn) or 0)

    return {
        "name": name,
        "instances": len(members),
        "multi_instance": len(members) > 1,
        "single_branch": len(branches) <= 1,
        "out_of_sync": any(not b["in_sync"] for b in branches),
        "backup": backup,
        "host_label": host_label,
        "host_unpushed": host_unpushed,
        "last_commit": max((m.get("last_commit") or "") for m in members),
        "surfaced": surfaced,
        "branches": branches,
    }
