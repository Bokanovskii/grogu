# Test objective

Prove that the repository distributes both skills, that their written contracts preserve the existing iMessage confirmation boundary and the new stim privacy/provider/scheduling boundaries, and that no runtime product behavior or private example data slipped into the change.

# Automated contract tests

1. Add repository-fixture tests in `tests/test_grogu_skills.py` that read the real worktree through `grogu_skills.installed_skills(ROOT)`.
   - Assert exactly one discoverable `imessage` entry and one discoverable `stim` entry, each backed by `.github/skills/<name>/SKILL.md`, valid matching front matter, and a procedural body above the hollow-skill threshold.
   - For `imessage`, assert the body contains both exact command shapes (`grogu imessage draft --recipient ... --message ...` and `grogu imessage send <draft-id> --confirm`), orders draft/review/confirm/send, requires the complete recipient and body to be shown, and says an edit requires a replacement/re-review rather than reusing approval.
   - For `stim`, assert the body independently names all pre-access consent fields; explicitly supports a user-selected configured model including Grok; forbids automatic/alternate/Azure fallback; separates draft-only work from scheduling/sending; confines private state beneath `$GROGU_HOME` and excludes repository/telemetry/public artifacts; requires review of every exact recipient/body plus schedule and disable command before initial enablement; schedules only immutable reviewed draft ids through the existing send command; and states the finite ceilings (one recipient, seven items, one per 24 hours, 30 days), expiry, no regeneration, no catch-up, failure stop, anti-bulk/opt-out rules, and LaunchAgent disablement.
   - Assert README names `stim` as a skill rather than documenting a nonexistent `grogu stim` command, and repeats the local-only/provider-fidelity/confirmation summary.
   - Use structural helpers or grouped assertions so wording can improve without weakening the required concepts; do not reduce the test to one large exact-string snapshot.

2. Run:
   - `python -m unittest tests.test_grogu_skills`
   - `python -m unittest tests.test_grogu_cli.GroguCliTests.test_imessage_drafts_require_confirmation`
   The second check protects the runtime premise the skills rely on even though source files should remain untouched.

# Independent repository checks

3. Inspect `git diff --name-only` and fail if the implementation changes product/runtime files. Expected implementation paths are `.github/skills/imessage/SKILL.md`, `.github/skills/stim/SKILL.md`, `README.md`, and `tests/test_grogu_skills.py`, plus plan/task artifacts managed by the pipeline.

4. Run `grogu skill list --json` from this worktree and verify both skill names resolve to this branch's paths without registration or setup changes.

5. Inspect the changed Markdown and tests, then run the repository privacy guard over publishable changes. Fail on any realistic phone/address/account identifier, conversation excerpt, generated message pool, draft id, provider credential, absolute user path, or scheduler plist carrying recipient/body data. Placeholder tokens and cited public URLs are allowed. Verify no test writes local stim state beneath the repository.

6. In an isolated `$GROGU_HOME`, create an iMessage draft through the CLI using synthetic tokens and verify the draft is stored only in that home and no send process is invoked. Attempting adapter send without confirmation must still raise/refuse. Do not execute a live `send --confirm` in tests.

# Pass criteria

All targeted tests and independent checks pass; only the intended documentation/skill/test files change; both skills are discoverable; every safety/provider/privacy/scheduling assertion is present; and validation sends no external message and uses no real conversation data.
