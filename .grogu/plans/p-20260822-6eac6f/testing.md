# Clear mistaken unapproved review requirements — testing plan

## Store tests
- Reproduce the prior recovery: create and populate one plan, require review by mistake, observe the implementation gate blocked, clear it with a reason, and observe the same plan/gate is usable without superseding it.
- Assert implementation/testing stage file bytes and all `stage_state` values are unchanged.
- Assert the manifest event records `review_cleared`, actor, timestamp, and trimmed non-empty reason.
- Assert `needs_review` remains `needs_review` and its gate remains blocked after clearing the independent review hold.
- Refuse blank reasons, non-architect roles, plans with no hold, approved plans, amending/superseded/complete plans, and unknown lifecycle states.

## CLI tests
- Assert `plan shape --help` clearly exposes `--clear-review` and the required `--why` relationship.
- Assert the exact mistaken-hold CLI flow succeeds, prints explicit narrow success output, preserves stage state/text, and writes the audit event.
- Assert missing reasons and non-architect callers fail with useful errors.

## Verification
Run targeted plan-store and CLI tests, then the full existing suite. Run guard scans and inspect the staged diff before commit.
