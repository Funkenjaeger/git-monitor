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


def _git(args, cwd):
    proc = subprocess.run(
        ["git"] + args, cwd=cwd, env=GIT_ENV,
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


if __name__ == "__main__":
    unittest.main()
