# Clear mistaken unapproved review requirements — design spec

## Surfaces
`grogu plan shape --help` gains one recovery option. `grogu plan shape <id> --clear-review --why <reason>` gains one success line and existing error output handles refusals.

## Hierarchy
`--clear-review` is a peer of the existing mutually exclusive shape actions. The required `--why` keeps the destructive-looking inverse deliberate; `--require-review` remains the primary safety action and no force flag is offered.

## States
Default help lists `--clear-review` as architect-only and says `--why` is required. Success says the unapproved review requirement was cleared and other plan state is unchanged. Missing reason says `clearing a review requirement needs a reason the next reader can audit`. Invalid role/state errors identify the role or plan state and make no mutation.

## Flow
The architect identifies the same plan, runs the inverse with a reason, receives one success line, then reruns `grogu plan gate`. On failure, the plan is unchanged and the architect resolves the named approval/lifecycle blocker instead. There is no interactive confirmation or partial state.

## Copy
Help option: `clear an unapproved review requirement (architect only; requires --why)`. Success: `<plan-id>: cleared the unapproved review requirement; other plan state is unchanged`. Blank reason error: `clearing a review requirement needs a reason the next reader can audit`.

## Tokens
Terminal-only surface. Preserve argparse's existing indentation, wrapping, typography, and exit-code conventions. Add no color, animation, spacing system, or new visual token.

## Accessibility
All information is text, available to screen readers and copy/paste. The operation is fully keyboard-driven. Errors do not rely on color or position, and success names both the action and its intentionally limited scope.

## Layout
```
$ grogu plan shape p-20260822-example --clear-review --why "The hold targeted the wrong plan"
p-20260822-example: cleared the unapproved review requirement; other plan state is unchanged
```

## Acceptance criteria
- `grogu plan shape --help` includes `--clear-review` and states that `--why` is required.
- Success output names the plan and states that other plan state is unchanged.
- Refusals use stderr and a non-zero exit through existing CLI error handling.
- No prompt, force option, color dependency, or output table is introduced.

## Left to the engineer
The internal store method name and exact placement of validation checks may follow existing PlanStore conventions. Tests may inspect JSON directly or use existing CLI helpers as long as they prove the preserved-state and audit requirements.
