"""Detect and prune stale `git worktree` checkouts left over from Grogu's own
self-modification workflow, and keep the primary checkout's `main` in sync
with `origin/main`.

Per the operating contract in `.github/AGENTS.md`, Grogu changes to its own
source checkout happen in a dedicated `git worktree` on a branch, and that
worktree is meant to be removed once its pull request merges or is
abandoned; the primary checkout itself must always stay on a clean `main`
that reflects `origin/main`. In practice both are easy to forget across
sessions, so this module gives Grogu a way to notice and fix them itself:
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
changes, so in-progress work is never discarded. Likewise, `main` is only
ever fast-forwarded when the primary checkout is already on a clean `main`;
it is never switched to `main` or reset over local changes.
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


def _current_branch(path: Path) -> Optional[str]:
    output = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], path)
    return output.strip() if output is not None else None


def main_behind_origin(root: Path) -> Optional[bool]:
    """Report (without fetching) whether local `main` trails `origin/main`.

    Diagnostic only, so it never mutates the checkout: it compares the refs
    already known locally rather than fetching, and returns `None` when that
    can't be determined (e.g. no `origin/main` ref cached yet).
    """
    output = _run(
        ["git", "rev-list", "--count", "main..origin/main"], root
    )
    if output is None:
        return None
    return output.strip() != "0"


def sync_main_with_origin(root: Path) -> Optional[str]:
    """Fast-forward the primary checkout's `main` to `origin/main`.

    Per the self-modification contract, the primary checkout must always
    stay on `main` and reflect `origin/main` so every new launch runs the
    latest merged code. Only acts when `root` is already on a clean `main`
    (never switches branches or discards work); returns a short description
    of what happened, or `None` if nothing changed or the sync was skipped.
    """
    if _current_branch(root) != "main" or not _worktree_is_clean(root):
        return None
    before = _run(["git", "rev-parse", "HEAD"], root)
    if _run(["git", "fetch", "origin", "main"], root) is None:
        return None
    if _run(["git", "pull", "--ff-only", "origin", "main"], root) is None:
        return None
    after = _run(["git", "rev-parse", "HEAD"], root)
    if before is None or after is None or before.strip() == after.strip():
        return None
    return f"updated main to {after.strip()[:12]}"


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
