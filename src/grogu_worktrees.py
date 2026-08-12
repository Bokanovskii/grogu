"""Detect and prune stale `git worktree` checkouts left over from Grogu's own
self-modification workflow.

Per the operating contract in `.github/AGENTS.md`, Grogu changes to its own
source checkout happen in a dedicated `git worktree` on a branch, and that
worktree is meant to be removed once its pull request merges or is
abandoned. In practice that cleanup step is easy to forget across sessions,
so this module gives Grogu a way to notice and clear those worktrees itself:
at the start of a new session (best-effort, non-blocking) and via the
explicit `grogu worktree list`/`grogu worktree prune` commands.

A worktree is considered stale when its branch is safe to discard:

* the branch is fully merged into `main` (a fast-forward or non-squash
  merge leaves it an ancestor), or
* the branch used to track a remote branch that no longer exists (the
  common case after a squash-merge, since GitHub deletes the head branch by
  default), or
* `gh` is available and reports the associated pull request as merged.

A worktree is only ever removed automatically when it has no uncommitted
changes, so in-progress work is never discarded.
"""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path
from typing import Optional


@dataclasses.dataclass
class Worktree:
    path: Path
    branch: Optional[str]
    is_main: bool


@dataclasses.dataclass
class StaleWorktree:
    worktree: Worktree
    reason: str


def _run(arguments: list[str], cwd: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            arguments,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def list_worktrees(root: Path) -> list[Worktree]:
    """Parse `git worktree list --porcelain` into structured entries."""
    output = _run(["git", "worktree", "list", "--porcelain"], root)
    if output is None:
        return []
    worktrees: list[Worktree] = []
    path: Optional[Path] = None
    branch: Optional[str] = None
    is_main = True
    for line in output.splitlines() + [""]:
        if line.startswith("worktree "):
            path = Path(line[len("worktree ") :])
        elif line.startswith("branch "):
            ref = line[len("branch ") :]
            branch = ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
        elif line == "" and path is not None:
            worktrees.append(Worktree(path=path, branch=branch, is_main=is_main))
            path = None
            branch = None
            is_main = False
    return worktrees


def _is_merged_into_main(root: Path, branch: str) -> bool:
    output = _run(
        ["git", "merge-base", "--is-ancestor", branch, "main"], root
    )
    # `_run` only returns non-None on a zero exit status, which is exactly
    # what `--is-ancestor` uses to signal "yes, merged".
    return output is not None


def _remote_branch_deleted(root: Path, branch: str) -> bool:
    had_upstream = _run(
        ["git", "config", f"branch.{branch}.remote"], root
    )
    if not had_upstream or not had_upstream.strip():
        return False
    remote = had_upstream.strip()
    listing = _run(["git", "ls-remote", "--heads", remote, branch], root)
    return listing is not None and listing.strip() == ""


def _pr_merged_via_gh(root: Path, branch: str) -> bool:
    output = _run(
        ["gh", "pr", "view", branch, "--json", "state"], root
    )
    if output is None:
        return False
    return '"state":"MERGED"' in output.replace(" ", "")


def _worktree_is_clean(path: Path) -> bool:
    output = _run(["git", "status", "--porcelain"], path)
    return output is not None and output.strip() == ""


def stale_worktrees(root: Path) -> list[StaleWorktree]:
    """Return worktrees whose branch is safe to remove.

    Only worktrees other than the primary checkout are considered, and only
    when the worktree has no uncommitted changes.
    """
    stale: list[StaleWorktree] = []
    for worktree in list_worktrees(root):
        if worktree.is_main or worktree.branch is None:
            continue
        if worktree.branch == "main":
            continue
        if not worktree.path.is_dir():
            # The directory was already removed outside of Grogu; git still
            # tracks it until `git worktree prune` runs.
            stale.append(StaleWorktree(worktree, "directory missing"))
            continue
        if not _worktree_is_clean(worktree.path):
            continue
        if _is_merged_into_main(root, worktree.branch):
            stale.append(StaleWorktree(worktree, "branch merged into main"))
        elif _remote_branch_deleted(root, worktree.branch):
            stale.append(StaleWorktree(worktree, "remote branch deleted"))
        elif _pr_merged_via_gh(root, worktree.branch):
            stale.append(StaleWorktree(worktree, "pull request merged"))
    return stale


def prune_stale_worktrees(root: Path) -> list[StaleWorktree]:
    """Remove stale worktrees (and their local branches) and report them."""
    pruned: list[StaleWorktree] = []
    for entry in stale_worktrees(root):
        removed = _run(
            ["git", "worktree", "remove", str(entry.worktree.path)], root
        )
        if removed is None and entry.reason != "directory missing":
            # Not clean, in use, or some other reason git refused; leave it
            # in place rather than forcing away work.
            continue
        if entry.reason == "directory missing":
            _run(["git", "worktree", "prune"], root)
        if entry.worktree.branch:
            _run(["git", "branch", "-D", entry.worktree.branch], root)
        pruned.append(entry)
    return pruned
