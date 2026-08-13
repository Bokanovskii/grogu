---
name: split-prs-integrate
description: Split unrelated changes into small single-purpose pull requests, each in its own worktree, and test them together on one local integration branch.
---

Use this when one session produced several unrelated changes, or when planned
work spans several independent concerns. Reviewers get small pull requests they
can reason about one at a time; testing still happens once, against everything
combined.

The two goals conflict if you pick only one branch strategy. A single big
branch is easy to test and miserable to review. Isolated branches are easy to
review but leave you building and installing each one separately, and never
prove they work together. Keep both: isolated branches for review, one
throwaway merge branch for testing.

## Split the work

1. Group the diff by concern, not by file. One reviewable claim per pull
   request: a bug fix, a heuristic change, a new feature, a performance fix.
2. Snapshot the combined work on a scratch branch first if it already exists as
   one lump, so nothing is lost while you carve it up.
3. Create one worktree per concern off the base branch, and apply only that
   concern's changes:

   ```sh
   git worktree add ../<repo>_worktrees/<slug> -b <type>/<slug> origin/main
   ```

4. Keep each branch independently correct: it must build and pass tests on its
   own, without the others. If two changes genuinely cannot be separated, that
   is one pull request, not two.
5. Open each pull request with what changed, why, and how it was verified.

## Integrate for testing

Create one local integration branch that merges every pull request branch, and
treat it as disposable:

```sh
git worktree add ../<repo>_worktrees/integration-test -b integration/all-prs origin/main
cd ../<repo>_worktrees/integration-test
git merge origin/<type>/<slug-1> origin/<type>/<slug-2> ...
```

Build, test, and run the application from this worktree only. It is where
cross-branch conflicts and interactions surface — two branches editing the same
heuristic table, or one branch's new file needing another's project
registration.

Never push the integration branch and never open a pull request from it. It
exists to be rebuilt, not reviewed or merged.

## Keep it current

After changing any pull request branch, push that branch, then re-merge it into
the integration worktree and rebuild:

```sh
cd ../<repo>_worktrees/<slug> && git push
cd ../<repo>_worktrees/integration-test && git merge origin/<type>/<slug>
```

Fix review feedback on the branch that owns the change, never on the
integration branch. A fix committed only to the integration branch is invisible
to reviewers and is lost when the branch is deleted.

## Verify

Run each branch's own tests on its own branch, and the full suite on the
integration branch. The counts should differ in a way you can explain: the
integration total equals the base suite plus the tests each branch added. An
unexplained difference means a merge dropped something.

Report both numbers, since they answer different questions — whether each pull
request is independently sound, and whether they work together.

## Clean up

Remove each worktree once its pull request merges or is abandoned, and delete
the integration branch outright:

```sh
git worktree remove ../<repo>_worktrees/<slug>
git worktree remove ../<repo>_worktrees/integration-test
git branch -D integration/all-prs
```

## Generated project files

When the build system generates project files from a manifest (XcodeGen,
CMake, Bazel), regenerate after every merge and after any branch adds a file.
A merge that brings in a new source file updates the manifest but not the
generated project, so the build silently omits it.

Per-developer build settings — signing identity, team, bundle prefix — belong
in the local worktree, not in a commit. Apply them once per worktree and mark
the file so Git ignores the change:

```sh
git update-index --skip-worktree <manifest>
```
