"""The reflex / reflex-bl double-count, 2026-09-08.

C:/projects/reflex is a worktree checkout on branch `integration`.
C:/projects/reflex-bl is a *linked* worktree of that same checkout --
`git worktree add` made it, so it shares reflex's object store: same refs,
same objects, same `refs/stash`. scan.py's depth-2 walk over C:/projects
finds both as top-level directories (is_worktree_repo() deliberately accepts
a `.git` FILE, which is exactly what a linked worktree has, so it was never
missed) and probes each independently. Every signal read from the shared
store -- `unpushed`, in particular, computed as `git rev-list --branches
HEAD --not --remotes` -- comes back byte-identical from both, because it IS
the same ref read from two directories. The 2026-09-08 dashboard reported
unpushed:reflex and unpushed:reflex-bl with identical detail: one real
unpushed commit, counted twice.

scan.py already understood worktrees before this file existed: it detects a
`.git` file as a worktree checkout, and collect_repo() already runs `git
worktree list --porcelain` to report how many siblings share a checkout's
store. What was missing was the scan noticing when two of ITS OWN top-level
results are two names for one store. dedupe_shared_worktrees() (scan.py)
closes that: it groups discovered checkouts by `git rev-parse
--git-common-dir`, and within a group attributes shared-store signals to the
PRIMARY worktree only -- the one whose `.git` is a real directory, chosen so
the name doesn't flip between scans -- while leaving genuinely per-checkout
signals (a dirty working tree, untracked files) reported for whichever
checkout actually has them.

Each case below is proven against a REAL fixture: `git init`/`git clone` +
`git worktree add` in a tempdir, run through scan.scan() exactly as the
production walk would encounter it -- not a mocked git.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scan

# Deliberately not `git config` (global or --local): tests must not depend on,
# or mutate, any user's git identity. Passing identity via the environment is
# git's documented mechanism for exactly this.
GIT_ENV = dict(os.environ)
GIT_ENV.update({
    "GIT_AUTHOR_NAME": "git-monitor tests",
    "GIT_AUTHOR_EMAIL": "tests@example.invalid",
    "GIT_COMMITTER_NAME": "git-monitor tests",
    "GIT_COMMITTER_EMAIL": "tests@example.invalid",
})


def _git(args, cwd, when=None):
    """`when` pins GIT_AUTHOR_DATE/GIT_COMMITTER_DATE for this one command.
    The lineage fixture below needs branches whose committerdate ORDER is
    unambiguous; without it every commit in a fast test lands in the same
    second and `for-each-ref --sort=-committerdate` ties arbitrarily."""
    env = GIT_ENV
    if when is not None:
        env = dict(GIT_ENV)
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
    proc = subprocess.run(
        ["git"] + args, cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "git %s (cwd=%s) failed: %s" % (args, cwd, proc.stderr.decode("utf-8", "replace"))
        )
    return proc.stdout.decode("utf-8", "replace")


def _append(path, text):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text + "\n")


class WorktreeDedupe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gm-worktree-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _make_shared_pair(self):
        """A bare origin, a main worktree with one unpushed commit, and a
        linked worktree sharing its object store. Returns (root, main_wt,
        linked_wt) as absolute paths."""
        root = os.path.join(self.tmp, "proj")
        os.makedirs(root)
        origin = os.path.join(root, "origin.git")
        main_wt = os.path.join(root, "reflex")             # primary: .git is a DIR
        linked_wt = os.path.join(root, "reflex-bl")         # linked: .git is a FILE

        _git(["init", "-q", "--bare", "--initial-branch=main", origin], root)
        _git(["clone", "-q", origin, main_wt], root)
        _append(os.path.join(main_wt, "f.txt"), "one")
        _git(["add", "f.txt"], main_wt)
        _git(["commit", "-q", "-m", "c1"], main_wt)
        # Neither commit is ever pushed to origin -- these two commits are
        # unpushed exactly once in reality and must not double-count to four
        # (two rows each claiming both).
        _append(os.path.join(main_wt, "f.txt"), "two")
        _git(["commit", "-q", "-am", "c2 unpushed"], main_wt)
        _git(["worktree", "add", "-q", linked_wt, "-b", "integration-bl"], main_wt)
        return root, main_wt, linked_wt

    def _scan(self, root):
        result = scan.scan({"roots": [{"path": root, "depth": 2}]})
        by_name = {r["name"]: r for r in result["repos"]}
        return result, by_name

    # -- (1) & (2): the shared signal appears exactly once, on the primary --

    def test_shared_unpushed_reported_once_on_primary(self):
        root, main_wt, linked_wt = self._make_shared_pair()
        result, by_name = self._scan(root)

        self.assertEqual(
            len(result["repos"]), 2,
            "expected one row per checkout (the walk still finds two "
            "directories); dedupe must not merge the rows themselves")

        main_name = os.path.basename(main_wt)     # "reflex"
        linked_name = os.path.basename(linked_wt)  # "reflex-bl"
        self.assertIn(main_name, by_name)
        self.assertIn(linked_name, by_name)

        main_unpushed = by_name[main_name]["unpushed"]
        linked_unpushed = by_name[linked_name]["unpushed"]

        # There is exactly one unpushed commit in reality. Summed across both
        # rows that must read 2 (git's own unit is "commits", and rev-list
        # --count here returns 2 for this fixture -- see collect_repo's
        # `--branches HEAD --not --remotes`), never 4 (both rows still
        # claiming it) and never 0 (dropped entirely).
        self.assertEqual(
            (main_unpushed or 0) + (linked_unpushed or 0), 2,
            "shared unpushed count must be attributed exactly once, "
            "not split, doubled, or dropped: reflex=%r reflex-bl=%r"
            % (main_unpushed, linked_unpushed))
        self.assertEqual(
            main_unpushed, 2,
            "the primary worktree (.git is a directory) must carry the count")
        self.assertEqual(
            linked_unpushed, 0,
            "the linked worktree's shared unpushed count must be "
            "suppressed, not left duplicated")
        self.assertTrue(
            by_name[main_name].get("worktree_note"),
            "primary row should note that a linked worktree's shared "
            "signal was folded in here, not silently swallow it")

    # -- (3): a dirty working tree in ONLY the linked worktree survives -----

    def test_dirty_in_linked_worktree_is_not_deduped_away(self):
        root, main_wt, linked_wt = self._make_shared_pair()
        _append(os.path.join(linked_wt, "f.txt"), "dirty in linked only")
        result, by_name = self._scan(root)

        main_name = os.path.basename(main_wt)
        linked_name = os.path.basename(linked_wt)
        self.assertEqual(
            by_name[main_name]["dirty"], 0,
            "the main worktree's own working tree is untouched and clean")
        self.assertGreater(
            by_name[linked_name]["dirty"], 0,
            "a dirty working tree is a per-checkout fact and must still be "
            "reported for the checkout that actually has it")

    # -- (4): two genuinely independent repos are never merged --------------

    def test_two_independent_repos_with_unpushed_stay_two_rows(self):
        root = os.path.join(self.tmp, "independents")
        os.makedirs(root)
        unpushed_by_name = {}
        for name in ("indep-a", "indep-b"):
            proj_root = os.path.join(root, name)
            os.makedirs(proj_root)
            origin = os.path.join(proj_root, "origin.git")
            repo = os.path.join(proj_root, name)
            _git(["init", "-q", "--bare", "--initial-branch=main", origin], proj_root)
            _git(["clone", "-q", origin, repo], proj_root)
            _append(os.path.join(repo, "f.txt"), name)
            _git(["add", "f.txt"], repo)
            _git(["commit", "-q", "-m", "c1"], repo)
            _append(os.path.join(repo, "f.txt"), name + " unpushed")
            _git(["commit", "-q", "-am", "c2"], repo)
            unpushed_by_name[name] = repo

        result, by_name = self._scan(root)
        self.assertEqual(len(result["repos"]), 2)
        for name in ("indep-a", "indep-b"):
            self.assertIn(name, by_name)
            self.assertEqual(
                by_name[name]["unpushed"], 2,
                "each genuinely independent repo keeps its own unpushed "
                "count -- they do not share a git-common-dir, so grouping "
                "must never fold one into the other")
            self.assertIsNone(
                by_name[name].get("worktree_note"),
                "a repo with no shared worktree has nothing to note")

    # -- (5) branch_tips: the duplicate ol-control's collect.sh actually reads

    def test_shared_branch_tips_reported_once_on_primary(self):
        """The 2026-09-11 gap. `unpushed` was deduped on 2026-09-08, but
        ol-control's collect.sh (:754) builds its unpushed row from
        branch_tips and skips a repo whose tips are empty, so the linked
        worktree kept producing a twin row -- with the ahead-count missing
        (:816's fallback), which is what made it look like a different bug."""
        root, main_wt, linked_wt = self._make_shared_pair()
        _result, by_name = self._scan(root)
        main_name = os.path.basename(main_wt)
        linked_name = os.path.basename(linked_wt)

        self.assertTrue(
            by_name[main_name]["branch_tips"],
            "the primary worktree must keep the shared refs it can see")
        self.assertEqual(
            by_name[linked_name]["branch_tips"], {},
            "refs/heads lives in the COMMON git dir: the linked worktree "
            "reads the identical tips, so reporting them again is a "
            "duplicate row downstream, not a second fact")

    def test_shared_branch_dates_and_commit_days_reported_once(self):
        """branch_dates rides the same `for-each-ref` as branch_tips.
        commit_days is `git log --all` over the shared object store, and
        storage.get_commit_days() sums it across every repo -- so a linked
        worktree double-counted every commit in the dashboard heatmap."""
        root, main_wt, linked_wt = self._make_shared_pair()
        _result, by_name = self._scan(root)
        main_name = os.path.basename(main_wt)
        linked_name = os.path.basename(linked_wt)

        for field in ("branch_dates", "commit_days"):
            self.assertTrue(
                by_name[main_name][field],
                "%s must survive on the primary" % field)
            self.assertEqual(
                by_name[linked_name][field], {},
                "%s is read from the shared store and must be attributed "
                "exactly once" % field)

    def test_dedupe_is_one_sided_primary_keeps_everything(self):
        """The dedupe must only ever empty the LINKED row. A symmetric
        implementation would zero both and the signal would vanish from the
        dashboard entirely -- worse than the duplicate it replaced."""
        root, main_wt, _linked_wt = self._make_shared_pair()
        _result, by_name = self._scan(root)
        primary = by_name[os.path.basename(main_wt)]
        # Pre-dedupe truth for the same checkout, read the way scan() reads it
        # (its own defaults: since_days=365, no author filter). Comparing
        # against this rather than against "is non-empty" keeps the assertion
        # honest for a signal the fixture happens not to exercise -- `stashes`
        # is legitimately 0 here, and an is-truthy check would have demanded
        # the dedupe invent one.
        expected = scan.collect_repo(main_wt, False, 365, [])

        for field in scan.SHARED_STORE_SIGNALS:
            self.assertEqual(
                primary.get(field), expected.get(field),
                "the primary worktree must keep %s exactly as collect_repo "
                "read it; the dedupe folds signals ONTO it, never off it"
                % field)

    # -- (6) the two shared-store fields that must NOT be deduped ----------

    def test_remotes_survive_on_the_linked_worktree(self):
        """`remotes` is byte-identical across worktrees (one shared config)
        and must still be left alone: projects.py _keys_for() makes the origin
        URL a project identity key, while its root-commit key is namespaced by
        repo NAME ("root:<sha>|reflex" vs "root:<sha>|reflex-bl"). Zero origin
        on the linked worktree and it shares no key with its primary, falls
        through to "solo:<machine>|<path>", and detaches into a phantom
        project of its own."""
        root, _main_wt, linked_wt = self._make_shared_pair()
        _result, by_name = self._scan(root)
        linked = by_name[os.path.basename(linked_wt)]

        self.assertIn(
            "origin", linked.get("remotes") or {},
            "the linked worktree must keep its origin URL: it is what groups "
            "it with its primary in the project view")
        self.assertNotIn(
            "remotes", scan.SHARED_STORE_SIGNALS,
            "remotes must never be added to the dedupe -- see the audit note "
            "beside SHARED_STORE_SIGNALS")

    def test_lineage_survives_on_the_linked_worktree(self):
        root, _main_wt, linked_wt = self._make_shared_pair()
        _result, by_name = self._scan(root)

        self.assertTrue(
            by_name[os.path.basename(linked_wt)].get("lineage"),
            "lineage is per-checkout (see the asymmetry test below) and must "
            "not be folded away")
        self.assertNotIn("lineage", scan.SHARED_STORE_SIGNALS)

    def test_lineage_is_not_byte_identical_across_worktrees(self):
        """The measurement that keeps lineage out of the dedupe, kept
        executable so a later edit cannot quietly invalidate it.

        collect_repo's lineage_for takes the top LINEAGE_BRANCHES tips by
        committerdate and then ALWAYS appends this worktree's own checked-out
        branch. Park a linked worktree on a branch too old to make that cut
        and it carries history the primary does not -- so zeroing lineage
        there would delete the only record of a branch checked out nowhere
        else, which projects.py's leader/behind computation reads."""
        root = os.path.join(self.tmp, "dated")
        os.makedirs(root)
        origin = os.path.join(root, "origin.git")
        main_wt = os.path.join(root, "proj")
        linked_wt = os.path.join(root, "proj-bl")
        _git(["init", "-q", "--bare", "--initial-branch=main", origin], root)
        _git(["clone", "-q", origin, main_wt], root)
        _append(os.path.join(main_wt, "f.txt"), "one")
        _git(["add", "f.txt"], main_wt)
        _git(["commit", "-q", "-m", "c1"], main_wt, when="2020-01-01T00:00:00")
        # `old-bl` shares that oldest commit; the topics below are all newer,
        # so old-bl cannot be in the top LINEAGE_BRANCHES.
        _git(["branch", "old-bl"], main_wt)
        for i in range(scan.LINEAGE_BRANCHES + 2):
            _git(["checkout", "-q", "-b", "topic%d" % i], main_wt)
            _append(os.path.join(main_wt, "f.txt"), "t%d" % i)
            _git(["commit", "-q", "-am", "t%d" % i], main_wt,
                 when="2026-01-0%dT00:00:00" % (i + 1))
        _git(["checkout", "-q", "main"], main_wt)
        _git(["worktree", "add", "-q", linked_wt, "old-bl"], main_wt)

        primary = scan.collect_repo(main_wt, False, 3650, None)
        linked = scan.collect_repo(linked_wt, False, 3650, None)

        self.assertEqual(primary["branch_tips"], linked["branch_tips"],
                         "control: the ref namespace IS shared")
        self.assertIn("old-bl", linked["lineage"])
        self.assertNotIn(
            "old-bl", primary["lineage"],
            "the primary does not carry the linked worktree's own old branch "
            "-- that asymmetry is why lineage stays out of the dedupe")
        for b in set(primary["lineage"]) & set(linked["lineage"]):
            self.assertEqual(
                primary["lineage"][b], linked["lineage"][b],
                "where both carry a branch the history is identical; the "
                "difference is which branches each one selects")

if __name__ == "__main__":
    unittest.main()
