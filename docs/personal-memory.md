# Personal memory

`grogu personal` is a user-scoped memory store about the person Grogu
assists — separate from `grogu memory`, which is repository-local and
code-focused (see `docs/memory.md`). Personal memory lives under
`GROGU_HOME/memory/` (default `~/.grogu/memory/`), never inside a repository
and never committed to Git.

## Why a separate store

Repository intelligence and personal memory answer different questions and
must not mix:

| | `grogu memory` | `grogu personal` |
| --- | --- | --- |
| Scope | one repository | the user, across all repositories |
| Storage | `<repo>/.grogu/intelligence/` | `GROGU_HOME/memory/` |
| Committed to Git | yes | never |
| Node types | architecture, component, decision, convention, workflow, service, file, work | person, preference, project, goal, event, fact, interest |

## Confirmed memory vs. pending candidates

Two write paths exist, and only one persists immediately:

* **`remember`** is explicit: the user or an agent acting on the user's
  explicit instruction records a confirmed fact directly into the graph.
* **`suggest`** is passive: something observed in conversation, email, or
  another integration is queued as a *candidate* in a separate pending file.
  A candidate is never merged into the confirmed graph on its own. Use
  `review` to see pending candidates, `confirm` to persist one (with
  provenance recording it as a confirmed suggestion), or `reject` to discard
  it. This is the mechanism that keeps Gmail/iMessage-derived context
  (see the integration issues) consent-gated rather than silently inferred.

## Commands

```sh
grogu personal status
grogu personal remember --type person --name "Jamie" --summary "Sister, lives in Denver"
grogu personal remember --type preference --name "standup-time" --summary "Prefers 9am meetings"
grogu personal link person:jamie event:denver-trip --kind relates-to
grogu personal list --type person
grogu personal recall --query denver --limit 20
grogu personal recall --node person:jamie --depth 1

grogu personal suggest --type event --name "jamie-birthday" \
  --summary "Mentioned Jamie's birthday is in March" --source gmail --confidence 0.5
grogu personal review
grogu personal confirm <candidate-id>
grogu personal reject <candidate-id>

grogu personal forget person:jamie
```

`recall` mirrors `grogu memory context`: it returns a bounded,
machine-readable neighborhood (by query or graph traversal from a node) rather
than an unbounded dump of everything known about the user.

## Privacy boundary

* Personal memory is never written under `<repo>/.grogu/`, so it is never
  shared through Git or another repository's task records.
* Passive capture always lands in the pending queue first; nothing is
  persisted without an explicit `confirm`.
* Provenance on every node and candidate records where a fact came from
  (`user`, or an integration name such as `gmail`/`imessage`), so confirmed
  memory can be audited and pruned.
* `GROGU_PERSONAL_MEMORY_DIR` is exported to Copilot sessions the same way
  `GROGU_MEMORY_DIR` is, so an agent can locate the store without embedding
  personal data in prompts or traces.
