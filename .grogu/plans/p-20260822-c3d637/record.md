# Reusable iMessage and stim skills

Plan `p-20260822-c3d637`. This is the account of how the plan changed while
it was being carried out; the stage files next to it are the plan.

## Stages judged unnecessary

- **design** — This change adds repository-distributed procedural skill Markdown and README guidance, not a new CLI/API/visual surface; exact command and safety contracts belong in implementation/testing rather than a separate design specification.

## Defects found by the tester

- **d1** (resolved, routed to engineer): Sealed testing plan (Automated contract tests, item 1) requires: 'Assert README names stim as a skill rather than documenting a nonexistent grogu stim command, and repeats the local-only/provider-fidelity/confirmation summary.' No such test exists. tests/test_grogu_skills.py's new RepositoryMessagingSkillContractTests class asserts only against the two SKILL.md bodies; it never reads README.md. A repo-wide grep for README in tests/ and src/ matches only unrelated pre-existing fixtures (throwaway README.md in worktree/plan test fixtures), none of which check the real top-level README's stim wording. Manual read confirms README.md's prose is currently correct (calls stim a skill, not a grogu stim subcommand, and repeats the local-only/provider-fidelity/confirmation summary), but nothing in the automated suite guards it from silently regressing, contrary to the sealed testing plan's explicit requirement. Needed: add a test that reads README.md and asserts it (a) names stim as a skill rather than a grogu stim subcommand, and (b) still states the local-only storage, provider-fidelity (Grok-only-if-selected, no auto/fallback/Azure), and draft/review/confirm summary.
