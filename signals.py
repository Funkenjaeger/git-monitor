"""Every fact the dashboard can report about one repo, declared once.

This file exists because the same bug kept coming back. A signal gets added --
stashes, untracked files, precious files, linked worktrees, "no remote", a repo
git can't read -- and it has to be threaded by hand through four independent
lists before anyone ever sees it:

    scan.py     produces the field
    storage.py  names it in CREATE TABLE, in _migrate, and in the INSERT
    projects.py copies it into the instance dict AND into the surfaced dict
    render.py   draws a chip for it, and rolls it up to the collapsed row

Miss any one of those and the signal does not raise, does not log, and does not
render empty. It renders as the word "clean". Every occurrence so far was found
by eye, weeks later, and fixed as a one-off:

  * `precious_files` was dropped from the surfaced dict, so a repo holding the
    only copy of a secret rendered clean (patched with _carry/coverage.FIELDS).
  * a bare mirror with a broken HEAD read "unreadable" on its machine card and
    "clean" in the project list (patched with a bespoke `errors` roll-up).
  * `worktrees` is scanned and drawn but was never given a DB column, so that
    chip could not render for anyone, ever.
  * `has_remote` is drawn but was never copied into the instance or surfaced
    dicts, so "no remote" could not render for anyone, ever.

The last two were found by tests/test_signals.py, not by eye.

So: one declaration per signal, and each of those four stages iterates this list
instead of restating it. Adding a signal means adding a Signal here and nothing
else. Forgetting to add it here fails a test rather than quietly reporting a
repo as clean.

Three attributes carry the load:

    stored    the DB column this needs, or None when the value is derived on
              the read side (the coverage buckets: a fact about backups, not
              about git -- see coverage.py)
    problem   whether a copy having this deserves attention. Drives the roll-up
              onto a collapsed project row: "bare" and "precious, backed up"
              are true, worth showing on the copy itself, and not problems.
    card      whether it also belongs in a machine card's totals.
"""

import html


def esc(s):
    return html.escape("" if s is None else str(s))


class Signal:
    """One reportable fact about a repo.

    Subclasses override `fires` / `count` / `chip_text` where the default
    "positive integer count" shape doesn't fit. Everything else -- storage,
    carrying, roll-up, machine totals -- is generic over the attributes below
    and needs no per-signal code.
    """

    #: DB column type, or None when derived on the read side.
    stored = "INTEGER"
    #: Worth attention: bubbles up to a collapsed project row.
    problem = True
    #: Also counted in a machine card's totals.
    card = True
    #: Cards count repos by default; list-valued signals count items, because
    #: "how much is at risk on this machine" is a file question.
    card_unit = "repos"
    #: Printed on a machine card unconditionally. Everything else appears only
    #: when non-zero, so a new signal can't bloat cards it never fires on.
    card_always = False
    #: Chip CSS class, and the noun used in chip and tooltip text.
    cls = ""
    word = ""
    plural = None
    prefix = ""
    suffix = ""
    card_label = None

    def __init__(self, key, **attrs):
        self.key = key
        for name, val in attrs.items():
            # Typos here would silently do nothing, which is the failure mode
            # this whole module exists to remove.
            if not hasattr(type(self), name):
                raise TypeError("%s has no attribute %r" % (type(self).__name__, name))
            setattr(self, name, val)

    # -- value shape ---------------------------------------------------------

    def value(self, r):
        return r.get(self.key)

    def fires(self, r):
        """Is this signal true of this repo/instance dict?"""
        return (self.value(r) or 0) > 0

    def count(self, r):
        """Magnitude, for totals and for the roll-up chip."""
        return self.value(r) or 0

    def to_db(self, r):
        """Value as stored. Only consulted for signals with a `stored` type."""
        return self.value(r)

    # -- words ---------------------------------------------------------------

    def noun(self, n):
        return self.plural if (n != 1 and self.plural) else self.word

    def phrase(self, n):
        """"3 dirty", "1 stash" -- used in chips and roll-up tooltips."""
        return "%d %s" % (n, self.noun(n))

    def chip_text(self, r):
        return self.phrase(self.count(r))

    def chip_tip(self, r):
        return ""

    def detail(self, r):
        """One copy's contribution to a roll-up tooltip."""
        return self.phrase(self.count(r))

    def card_text(self, n):
        return "%d %s" % (n, self.card_label or self.noun(n))

    # -- html ----------------------------------------------------------------

    def _span(self, cls, text, tip=""):
        return '<span class="badge %s"%s>%s%s%s</span>' % (
            cls, (' title="%s"' % esc(tip)) if tip else "",
            self.prefix, text, self.suffix)

    def chip(self, r):
        """The badge for one instance."""
        return self._span(self.cls, esc(self.chip_text(r)), self.chip_tip(r))

    def rollup_chip(self, items):
        """The badge a collapsed project row shows when this signal fires on a
        copy that row is not displaying. `items` is [{machine, path, n, detail},
        ...] for the copies it hides.

        Deliberately the instance chip's own colour and wording behind a
        "look below" arrow, rather than bespoke prose per signal: the collapsed
        row is width-constrained, and nobody should have to learn a second
        vocabulary to be told the thing they care about is one level down.
        """
        n = sum(i["n"] for i in items)
        shown = items[:4]
        tip = "; ".join("%s:%s: %s" % (i["machine"], i["path"], i["detail"])
                        for i in shown)
        if len(items) > len(shown):
            tip += "; and %d more" % (len(items) - len(shown))
        return self._span(self.cls + " elsewhere",
                          "&#8627;" + esc(self.phrase(n)), tip)


class CountSignal(Signal):
    """A plain integer count of things inside one repo."""


class ListSignal(Signal):
    """A list of {path, by} dicts, classified on the read side (coverage.py)
    rather than stored -- so no column, and machine cards count files."""

    stored = None
    card_unit = "items"

    def fires(self, r):
        return bool(self.value(r))

    def count(self, r):
        return len(self.value(r) or ())

    def _plist(self, r):
        return ", ".join(
            i["path"] + (" (%s)" % i["by"] if i.get("by") else "")
            for i in self.value(r) or ())

    def detail(self, r):
        return self._plist(r)


class FlagSignal(Signal):
    """True or false about the repo as a whole -- no count to report."""

    def fires(self, r):
        return bool(self.value(r))

    def count(self, r):
        return 1 if self.fires(r) else 0

    def chip_text(self, r):
        return self.word

    def phrase(self, n):
        return self.word if n == 1 else "%d %s" % (n, self.plural or self.word)

    def detail(self, r):
        return self.word

    def to_db(self, r):
        return 1 if self.value(r) else 0


class Unreadable(Signal):
    """git refused the repo. Every other field comes back NULL as a result,
    which is indistinguishable from a quiet repo unless this is said out loud.

    Kept off machine cards because they already carry a dedicated warning line
    for these, with per-path detail this chip can't fit."""

    stored = "TEXT"
    card = False
    cls = "err"
    word = "unreadable"
    prefix = "&#9888; "

    def fires(self, r):
        return bool(self.value(r))

    def count(self, r):
        return 1 if self.fires(r) else 0

    def chip_text(self, r):
        return "unreadable"

    def chip_tip(self, r):
        return self.value(r)

    def detail(self, r):
        return self.value(r)


class Precious(ListSignal):
    """Shared behaviour for the three declared-precious buckets. The split by
    declared backup coverage is what keeps the alarm readable -- see
    coverage.py."""

    tip_lead = ""

    def chip_tip(self, r):
        return self.tip_lead + self._plist(r)


class NoRemote(FlagSignal):
    """Nowhere to push. `unpushed` is structurally 0 for such a repo, so it
    reads as clean while being the one kind with no off-machine copy of its
    history at all. Bare repos are exempt -- /mnt/git IS the remote, so having
    no remote is correct there and flagging it would be noise on every one."""

    stored = "INTEGER"
    cls = "noremote"
    word = "no remote"
    card_label = "no remote"

    def fires(self, r):
        return (r.get(self.key) is not None and not r.get(self.key)
                and not r.get("is_bare"))


class Bare(FlagSignal):
    """What this copy is, not something wrong with it."""

    problem = False
    card = False
    cls = "bare"
    word = "bare"


# Order is render order, and it is the reading order of a row: what is broken,
# then what is uncommitted, then what is committed but unpushed, then what this
# copy simply is. Anything appended here reaches every stage automatically.
SIGNALS = (
    Unreadable("error"),
    CountSignal("dirty", cls="dirty", word="dirty", card_always=True),
    # Already folded into `dirty` by git status --porcelain; called out so a
    # whole new module nobody ran `git add` on can't hide inside a generic count.
    CountSignal("untracked", cls="untracked", word="untracked", card_always=True),
    # Invisible to every other check here: a stash lives on refs/stash, which no
    # ahead/behind or dirty count will ever look at.
    CountSignal("stashes", cls="stash", word="stash", plural="stashes",
                card_label="stashed", card_always=True),
    Precious("precious_orphaned", cls="orphaned", word="orphaned",
             prefix="&#9888; ",
             tip_lead="gitignored and covered by no declared backup -- "
                      "the only copy is on this disk: "),
    Precious("precious_unknown", cls="pmaybe", word="precious?",
             card_label="precious?",
             tip_lead="gitignored, and this target declares no "
                      "precious_coverage -- backup status unknown: "),
    Precious("precious_covered", cls="pcovered", word="precious",
             suffix=" &#10003;", problem=False, card=False,
             tip_lead="gitignored, but under a declared backup: "),
    # A linked worktree can carry its own uncommitted or unpushed work that
    # nothing above will ever see from this checkout's side.
    CountSignal("worktrees", cls="worktree", word="worktree", plural="worktrees"),
    CountSignal("unpushed", cls="unpushed", word="unpushed", card_always=True),
    NoRemote("has_remote"),
    Bare("is_bare"),
)


#: Every key an instance dict must carry from the scan row through to the
#: renderer. projects.py iterates this instead of restating the field names in
#: each of the three dicts it builds -- which is how `precious_files` and then
#: `has_remote` went missing.
CARRY = tuple(s.key for s in SIGNALS)

#: Signals with a DB column, as (key, sql_type). storage.py builds its migration
#: and its INSERT from this, so registering a signal is enough to persist it.
STORED = tuple((s.key, s.stored) for s in SIGNALS if s.stored)

#: Worth bubbling onto a collapsed project row.
PROBLEMS = tuple(s for s in SIGNALS if s.problem)

#: Worth counting on a machine card.
ON_CARD = tuple(s for s in SIGNALS if s.card)


def by_key(key):
    for s in SIGNALS:
        if s.key == key:
            return s
    raise KeyError(key)


def firing(r):
    """Every signal true of one repo/instance dict, in render order."""
    return [s for s in SIGNALS if s.fires(r)]


def hidden_from(members, shown):
    """Problem signals firing on a copy a collapsed row is NOT showing.

    Returns {signal_key: [{machine, path, n, detail}, ...]}. This is what makes
    a collapsed row trustworthy: it displays one copy out of several, and
    without this it says nothing whatsoever about the rest.
    """
    out = {}
    here = (shown.get("machine"), shown.get("path")) if shown else (None, None)
    for m in members:
        if (m.get("machine"), m.get("path")) == here:
            continue
        for s in PROBLEMS:
            if s.fires(m):
                out.setdefault(s.key, []).append({
                    "machine": m.get("machine"), "path": m.get("path"),
                    "n": s.count(m), "detail": s.detail(m),
                })
    return out
