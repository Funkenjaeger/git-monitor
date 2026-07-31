"""Backup-coverage classification for declared-precious files.

`precious_patterns` (see scan.py) finds files git is IGNORING -- so git will
never commit, diff or push them. That is a statement about git, not about risk.
A gitignored `.env` sitting under a path Duplicati backs up nightly is fine; the
same file on a machine with no backup at all is the only copy in existence.
Flagging both identically is what made the badge unreadable: four
correctly-ignored-and-backed-up files rendered the same alarm red as the one that
mattered, so the count could never reach zero and nobody read it.

Coverage is DECLARED per target, not detected. git-monitor cannot see whether
Duplicati, restic or Hyper Backup actually holds a path, and inferring it from
one backup tool's config would couple this tool to that tool while still only
speaking for one of them. So each target names the paths it knows are covered,
and by what:

    targets:
      - name: homelab
        precious_coverage:
          - { path: /home/evand/projects, by: 'duplicati "misc" job' }

Three outcomes, and the third is the one that keeps this honest:

  covered   -- under a declared path. Muted chip, informational.
  orphaned  -- the target declares coverage and this file falls outside all of
               it. Alarm: this really is the only copy.
  unknown   -- the target declares no `precious_coverage` at all. Warn, not
               alarm. Defaulting to "orphaned" would recreate the very
               noise-floor problem this split exists to fix for anyone who
               hasn't filled it in; defaulting to "covered" would lie. An
               EMPTY list is a real declaration -- "nothing here is backed up"
               -- and yields orphaned, which is exactly right for a machine
               like the desktop.

Classification runs on the read side rather than inside scan.py on purpose: it
is a fact about the host's backup arrangement, not about its git repos. scan.py
stays a pure git prober, the scan payload and DB schema don't grow a field that
means "what some other tool does", and editing coverage takes effect on the next
page load instead of the next scan.
"""

# The keys this module adds to every repo dict. These are the three signals with
# no database column: they are classified here on the read side, so signals.py
# declares them with `stored = None` and everything downstream treats them like
# any other signal.
FIELDS = ("precious_covered", "precious_orphaned", "precious_unknown")


def _norm(p):
    """Normalize a path for prefix comparison: forward slashes, no trailing
    slash, case-folded. Case-folding is for the Windows targets, where
    `C:/projects` and `c:/Projects` are the same directory. It costs a
    theoretical false match between two Linux paths differing only in case,
    which is pathological enough to prefer over silently missing a declaration
    because someone typed a drive letter in lower case."""
    return (p or "").replace("\\", "/").rstrip("/").casefold()


def declared_for(config, machine):
    """This machine's declared coverage as [(prefix, by_label), ...], or None if
    it declares nothing at all.

    Deliberately per-target with no global fallback: a shared default list of
    paths is meaningless across hosts (`/home/evand/projects` is not a thing on
    the Windows desktop) and would mark files covered on a machine nobody had
    actually thought about -- a false green, which is the one outcome worse than
    a false red."""
    for t in (config or {}).get("targets") or []:
        if t.get("name") != machine:
            continue
        entries = t.get("precious_coverage")
        if entries is None:
            return None                      # undeclared, not "nothing"
        out = []
        for e in entries or []:
            if isinstance(e, dict):
                path, by = e.get("path"), e.get("by")
            else:
                path, by = e, None           # bare string form
            if path:
                out.append((_norm(path), by or "declared backed up"))
        return out                           # [] means "declared: nothing here"
    return None


def annotate(repos, config):
    """Add precious_covered / precious_orphaned / precious_unknown to each repo
    in place. Each is a list of {"path", "by"}; `by` is set only on covered
    entries.

    All three stay None where `precious_files` is None -- that means no patterns
    were configured for the scan, i.e. no opinion, which must not render as
    "nothing wrong". Same NULL-vs-[] distinction storage.get_repos() preserves.
    """
    cache = {}
    for r in repos:
        files = r.get("precious_files")
        if files is None:
            for f in FIELDS:
                r[f] = None
            continue
        machine = r.get("machine")
        if machine not in cache:
            cache[machine] = declared_for(config, machine)
        declared = cache[machine]

        covered, orphaned, unknown = [], [], []
        for rel in files:
            if declared is None:
                unknown.append({"path": rel, "by": None})
                continue
            # precious paths are repo-root-relative; compare the absolute path
            # so a declaration can be narrower than a repo root.
            full = _norm("%s/%s" % (r.get("path") or "", rel))
            by = next((label for pre, label in declared
                       if pre and (full == pre or full.startswith(pre + "/"))),
                      None)
            if by:
                covered.append({"path": rel, "by": by})
            else:
                orphaned.append({"path": rel, "by": None})
        r["precious_covered"] = covered
        r["precious_orphaned"] = orphaned
        r["precious_unknown"] = unknown
    return repos
