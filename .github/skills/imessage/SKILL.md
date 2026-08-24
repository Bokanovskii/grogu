---
name: imessage
description: Use the opt-in local macOS iMessage adapter for read/search and confirmation-gated drafts with optional attachments.
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

1. Run `grogu imessage draft --recipient <recipient> --message <body>` with
   zero or more `--attachment <path>` flags and retain the returned draft id.
   A contact name is resolved through seaglass to the concrete handle of one
   direct Messages conversation; unresolved, ambiguous, and group recipients
   fail before a draft is created. Drafting is local and non-sending.
2. Display the exact resolved recipient, complete final body, and every ordered
   attachment's index, filename, canonical source path, byte count, and SHA-256
   from that draft. Never infer a recipient or attachment from conversation
   context or search output.
3. Wait for an unambiguous user confirmation of that exact recipient/body/
   attachment bundle. Approval of a Grogu software plan, topic, tone, intent,
   or partial message is not confirmation of the reviewed payload.
4. Only then run `grogu imessage send <draft-id> --confirm`. This send command
   is the sole external side effect. Success means Messages accepted the
   submission; delivery remains asynchronous and is not confirmed by Grogu.

Drafting snapshots and hashes local files only. It does not upload files, open
Messages, create Messages staging files, or submit anything. At confirmed send
time, Grogu revalidates both the reviewed source and immutable private snapshot,
then copies the verified snapshot into private Messages-accessible staging
beneath `GROGU_IMESSAGE_STAGING_ROOT` (default
`~/Library/Messages/.grogu-send-staging`). The private staging base, attempt,
and index directories use mode `0700`; files use mode `0600`. An existing
configured base with group or other permissions is rejected rather than
modified, and per-attempt staging paths are not printed.
Messages receives the body first, waits 0.5 seconds, then receives each
attachment in review order with 0.5 seconds between attachment submissions.
These are separate operations. Staged files are retained because Messages
consumes aliases asynchronously; remove an old attempt directory only after
checking Messages and deciding the attachment no longer needs it.

Never send directly from search output. If the recipient, body, attachment
set, order, path, filename, size, digest, or contents changes after review,
discard the old approval, create and display a replacement draft, and obtain
fresh confirmation before sending it. Never put message content, recipient
identifiers, attachment paths, filenames, hashes, or bytes in repository files,
telemetry, plans, commits, issues, or pull requests. The adapter is unavailable
on non-macOS systems.
