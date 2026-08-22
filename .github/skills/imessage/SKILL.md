---
name: imessage
description: Use the opt-in local macOS iMessage adapter for read/search and confirmation-gated drafts.
---

Use only when the user explicitly asks to inspect or send iMessages. Run
`grogu imessage status` first, use read-only search, and explain Full Disk
Access failures without bypassing macOS privacy controls.

`grogu imessage search` automatically prefers a configured seaglass MCP
server (semantic/ranked retrieval) when `status` reports `seaglass: true`,
falling back to a plain substring scan otherwise -- no extra flag is needed
in normal use. Pass `--no-seaglass` only if the substring-scan behavior is
specifically wanted.

If seaglass is configured but its call fails, a warning is printed to
stderr and the substring scan answers instead. Treat that warning as a
broken result, not a slower one: the fallback matches literal text only, so
a topical query ("what did we decide about the trip") legitimately returns
nothing. Check seaglass's index path rather than rephrasing the query.

Messages the user sent themselves come back with the handle `me`.

seaglass indexes a snapshot, so it can lag the live Messages database. A
search prints a warning to stderr when the index is behind, and
`grogu imessage status` reports `seaglass_index.n_messages_since_index`.
When the user asks about something recent ("what did she just say", "did
he reply yet") and the index is stale, run `grogu imessage sync` before
answering rather than reporting an older message as the latest.

Phrase person-and-recency queries naturally -- "latest from <sender>",
"what did <sender> say yesterday". seaglass reads "from <sender>" as the
*sender* and routes recency-only wording to a time-ordered browse, so there
is no need to rewrite them into keyword form.

For every outbound message, use this procedure and no other send path:

1. Run `grogu imessage draft --recipient <recipient> --message <body>` and
   retain the returned draft id. Drafting is local and non-sending.
2. Display the exact resolved recipient and the complete final body from that
   draft. Never infer a recipient from conversation context or search output.
3. Wait for an unambiguous user confirmation of that exact recipient/body
   pair. Approval of a topic/tone, intent, or general idea is not approval of
   the final body.
4. Only then run `grogu imessage send <draft-id> --confirm`. This send command
   is the sole external side effect.

Never send directly from search output. If either the recipient or body changes
after review, discard the old approval, create and display a replacement
draft, and obtain fresh confirmation before sending it. Never put message
content or recipient identifiers in repository files, telemetry, plans,
commits, issues, or pull requests. The adapter is unavailable on non-macOS
systems.
