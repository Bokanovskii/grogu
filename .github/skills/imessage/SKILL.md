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

For outbound messages, create a draft, show the exact recipient and body, obtain
explicit confirmation, then run `grogu imessage send <draft-id> --confirm`.
Never send directly from search results or include message content in
telemetry. The adapter is unavailable on non-macOS systems.
