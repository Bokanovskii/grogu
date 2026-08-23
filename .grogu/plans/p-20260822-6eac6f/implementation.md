# Clear mistaken unapproved review requirements — implementation plan

## Goal
Add a supported inverse for an architect who mistakenly ran `grogu plan shape --require-review`, without replacing or superseding the plan and without allowing an undeclared caller to bypass user approval.

## Store behavior
- Add a `PlanStore.clear_review_requirement` operation restricted to a declared architect or an explicit `as_user=True` caller.
- Default-deny when neither a role nor `as_user=True` is supplied, matching `finalize`; never infer architect authority from an undeclared caller.
- Refuse `as_user=True` when the current session declares an agent role, and refuse every declared non-architect role.
- Require a non-empty audited reason after trimming whitespace.
- Permit only unapproved plans whose lifecycle status can still be safely edited (`draft` or `needs_review`). Refuse plans with no review hold, any approval timestamp/status, amendments in progress, terminal states, or unknown states.
- Mutate only `review_required` plus normal manifest audit metadata. The event records actor, time, reason, authorization role, and whether `as_user` was used. Do not rewrite stage files/state, approval data, steering, amendments, defects, workstreams, or other safety constraints.

## CLI behavior
- Extend `grogu plan shape` with mutually exclusive `--clear-review`, document that `--why` is required, and add `--as-user` for a deliberate human override.
- Pass role and `as_user` independently to the store so an undeclared invocation is refused instead of silently becoming architect.
- On success print that the unapproved review requirement was cleared and all other plan state remains unchanged.
- Route errors through the existing `PlanError` handling with actionable role/`--as-user` guidance.

## Documentation
Update `docs/pipeline.md` and `.github/skills/grogu-pipeline/SKILL.md` with the default-deny rule, explicit human override, audit fields, narrow valid state, and the fact that it does not clear other blockers.

## Compatibility
Do not change `--require-review`, approval, gate, stage, sealing, or supersession semantics. Existing manifests without the new event fields continue to load unchanged.
