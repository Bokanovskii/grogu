# Plan control room

The Control room is the default operational mode of the local plan workspace.
It shows the explicit repository program: the current plan plus plans named by
registered sessions in the same repository. It is a privacy-bounded view of
observable metadata, not a transcript or a model-generated progress report.

## First use

The board remains useful before any session registers. Its program strip shows
the current plan's status, revision, stage written/state metadata, review and
gate state, open defect and amendment counts, stage completion, and
waiting-on-user count. Metadata that cannot be read is labelled unavailable
instead of being rendered as zero.

The scope selector has two choices:

* **Repository program** shows all explicitly registered agents and plans in
  this repository.
* **This plan** shows the current plan and its registered agents.

The repository program is finite and explicit. The server does not scan other
repositories or infer plan membership from working-directory prefixes.

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

Registration binds repository, plan, run, agent, role, workstream, session, and
event-agent identity. Correlation never uses display-name similarity or
timestamps. Registration changes are reconciled on each collector sample, so a
new registration appears without restarting the workspace server.

Registration records are machine-local under `.grogu/state/`; they are not
committed. An unregistered event stream is never opened as an agent source.
Safe command metadata may appear separately under **Unregistered sessions**,
where lifecycle and activity remain `Unknown`.

## Independent operational facts

Each registered agent row keeps these facts separate:

* **lifecycle** is established only by an explicitly correlated run transition:
  `registered`, `running`, `finished`, `failed`, `cancelled`, or `unknown`;
* **activity** is attributable event activity: `active`, `quiet`,
  `possibly_stuck`, `blocked`, or `unknown`;
* **connection** is observer/source health: `live`, `stale`, `disconnected`, or
  `unknown`;
* **stage ownership** comes from recorded plan metadata, never from role alone;
* **revision relation** requires a recorded read receipt; current plan head is
  not proof that an agent read it.

Registration and recent Grogu commands are not evidence that a process is
running. Command recency changes neither lifecycle nor activity. A disconnected
source does not turn a previously observed lifecycle into `Finished`, and a
quiet readable source remains connected.

Elapsed time always names its evidence. A lifecycle-start span requires an
observed run start. A first-observed span is an observation lower bound, not
process runtime. Without either timestamp the board displays
`Unknown · no start evidence`.

## Events, sources, and coverage

The table and cards show allowlisted recent operational events without opening
the timeline. **Watch** opens the selected agent's timeline and moves keyboard
focus to it; `Escape` returns focus to the row or card that opened it.

Operational events contain only:

* validated command or tool name;
* event kind and phase;
* timestamp and bounded duration;
* success when known;
* a bounded machine error code when present.

No source and a readable empty source are different states. The UI also
distinguishes stale, disconnected, permission-limited, filter-empty, and
unavailable sources. The board keeps the last successful sample visible when a
poll fails and labels its age.

This checkout has no supported Python task-runtime status bridge. Unless a
registered normalized event reports a run transition, lifecycle is `Unknown`
and runtime coverage is unavailable. The Control room does not inspect OS
processes, personal session history, command arguments, or tool results to fill
that gap.

## Privacy boundary

The scope bar always exposes the server's fixed withheld-fields list. The
board, timeline, HTTP responses, caches, and receipts exclude:

* prompts and task descriptions;
* assistant message bodies and model reasoning;
* command and tool arguments;
* tool results;
* permission intention text and file-edit content;
* sealed-stage content and object identifiers;
* local source paths;
* credentials and personal or health data.

An unavailable source never changes this list and never makes withheld content
appear absent.

## Feedback and receipts

Feedback is scoped to an exact agent, role, role-on-plan, or plan. Agent
feedback resolves through that registration's plan store even when the agent is
registered on another program plan. Bare role feedback is visibly scoped to the
current plan.

The composer obtains binding support and consequences from
`feedback_capabilities`. If the backend cannot report a binding mapping for the
target, binding is disabled. Advisory feedback closes no gate. Binding feedback
shows the actual mapped gate or replan consequence and its recorded release
condition.

An HTTP success means the feedback was durably **Sent**, not delivered.
Delivery state advances only from recorded routing, delivery, acknowledgement,
or withdrawal evidence. Receipts include both plan and feedback ID so the
ledger remains unambiguous across a multi-plan program. Withdrawal sends the
receipt's plan back to the server and remains limited to feedback the existing
authority rules permit withdrawing.

## Browser API

* `GET /api/control?scope=repository_program|current_plan` returns one sampled
  repository-program snapshot with plan summaries, registered agents, source
  coverage, feedback capabilities, and bounded recent events.
* `GET /api/control/<agent-key>` returns the selected agent and its bounded
  allowlisted event timeline.
* `POST /api/control/<agent-key>/feedback` sends exact-agent feedback.
* `GET /api/feedback?plan=<id>` returns plan-qualified delivery receipts.
* `POST /api/feedback` sends scoped feedback.
* `POST /api/feedback/<id>/withdraw` withdraws an eligible binding receipt using
  the supplied plan.

All routes use the same loopback cookie, token, Origin, Host, CSP, and no-CORS
envelope as the plan document API.
