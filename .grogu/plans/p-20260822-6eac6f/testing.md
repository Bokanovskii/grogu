# Clear mistaken unapproved review requirements — testing plan

## Store tests
- Reproduce the prior recovery: create and populate one plan, require review by mistake, observe the implementation gate blocked, clear it as an architect with a reason, and observe the same plan/gate is usable without superseding it.
- Assert an undeclared caller is refused and leaves the hold intact.
- Assert explicit `as_user=True` succeeds only without a declared agent role, records user authorization, and preserves stage text/state.
- Assert architect success records the architect role and `as_user: false` alongside actor, timestamp, and trimmed reason.
- Assert `needs_review` remains `needs_review` and its gate remains blocked after clearing the independent review hold.
- Refuse blank reasons, non-architect roles, role-bound `as_user`, plans with no hold, approved plans, amending/superseded/complete plans, and unknown lifecycle states.

## CLI tests
- Assert `plan shape --help` exposes `--clear-review`, required `--why`, default-deny role guidance, and explicit `--as-user` human override.
- Assert the exact mistaken-hold architect flow succeeds, preserves stage state/text, and writes the audit event.
- Assert an invocation with no role and no `--as-user` fails without changing the manifest.
- Assert `--as-user` succeeds from an undeclared human shell and is refused from a role-bound agent session.

## Verification
Run targeted plan-store and CLI tests, then the full existing suite. Run guard scans and inspect the staged diff before commit.
