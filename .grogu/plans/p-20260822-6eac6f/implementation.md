# Clear mistaken unapproved review requirements — implementation plan

## Goal
Add a supported inverse for an architect who mistakenly ran `grogu plan shape --require-review`, without replacing or superseding the plan.

## Store behavior
- Add a `PlanStore.clear_review_requirement` operation restricted to the architect role.
- Require a non-empty audited reason after trimming whitespace.
- Permit only unapproved plans whose lifecycle status can still be safely edited (`draft` or `needs_review`). Refuse plans with no review hold, any approval timestamp/status, amendments in progress, terminal states, or unknown states.
- Mutate only `review_required` plus normal manifest audit metadata (`updated_at` and an event containing actor, time, and reason). Do not rewrite stage files, stage state, approval data, steering, amendments, defects, workstreams, or other safety constraints.

## CLI behavior
- Extend `grogu plan shape` with mutually exclusive `--clear-review` and document that it is architect-only and requires `--why`.
- On success print that the unapproved review requirement was cleared and all other plan state remains unchanged.
- Route errors through the existing `PlanError` handling with actionable messages.

## Documentation
Update `docs/pipeline.md` and `.github/skills/grogu-pipeline/SKILL.md` with the recovery command, its narrow valid state, required reason, audit record, and the fact that it does not clear other blockers.

## Compatibility
Do not change `--require-review`, approval, gate, stage, sealing, or supersession semantics. Existing manifests without the new event continue to load unchanged.
