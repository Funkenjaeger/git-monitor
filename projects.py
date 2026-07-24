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
    groups = {}
    for r in repos:
        key = r.get("root_key")
        if not key:
            # No shared root to group on -- keep it as its own project so it
            # still appears, keyed uniquely so two such repos don't merge.
            key = "solo:%s:%s" % (r.get("machine"), r.get("path"))
        groups.setdefault((key, _norm_name(r.get("name"))), []).append(r)

    projects = [_build_one(m, lineages) for m in groups.values()]
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
    name = next((m.get("name") for m in members if working(m)),
                members[0].get("name"))

    return {
        "name": name,
        "instances": len(members),
        "multi_instance": len(members) > 1,
        "single_branch": len(branches) <= 1,
        "out_of_sync": any(not b["in_sync"] for b in branches),
        "last_commit": max((m.get("last_commit") or "") for m in members),
        "surfaced": {
            "branch": surf.get("branch"),
            "machine": surf.get("machine"), "path": surf.get("path"),
            "dirty": surf.get("dirty"), "unpushed": surf.get("unpushed"),
            "is_bare": bool(surf.get("is_bare")), "error": surf.get("error"),
            "last_commit": surf.get("last_commit"),
        },
        "branches": branches,
    }
