"""Two different repos, one dashboard id: the fallback project label.

projects.py names a project after the repo name in its *hosting* origin URL,
which is stable and canonical. A project with no hosting origin -- a checkout
that only ever pushes to the bare mirror on the NAS, a repo with no remote at
all -- has no such name, so the label falls back to a member row's `name`, and
scan.collect_repo() sets that from the checkout's directory BASENAME (stripping
`.git` only for a bare repo, scan.py:325-327).

Two unrelated repos whose checkouts are both called `reflex` therefore render
the SAME id. The 2026-09-19 morning digest carried two `unpushed:reflex` lines
that meant different repos on different machines: a reader cannot tell which
one to go and open, and anything keyed on the id lands on whichever row it hits
first. scan.py cannot fix this by itself -- each scan runs on ONE target and
cannot see the other target's repos -- so uniqueness is settled in
projects.build_projects, which is the first place that sees the whole estate.

The four cases below are the contract:

  A. two targets whose checkouts share a basename get DIFFERENT ids   (the bug)
  B. a repo WITH a hosting origin, and any repo that does not collide, keeps
     the id it renders today -- an id change is a consumer break
  C. the id is stable across two consecutive scans of an unchanged target
  D. a bare repo still strips `.git`

Each is proven against a REAL fixture driven down the production path: `git
init` in a tempdir -> scan.scan() -> storage.save_scan() -> storage.get_projects(),
not a hand-built row dict. That matters here in particular because the
disambiguator's uniqueness claim rests on (machine, path) being the repos
table's PRIMARY KEY, and only the real storage path can show that.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import projects
import scan
import storage

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
            "git %s (cwd=%s) failed: %s"
            % (args, cwd, proc.stderr.decode("utf-8", "replace"))
        )
    return proc.stdout.decode("utf-8", "replace")


class LabelFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gm-projects-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.conn = storage.connect(os.path.join(self.tmp, "t.db"))
        self.addCleanup(self.conn.close)

    # -- fixture builders --------------------------------------------------

    def work_repo(self, root, name, content, when, origin=None):
        """A working checkout at <root>/<name> with one commit. `content` and
        `when` are what make the ROOT COMMIT differ between two fixtures: two
        repos that shared a root commit would group into one project and there
        would be no collision to test."""
        path = os.path.join(root, name)
        os.makedirs(path)
        _git(["init", "-q", "--initial-branch=main", "."], path)
        with open(os.path.join(path, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write(content + "\n")
        _git(["add", "f.txt"], path)
        _git(["commit", "-q", "-m", "c1 " + content], path, when=when)
        if origin:
            _git(["remote", "add", "origin", origin], path)
        return path

    def bare_repo(self, root, name, content, when):
        """A bare mirror at <root>/<name>.git, with real history in it."""
        bare = os.path.join(root, name + ".git")
        _git(["init", "-q", "--bare", "--initial-branch=main", bare], root)
        seed = os.path.join(self.tmp, "seed-" + name)
        _git(["clone", "-q", bare, seed], self.tmp)
        with open(os.path.join(seed, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write(content + "\n")
        _git(["add", "f.txt"], seed)
        _git(["commit", "-q", "-m", "c1 " + content], seed, when=when)
        _git(["push", "-q", "origin", "main"], seed)
        shutil.rmtree(seed, ignore_errors=True)
        return bare

    # -- the production path -----------------------------------------------

    def scan_target(self, machine, root, bare=False):
        """Scan one root as one collector target and store it, exactly as
        collector.py would. Re-scanning the same `machine` replaces its rows,
        which is what makes case C a genuine second scan."""
        result = scan.scan({
            "machine": machine,
            "roots": [{"path": root, "depth": 2, "bare": bare}],
        })
        self.assertEqual(result["errors"], [], "the fixture scan itself failed")
        storage.save_scan(self.conn, machine, "local", "python3", result)
        return result

    def ids(self):
        """Every project id currently on the dashboard, sorted."""
        return sorted(p["name"] for p in storage.get_projects(self.conn))

    def id_for(self, substring):
        got = [i for i in self.ids() if substring in i]
        self.assertEqual(len(got), 1,
                         "expected exactly one id containing %r, got %r"
                         % (substring, self.ids()))
        return got[0]


class TwoReposMustNotShareAnId(LabelFixture):
    """Case A -- the bug itself."""

    def _two_reflexes(self):
        desktop = os.path.join(self.tmp, "desktop", "projects")
        nas = os.path.join(self.tmp, "nas", "projects")
        os.makedirs(desktop)
        os.makedirs(nas)
        # Same basename, unrelated histories, no hosting origin on either --
        # so both fall back to the directory name and both want to be "reflex".
        self.work_repo(desktop, "reflex", "els", "2026-01-01T10:00:00")
        self.work_repo(nas, "reflex", "unrelated", "2026-02-02T11:00:00")
        self.scan_target("desktop", desktop)
        self.scan_target("nas", nas)

    def test_same_basename_on_two_targets_renders_two_ids(self):
        self._two_reflexes()
        ids = self.ids()
        self.assertEqual(len(ids), 2,
                         "fixture is wrong: the two repos grouped into one "
                         "project, so there is no collision to test (%r)" % (ids,))
        self.assertEqual(len(set(ids)), 2,
                         "two different repos render the SAME id %r. A digest "
                         "line or dashboard row carrying it names two repos at "
                         "once and the reader cannot tell which to open."
                         % ids[0])

    def test_both_ids_still_say_reflex(self):
        """Uniqueness must not be bought by throwing the name away: a human
        reading the row has to still find the project he is looking for."""
        self._two_reflexes()
        for i in self.ids():
            self.assertIn("reflex", i,
                          "id %r no longer contains the project name" % i)

    def test_each_id_names_the_machine_it_is_on(self):
        """The whole point of disambiguating is that the reader can go and
        open the right one."""
        self._two_reflexes()
        ids = self.ids()
        self.assertTrue(any("desktop" in i for i in ids),
                        "no id points at the desktop copy: %r" % (ids,))
        self.assertTrue(any("nas" in i for i in ids),
                        "no id points at the nas copy: %r" % (ids,))

    def test_a_third_reflex_does_not_rename_the_first_two(self):
        """Stability under growth. A scheme that numbers collisions, or that
        uses the shortest suffix that happens to tell the current set apart,
        renames existing rows when an unrelated repo appears. This label is
        computed from the project's OWN rows, so it cannot."""
        self._two_reflexes()
        before = set(self.ids())
        third = os.path.join(self.tmp, "elspi", "projects")
        os.makedirs(third)
        self.work_repo(third, "reflex", "a third one", "2026-03-03T12:00:00")
        self.scan_target("elspi", third)
        after = set(self.ids())
        self.assertEqual(len(after), 3, "the third repo did not land: %r" % (after,))
        self.assertTrue(before <= after,
                        "adding a third `reflex` renamed an existing row: %r -> %r"
                        % (sorted(before), sorted(after)))


class NothingThatDoesNotCollideIsRenamed(LabelFixture):
    """Case B -- an id change is a consumer break."""

    def _mixed_root(self):
        root = os.path.join(self.tmp, "desktop", "projects")
        os.makedirs(root)
        # A checkout sitting in a differently-named directory, with a hosting
        # origin: the origin name wins today and must keep winning.
        self.work_repo(root, "linuxcnc", "cfg", "2026-01-01T10:00:00",
                       origin="https://github.com/funkenjaeger/fj-lcnc-cfg.git")
        # And a plain, unremarkable, non-colliding repo.
        self.work_repo(root, "digestif", "recipes", "2026-01-02T10:00:00")
        self.scan_target("desktop", root)

    def test_a_hosting_origin_still_names_the_project(self):
        self._mixed_root()
        self.assertIn("fj-lcnc-cfg", self.ids(),
                      "the hosting origin no longer names the project; it fell "
                      "back to the directory basename: %r" % (self.ids(),))

    def test_a_hosting_origin_id_carries_no_qualifier(self):
        """Byte-for-byte what it renders today -- not `fj-lcnc-cfg @ desktop:...`.
        This row never collided, so nothing may be appended to it."""
        self._mixed_root()
        self.assertEqual(self.id_for("fj-lcnc-cfg"), "fj-lcnc-cfg")

    def test_a_plain_uncollided_repo_keeps_its_bare_name(self):
        self._mixed_root()
        self.assertEqual(self.id_for("digestif"), "digestif")

    def test_a_collision_elsewhere_does_not_touch_an_innocent_row(self):
        """Two `reflex` checkouts colliding must not drag the rest of the
        dashboard into a rename."""
        self._mixed_root()
        untouched = self.ids()
        other = os.path.join(self.tmp, "nas", "projects")
        os.makedirs(other)
        self.work_repo(other, "reflex", "one", "2026-04-01T10:00:00")
        self.work_repo(other, "reflex2", "two", "2026-04-02T10:00:00")
        # Two colliding `reflex` copies, on two targets.
        third = os.path.join(self.tmp, "elspi", "projects")
        os.makedirs(third)
        self.work_repo(third, "reflex", "another", "2026-04-03T10:00:00")
        self.scan_target("nas", other)
        self.scan_target("elspi", third)
        for i in untouched:
            self.assertIn(i, self.ids(),
                          "%r was renamed by a collision it is not part of: %r"
                          % (i, self.ids()))


class TheIdIsStableAcrossScans(LabelFixture):
    """Case C -- rescanning an unchanged target must not move any id, the
    qualified ones included."""

    def test_two_consecutive_scans_of_the_same_targets_agree(self):
        desktop = os.path.join(self.tmp, "desktop", "projects")
        nas = os.path.join(self.tmp, "nas", "projects")
        os.makedirs(desktop)
        os.makedirs(nas)
        # A collision is in the fixture on purpose: a qualifier built out of
        # anything volatile (an mtime, a scan timestamp, a row ordering) would
        # be invisible to a fixture that never collides.
        self.work_repo(desktop, "reflex", "els", "2026-01-01T10:00:00")
        self.work_repo(desktop, "digestif", "recipes", "2026-01-02T10:00:00")
        self.work_repo(nas, "reflex", "unrelated", "2026-02-02T11:00:00")
        self.scan_target("desktop", desktop)
        self.scan_target("nas", nas)
        first = self.ids()
        self.assertEqual(len(first), 3, "fixture is wrong: %r" % (first,))
        self.assertEqual(len(set(first)), 3, "fixture never collides: %r" % (first,))

        # Nothing on disk changed. Scan both targets again.
        self.scan_target("desktop", desktop)
        self.scan_target("nas", nas)
        second = self.ids()
        self.assertEqual(first, second,
                         "an id moved between two scans of unchanged targets:\n"
                         "  first:  %r\n  second: %r" % (first, second))


class ABareRepoStillStripsDotGit(LabelFixture):
    """Case D -- the bare mirror renders as `digestif`, not `digestif.git`."""

    def test_bare_mirror_id_has_no_dot_git(self):
        root = os.path.join(self.tmp, "mnt", "git")
        os.makedirs(root)
        self.bare_repo(root, "digestif", "recipes", "2026-01-01T10:00:00")
        self.scan_target("nas-bares", root, bare=True)
        self.assertEqual(self.ids(), ["digestif"])

    def test_bare_mirror_keeps_the_stripped_name_when_it_collides(self):
        """And the strip survives disambiguation -- the qualifier is appended,
        it does not put `.git` back."""
        root = os.path.join(self.tmp, "mnt", "git")
        other = os.path.join(self.tmp, "desktop", "projects")
        os.makedirs(root)
        os.makedirs(other)
        self.bare_repo(root, "digestif", "recipes", "2026-01-01T10:00:00")
        self.work_repo(other, "digestif", "something else", "2026-02-02T11:00:00")
        self.scan_target("nas-bares", root, bare=True)
        self.scan_target("desktop", other)
        ids = self.ids()
        self.assertEqual(len(set(ids)), 2, "the bare and the checkout collide: %r" % (ids,))
        # The NAME part is what must still be stripped. The location appended
        # after it is a path, and that path really does end in `.git`.
        for i in ids:
            name = i.split(projects._QUALIFIER, 1)[0]
            self.assertEqual(name, "digestif",
                             "the `.git` strip was lost under disambiguation: %r" % i)


class AForgejoOriginGroupsWithItsBare(LabelFixture):
    """A repo migrating to the NAS's Forgejo must not split into two rows.

    Before the move, a checkout's origin is the bare mirror directly
    (/mnt/git/foo.git) and it groups with that bare via _tail2. After the
    move the origin becomes `estate/foo` on forge.dudzik.app -- a different
    string, but the SAME project: /mnt/git/foo.git still exists as the
    follower copy Forgejo pushes to. Forgejo is not in HOSTS (it is a local
    backup, not a hosting fork point), so without _forge_tail this origin
    falls back to _tail2 and keys as `local:forge.dudzik.app/estate/foo` --
    sharing nothing with the bare's `local:git/foo` -- and the dashboard
    would show two rows for one project.
    """

    def _migrated_repo(self, origin):
        bare_root = os.path.join(self.tmp, "mnt", "git")
        checkout_root = os.path.join(self.tmp, "desktop", "projects")
        os.makedirs(bare_root)
        os.makedirs(checkout_root)
        self.bare_repo(bare_root, "foo", "bare copy", "2026-01-01T10:00:00")
        self.work_repo(checkout_root, "foo", "checkout", "2026-01-02T10:00:00",
                       origin=origin)
        self.scan_target("nas-bares", bare_root, bare=True)
        self.scan_target("desktop", checkout_root)

    def test_ssh_forge_origin_groups_with_the_bare_it_leads(self):
        self._migrated_repo("ssh://git@forge.dudzik.app:2222/estate/foo.git")
        ids = self.ids()
        self.assertEqual(len(ids), 1,
                         "the migrated checkout and its bare rendered as two "
                         "rows instead of one: %r" % (ids,))
        project = storage.get_projects(self.conn)[0]
        self.assertEqual(project["instances"], 2,
                         "grouped into one row but didn't count both copies: %r"
                         % (project,))

    def test_https_forge_origin_groups_with_the_bare_it_leads(self):
        self._migrated_repo("https://forge.dudzik.app/estate/foo.git")
        self.assertEqual(len(self.ids()), 1)

    def test_scp_like_forge_origin_groups_with_the_bare_it_leads(self):
        self._migrated_repo("git@forge.dudzik.app:estate/foo.git")
        self.assertEqual(len(self.ids()), 1)

    def test_forge_host_is_not_added_to_hosting(self):
        """Guard against the wrong fix: Forgejo must stay OUT of HOSTS (it is
        on the NAS, a local backup, not a hosting remote) -- it must group
        through the local path-tail, not through a `url:` hosting key."""
        self.assertNotIn("forge.dudzik.app", projects.HOSTS)
        self.assertFalse(projects._is_hosting(
            "https://forge.dudzik.app/estate/foo.git"))


class ForgeTailUnitChecks(unittest.TestCase):
    """_forge_tail in isolation, on the three URL shapes named in the task
    plus a non-forge URL, without going through the scan/storage fixture."""

    def test_ssh_with_port(self):
        self.assertEqual(
            projects._forge_tail("ssh://git@forge.dudzik.app:2222/estate/foo.git"),
            "git/foo")

    def test_https_no_port(self):
        self.assertEqual(
            projects._forge_tail("https://forge.dudzik.app/estate/foo.git"),
            "git/foo")

    def test_scp_like(self):
        self.assertEqual(
            projects._forge_tail("git@forge.dudzik.app:estate/foo.git"),
            "git/foo")

    def test_case_and_missing_dot_git_are_tolerated(self):
        self.assertEqual(
            projects._forge_tail("HTTPS://FORGE.DUDZIK.APP/estate/Foo"),
            "git/foo")

    def test_non_forge_url_returns_none(self):
        self.assertIsNone(
            projects._forge_tail("https://github.com/org/foo.git"))

    def test_forge_host_wrong_path_shape_returns_none(self):
        # Not under estate/ -- some other Forgejo org/repo is not this project.
        self.assertIsNone(
            projects._forge_tail("git@forge.dudzik.app:other/foo.git"))

    def test_empty_and_none_return_none(self):
        self.assertIsNone(projects._forge_tail(""))
        self.assertIsNone(projects._forge_tail(None))


class TheLabelKeyIsUniqueByConstruction(unittest.TestCase):
    """The uniqueness argument itself, stated as a test rather than only as a
    comment: the qualifier is the project's smallest (machine, path), which is
    the repos table's PRIMARY KEY, so it cannot repeat and cannot depend on the
    order the rows came back in."""

    def test_the_key_does_not_depend_on_row_order(self):
        a = {"machine": "nas", "path": "/path/to/projects/reflex"}
        b = {"machine": "desktop", "path": "C:/projects/reflex"}
        self.assertEqual(projects._canonical_key([a, b]),
                         projects._canonical_key([b, a]))

    def test_the_key_does_not_depend_on_which_copy_is_newest(self):
        a = {"machine": "desktop", "path": "C:/projects/reflex",
             "last_commit": "2026-01-01T00:00:00Z"}
        b = {"machine": "nas", "path": "/srv/reflex",
             "last_commit": "2026-09-01T00:00:00Z"}
        first = projects._canonical_key([a, b])
        a["last_commit"] = "2026-12-31T00:00:00Z"
        self.assertEqual(projects._canonical_key([a, b]), first)

    def test_two_projects_never_share_a_key(self):
        """A project is a disjoint group of repo rows and (machine, path) is
        unique per row, so two projects' keys differ."""
        one = [{"machine": "desktop", "path": "C:/projects/reflex"}]
        two = [{"machine": "desktop", "path": "C:/work/reflex"}]
        three = [{"machine": "nas", "path": "C:/projects/reflex"}]
        keys = [projects._canonical_key(g) for g in (one, two, three)]
        self.assertEqual(len(set(keys)), 3)


if __name__ == "__main__":
    unittest.main()
