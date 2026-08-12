import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

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


if __name__ == "__main__":
    unittest.main()
