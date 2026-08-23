# Clear mistaken unapproved review requirements — design spec

## Surfaces
`grogu plan shape --help` gains the recovery option and a deliberate human override. `grogu plan shape <id> --clear-review --why <reason>` is for a declared architect; the same command with `--as-user` is for the person at an undeclared shell.

## Hierarchy
`--clear-review` is a peer of the mutually exclusive shape actions. The required `--why` keeps the inverse deliberate. Architect authority must be declared; `--as-user` is the only alternative and is refused to role-bound agent sessions. `--require-review` remains the primary safety action and no force flag is offered.

## States
Default help lists `--clear-review`, requires `--why`, and explains `--as-user`. Success says the unapproved review requirement was cleared and other plan state is unchanged. An undeclared call without `--as-user` says to export/pass architect or use `--as-user` if the caller is the user. Missing reason, agent misuse, approval, and lifecycle errors make no mutation.

## Flow
A declared architect runs the inverse with a reason. A human without an agent role adds `--as-user`. An undeclared call without that flag fails closed. On success the caller reruns `grogu plan gate`; on failure the plan remains unchanged and the named blocker is resolved instead. There is no prompt or partial state.

## Copy
Clear option: `clear an unapproved review requirement (declared architect or --as-user; requires --why)`. Human option: `you are the user, not an agent; permits --clear-review without an architect role`. Undeclared error: `clearing a review requirement needs a role: export GROGU_ROLE=architect, pass --role architect, or pass --as-user if you are the user`. Success: `<plan-id>: cleared the unapproved review requirement; other plan state is unchanged`.

## Tokens
Terminal-only surface. Preserve argparse's existing indentation, wrapping, typography, and exit-code conventions. Add no color, animation, spacing system, or new visual token.

## Accessibility
All information is text, available to screen readers and copy/paste. The operation is fully keyboard-driven. Errors do not rely on color or position, and success names both the action and its intentionally limited scope.

## Layout
```
$ grogu plan shape p-20260822-example --clear-review --why "The hold targeted the wrong plan"
grogu: clearing a review requirement needs a role: export GROGU_ROLE=architect, pass --role architect, or pass --as-user if you are the user

$ grogu plan shape p-20260822-example --clear-review --why "The hold targeted the wrong plan" --as-user
p-20260822-example: cleared the unapproved review requirement; other plan state is unchanged
```

## Acceptance criteria
- `grogu plan shape --help` includes `--clear-review`, required `--why`, and the explicit `--as-user` human path.
- No role and no `--as-user` exits non-zero without changing the plan.
- A role-bound agent cannot use `--as-user` to bypass its role.
- Success output names the plan and states that other plan state is unchanged.
- No prompt, force option, color dependency, or output table is introduced.

## Left to the engineer
The internal store method name and exact validation ordering may follow existing `finalize` conventions. Tests may inspect JSON directly or use existing CLI helpers as long as they prove authorization, preservation, and audit requirements.
