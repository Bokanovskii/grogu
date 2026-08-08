---
name: imessage
description: Use the opt-in local macOS iMessage adapter for read/search and confirmation-gated drafts.
---

Use only when the user explicitly asks to inspect or send iMessages. Run
`grogu imessage status` first, use read-only search, and explain Full Disk
Access failures without bypassing macOS privacy controls.

For outbound messages, create a draft, show the exact recipient and body, obtain
explicit confirmation, then run `grogu imessage send <draft-id> --confirm`.
Never send directly from search results or include message content in
telemetry. The adapter is unavailable on non-macOS systems.
