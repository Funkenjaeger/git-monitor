"""Archived-project classification for the `unpushed` alarm.

Four repo rows on this dashboard (desktop/reflex-ui, desktop/reflex-fw,
elspi/reflex-ui, elspi/reflex-fw) can never clear their `unpushed` count: by
standing decision (2026-08-17), the split reflex-ui/reflex-fw repos are pushed
to dserver only, never to GitHub, for the duration of the monorepo
transition -- but origin is still configured as
github.com/Funkenjaeger/reflex-{ui,fw} on every copy, so `unpushed` measures
against a remote that will never receive those commits. That is a fact about
the target, not the tool -- so, like backup coverage (see coverage.py), it is
DECLARED, not detected.

Declare a project's frozen tip once, keyed by remote URL:

    archived:
      github.com/Funkenjaeger/reflex-ui: 1a6d2d6
      github.com/Funkenjaeger/reflex-fw: 9f2e1ab

That global form is right only when every copy of the project is frozen at the
SAME commit. When two machines hold the same project on different branches --
which is the actual case here -- pin each copy under its own target instead,
the same way precious_coverage already is:

    targets:
      - name: desktop
        archived:
          github.com/Funkenjaeger/reflex-ui: 96bb910
          github.com/Funkenjaeger/reflex-fw: 64f033a
      - name: elspi
        archived:
          github.com/Funkenjaeger/reflex-ui: 89b09bb
          github.com/Funkenjaeger/reflex-fw: 4eec973

A per-target entry overrides the global map for that URL on that machine only;
anything not named per-target still falls back to the global map.

Keyed by the SAME `_norm_url()` projects.py already uses to collapse
cross-machine copies of one project, so ONE entry per project covers every
copy of it -- desktop's and elspi's reflex-ui checkouts both normalize their
origin to the same `github.com/funkenjaeger/reflex-ui` key, so the two
entries above are enough to cover all four rows this was written for.

Two outcomes:

  pinned, not diverged -- this copy's HEAD is at (or is a longer form of) the
                           declared tip. The `unpushed` alarm for THIS repo is
                           suppressed (see ArchivableCount in signals.py) and
                           an `archived` chip renders instead: muted,
                           informational, matches the declared intent.
  pinned, diverged      -- HEAD is no longer the declared tip. Something
                           landed on a working copy that was supposed to be
                           frozen. `unpushed` renders as normal (NOT
                           suppressed) AND a loud `archived_diverged` chip
                           calls it out by name -- "a commit landed on a
                           frozen archive" is a more specific and more urgent
                           fact than "N commits unpushed" alone, and is
                           exactly the case this module exists to keep loud.

A repo whose origin is not declared archived at all gets both fields False,
which is the ordinary, unremarkable case: `unpushed` behaves exactly as it
does today, and neither new chip appears.

Classification runs on the read side (get_repos, alongside coverage.annotate),
same reasoning as coverage.py: it's a fact about a standing decision, not
about git, so scan.py and the DB schema don't grow a field, and re-declaring
(or retiring) a project's pin takes effect on the next page load, not the
next scan.
"""

from projects import _norm_url

# The keys this module adds to every repo dict. Neither has a DB column --
# see signals.py's Archived / ArchivedDiverged, both `stored = None`.
FIELDS = ("archived_pinned", "archived_diverged")


def declared(config):
    """{norm_url: pinned_sha} from config's top-level `archived:` map, or {}
    if none is declared. This is the FALLBACK layer: it applies to every
    machine's copy of a project, which is correct only when every copy is
    frozen at the same commit. See declared_for() for why that is not always
    true and what overrides it."""
    raw = (config or {}).get("archived") or {}
    return {_norm_url(k): str(v).strip() for k, v in raw.items() if v}


def declared_for(config, machine):
    """{norm_url: pinned_sha} for ONE machine: the top-level `archived:` map,
    overridden per URL by that machine's own `archived:` map under `targets:`.

    WHY THIS IS PER-TARGET (2026-08-20). The original design pinned one SHA per
    project on the reasoning that a pin "does not depend on which machine's copy
    you're looking at". That is false in this estate: the two copies of a frozen
    project sit on DIFFERENT BRANCHES -- desktop's reflex-ui on fix/els-mode-watch
    (96bb910) and elspi's on fix/els-disengage-feed-latch (89b09bb), likewise
    reflex-fw at 64f033a and 4eec973. Since the comparison below is a SHA prefix
    match and not an ancestry test, a single pin necessarily leaves one copy
    reading `archived_diverged` -- the LOUD chip, asserting that a commit landed
    on a frozen archive, which is simply untrue. That is strictly worse than the
    permanent `unpushed` this module exists to quiet.

    Unlike precious_coverage -- deliberately per-target with NO global fallback,
    because a path list is meaningless across hosts -- a frozen tip often IS the
    same everywhere, so the global map stays genuinely useful and is kept. It
    just cannot be the only form."""
    out = dict(declared(config))
    for t in (config or {}).get("targets") or []:
        if t.get("name") != machine:
            continue
        raw = t.get("archived") or {}
        out.update({_norm_url(k): str(v).strip() for k, v in raw.items() if v})
        break
    return out


def annotate(repos, config):
    """Add archived_pinned / archived_diverged to each repo in place."""
    cache = {}
    for r in repos:
        machine = r.get("machine")
        if machine not in cache:
            cache[machine] = declared_for(config, machine)
        decl = cache[machine]
        origin = (r.get("remotes") or {}).get("origin")
        pin = decl.get(_norm_url(origin)) if origin else None
        if not pin:
            r["archived_pinned"] = False
            r["archived_diverged"] = False
            continue
        head = (r.get("head_sha") or "").strip()
        # Short-SHA tolerant in either direction: config may pin a short or
        # full SHA, and head_sha's length shouldn't matter either way. A repo
        # with no head_sha at all reads as diverged -- "we can't confirm it
        # didn't move" must not render as the quiet, confirmed-clean case.
        diverged = not head or not (head.startswith(pin) or pin.startswith(head))
        r["archived_pinned"] = True
        r["archived_diverged"] = diverged
    return repos
