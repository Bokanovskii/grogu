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

Phrase person-and-recency queries naturally -- "latest from Adrian",
"what did Kaya say yesterday". seaglass reads "from X" as the *sender*
and routes recency-only wording to a time-ordered browse, so there is no
need to rewrite them into keyword form.

For outbound messages, create a draft, show the exact recipient and body, obtain
explicit confirmation, then run `grogu imessage send <draft-id> --confirm`.
Never send directly from search results or include message content in
telemetry. The adapter is unavailable on non-macOS systems.
