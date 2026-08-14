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


if __name__ == "__main__":
    unittest.main()
