import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import _sandbox  # noqa: E402,F401  (redirects GROGU_HOME and HOME away from the real one)

import grogu_worktrees  # noqa: E402


def run(arguments, cwd):
    result = subprocess.run(
        arguments,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf8",
    )
    assert result.returncode == 0, f"{arguments} failed: {result.stderr}"
    return result.stdout


class GroguWorktreesTests(unittest.TestCase):
    def setUp(self):
        self.temporary_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary_dir.name) / "repo"
        self.repo.mkdir()
        run(["git", "init", "-b", "main"], self.repo)
        run(["git", "config", "user.email", "test@example.com"], self.repo)
        run(["git", "config", "user.name", "Test"], self.repo)
        (self.repo / "README.md").write_text("hello\n")
        run(["git", "add", "README.md"], self.repo)
        run(["git", "commit", "-m", "initial"], self.repo)

    def tearDown(self):
        self.temporary_dir.cleanup()

    def add_worktree(self, name):
        path = self.repo.parent / f"worktree-{name}"
        run(["git", "worktree", "add", str(path), "-b", name, "main"], self.repo)
        return path

    def test_list_worktrees_reports_main_and_branches(self):
        self.add_worktree("feature-a")
        worktrees = grogu_worktrees.list_worktrees(self.repo)
        self.assertEqual(len(worktrees), 2)
        self.assertTrue(worktrees[0].is_main)
        self.assertEqual(worktrees[0].branch, "main")
        self.assertFalse(worktrees[1].is_main)
        self.assertEqual(worktrees[1].branch, "feature-a")

    def test_merged_branch_worktree_is_stale(self):
        path = self.add_worktree("feature-merged")
        (path / "file.txt").write_text("change\n")
        run(["git", "add", "file.txt"], path)
        run(["git", "commit", "-m", "feature work"], path)
        run(["git", "merge", "feature-merged"], self.repo)

        stale = grogu_worktrees.stale_worktrees(self.repo)
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0].worktree.path.resolve(), path.resolve())
        self.assertEqual(stale[0].reason, "branch merged into main")

    def test_unmerged_branch_worktree_is_not_stale(self):
        path = self.add_worktree("feature-open")
        (path / "file.txt").write_text("change\n")
        run(["git", "add", "file.txt"], path)
        run(["git", "commit", "-m", "feature work"], path)

        stale = grogu_worktrees.stale_worktrees(self.repo)
        self.assertEqual(stale, [])

    def test_dirty_worktree_is_never_stale_even_if_merged(self):
        path = self.add_worktree("feature-dirty")
        (path / "file.txt").write_text("change\n")
        run(["git", "add", "file.txt"], path)
        run(["git", "commit", "-m", "feature work"], path)
        run(["git", "merge", "feature-dirty"], self.repo)
        (path / "scratch.txt").write_text("uncommitted\n")

        stale = grogu_worktrees.stale_worktrees(self.repo)
        self.assertEqual(stale, [])

    def test_prune_removes_stale_worktree_and_branch(self):
        path = self.add_worktree("feature-to-prune")
        (path / "file.txt").write_text("change\n")
        run(["git", "add", "file.txt"], path)
        run(["git", "commit", "-m", "feature work"], path)
        run(["git", "merge", "feature-to-prune"], self.repo)

        pruned = grogu_worktrees.prune_stale_worktrees(self.repo)

        self.assertEqual(len(pruned), 1)
        self.assertFalse(path.exists())
        branches = run(["git", "branch", "--list", "feature-to-prune"], self.repo)
        self.assertEqual(branches.strip(), "")

    def test_prune_leaves_dirty_worktree_untouched(self):
        path = self.add_worktree("feature-dirty-prune")
        (path / "file.txt").write_text("change\n")
        run(["git", "add", "file.txt"], path)
        run(["git", "commit", "-m", "feature work"], path)
        run(["git", "merge", "feature-dirty-prune"], self.repo)
        (path / "scratch.txt").write_text("uncommitted\n")

        pruned = grogu_worktrees.prune_stale_worktrees(self.repo)

        self.assertEqual(pruned, [])
        self.assertTrue(path.exists())

    # -- friction #46: a branch that never diverged is not "already merged" --

    def test_fresh_zero_commit_branch_is_not_stale(self):
        """A branch fresh off `worktree add -b`, with zero commits of its
        own, is trivially an ancestor of `main` -- ancestry alone cannot
        tell that apart from a branch whose real work already landed there
        by fast-forward. Treating the first as the second is exactly the
        bug harness friction #46 reported: a session's own just-created,
        not-yet-touched worktree was pruned before it could make a single
        commit."""
        path = self.add_worktree("feature-fresh")

        stale = grogu_worktrees.stale_worktrees(self.repo)

        self.assertEqual(stale, [])
        self.assertTrue(path.is_dir())

    def test_fresh_branch_with_uncommitted_work_is_not_stale(self):
        """The literal friction #46 incident: a freshly created worktree
        with in-progress untracked and modified files, none of it committed
        yet, must survive a staleness sweep -- both because it is dirty and
        because its branch has zero commits so far."""
        path = self.add_worktree("feature-in-progress")
        (path / "scratch.txt").write_text("in progress, not yet committed\n")
        (path / "README.md").write_text("modified but uncommitted\n")

        stale = grogu_worktrees.stale_worktrees(self.repo)

        self.assertEqual(stale, [])

    def test_prune_does_not_remove_fresh_zero_commit_worktree(self):
        """Same scenario through the actual removal path, not just
        classification: a session must never find its own brand-new
        worktree deleted out from under it before its first commit."""
        path = self.add_worktree("feature-brand-new")

        pruned = grogu_worktrees.prune_stale_worktrees(self.repo)

        self.assertEqual(pruned, [])
        self.assertTrue(path.is_dir())
        branches = run(["git", "branch", "--list", "feature-brand-new"], self.repo)
        # `git branch --list` marks a branch checked out in another worktree
        # with a leading "+", so check for its presence rather than an exact
        # match against the bare name.
        self.assertIn("feature-brand-new", branches)

    def test_genuinely_merged_branch_is_still_stale_after_the_fix(self):
        """Regression guard for the fix itself: a branch that has advanced
        (at least one commit of its own) and is later fast-forward merged
        must still be reported stale -- `_branch_ever_advanced` must key off
        the branch's own reflog, not merely "has commits now", or this would
        quietly undo the existing merged-branch contract."""
        path = self.add_worktree("feature-really-merged")
        (path / "file.txt").write_text("change\n")
        run(["git", "add", "file.txt"], path)
        run(["git", "commit", "-m", "feature work"], path)
        run(["git", "commit", "--allow-empty", "-m", "more feature work"], path)
        run(["git", "merge", "feature-really-merged"], self.repo)

        stale = grogu_worktrees.stale_worktrees(self.repo)

        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0].reason, "branch merged into main")

    # -- friction #46: active/session-owned worktrees are never pruned ------

    def test_active_path_worktree_is_never_pruned_even_if_stale(self):
        """A worktree a live process is sitting in must never be pruned,
        independent of the branch-history checks -- a caller names it via
        `active_paths` (typically its own current working directory) and it
        is exempt even when every other signal says "safe to delete"."""
        path = self.add_worktree("feature-active")
        (path / "file.txt").write_text("change\n")
        run(["git", "add", "file.txt"], path)
        run(["git", "commit", "-m", "feature work"], path)
        run(["git", "merge", "feature-active"], self.repo)

        stale = grogu_worktrees.stale_worktrees(self.repo, active_paths=[path])
        self.assertEqual(stale, [])

        pruned = grogu_worktrees.prune_stale_worktrees(self.repo, active_paths=[path])
        self.assertEqual(pruned, [])
        self.assertTrue(path.is_dir())

    def test_active_path_nested_inside_worktree_protects_it(self):
        """A path nested inside the worktree (e.g. a subdirectory a process
        happens to have as its cwd) counts as active too, not just the
        worktree's own root."""
        path = self.add_worktree("feature-active-nested")
        (path / "file.txt").write_text("change\n")
        run(["git", "add", "file.txt"], path)
        run(["git", "commit", "-m", "feature work"], path)
        run(["git", "merge", "feature-active-nested"], self.repo)
        nested = path / "sub"
        nested.mkdir()

        stale = grogu_worktrees.stale_worktrees(self.repo, active_paths=[nested])

        self.assertEqual(stale, [])

    def test_active_paths_do_not_protect_unrelated_worktrees(self):
        """`active_paths` is scoped to the paths it is given -- it must not
        blanket-exempt every worktree, only the ones it actually names."""
        active_path = self.add_worktree("feature-active-only")
        other_path = self.add_worktree("feature-unrelated-merged")
        for path, branch in (
            (active_path, "feature-active-only"),
            (other_path, "feature-unrelated-merged"),
        ):
            (path / "file.txt").write_text("change\n")
            run(["git", "add", "file.txt"], path)
            run(["git", "commit", "-m", "feature work"], path)
            run(["git", "merge", branch], self.repo)

        stale = grogu_worktrees.stale_worktrees(self.repo, active_paths=[active_path])

        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0].worktree.path.resolve(), other_path.resolve())


class GroguMainSyncTests(unittest.TestCase):
    """Tests for `sync_main_with_origin` / `main_behind_origin`, which need a
    real `origin` remote (a bare clone) rather than the single-repo setup
    used above."""

    def setUp(self):
        self.temporary_dir = tempfile.TemporaryDirectory()
        base = Path(self.temporary_dir.name)
        self.bare = base / "origin.git"
        run(["git", "init", "--bare", "-b", "main", str(self.bare)], base)

        self.upstream = base / "upstream"
        self.upstream.mkdir()
        run(["git", "init", "-b", "main"], self.upstream)
        run(["git", "config", "user.email", "test@example.com"], self.upstream)
        run(["git", "config", "user.name", "Test"], self.upstream)
        (self.upstream / "README.md").write_text("hello\n")
        run(["git", "add", "README.md"], self.upstream)
        run(["git", "commit", "-m", "initial"], self.upstream)
        run(["git", "remote", "add", "origin", str(self.bare)], self.upstream)
        run(["git", "push", "origin", "main"], self.upstream)

        self.repo = base / "clone"
        run(["git", "clone", str(self.bare), str(self.repo)], base)
        run(["git", "config", "user.email", "test@example.com"], self.repo)
        run(["git", "config", "user.name", "Test"], self.repo)

    def tearDown(self):
        self.temporary_dir.cleanup()

    def push_new_commit_upstream(self):
        (self.upstream / "README.md").write_text("hello again\n")
        run(["git", "add", "README.md"], self.upstream)
        run(["git", "commit", "-m", "update"], self.upstream)
        run(["git", "push", "origin", "main"], self.upstream)

    def test_sync_fast_forwards_clean_main_checkout(self):
        self.push_new_commit_upstream()

        result = grogu_worktrees.sync_main_with_origin(self.repo)

        self.assertIsNotNone(result)
        local_head = run(["git", "rev-parse", "HEAD"], self.repo).strip()
        remote_head = run(["git", "rev-parse", "origin/main"], self.repo).strip()
        self.assertEqual(local_head, remote_head)

    def test_sync_is_noop_when_already_up_to_date(self):
        result = grogu_worktrees.sync_main_with_origin(self.repo)
        self.assertIsNone(result)

    def test_sync_skips_dirty_checkout(self):
        self.push_new_commit_upstream()
        (self.repo / "scratch.txt").write_text("uncommitted\n")

        result = grogu_worktrees.sync_main_with_origin(self.repo)

        self.assertIsNone(result)
        local_head = run(["git", "rev-parse", "HEAD"], self.repo).strip()
        upstream_head = run(["git", "rev-parse", "HEAD"], self.upstream).strip()
        self.assertNotEqual(local_head, upstream_head)

    def test_sync_skips_when_not_on_main(self):
        run(["git", "checkout", "-b", "feature"], self.repo)
        self.push_new_commit_upstream()

        result = grogu_worktrees.sync_main_with_origin(self.repo)

        self.assertIsNone(result)

    def test_main_behind_origin_reports_true_after_fetch(self):
        self.push_new_commit_upstream()
        run(["git", "fetch", "origin"], self.repo)

        self.assertTrue(grogu_worktrees.main_behind_origin(self.repo))

    def test_main_behind_origin_reports_false_when_up_to_date(self):
        run(["git", "fetch", "origin"], self.repo)

        self.assertFalse(grogu_worktrees.main_behind_origin(self.repo))


class RemoteBranchStalenessTests(unittest.TestCase):
    """`stale_worktrees`'s "remote branch deleted" and "no upstream at all"
    paths (friction #46 regression coverage). These need a real bare
    `origin` so a branch can actually be pushed and its remote head actually
    deleted, unlike `GroguWorktreesTests`'s single-repo setup -- and closing
    this gap matters here specifically because the friction #46 fix touches
    the same `if`/`elif` chain in `stale_worktrees` that these two other
    branches live in."""

    def setUp(self):
        self.temporary_dir = tempfile.TemporaryDirectory()
        base = Path(self.temporary_dir.name)
        self.bare = base / "origin.git"
        run(["git", "init", "--bare", "-b", "main", str(self.bare)], base)

        self.repo = base / "repo"
        run(["git", "clone", str(self.bare), str(self.repo)], base)
        run(["git", "config", "user.email", "test@example.com"], self.repo)
        run(["git", "config", "user.name", "Test"], self.repo)
        (self.repo / "README.md").write_text("hello\n")
        run(["git", "add", "README.md"], self.repo)
        run(["git", "commit", "-m", "initial"], self.repo)
        run(["git", "push", "-u", "origin", "main"], self.repo)

    def tearDown(self):
        self.temporary_dir.cleanup()

    def add_worktree(self, name):
        path = self.repo.parent / f"worktree-{name}"
        run(["git", "worktree", "add", str(path), "-b", name, "main"], self.repo)
        return path

    def test_squash_merged_branch_with_deleted_remote_is_stale(self):
        """The common real-world case this signal exists for: GitHub
        squash-merges the PR (so the branch never becomes an ancestor of
        `main` locally) and deletes the head branch on the remote."""
        path = self.add_worktree("feature-squashed")
        (path / "file.txt").write_text("change\n")
        run(["git", "add", "file.txt"], path)
        run(["git", "commit", "-m", "feature work"], path)
        run(["git", "push", "-u", "origin", "feature-squashed"], path)
        run(["git", "push", "origin", "--delete", "feature-squashed"], path)

        stale = grogu_worktrees.stale_worktrees(self.repo)

        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0].worktree.path.resolve(), path.resolve())
        self.assertEqual(stale[0].reason, "remote branch deleted")

    def test_prune_removes_worktree_with_deleted_remote_branch(self):
        path = self.add_worktree("feature-squash-prune")
        (path / "file.txt").write_text("change\n")
        run(["git", "add", "file.txt"], path)
        run(["git", "commit", "-m", "feature work"], path)
        run(["git", "push", "-u", "origin", "feature-squash-prune"], path)
        run(["git", "push", "origin", "--delete", "feature-squash-prune"], path)

        pruned = grogu_worktrees.prune_stale_worktrees(self.repo)

        self.assertEqual(len(pruned), 1)
        self.assertFalse(path.exists())
        branches = run(
            ["git", "branch", "--list", "feature-squash-prune"], self.repo
        )
        self.assertEqual(branches.strip(), "")

    def test_unpublished_branch_is_not_stale_even_with_origin_configured(self):
        """Friction #46, stated explicitly: an unpublished local branch must
        not be considered stale solely because no remote branch exists for
        it. `_remote_branch_deleted` only fires once a remote WAS
        configured and its branch is now gone -- never for "was never
        pushed" -- and this must hold even in a repo that does have a real
        `origin` for other branches, not just when there is no remote at
        all."""
        path = self.add_worktree("feature-never-pushed")
        (path / "file.txt").write_text("change\n")
        run(["git", "add", "file.txt"], path)
        run(["git", "commit", "-m", "feature work"], path)

        stale = grogu_worktrees.stale_worktrees(self.repo)

        self.assertEqual(stale, [])

    def test_fresh_zero_commit_branch_is_not_stale_with_origin_configured(self):
        """Friction #46's core scenario, repeated here with a real `origin`
        present: a brand-new, untouched, unpushed branch must not be
        pruned, whether or not the repository happens to have a remote."""
        path = self.add_worktree("feature-fresh-with-origin")

        stale = grogu_worktrees.stale_worktrees(self.repo)

        self.assertEqual(stale, [])
        self.assertTrue(path.is_dir())


class WorkstreamWorktreeTests(unittest.TestCase):
    """Harness friction #40: declared parallel workstreams need a dedicated
    worktree each, computed the same deterministic way by every caller,
    rather than running in one shared checkout where scratch files can
    cross ownership."""

    def setUp(self):
        self.temporary_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary_dir.name) / "repo"
        self.repo.mkdir()
        run(["git", "init", "-b", "main"], self.repo)
        run(["git", "config", "user.email", "test@example.com"], self.repo)
        run(["git", "config", "user.name", "Test"], self.repo)
        (self.repo / "README.md").write_text("hello\n")
        run(["git", "add", "README.md"], self.repo)
        run(["git", "commit", "-m", "initial"], self.repo)

    def tearDown(self):
        self.temporary_dir.cleanup()

    # -- creation, and staying out of the shared checkout -------------------

    def test_ensure_creates_a_worktree_distinct_from_the_primary_checkout(self):
        result = grogu_worktrees.ensure_workstream_worktree(self.repo, "p1", "scaffold")

        self.assertTrue(result.created)
        self.assertEqual(result.branch, "workstream/p1/scaffold")
        self.assertNotEqual(result.path.resolve(), self.repo.resolve())
        self.assertTrue(result.path.is_dir())
        self.assertTrue((result.path / "README.md").is_file())

    def test_ensure_is_idempotent(self):
        first = grogu_worktrees.ensure_workstream_worktree(self.repo, "p1", "scaffold")
        second = grogu_worktrees.ensure_workstream_worktree(self.repo, "p1", "scaffold")

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.path, second.path)
        self.assertEqual(first.branch, second.branch)

    def test_distinct_workstreams_of_one_plan_get_distinct_paths(self):
        scaffold = grogu_worktrees.ensure_workstream_worktree(self.repo, "p1", "scaffold")
        ingest = grogu_worktrees.ensure_workstream_worktree(self.repo, "p1", "ingest")

        self.assertNotEqual(scaffold.path.resolve(), ingest.path.resolve())
        self.assertNotEqual(scaffold.branch, ingest.branch)
        self.assertTrue(scaffold.path.is_dir())
        self.assertTrue(ingest.path.is_dir())

    def test_same_workstream_name_in_different_plans_does_not_collide(self):
        plan_one = grogu_worktrees.ensure_workstream_worktree(self.repo, "p1", "api")
        plan_two = grogu_worktrees.ensure_workstream_worktree(self.repo, "p2", "api")

        self.assertNotEqual(plan_one.path.resolve(), plan_two.path.resolve())
        self.assertNotEqual(plan_one.branch, plan_two.branch)

    def test_scratch_files_do_not_cross_workstreams(self):
        """The literal friction #40 scenario: an engineer's untracked
        scratch/debug file must not be visible to another workstream, or to
        the primary checkout, once each has its own worktree."""
        scaffold = grogu_worktrees.ensure_workstream_worktree(self.repo, "p1", "scaffold")
        ingest = grogu_worktrees.ensure_workstream_worktree(self.repo, "p1", "ingest")

        (ingest.path / ".hourly_test.dat").write_text("scratch\n")

        self.assertFalse((scaffold.path / ".hourly_test.dat").exists())
        self.assertFalse((self.repo / ".hourly_test.dat").exists())

    def test_ensure_reuses_a_branch_that_already_exists_without_a_worktree(self):
        run(["git", "branch", "workstream/p1/scaffold"], self.repo)

        result = grogu_worktrees.ensure_workstream_worktree(self.repo, "p1", "scaffold")

        self.assertTrue(result.created)
        self.assertEqual(result.branch, "workstream/p1/scaffold")
        self.assertTrue(result.path.is_dir())

    def test_ensure_forks_from_explicit_base(self):
        run(["git", "checkout", "-b", "feature"], self.repo)
        (self.repo / "feature.txt").write_text("only on feature\n")
        run(["git", "add", "feature.txt"], self.repo)
        run(["git", "commit", "-m", "feature work"], self.repo)
        run(["git", "checkout", "main"], self.repo)

        result = grogu_worktrees.ensure_workstream_worktree(
            self.repo, "p1", "scaffold", base="feature"
        )

        self.assertTrue((result.path / "feature.txt").is_file())

    def test_ensure_rejects_blank_plan_id_or_name(self):
        with self.assertRaises(grogu_worktrees.WorktreeError):
            grogu_worktrees.ensure_workstream_worktree(self.repo, "", "scaffold")
        with self.assertRaises(grogu_worktrees.WorktreeError):
            grogu_worktrees.ensure_workstream_worktree(self.repo, "p1", "  ")

    # -- collisions raise instead of guessing --------------------------------

    def test_ensure_errors_when_path_is_a_stray_unrelated_directory(self):
        path = grogu_worktrees.workstream_worktree_path(self.repo, "p1", "scaffold")
        path.mkdir(parents=True)
        (path / "leftover.txt").write_text("not a worktree\n")

        with self.assertRaises(grogu_worktrees.WorktreeError):
            grogu_worktrees.ensure_workstream_worktree(self.repo, "p1", "scaffold")

        # Never clobbers what was already there.
        self.assertTrue((path / "leftover.txt").is_file())

    def test_ensure_errors_when_branch_is_checked_out_elsewhere(self):
        branch = grogu_worktrees.workstream_branch("p1", "scaffold")
        elsewhere = self.repo.parent / "elsewhere"
        run(["git", "worktree", "add", str(elsewhere), "-b", branch, "main"], self.repo)

        with self.assertRaises(grogu_worktrees.WorktreeError):
            grogu_worktrees.ensure_workstream_worktree(self.repo, "p1", "scaffold")

    # -- listing --------------------------------------------------------------

    def test_list_workstream_worktrees_scopes_to_one_plan(self):
        grogu_worktrees.ensure_workstream_worktree(self.repo, "p1", "scaffold")
        grogu_worktrees.ensure_workstream_worktree(self.repo, "p1", "ingest")
        grogu_worktrees.ensure_workstream_worktree(self.repo, "p2", "scaffold")

        scoped = grogu_worktrees.list_workstream_worktrees(self.repo, "p1")
        self.assertEqual({w.branch for w in scoped}, {
            "workstream/p1/scaffold", "workstream/p1/ingest",
        })

        every_plan = grogu_worktrees.list_workstream_worktrees(self.repo)
        self.assertEqual(len(every_plan), 3)

    def test_list_workstream_worktrees_ignores_unrelated_branches(self):
        self.add_plain_worktree("unrelated")

        self.assertEqual(grogu_worktrees.list_workstream_worktrees(self.repo), [])

    def add_plain_worktree(self, name):
        path = self.repo.parent / f"worktree-{name}"
        run(["git", "worktree", "add", str(path), "-b", name, "main"], self.repo)
        return path

    # -- cleanup ----------------------------------------------------------------

    def test_remove_cleans_up_and_is_idempotent(self):
        result = grogu_worktrees.ensure_workstream_worktree(self.repo, "p1", "scaffold")

        removed = grogu_worktrees.remove_workstream_worktree(self.repo, "p1", "scaffold")

        self.assertIsNotNone(removed)
        self.assertEqual(removed.path.resolve(), result.path.resolve())
        self.assertFalse(result.path.exists())

        again = grogu_worktrees.remove_workstream_worktree(self.repo, "p1", "scaffold")
        self.assertIsNone(again)

    def test_remove_of_never_created_workstream_is_a_noop(self):
        removed = grogu_worktrees.remove_workstream_worktree(self.repo, "p1", "never-made")
        self.assertIsNone(removed)

    def test_remove_refuses_a_dirty_worktree_without_force(self):
        result = grogu_worktrees.ensure_workstream_worktree(self.repo, "p1", "scaffold")
        (result.path / "scratch.txt").write_text("uncommitted\n")

        with self.assertRaises(grogu_worktrees.WorktreeError):
            grogu_worktrees.remove_workstream_worktree(self.repo, "p1", "scaffold")
        self.assertTrue(result.path.exists())

    def test_remove_with_force_discards_a_dirty_worktree(self):
        result = grogu_worktrees.ensure_workstream_worktree(self.repo, "p1", "scaffold")
        (result.path / "scratch.txt").write_text("uncommitted\n")

        removed = grogu_worktrees.remove_workstream_worktree(
            self.repo, "p1", "scaffold", force=True
        )

        self.assertIsNotNone(removed)
        self.assertFalse(result.path.exists())

    def test_remove_with_delete_branch_deletes_a_merged_branch(self):
        result = grogu_worktrees.ensure_workstream_worktree(self.repo, "p1", "scaffold")
        (result.path / "file.txt").write_text("change\n")
        run(["git", "add", "file.txt"], result.path)
        run(["git", "commit", "-m", "work"], result.path)
        run(["git", "merge", "workstream/p1/scaffold"], self.repo)

        removed = grogu_worktrees.remove_workstream_worktree(
            self.repo, "p1", "scaffold", delete_branch=True
        )

        self.assertTrue(removed.branch_deleted)
        self.assertEqual(removed.branch_kept_reason, "")
        branches = run(
            ["git", "branch", "--list", "workstream/p1/scaffold"], self.repo
        )
        self.assertEqual(branches.strip(), "")

    def test_remove_with_delete_branch_keeps_an_unmerged_branch(self):
        result = grogu_worktrees.ensure_workstream_worktree(self.repo, "p1", "scaffold")
        (result.path / "file.txt").write_text("change\n")
        run(["git", "add", "file.txt"], result.path)
        run(["git", "commit", "-m", "work"], result.path)

        removed = grogu_worktrees.remove_workstream_worktree(
            self.repo, "p1", "scaffold", delete_branch=True
        )

        # Removing the worktree still succeeds even though the branch is not
        # safe to delete yet -- a declined optional step must not look like
        # total failure.
        self.assertFalse(result.path.exists())
        self.assertFalse(removed.branch_deleted)
        self.assertNotEqual(removed.branch_kept_reason, "")
        branches = run(
            ["git", "branch", "--list", "workstream/p1/scaffold"], self.repo
        )
        self.assertEqual(branches.strip(), "workstream/p1/scaffold")

    def test_remove_with_delete_branch_keeps_a_fresh_zero_commit_branch(self):
        """Friction #46 applies equally here: a workstream branch that was
        just created and never touched is trivially an ancestor of its
        base, but that must not be read as "already merged, safe to
        delete" -- it has not started, not finished."""
        result = grogu_worktrees.ensure_workstream_worktree(self.repo, "p1", "scaffold")

        removed = grogu_worktrees.remove_workstream_worktree(
            self.repo, "p1", "scaffold", delete_branch=True
        )

        self.assertFalse(result.path.exists())
        self.assertFalse(removed.branch_deleted)
        self.assertNotEqual(removed.branch_kept_reason, "")
        branches = run(
            ["git", "branch", "--list", "workstream/p1/scaffold"], self.repo
        )
        self.assertEqual(branches.strip(), "workstream/p1/scaffold")

    # -- helpers used directly by callers that only need the path -----------

    def test_slug_sanitizes_unsafe_characters(self):
        branch = grogu_worktrees.workstream_branch("plan one", "api gateway!!")
        self.assertEqual(branch, "workstream/plan-one/api-gateway")

    def test_workstream_worktree_path_is_pure_and_deterministic(self):
        first = grogu_worktrees.workstream_worktree_path(self.repo, "p1", "scaffold")
        second = grogu_worktrees.workstream_worktree_path(self.repo, "p1", "scaffold")
        self.assertEqual(first, second)
        self.assertEqual(first.parent, grogu_worktrees.workstreams_root(self.repo))
        self.assertNotEqual(first.resolve(), self.repo.resolve())


if __name__ == "__main__":
    unittest.main()
