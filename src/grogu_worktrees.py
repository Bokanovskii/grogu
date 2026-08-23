"""Detect and prune stale `git worktree` checkouts left over from Grogu's own
self-modification workflow, keep the primary checkout's `main` in sync with
`origin/main`, and give a parallel pipeline workstream a dedicated worktree of
its own instead of a shared checkout.

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

Harness friction #46 found the "merged into main" check above too eager: a
branch fresh off `git worktree add -b` has zero commits of its own, so it is
trivially an ancestor of `main` -- indistinguishable, by ancestry alone,
from a branch whose real work already landed there by fast-forward. A
worktree created and not yet touched was being pruned as if it were one
whose pull request had already merged. `_branch_ever_advanced` closes that
gap by consulting the branch's own reflog, which a from-created-to-now
untouched branch cannot fake; `stale_worktrees`'s `active_paths` closes a
second, independent gap by letting a caller name paths -- typically its own
current working directory -- that must never be pruned regardless of what
the branch-history checks decide, because a worktree a live process is
sitting in is active by definition.

Harness friction #40 recorded the same problem one level up: a plan's
declared parallel workstreams were run against one shared checkout rather
than a worktree each, and a workstream's untracked scratch files ended up
sitting in the repository root instead of confined to any workstream's
declared paths -- the disjoint-globs guarantee this pipeline advertises had
nothing enforcing it. `ensure_workstream_worktree` and its companions below
are that enforcement: a workstream's dedicated worktree is a deterministic
function of the plan id and the workstream name, so every caller that asks
for it -- the engineer that owns it, the supervisor that spawned that
engineer -- computes the identical path and branch without a message ever
passing between them, and a second call finds exactly what the first one
made rather than picking a fresh name or falling back to the shared
checkout.
"""

from __future__ import annotations

import dataclasses
import re
import subprocess
from pathlib import Path
from typing import Iterable, Optional


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


def _branch_ever_advanced(root: Path, branch: str) -> bool:
    """Whether `branch`'s own ref has moved since the moment it was created.

    Friction #46: a branch that is merged into `target` by fast-forward (the
    common case, since `target` has usually not moved either) becomes
    indistinguishable from a branch that was just created and never
    touched -- ancestry alone says "yes" for both, because `target` ends up
    sitting at the exact same commit either way. Even a non-fast-forward
    merge commit does not help, since `target..branch` is empty once
    `branch`'s tip is reachable from `target`, whether that happened last
    week or a second ago.

    The branch ref's own reflog is not ambiguous: creating a branch (via
    `git branch`, `git checkout -b`, or `git worktree add -b`) writes exactly
    one reflog entry for it, and a real commit made on that branch always
    adds another one, which survives regardless of what `target` does
    afterwards. So a branch stuck at a single reflog entry has not diverged
    from its own starting point -- there is nothing on it for `target` to
    have merged, and an ancestor result for it means "not started", not
    "finished".
    """
    output = _run(["git", "reflog", "show", "--format=%gs", branch], root)
    if not output:
        # No reflog to consult (e.g. expired, or reflogs disabled) --
        # fail closed: treat it as unadvanced so it is never mistaken for a
        # completed merge rather than risk the opposite.
        return False
    entries = [line for line in output.splitlines() if line.strip()]
    return len(entries) > 1


def _is_merged_into(root: Path, branch: str, target: str) -> bool:
    if not _branch_ever_advanced(root, branch):
        return False
    output = _run(
        ["git", "merge-base", "--is-ancestor", branch, target], root
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


def _resolved_paths(paths: Optional[Iterable[Path]]) -> list[Path]:
    resolved: list[Path] = []
    for candidate in paths or ():
        try:
            resolved.append(candidate.resolve())
        except OSError:
            continue
    return resolved


def _is_active(worktree_path: Path, active: list[Path]) -> bool:
    """Whether one of `active` sits at or inside `worktree_path`.

    Friction #46: a worktree a live process is actually sitting in is active
    by definition, not by inference from branch history -- this is what lets
    a caller protect its own current working directory (or any other path it
    knows is in use) regardless of what the staleness checks below decide.
    """
    try:
        resolved_worktree = worktree_path.resolve()
    except OSError:
        return False
    for candidate in active:
        if candidate == resolved_worktree or resolved_worktree in candidate.parents:
            return True
    return False


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


def stale_worktrees(
    root: Path, *, active_paths: Optional[Iterable[Path]] = None
) -> list[StaleWorktree]:
    """Return worktrees whose branch is safe to remove.

    Only worktrees other than the primary checkout are considered, and only
    when the worktree has no uncommitted changes. `active_paths` (friction
    #46) is an additional, independent guard: any worktree that contains one
    of these paths -- typically the caller's own current working directory
    -- is never reported stale no matter what the branch-history checks
    below conclude, because a worktree a live process is sitting in is
    active by definition, not by inference from its git state.
    """
    active = _resolved_paths(active_paths)
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
        if _is_active(worktree.path, active):
            continue
        if not _worktree_is_clean(worktree.path):
            continue
        if _is_merged_into(root, worktree.branch, "main"):
            stale.append(StaleWorktree(worktree, "branch merged into main"))
        elif _remote_branch_deleted(root, worktree.branch):
            stale.append(StaleWorktree(worktree, "remote branch deleted"))
        elif _pr_merged_via_gh(root, worktree.branch):
            stale.append(StaleWorktree(worktree, "pull request merged"))
    return stale


def prune_stale_worktrees(
    root: Path, *, active_paths: Optional[Iterable[Path]] = None
) -> list[StaleWorktree]:
    """Remove stale worktrees (and their local branches) and report them."""
    pruned: list[StaleWorktree] = []
    for entry in stale_worktrees(root, active_paths=active_paths):
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


# -- workstream worktrees ---------------------------------------------------
#
# One dedicated worktree per declared pipeline workstream, computed the same
# way by every caller so parallel agents never have to agree on a name or
# fall back to sharing `root`.


class WorktreeError(Exception):
    """A workstream worktree could not be found, created or removed safely.

    Raised instead of guessing whenever the deterministic path or branch is
    already claimed by something unexpected -- a stray file, a different
    branch, or the same branch checked out somewhere else -- so a collision
    is surfaced to the caller rather than silently papered over by deleting
    or reusing the wrong thing.
    """


@dataclasses.dataclass
class WorkstreamWorktree:
    path: Path
    branch: str
    created: bool


_SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(value: str) -> str:
    """A filesystem- and Git-ref-safe token derived from `value`.

    Plan ids are already safe (they are generated internally), but a
    workstream name is whatever the architect typed, so this is what keeps a
    name like "api gateway" from breaking a branch name or a path component.
    """
    cleaned = _SLUG_UNSAFE.sub("-", value.strip()).strip("-")
    return cleaned or "workstream"


def workstream_branch(plan_id: str, name: str) -> str:
    """Deterministic branch name for one workstream of one plan.

    Grouped under `workstream/<plan>/` so every workstream of a plan shares a
    prefix (`git branch --list 'workstream/<plan>/*'` finds them all), and
    two different plans never collide even if an architect reuses a
    workstream name like "api" across them.
    """
    return f"workstream/{_slug(plan_id)}/{_slug(name)}"


def workstreams_root(root: Path) -> Path:
    """Sibling directory holding every dedicated workstream worktree for `root`.

    Mirrors the self-modification convention in `.github/AGENTS.md`
    (`git worktree add ../grogu-worktrees/<branch> ...`), generalised to
    whichever repository a plan targets -- which is not always Grogu's own
    checkout.
    """
    return root.parent / f"{root.name}-worktrees"


def workstream_worktree_path(root: Path, plan_id: str, name: str) -> Path:
    """Deterministic dedicated worktree path for one workstream.

    Pure and side-effect free: a supervisor computing this to fan out and the
    engineer it spawned both land on the exact same path without a message
    ever passing between them, which is what makes `ensure_workstream_worktree`
    idempotent rather than merely retryable.
    """
    return workstreams_root(root) / f"{_slug(plan_id)}-{_slug(name)}"


def _branch_exists(root: Path, branch: str) -> bool:
    return (
        _run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], root)
        is not None
    )


def _branch_worktree_path(root: Path, branch: str) -> Optional[Path]:
    for worktree in list_worktrees(root):
        if worktree.branch == branch:
            return worktree.path
    return None


def ensure_workstream_worktree(
    root: Path, plan_id: str, name: str, *, base: Optional[str] = None
) -> WorkstreamWorktree:
    """Get or create the one dedicated worktree for this workstream.

    Idempotent and safe to call from every agent that picks up the
    workstream: the path and branch are computed the same way every time (see
    `workstream_worktree_path`), so a second caller finds exactly what the
    first one made instead of creating a sibling copy or falling back to the
    shared checkout the way friction #40 described.

    Never removes or overwrites anything already at the deterministic path.
    A collision -- the path exists but is not this workstream's worktree, or
    the branch is already checked out somewhere else -- raises
    `WorktreeError` instead of guessing which side is right.

    `base` is the commit the workstream's branch forks from when it does not
    already exist; it defaults to whatever branch `root` is currently on (or
    its bare `HEAD` commit when that cannot be named), which is the same
    convention the self-modification worktrees use with `main`.
    """
    if not plan_id.strip() or not name.strip():
        raise WorktreeError("a workstream worktree needs both a plan id and a name")
    branch = workstream_branch(plan_id, name)
    path = workstream_worktree_path(root, plan_id, name)

    existing_branch_path = _branch_worktree_path(root, branch)
    if existing_branch_path is not None:
        if existing_branch_path.resolve() != path.resolve():
            raise WorktreeError(
                f"workstream {name!r} of {plan_id} is already checked out at "
                f"{existing_branch_path}, not {path}; remove it first if it is "
                "stale, or use the worktree that is already there"
            )
        return WorkstreamWorktree(path=path, branch=branch, created=False)

    registered_paths = {worktree.path.resolve() for worktree in list_worktrees(root)}
    if path.exists() or path.resolve() in registered_paths:
        raise WorktreeError(
            f"{path} already exists and is not the worktree for workstream "
            f"{name!r} of {plan_id}; move or remove it before this workstream "
            "can claim it"
        )

    workstreams_root(root).mkdir(parents=True, exist_ok=True)
    if _branch_exists(root, branch):
        created = _run(["git", "worktree", "add", str(path), branch], root)
    else:
        fork_point = base or _current_branch(root) or "HEAD"
        created = _run(
            ["git", "worktree", "add", str(path), "-b", branch, fork_point], root
        )
    if created is None:
        # Two agents picking up the same workstream at once can both pass
        # every check above before either calls `git worktree add`; re-check
        # rather than fail the loser of that race.
        winner = _branch_worktree_path(root, branch)
        if winner is not None and winner.resolve() == path.resolve():
            return WorkstreamWorktree(path=path, branch=branch, created=False)
        raise WorktreeError(f"git worktree add failed for branch {branch!r} at {path}")
    return WorkstreamWorktree(path=path, branch=branch, created=True)


def list_workstream_worktrees(
    root: Path, plan_id: Optional[str] = None
) -> list[WorkstreamWorktree]:
    """Every dedicated workstream worktree registered for `root`.

    Scoped to one plan when `plan_id` is given, otherwise every workstream
    worktree for every plan this repository has run.
    """
    prefix = f"workstream/{_slug(plan_id)}/" if plan_id else "workstream/"
    return [
        WorkstreamWorktree(path=worktree.path, branch=worktree.branch, created=False)
        for worktree in list_worktrees(root)
        if worktree.branch and worktree.branch.startswith(prefix)
    ]


def workstream_branch_is_stale(
    root: Path, plan_id: str, name: str, *, base: Optional[str] = None
) -> Optional[str]:
    """Why a workstream's branch would be safe to delete, or `None`.

    Reuses the exact staleness rules `stale_worktrees` applies to
    self-modification worktrees -- merged into its base, its remote branch
    deleted, or its pull request merged -- rather than inventing a second,
    looser rule for the same question.
    """
    branch = workstream_branch(plan_id, name)
    target = base or _current_branch(root) or "HEAD"
    if _is_merged_into(root, branch, target):
        return f"merged into {target}"
    if _remote_branch_deleted(root, branch):
        return "remote branch deleted"
    if _pr_merged_via_gh(root, branch):
        return "pull request merged"
    return None


@dataclasses.dataclass
class RemovedWorkstreamWorktree:
    path: Path
    branch_deleted: bool
    branch_kept_reason: str = ""


def remove_workstream_worktree(
    root: Path,
    plan_id: str,
    name: str,
    *,
    force: bool = False,
    delete_branch: bool = False,
    base: Optional[str] = None,
) -> Optional[RemovedWorkstreamWorktree]:
    """Remove one workstream's dedicated worktree, and report where it was.

    Returns `None` when there was nothing registered to remove, which makes
    this safe to call twice. Refuses a worktree that still has uncommitted
    changes unless `force` is set -- the same rule self-modification pruning
    uses, except here it is one explicit, named workstream a caller asked to
    clean up rather than a background sweep, so an unmerged branch is fine;
    only losing uncommitted work by accident is not.

    `delete_branch` additionally deletes the local branch, but only when
    `workstream_branch_is_stale` finds a reason it is safe to. When it is not,
    this does not raise -- the worktree was still what the caller asked to
    remove and that part succeeded -- it reports `branch_kept_reason` instead,
    the same way a partially-successful cleanup should never be indistinguishable
    from a failed one.
    """
    branch = workstream_branch(plan_id, name)
    path = _branch_worktree_path(root, branch)
    if path is None:
        return None
    if not force and path.is_dir() and not _worktree_is_clean(path):
        raise WorktreeError(
            f"{path} has uncommitted changes; commit or stash them, or pass "
            "force to remove it anyway"
        )
    arguments = ["git", "worktree", "remove", str(path)]
    if force:
        arguments.append("--force")
    removed = _run(arguments, root)
    if removed is None and path.is_dir():
        raise WorktreeError(f"failed to remove the worktree at {path}")
    _run(["git", "worktree", "prune"], root)
    branch_deleted = False
    branch_kept_reason = ""
    if delete_branch:
        reason = workstream_branch_is_stale(root, plan_id, name, base=base)
        if reason is None:
            branch_kept_reason = "not merged and no closed pull request was found"
        else:
            _run(["git", "branch", "-D", branch], root)
            branch_deleted = True
    return RemovedWorkstreamWorktree(
        path=path, branch_deleted=branch_deleted, branch_kept_reason=branch_kept_reason
    )

