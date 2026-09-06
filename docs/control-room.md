# Plan control room

The Control room is the fifth mode of the local plan workspace. It is a
privacy-bounded view of observable agent activity, not a transcript and not a
model-generated progress report.

## Explicit registration

Copilot session event files are never discovered by scanning. A supervisor
registers one exact source:

```sh
grogu plan doc register <plan-id> \
  --run-id <run> \
  --session-id <session> \
  --agent-id <agent-id> \
  --agent <GROGU_AGENT> \
  --session-role engineer \
  --workstream integration \
  --events "$COPILOT_HOME/session-state/<session>/events.jsonl"
```

The registration binds repository, plan, run, agent, role, workstream, session,
and event-agent identity. Correlation never uses a cwd prefix, display-name
similarity, or timestamps. At most 32 distinct event sources are registered per
reader. A path outside `session-state/<id>/events.jsonl`, including a symlink
escape, is refused by the event reader.

Registration records are machine-local under `.grogu/state/`; they are not
committed. An unregistered event stream is never opened and never appears on
the API.

## Read model

```sh
grogu plan doc control <plan-id> [--filter-plan ID] [--filter-role ROLE]
                              [--workstream NAME] [--state STATE]
                              [--window MINUTES] [--json]
grogu plan doc control <plan-id> --agent <agent-key> [--json]
```

Each agent row keeps three facts separate:

* **connection** — health of the observer (`live`, `stale`, `disconnected`,
  `unknown`);
* **lifecycle** — explicit run evidence (`registered`, `running`, `finished`,
  `failed`, `cancelled`, `unknown`);
* **activity** — recent observable action (`active`, `quiet`,
  `possibly_stuck`, `blocked`, `unknown`).

Silence never means finished. A terminal lifecycle stays terminal if the
collector later disconnects. `possibly_stuck` is a displayed heuristic with
fixed evidence, never an automatic stop or replan.

The browser-facing DTO is constructed from the allowlisted control-room model.
It does not pass through arbitrary event dictionaries.

## Sources and coverage

The command feed under `$GROGU_HOME/activity.jsonl` provides command names,
roles, agents, plans, exit status, and timestamps. It never records command
arguments.

An explicitly registered Copilot `events.jsonl` may add lifecycle, tool name,
phase, duration, success, and bounded error-code evidence. The normalizer drops
everything else before returning a record. Missing, rotated, truncated,
oversized, malformed, or unsupported sources produce a coverage gap rather
than an invented count.

`traces.db` may be probed for scalar coverage only. Arbitrary trace payloads are
never selected, joined, returned, cached, or mentioned by value.

## Privacy boundary

The board, drill-in, HTTP responses, exceptions, and in-memory cache exclude:

* prompts and task descriptions;
* assistant message bodies;
* model reasoning and opaque reasoning fields;
* raw command or tool arguments;
* raw tool results;
* permission intention text;
* file contents and edits;
* sealed-stage content and identifiers;
* arbitrary trace payloads.

Tool rows contain only a validated tool name, phase, duration, bounded outcome,
and safe error code. Limits are always shown so unavailable coverage cannot be
mistaken for a clean run.

## Feedback and receipts

Feedback uses the existing `PlanStore.steer` channel:

```sh
grogu plan doc control <plan-id> --agent <agent-key> \
  --feedback - [--binding]
```

The workspace also supports agent, role, plan, and role-on-plan scopes.
Every message receives a durable `f-NNN` receipt derived from its steering
record. The delivery ledger reports `sent`, `routed`, `delivered`,
`acknowledged`, or `withdrawn`.

Binding feedback closes only the mapped next gate while it remains
unacknowledged. `grogu plan gate` names the feedback id and a bounded summary.
The target's normal steering acknowledgement reopens the gate. An
unacknowledged binding item may be withdrawn; acknowledged feedback cannot.

The relay receipt contains a command that lets a supervisor prompt the target
to poll steering. It never embeds the feedback text in a second channel.

## Browser API

* `GET /api/control` — normalized snapshot with freshness, fixed limits,
  waiting items, registered agents, and plan counts.
* `GET /api/control/<agent-key>` — allowlisted activity, authorized evidence,
  fixed limits, and blockers.
* `POST /api/control/<agent-key>/feedback` — agent-scoped feedback.
* `GET /api/feedback` — delivery ledger.
* `POST /api/feedback` — scoped feedback.
* `POST /api/feedback/<id>/withdraw` — withdraw unacknowledged binding
  feedback.

All routes use the same loopback cookie, token, Origin, Host, CSP, and
no-CORS envelope as the plan document API.
