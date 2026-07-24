"""Relate copies of the same repo living on different machines.

The same project routinely exists several times over: a working checkout on the
desktop, another on the server that actually runs it, and a bare mirror on the
NAS acting as the "local remote". Those render as unrelated rows with identical
names, and each one's `behind` count is computed against its own
remote-tracking ref -- which is only as fresh as that machine's last `git
fetch`. A copy that has silently fallen behind therefore reports behind=0,
which is exactly backwards from what you want to see.

This module sidesteps fetching entirely. Every copy ships its branch tips plus
a capped ordered history per branch (see scan.py), so the relationship can be
computed centrally from data we already hold:

    tip_b == tip_a                 -> in sync
    tip_b appears in a's lineage   -> b is behind a by that many commits
    neither contains the other     -> genuinely diverged

Copies are grouped by root-commit SHA, which survives cloning and so is stable
across every copy of a project regardless of where it lives or what it's named.
"""

# A copy further back than scan.py's LINEAGE_DEPTH can't be counted exactly.
FAR = "far"


def _norm_name(name):
    n = (name or "").lower()
    return n[:-4] if n.endswith(".git") else n


def _relation(tip, other_tip, other_lineage, own_lineage):
    """Where `tip` sits relative to `other_tip`. Returns (state, count)."""
    if tip and tip == other_tip:
        return ("sync", 0)
    if tip and other_lineage:
        try:
            return ("behind", other_lineage.index(tip))
        except ValueError:
            pass
    if other_tip and own_lineage:
        try:
            return ("ahead", own_lineage.index(other_tip))
        except ValueError:
            pass
    return ("diverged", None)


def build_groups(repos, lineages):
    """Group repo copies by root commit and rank them against a leader.

    `repos` are storage rows (need machine, path, name, root_key, branch_tips,
    last_commit, is_bare). `lineages` maps (machine, path) -> {branch: [sha]}.
    Returns a list of groups, most-recently-active first; only projects that
    actually exist in more than one place are included.
    """
    by_root = {}
    for r in repos:
        key = r.get("root_key")
        if not key:
            continue
        # Root commit alone over-groups: a project started by branching off
        # another (reflex-ui from rotary-controller-python) keeps the original
        # root forever. Pair it with the project name so derivatives stay
        # separate, while a checkout and its bare mirror (foo / foo.git) match.
        by_root.setdefault((key, _norm_name(r.get("name"))), []).append(r)

    groups = []
    for _key, members in by_root.items():
        if len(members) < 2:
            continue

        # Compare on the branch the most copies have in common; ties go to the
        # branch carrying the newest commit.
        counts = {}
        for m in members:
            for b in (m.get("branch_tips") or {}):
                counts[b] = counts.get(b, 0) + 1
        shared = [b for b, n in counts.items() if n > 1]
        if not shared:
            continue
        for pref in ("main", "master"):
            if pref in shared:
                branch = pref
                break
        else:
            branch = max(shared, key=lambda b: counts[b])

        present = [m for m in members if branch in (m.get("branch_tips") or {})]

        def lin(m):
            return (lineages.get((m["machine"], m["path"])) or {}).get(branch) or []

        # The leader is the copy whose history contains the most other copies'
        # tips -- i.e. the one furthest ahead, the copy others should pull from.
        def reach(m):
            mine = lin(m)
            return sum(1 for o in present
                       if o is not m and (o.get("branch_tips") or {}).get(branch) in mine)

        leader = max(present, key=lambda m: (reach(m), m.get("last_commit") or ""))
        leader_lin = lin(leader)
        leader_tip = (leader.get("branch_tips") or {}).get(branch)

        entries, stale = [], 0
        for m in members:
            tip = (m.get("branch_tips") or {}).get(branch)
            if m is leader:
                state, count = "leader", 0
            elif tip is None:
                state, count = "nobranch", None
            else:
                state, count = _relation(tip, leader_tip, leader_lin, lin(m))
                if state == "diverged" and tip and leader_tip:
                    # Beyond our lineage window we can't tell "far behind" from
                    # a real fork, so don't claim more than we know.
                    state = FAR if len(leader_lin) >= 80 else "diverged"
            if state in ("behind", FAR):
                stale += 1
            entries.append({
                "machine": m["machine"], "path": m["path"], "name": m["name"],
                "is_bare": m.get("is_bare"), "dirty": m.get("dirty"),
                "last_commit": m.get("last_commit"),
                "branch": branch, "tip": tip, "state": state, "count": count,
            })

        entries.sort(key=lambda e: (e["state"] != "leader", e["machine"]))
        groups.append({
            "name": leader.get("name") or members[0].get("name"),
            "branch": branch,
            "copies": len(members),
            "stale": stale,
            "in_sync": stale == 0 and all(
                e["state"] in ("leader", "sync") for e in entries),
            "last_commit": max((m.get("last_commit") or "") for m in members),
            "entries": entries,
        })

    groups.sort(key=lambda g: (g["in_sync"], _neg(g["last_commit"])))
    return groups


def _neg(iso):
    # Sort newest-first without needing to parse the timestamp.
    return tuple(-ord(c) for c in (iso or ""))
