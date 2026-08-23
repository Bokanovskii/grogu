# Bind claimed pipeline roles to agent identities

Plan `p-20260822-11f261`. This is the account of how the plan changed while
it was being carried out; the stage files next to it are the plan.

## Stages judged unnecessary

- **design** — The change adds an authorization invariant and refusal text to an existing CLI; no new interaction flow or visual surface is introduced.
- **evaluation** — Deterministic store and subprocess CLI tests fully verify the security boundary; no qualitative end-to-end evaluation is needed.
