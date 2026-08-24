---
name: stim
description: Use only when the user explicitly asks to derive a finite pool of future messages from selected local conversation context.
---

This is a repository-distributed procedure layered on the existing iMessage
read, draft, and send commands. It is not a `grogu stim` subcommand, provider
router, scheduler service, or repository daemon.

## 1. Obtain consent before access

Do not read message history or invoke a model until the user explicitly
approves all of the following:

- the source conversation or conversations whose local context may be read;
- one target recipient;
- the purpose and tone;
- a finite candidate-pool size;
- the cadence;
- the active window or end date;
- quiet hours; and
- the exact model and provider choice.

Collect these settings without a questionnaire. Extract every value already
present in the user's request, propose reasonable concrete values for anything
missing, and present the complete configuration in one compact confirmation.
That confirmation must name the exact model and provider and state that the
approved excerpts or a derived prompt will be sent to it. Do not ask for fields
one at a time, and do not re-ask for a value the user already supplied. A single
unambiguous approval of the complete proposal satisfies this access consent.

A general request to search messages is not consent for this workflow. Sending
the approved excerpts or derived prompt to the selected model is also part of
the opt-in and must be stated before access.

## 2. Preserve provider fidelity

Use the currently active model/provider only when it is the user's explicit
choice. A configured Grok model is valid only when the user selected it through
Copilot's normal model selection and entitlement or policy makes it available.
Never translate a requested model to another provider. Never enable `auto`,
silently fall back, or route to Azure. If the exact choice is unavailable, stop
and explain how to select or configure it; do not draft with a substitute.

## 3. Keep private input and state local

Read only the minimum approved context through the existing read-only iMessage
workflow. Keep conversation excerpts, identifiers, prompts, generated pools,
draft ids, schedule state, and logs out of the repository and every publishable
artifact, including telemetry, plans, commits, issues, and pull requests.

Store workflow data only under `$GROGU_HOME/stim/<workflow-id>/`. Create the
workflow with user-only permissions: directory mode `0700` and file mode
`0600`. Use opaque workflow and draft ids in scheduler definitions. Do not pass
private text in command-line arguments or telemetry. The existing local
`grogu imessage draft --recipient <recipient> --message <body>` interface is
the only exception: its required recipient/body parameters are the sole
permitted command-line handoff for one approved candidate at a time. Never
echo, log, or persist its invocation.

## 4. Draft a finite pool without enabling anything

Generate a finite candidate pool locally. This phase must not send, schedule,
install, or enable anything. Reject cold outreach; bulk or multi-recipient use;
content intended to pressure or repeatedly contact someone who has not
responded; and any recipient who has asked not to be contacted.

After candidate review, convert each approved candidate into an ordinary local
iMessage draft. Every potentially scheduled item must have its own immutable
reviewed draft id. A changed recipient or body invalidates that draft and
requires a replacement draft plus a fresh exact review.

## 5. Review before initial enablement

Present every exact recipient/body pair and the complete proposed schedule:
cadence, first run, last run, message count, quiet hours, expiry, and the exact
disable command. Confirmation to derive or draft candidates is not
confirmation to send or schedule them.

Require a separate, unambiguous confirmation that authorizes enabling only the
reviewed finite schedule. Any edit before enablement invalidates the affected
draft and its approval.

## 6. Enforce bounded automation

Each workflow has exactly one recipient and may schedule at most seven messages.
Allow no more than five sends in any rolling 24-hour period, keep scheduled
sends at least 60 minutes apart, and enforce a maximum 30-day lifetime. The
reviewed schedule's active window is also its allowed-hours boundary, so a
separate quiet-hours prompt is unnecessary when that window is explicit. Do not
regenerate indefinitely. A request outside these limits remains draft-only or
manual-send only.

The scheduler may execute only already-reviewed immutable draft ids with
`grogu imessage send <draft-id> --confirm`. It must never call a model, choose a
provider, or mutate a recipient or body at run time. Stop immediately when the
pool is exhausted, the workflow expires, the user disables it, or a send fails.
After downtime, skip missed windows: allow no catch-up bursts, bunching, or
other attempts to replay missed sends.

## 7. Use a local macOS scheduler with an immediate off switch

For iMessage, place a per-user LaunchAgent plist under the user's
`~/Library/LaunchAgents/` directory and a small runner and state file beneath
`$GROGU_HOME/stim/<workflow-id>/`, all outside the repository. The runner may
select only the next due reviewed draft, record local status, and skip missed
windows. Never install a repository daemon or commit a plist containing private
state.

Before enablement, show the exact fully expanded disable/unload command. Use
this template only while constructing it:

`launchctl bootout gui/$(id -u) "$HOME/Library/LaunchAgents/<opaque-label>.plist"`

Test that exact disable path before enabling sends: bootstrap the LaunchAgent
with its runner hard-disabled from sending, execute the fully expanded
`launchctl bootout` command, and verify that it is unloaded. Only after that
test and the separate schedule confirmation may it be bootstrapped with sending
enabled. Disabling must prevent every future send immediately. Optional cleanup
may then remove the local LaunchAgent and
`$GROGU_HOME/stim/<workflow-id>/`; cleanup is not required for disablement to
take effect.
