---
name: gmail
description: Use the opt-in Gmail adapter for read/search and draft-first email assistance.
---

Use only when the user explicitly asks to inspect or send Gmail. It is disabled
by default and reads credentials from the environment; never ask Grogu to store
tokens. Read operations use the least-privilege readonly scope.

Search with `grogu gmail search "query"` only after the user has explicitly
enabled Gmail. Create outbound mail with `grogu gmail draft`, show the exact
recipient, subject, and body, obtain explicit confirmation, then run
`grogu gmail send <draft-id> --confirm`. Never send, delete, label, or modify
mail implicitly, and never write message bodies or credentials to telemetry.
