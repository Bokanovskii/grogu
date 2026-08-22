# Objective

Publish two repository-distributed, reviewable skills: strengthen the existing `imessage` skill's draft/send boundary, and add a `stim` skill for opt-in conversation-derived message-pool drafting with bounded local scheduling. This is instruction/documentation work only; do not add a `grogu stim` command, provider router, scheduler service, database, or other product runtime.

# Evidence and constraints

- `.github/skills/imessage/SKILL.md` already requires `grogu imessage draft`, exact recipient/body review, explicit confirmation, then `grogu imessage send <draft-id> --confirm`.
- `src/grogu_cli.py` and `src/grogu_imessage.py` already persist drafts under `$GROGU_HOME`, refuse sends without `--confirm`, and reject reuse of a sent draft. Reuse these commands; do not bypass or duplicate them.
- `README.md` documents that Grogu preserves explicit Copilot options such as `--model`, never selects a provider through `GROGU_AZURE`, and automatically discovers `.github/skills/<name>/SKILL.md` without registration.
- GitHub's 2026-08-14 Copilot changelog says Grok can be explicitly selected in Copilot CLI when entitlement/policy makes it available: https://github.blog/changelog/2026-08-14-grok-4-6-is-now-available-in-github-copilot/ . Treat availability as configuration, not a fallback promise.
- Apple's per-user scheduling mechanism is a LaunchAgent managed by `launchd`/`launchctl`, outside the repository: https://support.apple.com/guide/terminal/script-management-with-launchd-apdc6c1077b/mac (checked 2026-08-22). iMessage scheduling is therefore macOS-local.

# Implementation

1. Update `.github/skills/imessage/SKILL.md` without weakening its existing read/search guidance.
   - Add an explicit outbound procedure using only the existing commands: create a draft with `grogu imessage draft --recipient <recipient> --message <body>`, retain the returned draft id, display the exact resolved recipient and complete body, wait for an unambiguous user confirmation, then call `grogu imessage send <draft-id> --confirm`.
   - State that drafting is local and non-sending, while `send` is the sole external side effect. Never send directly from search output, infer a recipient, or treat approval of a topic/tone as approval of the final body.
   - If recipient or body changes after review, create/review a replacement draft and obtain fresh confirmation. Never put message content or recipient identifiers in repository files, telemetry, plans, commits, issues, or pull requests.

2. Add `.github/skills/stim/SKILL.md` with valid front matter and a narrow trigger: use only when the user explicitly asks to derive a finite pool of future messages from selected local conversation context.
   - **Consent before access:** before reading history or invoking a model, collect explicit approval for the source conversation(s), one target recipient, purpose/tone, finite pool size, cadence, active window/end date, quiet hours, and the exact model/provider choice. Do not infer consent from a general request to search messages.
   - **Provider fidelity:** use the currently active model/provider only when it is the user's explicit choice. A configured Grok model is valid when selected through Copilot's normal model selection. Never translate a requested model to another provider, enable `auto`, silently fall back, or route to Azure. If the chosen model is unavailable, stop and explain how to select/configure it; do not draft with a substitute.
   - **Private input boundary:** read only the minimum user-approved context through the existing read-only iMessage workflow. Sending that context to the selected model is itself part of the opt-in. Keep conversation excerpts, identifiers, prompts, generated pools, draft ids, schedule state, and logs out of the repository and all publishable artifacts. Store workflow data only beneath `$GROGU_HOME/stim/<workflow-id>/` with user-only permissions; use opaque workflow/draft ids in scheduler definitions. Do not pass private text in command-line arguments or telemetry.
   - **Draft-only phase:** generate a finite candidate pool locally; this phase must not send, schedule, or enable anything. Reject cold outreach, bulk/multi-recipient use, content intended to pressure or repeatedly contact someone who has not responded, and any recipient who has asked not to be contacted. Convert approved candidates to ordinary local iMessage drafts so every scheduled item has an immutable draft id.
   - **Review and initial enablement:** present every exact recipient/body pair plus the cadence, first/last run, count, quiet hours, expiry, and the exact disable command. Any edit invalidates the corresponding draft. Require a separate explicit confirmation that clearly authorizes enabling this reviewed finite schedule; confirmation to draft is not confirmation to send or schedule.
   - **Bounded automation:** one recipient per workflow; at most seven messages; no more than one send in any 24-hour period; maximum 30-day lifetime; no indefinite regeneration; no catch-up bursts after downtime; stop when the pool is exhausted, expired, disabled, or a send fails. A request outside those limits remains draft/manual-send only. The scheduler may execute only already-reviewed draft ids via `grogu imessage send <draft-id> --confirm`; it must never call a model or mutate recipient/body at run time.
   - **Platform scheduler and disablement:** for iMessage, create a user LaunchAgent and a small local runner/state file outside the repository. The runner selects only the next due reviewed draft, records local status, and skips missed windows rather than bunching sends. Show and test a `launchctl` disable/unload command before enablement; disabling must prevent future sends immediately, and optional cleanup may then remove the local LaunchAgent and `$GROGU_HOME/stim/<workflow-id>/` state. Never install a repository daemon or commit a plist containing private state.

3. Expand the `README.md` messaging section.
   - List `stim` as a reusable skill layered on the existing iMessage commands, not as a new Grogu subcommand.
   - Summarize the phases (opt in/read, draft pool, exact review, explicit enable, bounded local schedule, disable), provider-selection/no-fallback rule, local-only storage, and repository-publication prohibition.
   - Use placeholders only; include no realistic handles, transcripts, generated messages, or saved pools.

4. Extend `tests/test_grogu_skills.py` with repository-skill contract coverage rather than changing runtime code.
   - Verify `installed_skills(ROOT)` discovers both `imessage` and `stim` from this worktree with valid front matter/non-empty procedural bodies.
   - Pin the iMessage draft-before-send command sequence, exact recipient/body review, explicit confirmation, and re-review after edits.
   - Pin the stim consent fields, explicit model/provider and Grok/configured wording, refusal to fall back/route silently, repository-exclusion/local-state rules, separation of drafting from sending/scheduling, immutable reviewed draft ids, finite numeric bounds, native scheduler/disable path, and anti-bulk/no-catch-up behavior.
   - Keep fixtures synthetic and generic. Tests must not contain plausible personal identifiers, message history, or an example generated pool.

# Non-goals and sequencing

- No design stage: there is no new visual, CLI, or API surface; the observable contract is the skill text and README.
- Do not modify `src/`, `bin/`, setup, telemetry, iMessage adapter semantics, or provider selection behavior unless a separately reported defect proves the existing commands cannot support the documented workflow.
- Implement the two skills first, then README, then contract tests. The files are tightly coupled and should remain one sequential workstream.
