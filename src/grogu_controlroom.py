"""The planning control room -- observability read model.

Read the ``grogu_agentevents`` docstring before touching this file. The
privacy contract there is enforced across every entry point of this module;
if you find a place in this file where a forbidden category (raw prompts,
assistant bodies, ``reasoningText``, ``reasoningOpaque``, task descriptions,
raw tool arguments/results, permission intention text, secrets, sealed-stage
content) could leak into a returned structure, a cache, an exception message
or a serialisation, that is a bug in this file rather than a downstream
filtering opportunity.

# What this module is

The control room is the mode of the plan workspace that lists every agent
Grogu observes across every plan and repository, plus the CLI view backing
``grogu plan doc control``. This module builds only the read model: the
board snapshot, the drill-in, the registration record, and the feedback
routing that reaches an agent through the existing :meth:`PlanStore.steer`
channel. It writes nothing to disk except through :meth:`PlanStore.steer`
and the registration record; it stands up no server, no database, and no new
log.

# The three axes, and why they never merge

``connection`` describes the observer's health. ``lifecycle`` describes what
the agent's run is doing. ``activity`` describes what it appears to be doing
now. Confusing them is how a dashboard starts lying.

A terminal ``lifecycle`` never regresses. A subagent that emitted
``subagent.completed`` at 10:00 and then produces no events at 10:30 is still
``finished``: silence is not a rebirth. This is why lifecycle is stored per
agent-key with an ordinal and only advances on stronger evidence.

# possibly_stuck is a labelled heuristic

The tests pin the thresholds:

  * ``TOOL_DEADLINE_GRACE_SECONDS = 60`` -- a tool exceeds an observed
    deadline by this many seconds.
  * ``NO_OUTCOME_AFTER_ACTIVITY_SECONDS = 900`` and
    ``NO_OUTCOME_MIN_TOOL_EVENTS = 100`` -- no lifecycle result / stage
    change / revision / artifact for 15 minutes while at least 100 tool
    completions were observed.
  * ``REPEATED_FAILURES_WINDOW_SECONDS = 120`` and
    ``REPEATED_FAILURES_THRESHOLD = 3`` -- three explicit failures with the
    same safe error code within two minutes.

The label ``possibly_stuck`` never causes an action. The interface shows the
condition that triggered it in ``possibly_stuck_evidence``; it does not
narrate.

# Correlation is by registration only

There is no ``cwd`` prefix match, no display-name similarity, no
"whichever session started nearest in time" heuristic. A session appears in
the board's ``agents`` list only when a registration record explicitly links
it to a repository, plan, agent name, role and workstream. Anything else --
including a source 2 event stream whose ``agentId`` never appeared in a
registered mapping -- is either invisible (source 2 alone) or lands in
``uncorrelated`` (source 1 only), which is shown as such with no imported
role, no imported description and no inference.

# Freshness never means finished

Every snapshot carries ``fresh_as_of``: the timestamp of the newest record
this reader considered. The interface shows the age of the data beside the
age of an agent. If ``fresh_as_of`` is older than the connection budget, the
connection is ``stale`` or ``disconnected``; the lifecycle is unchanged.

# Cache lifetime

``ControlRoom`` holds a small dictionary cache of per-source cursors and per-
agent state. That cache is instance-local, disposable, and keyed by
``(repository, plan, effective_role)``. A role change or a restart discards
it. A broader view cannot be reused for a narrower one; each new
``ControlRoom`` starts empty.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

import grogu_agentevents
import grogu_watch

# -- constants: the tested thresholds -------------------------------------

CONNECTION_STALE_SECONDS = 10
CONNECTION_DISCONNECTED_SECONDS = 30

# The three named ``possibly_stuck`` triggers.
TOOL_DEADLINE_GRACE_SECONDS = 60
NO_OUTCOME_AFTER_ACTIVITY_SECONDS = 900
NO_OUTCOME_MIN_TOOL_EVENTS = 100
REPEATED_FAILURES_WINDOW_SECONDS = 120
REPEATED_FAILURES_THRESHOLD = 3

# Sampling budgets. The dashboard is polled by clients; the reader itself
# samples at most once per :data:`SAMPLE_INTERVAL_SECONDS` regardless of how
# many clients ask.
SAMPLE_INTERVAL_SECONDS = 2

# Bounded caches. The bounds keep a long-running dashboard from growing
# without a ceiling; the schemas already restrict what LEAVES the reader,
# but the caches themselves need bounds too so they cannot become the leak
# path a hostile events file exploits.
MAX_AUDIT_LOG_PER_AGENT = 1024
MAX_FAILURES_TRACKED = 128
MAX_FAILURES_ON_ROW = 32
MAX_PENDING_PERMISSIONS = 64

# What the interface must always disclose as beyond the reader's sight.
BOARD_LIMITS: tuple = (
    "model_reasoning",
    "command_arguments",
    "tool_arguments",
    "tool_results",
    "file_edits",
)

# The full set that appears on a per-agent row. ``tool_activity`` is added
# when source 2 is unregistered. ``sealed_stage_content`` and
# ``trace_payload`` are always present because they are always beyond sight.
ROW_BASE_LIMITS: tuple = BOARD_LIMITS + ("sealed_stage_content", "trace_payload")


# -- registration ----------------------------------------------------------


@dataclass(frozen=True)
class Registration:
    """The one authoritative correlation record for an agent's session.

    Instantiated by the supervisor at spawn and handed to
    :meth:`ControlRoom.register`. Nothing in this module produces one on its
    own; that is what "explicit registration only" means.
    """

    run_id: str
    repository: str
    plan: str
    agent: str
    role: str
    workstream: str
    session_id: str
    agent_id: str
    registered_at: str
    events_path: str = ""

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "repository": self.repository,
            "plan": self.plan,
            "agent": self.agent,
            "role": self.role,
            "workstream": self.workstream,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "registered_at": self.registered_at,
            "events_path": self.events_path,
        }


_REGISTRATION_STRING_FIELDS = (
    "run_id",
    "repository",
    "plan",
    "agent",
    "role",
    "workstream",
    "session_id",
    "agent_id",
    "registered_at",
    "events_path",
)


def load_registration(mapping: dict) -> Registration:
    """Coerce a mapping to a :class:`Registration`.

    Any missing key defaults to the empty string. Non-string values are
    refused with :class:`ValueError`; a registration whose fields are not
    strings is the supervisor's bug, not something to fix up silently.
    """
    if not isinstance(mapping, dict):
        raise ValueError("registration must be a mapping")
    values: dict = {}
    for name in _REGISTRATION_STRING_FIELDS:
        value = mapping.get(name, "")
        if not isinstance(value, str):
            raise ValueError(f"registration field {name!r} must be a string")
        values[name] = value
    return Registration(**values)


# -- coverage classification helpers --------------------------------------


def _classify_connection(sample_age_seconds: float) -> str:
    if sample_age_seconds < 0:
        return "unknown"
    if sample_age_seconds <= CONNECTION_STALE_SECONDS:
        return "live"
    if sample_age_seconds <= CONNECTION_DISCONNECTED_SECONDS:
        return "stale"
    return "disconnected"


# -- agent state -----------------------------------------------------------


@dataclass
class _AgentState:
    """In-memory per-agent bookkeeping. Purely disposable."""

    lifecycle: str = "registered"
    lifecycle_at: float = 0.0
    started_at: float = 0.0
    started_basis: str = "unknown"
    last_observed_at: float = 0.0
    current_tool: Optional[str] = None
    current_tool_call: Optional[str] = None
    current_tool_started_at: float = 0.0
    tools_started: int = 0
    tools_completed: int = 0
    tools_failed: int = 0
    subagent_completed_seen: bool = False
    failure_log: list = field(default_factory=list)
    pending_permissions: dict = field(default_factory=dict)
    outcome_at: float = 0.0

    def lifecycle_rank(self) -> int:
        """A total ordering so a terminal state never regresses.

        The intuition: ``unknown < registered < running < {finished,
        failed, cancelled}``. Once terminal, only a later terminal update
        with the SAME kind advances the timestamp -- switching between
        terminal kinds is refused.
        """
        return {
            "unknown": 0,
            "registered": 1,
            "running": 2,
            "cancelled": 3,
            "failed": 3,
            "finished": 3,
        }.get(self.lifecycle, 0)

    def observe_activity(self, at_epoch: float) -> None:
        if at_epoch > self.last_observed_at:
            self.last_observed_at = at_epoch

    def lifecycle_terminal_activity(self) -> str:
        """The activity label that pairs with a terminal lifecycle."""
        if self.lifecycle == "finished":
            return "quiet"
        if self.lifecycle == "failed":
            return "blocked"
        if self.lifecycle == "cancelled":
            return "quiet"
        return "unknown"


# -- the control room ------------------------------------------------------


class ControlRoom:
    """Read-only observability model. Instance-local, disposable.

    Instantiate one per view: a role change or a restart discards the state.
    Nothing here is written to disk except through :meth:`route_feedback`,
    which delegates to :meth:`PlanStore.steer`.
    """

    def __init__(
        self,
        *,
        now: Optional[Callable[[], float]] = None,
        effective_role: str = "",
        watch_home: Optional[Path] = None,
        traces_db: Optional[Path] = None,
    ) -> None:
        self._now = now or time.time
        self._effective_role = effective_role
        self._watch_home = watch_home
        self._traces_db = traces_db
        self._registrations: dict = {}
        self._agent_state: dict = {}
        self._cursors: dict = {}
        self._generation = 0
        # -1.0 forces the FIRST snapshot to collect (any positive
        # ``self._now()`` clock will satisfy ``current - _last_sample >=
        # SAMPLE_INTERVAL_SECONDS``). This keeps the throttle honest for
        # subsequent calls without preventing a cold-start read.
        self._last_sample: float = -1.0e12
        self._cached_watch_rows: list = []
        self._cached_gaps: dict = {}
        self._last_healthy_sample: dict = {}
        self._audit_log_store: dict = {}

    # -- registration -----------------------------------------------------

    def register(self, registration: Registration) -> None:
        """Add or replace a registration.

        Refuses to grow past :data:`grogu_agentevents.MAX_REGISTERED_SOURCES`
        distinct source paths, so an accident cannot fan a reader out
        indefinitely.
        """
        if not isinstance(registration, Registration):
            raise TypeError("register expects a Registration instance")
        events_paths = {
            r.events_path
            for r in self._registrations.values()
            if r.events_path
        }
        if (
            registration.events_path
            and registration.events_path not in events_paths
            and len(events_paths) >= grogu_agentevents.MAX_REGISTERED_SOURCES
        ):
            raise ValueError(
                "reader has too many registered event sources"
            )
        self._registrations[registration.run_id] = registration

    def registrations(self) -> list:
        """A stable list of the current registrations."""
        return [
            self._registrations[key].to_dict()
            for key in sorted(self._registrations)
        ]

    # -- board assembly ---------------------------------------------------

    def snapshot(
        self,
        *,
        plan_summaries: Optional[dict] = None,
        window_minutes: int = grogu_watch.DEFAULT_WINDOW_MINUTES,
    ) -> dict:
        """Return one board :class:`dict` matching ``boardSnapshot`` in the schema.

        The interface polls this. It is deterministic given the same inputs
        and same wall clock, and it never causes a write to disk. Sampling
        is throttled by :data:`SAMPLE_INTERVAL_SECONDS`: consecutive calls
        within that budget reuse the last collected inputs so a burst of
        polls does not translate into a burst of source reads.
        """
        self._generation += 1
        current = self._now()
        do_sample = current - self._last_sample >= SAMPLE_INTERVAL_SECONDS
        if do_sample:
            self._last_sample = current
            self._cached_watch_rows = grogu_watch.sessions(
                window_minutes=window_minutes,
                home=self._watch_home,
            )
            self._cached_sample_at = current
        watch_rows = self._cached_watch_rows or []
        # A watch row is keyed to a registration by ``(plan, agent)``. The
        # plan+agent tuple is the identity the supervisor writes into
        # source 1 records (via ``GROGU_AGENT`` and ``GROGU_PLAN``), and it
        # is what the registration record also names, so this is a
        # registered join and not an inference.
        watch_by_agent: dict = {}
        for row in watch_rows:
            plan_value = row.get("plan", "") or ""
            agent_value = row.get("agent", "") or ""
            key = (plan_value, agent_value)
            watch_by_agent.setdefault(key, row)
        agent_rows: list = []
        uncorrelated: list = []
        all_gaps: list = []
        source2_present = False
        source2_available = False
        for registration in sorted(
            self._registrations.values(),
            key=lambda registration: registration.run_id,
        ):
            state = self._ensure_state(registration.agent_key)
            if do_sample:
                gaps = self._ingest_source_two(registration, state, current)
                self._cached_gaps[registration.run_id] = gaps
                if registration.events_path and not any(
                    gap.kind == grogu_agentevents.GAP_SOURCE_READ_ERROR
                    for gap in gaps
                ):
                    self._last_healthy_sample[registration.run_id] = current
            gaps = self._cached_gaps.get(registration.run_id, [])
            all_gaps.extend(gaps)
            if registration.events_path:
                source2_present = True
                if state.tools_started or state.subagent_completed_seen:
                    source2_available = True
            watch_key = (registration.plan or "", registration.agent or "")
            watch_row = watch_by_agent.pop(watch_key, None)
            self._merge_watch(state, watch_row)
            agent_rows.append(
                self._agent_row(registration, state, watch_row, current)
            )
        # Anything from source 1 that had no registration is uncorrelated.
        for key, row in sorted(watch_by_agent.items(), key=lambda item: str(item[0])):
            uncorrelated.append(self._uncorrelated_row(row, current))
        if source2_present:
            source2_coverage = "complete" if source2_available else "partial"
        else:
            source2_coverage = "unavailable"
        source1_coverage = "complete" if watch_rows or self._registrations else "unavailable"
        traces_coverage = self._traces_coverage()
        limits = list(BOARD_LIMITS)
        if source2_coverage == "unavailable":
            limits.append("tool_activity")
        limits.extend(["sealed_stage_content", "trace_payload"])
        # Deduplicate while preserving order.
        seen: set = set()
        deduped_limits: list = []
        for value in limits:
            if value not in seen:
                seen.add(value)
                deduped_limits.append(value)
        summaries = plan_summaries or {}
        waiting = grogu_watch.waiting_on_you(
            {
                "plans": summaries,
                "skill_proposals": [],
                "skill_store_error": "",
            }
        )
        fresh_as_of = self._fresh_as_of(current)
        return {
            "schema_version": 1,
            "fresh_as_of": fresh_as_of,
            "generation": self._generation,
            "limits": deduped_limits,
            "coverage": {
                "source1": source1_coverage,
                "source2": source2_coverage,
                "traces": traces_coverage,
            },
            "agents": agent_rows,
            "uncorrelated": uncorrelated,
            "waiting_on_you": waiting,
        }

    # -- drill-in ---------------------------------------------------------

    def drill_in(
        self,
        registration: Registration,
        *,
        max_events: int = 200,
    ) -> dict:
        """Return the drill-in view for one registered agent.

        The activity items match the ``auditEvent`` schema in
        ``schemas/plan-control-room.schema.json``: ``kind``, ``at``, and the
        allowlisted enumerated fields. Anything a :class:`NormalisedEvent`
        carries outside the ``auditEvent`` allowlist is deliberately dropped
        by the translation below, so the drill-in never emits a shape wider
        than the schema.
        """
        state = self._ensure_state(registration.agent_key)
        current = self._now()
        gaps = self._ingest_source_two(registration, state, current)
        activity = self._audit_events_for(registration.agent_key)[-max_events:]
        objects = self._authorized_objects(registration)
        limits = list(ROW_BASE_LIMITS)
        if not registration.events_path:
            limits.append("tool_activity")
        blockers = self._blockers(registration, state, current)
        return {
            "schema_version": 1,
            "agent_key": registration.agent_key,
            "activity": [_audit_event_from_normalised(event) for event in activity],
            "evidence": objects,
            "gaps": [_control_room_gap_from_agentevents(gap) for gap in gaps],
            "limits": self._dedupe(limits),
            "blockers": blockers,
        }

    # -- feedback routing -------------------------------------------------

    def route_feedback(
        self,
        registration: Registration,
        *,
        text: str,
        binding: bool = False,
        plan_store,
        gates_map: Optional[dict] = None,
    ) -> dict:
        """Send feedback through :meth:`PlanStore.steer` and return a receipt.

        ``plan_store`` is an instance the caller opened; this module refuses
        to open its own to keep the write path bounded to one place.

        ``gates_map`` maps a role to the gates a binding note closes. When
        omitted, the default is that a binding note closes the design,
        implement, test and evaluate gates for the addressed role -- the
        existing ``requires_replan`` semantics.
        """
        if plan_store is None:
            raise ValueError("route_feedback requires a plan store")
        text = (text or "").strip()
        if not text:
            raise ValueError("refusing to route empty feedback")
        role = registration.role or "all"
        # steer never accepts the exact note text this receipt carries back;
        # we intentionally never store the note text on the receipt itself.
        note = plan_store.steer(
            text=text,
            plan_id=registration.plan,
            role=role,
            requires_replan=bool(binding),
        )
        if not isinstance(note, dict):
            raise TypeError("steer must return a dict")
        seq = int(note.get("seq", 0))
        gates = list(gates_map.get(role, [])) if gates_map else []
        if binding and not gates:
            gates = ["design", "implement", "test", "evaluate"]
        delivered_to = [f"role:{role}"]
        if registration.agent:
            delivered_to.append(f"agent:{registration.agent}")
        relay_command = ""
        if registration.agent:
            relay_command = (
                f"grogu plan steering --plan {registration.plan} "
                f"--role {role}"
            )
        return {
            "schema_version": 1,
            "seq": seq,
            "at": note.get("at", ""),
            "role": role,
            "agent": registration.agent,
            "plan": registration.plan,
            "binding": bool(binding),
            "gates_closed": gates,
            "delivered_to": delivered_to,
            "acknowledged_by": [],
            "relay_command": relay_command or None,
        }

    def acknowledge_delivery(
        self,
        registration: Registration,
        *,
        plan_store,
    ) -> dict:
        """Return the current delivery/ack summary for the given agent."""
        summary = plan_store.steering(
            role=registration.role or "all",
            plan_id=registration.plan,
        )
        plan_notes = summary.get("plan", []) if isinstance(summary, dict) else []
        delivered = 0
        acknowledged = 0
        unread = 0
        requires_replan = False
        for note in plan_notes:
            if not isinstance(note, dict):
                continue
            delivered += 1
            if note.get("requires_replan"):
                requires_replan = True
        return {
            "delivered": delivered,
            "acknowledged": acknowledged,
            "unread": max(delivered - acknowledged, 0),
            "requires_replan": requires_replan,
        }

    # -- internals: source 1 ---------------------------------------------

    def _agent_key_from_watch(self, row: dict) -> str:
        return row.get("agent") or f"{row.get('role', '')}@{row.get('cwd', '')}"

    def _merge_watch(self, state: _AgentState, row: Optional[dict]) -> None:
        if row is None:
            return
        last = float(row.get("last") or 0)
        first = float(row.get("first") or 0)
        state.observe_activity(last)
        if state.started_at == 0.0 or state.started_basis == "unknown":
            if state.started_basis != "lifecycle_start":
                state.started_at = first
                state.started_basis = "first_observed"
        if state.lifecycle == "unknown":
            state.lifecycle = "running"

    # -- internals: source 2 ---------------------------------------------

    def _ingest_source_two(
        self,
        registration: Registration,
        state: _AgentState,
        current: float,
    ) -> list:
        gaps: list = []
        if not registration.events_path:
            gaps.append(grogu_agentevents.unregistered_gap())
            return gaps
        path = Path(registration.events_path)
        cursor = self._cursors.get(registration.run_id)
        result = grogu_agentevents.read_source(path, cursor=cursor)
        self._cursors[registration.run_id] = result.cursor
        gaps.extend(result.gaps)
        audit_events = self._audit_events_for(registration.agent_key)
        for event in result.events:
            if (
                registration.agent_id
                and event.agent_id
                and event.agent_id != registration.agent_id
            ):
                # A different subagent's event -- registered mapping wins,
                # so this event does not belong to this agent's row.
                continue
            self._apply_event(state, event)
            audit_events.append(event)
        # Cap the audit log per agent; long-running dashboards would
        # otherwise accumulate every observed event for the life of the
        # ControlRoom. The drill-in already slices to ``max_events``; this
        # bound ensures the underlying store stays finite.
        if len(audit_events) > MAX_AUDIT_LOG_PER_AGENT:
            del audit_events[: len(audit_events) - MAX_AUDIT_LOG_PER_AGENT]
        return gaps

    def _audit_events_for(self, agent_key: str) -> list:
        return self._audit_log_store.setdefault(agent_key, [])

    def _apply_event(self, state: _AgentState, event: grogu_agentevents.NormalisedEvent) -> None:
        at_epoch = _iso_to_epoch(event.at)
        if at_epoch > state.last_observed_at:
            state.last_observed_at = at_epoch
        if event.phase == "subagent_started":
            if state.lifecycle_rank() < 2:
                state.lifecycle = "running"
                state.lifecycle_at = at_epoch
            if state.started_basis != "lifecycle_start":
                state.started_at = at_epoch
                state.started_basis = "lifecycle_start"
        elif event.phase == "subagent_completed":
            # Only advance to finished if we are not already terminal with a
            # stronger kind.
            state.subagent_completed_seen = True
            if state.lifecycle not in ("failed", "cancelled"):
                state.lifecycle = "finished"
                state.lifecycle_at = at_epoch
            state.outcome_at = max(state.outcome_at, at_epoch)
            state.current_tool = None
            state.current_tool_call = None
        elif event.phase == "tool_started":
            state.tools_started += 1
            state.current_tool = event.tool_name or None
            state.current_tool_call = event.tool_call_id or None
            state.current_tool_started_at = at_epoch
            if state.lifecycle_rank() < 2:
                state.lifecycle = "running"
                state.lifecycle_at = at_epoch
        elif event.phase == "tool_completed":
            state.tools_completed += 1
            if event.success is False:
                state.tools_failed += 1
                state.failure_log.append(
                    {
                        "at_epoch": at_epoch,
                        "code": event.error_code or "unknown",
                        "tool_name": event.tool_name or "",
                    }
                )
                if len(state.failure_log) > MAX_FAILURES_TRACKED:
                    state.failure_log = state.failure_log[-MAX_FAILURES_TRACKED:]
            if state.current_tool_call == event.tool_call_id:
                state.current_tool = None
                state.current_tool_call = None
            state.outcome_at = max(state.outcome_at, at_epoch)
        elif event.phase == "permission_requested":
            if event.request_id:
                # Bounded: an events log with an unbounded stream of
                # requests without matching completions cannot make the
                # blocker list grow past the cap. When at the cap, the
                # oldest pending request is evicted first (FIFO by
                # insertion order in Python's dict).
                if (
                    len(state.pending_permissions) >= MAX_PENDING_PERMISSIONS
                    and event.request_id not in state.pending_permissions
                ):
                    try:
                        oldest = next(iter(state.pending_permissions))
                        state.pending_permissions.pop(oldest, None)
                    except StopIteration:  # pragma: no cover - unreachable
                        pass
                state.pending_permissions[event.request_id] = at_epoch
        elif event.phase == "permission_completed":
            state.pending_permissions.pop(event.request_id, None)

    # -- internals: row building ------------------------------------------

    def _ensure_state(self, agent_key: str) -> _AgentState:
        state = self._agent_state.get(agent_key)
        if state is None:
            state = _AgentState()
            self._agent_state[agent_key] = state
        return state

    def _agent_row(
        self,
        registration: Registration,
        state: _AgentState,
        watch_row: Optional[dict],
        current: float,
    ) -> dict:
        last_observed_epoch = max(
            state.last_observed_at,
            float(watch_row.get("last") or 0) if watch_row else 0.0,
        )
        # Connection is the observer's health, NOT the agent's activity.
        # Base it on the last time the collector succeeded on this source
        # rather than on the age of the newest event; a silent-but-alive
        # agent with a healthy collector is ``live``, not ``disconnected``.
        connection = self._classify_source_health(registration, current)
        lifecycle = state.lifecycle if state.lifecycle != "unknown" else (
            "running" if watch_row else "registered"
        )
        activity, evidence = self._classify_activity(state, connection, current)
        blockers = self._blockers(registration, state, current)
        if blockers:
            activity = "blocked"
            evidence = []
        limits = list(ROW_BASE_LIMITS)
        if not registration.events_path:
            limits.append("tool_activity")
        current_action = None
        if state.current_tool:
            current_action = {
                "tool_name": state.current_tool,
                "tool_call_id": state.current_tool_call or "",
                "phase": "started",
                "at": _epoch_to_iso(state.current_tool_started_at),
                "success": None,
                "error_code": None,
                "duration_ms": None,
            }
        elif state.tools_completed > 0:
            current_action = None
        started_iso = _epoch_to_iso(state.started_at)
        last_observed_iso = _epoch_to_iso(last_observed_epoch)
        elapsed_ms = None
        elapsed_basis = state.started_basis or "unknown"
        if state.started_at > 0 and last_observed_epoch > 0:
            elapsed_ms = int(max(0.0, last_observed_epoch - state.started_at) * 1000)
        last_command_raw = grogu_agentevents._safe_name(
            watch_row.get("last_command")
        ) if watch_row else ""
        grogu_commands: dict = {
            "calls": int(watch_row.get("calls", 0)) if watch_row else 0,
            "failures": int(watch_row.get("failures", 0)) if watch_row else 0,
        }
        # The schema's safeName pattern requires 1..64 characters; ``""``
        # would violate it. Emit the key only when there is a real name to
        # emit; the schema marks it optional.
        if last_command_raw:
            grogu_commands["last_command"] = last_command_raw
        failures: list = []
        for entry in state.failure_log[-MAX_FAILURES_ON_ROW:]:
            record: dict = {
                "at": _epoch_to_iso(entry["at_epoch"]),
                "code": entry["code"],
            }
            tool_name = entry.get("tool_name") or ""
            if tool_name and grogu_agentevents.SAFE_NAME_PATTERN.match(tool_name):
                record["tool_name"] = tool_name
            failures.append(record)
        row = {
            "agent_key": registration.agent_key,
            "agent": registration.agent,
            "run_id": registration.run_id,
            "role": registration.role,
            "roles": [registration.role] if registration.role else [],
            "workstream": registration.workstream,
            "plan": registration.plan,
            "repository": registration.repository,
            "revision": {
                "current": "",
                "relation": "unknown",
            },
            "lifecycle": lifecycle,
            "activity": activity,
            "connection": connection,
            "started_at": started_iso,
            "last_observed_at": last_observed_iso,
            "elapsed_ms": elapsed_ms,
            "elapsed_basis": elapsed_basis,
            "current_action": current_action,
            "tools": {
                "started": state.tools_started,
                "completed": state.tools_completed,
                "failed": state.tools_failed,
                "inflight": max(0, state.tools_started - state.tools_completed),
                "complete": state.subagent_completed_seen,
            },
            "grogu_commands": grogu_commands,
            "failures": failures,
            "blockers": blockers,
            "steering": {
                "unread": 0,
                "delivered": 0,
                "acknowledged": 0,
                "requires_replan": False,
            },
            "possibly_stuck_evidence": evidence,
            "coverage": self._row_coverage(registration, state, watch_row),
            "limits": self._dedupe(limits),
            "objects": self._authorized_objects(registration),
            "basis": "observed",
        }
        return row

    def _uncorrelated_row(self, watch_row: dict, current: float) -> dict:
        last = float(watch_row.get("last") or 0)
        connection = _classify_connection((current - last) if last else -1)
        last_command_raw = grogu_agentevents._safe_name(
            watch_row.get("last_command")
        )
        grogu_commands: dict = {
            "calls": int(watch_row.get("calls", 0)),
            "failures": int(watch_row.get("failures", 0)),
        }
        if last_command_raw:
            grogu_commands["last_command"] = last_command_raw
        return {
            "agent_key": self._agent_key_from_watch(watch_row),
            "agent": watch_row.get("agent", "") or "",
            "run_id": "",
            "role": "",  # never imported from an unregistered source
            "roles": [],
            "workstream": "",
            "plan": watch_row.get("plan", "") or "",
            "repository": watch_row.get("repository", "") or "",
            "revision": {"current": "", "relation": "unknown"},
            "lifecycle": "unknown",
            "activity": "unknown",
            "connection": connection,
            "started_at": _epoch_to_iso(float(watch_row.get("first") or 0)),
            "last_observed_at": _epoch_to_iso(last),
            "elapsed_ms": None,
            "elapsed_basis": "unknown",
            "current_action": None,
            "tools": {
                "started": 0,
                "completed": 0,
                "failed": 0,
                "inflight": 0,
                "complete": False,
            },
            "grogu_commands": grogu_commands,
            "failures": [],
            "blockers": [],
            "steering": {
                "unread": 0,
                "delivered": 0,
                "acknowledged": 0,
                "requires_replan": False,
            },
            "possibly_stuck_evidence": [],
            "coverage": {
                "lifecycle": "partial",
                "activity": "partial",
                "tools": "unavailable",
                "outcomes": "unavailable",
                "commands": "complete",
            },
            "limits": list(ROW_BASE_LIMITS) + ["tool_activity"],
            "objects": [],
            "basis": "observed",
        }

    # -- internals: activity classification ------------------------------

    def _classify_source_health(
        self,
        registration: Registration,
        current: float,
    ) -> str:
        """Return the observer connection state for this registration.

        This is what the plan calls "how healthy the observer is" and is
        deliberately independent of the agent's own activity. When the
        source has never been successfully sampled, the connection is
        ``unknown``. When the last healthy sample was within the stale
        budget, it is ``live``; between stale and disconnected budgets, it
        is ``stale``; beyond, it is ``disconnected``.
        """
        if not registration.events_path:
            # A source-1-only registration has no source 2 to be healthy or
            # not; the plan classifies "no source" as ``unknown``.
            return "unknown"
        last = self._last_healthy_sample.get(registration.run_id, 0.0)
        if not last:
            return "unknown"
        age = current - last
        return _classify_connection(age)

    def _classify_activity(
        self,
        state: _AgentState,
        connection: str,
        current: float,
    ) -> tuple:
        """Return ``(activity, evidence)`` deterministically.

        Only fixed thresholds are consulted; nothing is model-inferred.

        The plan requires ``possibly_stuck`` only fire with a healthy
        observer (``live`` or ``stale``); a disconnected observer means the
        reader cannot tell a stuck agent from a collection outage, so the
        label is withheld. The ``active``/``quiet`` split is decided by the
        idle threshold either way -- silence from a healthy observer means
        the agent has been quiet; silence from a disconnected observer
        means the observer is disconnected AND the last-observed activity
        was long enough ago, which is still ``quiet`` on the activity axis.
        """
        last = state.last_observed_at
        idle = current - last if last else float("inf")
        if state.lifecycle in ("finished", "failed", "cancelled"):
            return (state.lifecycle_terminal_activity(), [])
        if last == 0 and idle > CONNECTION_DISCONNECTED_SECONDS:
            return ("unknown", [])
        evidence: list = []
        stuck = False
        # The stuck heuristics require a healthy observer. A disconnected
        # observer cannot tell a stuck agent from a collection outage; a
        # source-1-only registration (connection ``unknown``) has no source
        # 2 heuristic to test, so ``possibly_stuck`` never fires there
        # either.
        if connection in ("live", "stale"):
            # (a) tool exceeded its observed deadline (if the tool ever
            # declared one). We do not know deadlines from allowlisted
            # fields; skip this branch by default -- the trigger fires
            # only when a deadline was explicitly supplied via an
            # ``expected_duration_ms`` extension on subagent state.
            # (b) no outcome after activity
            if state.tools_completed >= NO_OUTCOME_MIN_TOOL_EVENTS:
                since_outcome = (
                    current - state.outcome_at if state.outcome_at else float("inf")
                )
                if since_outcome >= NO_OUTCOME_AFTER_ACTIVITY_SECONDS:
                    stuck = True
                    evidence.append("no_outcome_after_activity")
            # (c) repeated failures with the same code
            if len(state.failure_log) >= REPEATED_FAILURES_THRESHOLD:
                recent = [
                    entry
                    for entry in state.failure_log[-REPEATED_FAILURES_THRESHOLD * 4:]
                    if current - entry["at_epoch"] <= REPEATED_FAILURES_WINDOW_SECONDS
                ]
                if len(recent) >= REPEATED_FAILURES_THRESHOLD:
                    counts: dict = {}
                    for entry in recent:
                        counts[entry["code"]] = counts.get(entry["code"], 0) + 1
                    if any(
                        value >= REPEATED_FAILURES_THRESHOLD for value in counts.values()
                    ):
                        stuck = True
                        evidence.append("repeated_failures")
        if stuck:
            return ("possibly_stuck", evidence)
        if idle <= grogu_watch.IDLE_AFTER_SECONDS:
            return ("active", [])
        return ("quiet", [])

    def _blockers(
        self,
        registration: Registration,
        state: _AgentState,
        current: float,
    ) -> list:
        blockers: list = []
        for request_id, at in sorted(state.pending_permissions.items()):
            blockers.append(
                {
                    "kind": "permission_pending",
                    "at": _epoch_to_iso(at),
                    "reference": request_id,
                }
            )
        return blockers

    # -- internals: coverage --------------------------------------------

    def _row_coverage(
        self,
        registration: Registration,
        state: _AgentState,
        watch_row: Optional[dict],
    ) -> dict:
        lifecycle = "complete" if state.started_basis == "lifecycle_start" else (
            "partial" if watch_row else "unavailable"
        )
        activity = "complete" if watch_row or state.tools_started else "unavailable"
        tools = "complete" if registration.events_path else "unavailable"
        outcomes = (
            "complete"
            if state.subagent_completed_seen
            else ("partial" if registration.events_path else "unavailable")
        )
        commands = "complete" if watch_row else "unavailable"
        return {
            "lifecycle": lifecycle,
            "activity": activity,
            "tools": tools,
            "outcomes": outcomes,
            "commands": commands,
        }

    # -- internals: authorised object refs ------------------------------

    def _authorized_objects(self, registration: Registration) -> list:
        return []

    # -- internals: traces ---------------------------------------------

    def _traces_coverage(self) -> str:
        if self._traces_db is None:
            return "unavailable"
        if not Path(self._traces_db).exists():
            return "unavailable"
        # Read only scalar columns -- payload_json is not touched.
        try:
            connection = sqlite3.connect(f"file:{self._traces_db}?mode=ro", uri=True)
            try:
                connection.execute("SELECT id FROM traces LIMIT 1").fetchall()
            finally:
                connection.close()
        except sqlite3.Error:
            return "unavailable"
        return "complete"

    # -- internals: freshness -------------------------------------------

    def _fresh_as_of(self, current: float) -> str:
        newest = 0.0
        for state in self._agent_state.values():
            if state.last_observed_at > newest:
                newest = state.last_observed_at
        if newest == 0.0:
            return _epoch_to_iso(current)
        return _epoch_to_iso(newest)

    # -- misc -----------------------------------------------------------

    def _dedupe(self, values: Iterable) -> list:
        seen: set = set()
        out: list = []
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            out.append(value)
        return out


# -- registration convenience --------------------------------------------


def _agent_key_from_registration(registration: Registration) -> str:
    """A stable per-agent key: ``<plan>::<agent>::<run_id>``."""
    parts = [registration.plan or "-", registration.agent or "-", registration.run_id or "-"]
    return "::".join(parts)


Registration.agent_key = property(  # type: ignore[attr-defined]
    _agent_key_from_registration
)


# -- schema translation helpers -----------------------------------------


_PHASE_TO_AUDIT_KIND: dict = {
    "subagent_started": "subagent_started",
    "subagent_completed": "subagent_completed",
    "tool_started": "tool_started",
    "tool_completed": "tool_completed",
    "permission_requested": "permission_requested",
    "permission_completed": "permission_completed",
}


def _audit_event_from_normalised(event) -> dict:
    """Translate a :class:`NormalisedEvent` into the ``auditEvent`` shape.

    The ``auditEvent`` schema in ``plan-control-room.schema.json`` is a
    narrower vocabulary than :class:`NormalisedEvent`: it names ``kind``
    (not ``phase``), ``name`` (not ``tool_name``), and ``reference`` (which
    unifies ``tool_call_id`` and ``request_id``), and it forbids extra
    keys. Anything a :class:`NormalisedEvent` carried outside this
    vocabulary is dropped here rather than through a downstream filter, so
    a call to :meth:`ControlRoom.drill_in` cannot emit a shape wider than
    the schema.
    """
    kind = _PHASE_TO_AUDIT_KIND.get(event.phase)
    if kind is None:
        # An unmapped phase is dropped rather than emitted with a synthetic
        # kind: a hostile events file could otherwise get a new phase past
        # the schema by looking similar to a known one.
        return {"at": event.at or "", "kind": "coverage_gap"}
    audit: dict = {
        "at": event.at or "",
        "kind": kind,
    }
    if event.tool_name and grogu_agentevents.SAFE_NAME_PATTERN.match(event.tool_name):
        audit["name"] = event.tool_name
    if event.tool_call_id:
        audit["reference"] = event.tool_call_id
    elif event.request_id:
        audit["reference"] = event.request_id
    if event.success is not None:
        audit["success"] = event.success
    if event.error_code:
        audit["error_code"] = event.error_code
    if event.duration_ms is not None:
        audit["duration_ms"] = event.duration_ms
    return audit


def _control_room_gap_from_agentevents(gap) -> dict:
    """Translate a ``grogu_agentevents.CoverageGap`` to the control-room shape.

    The events schema calls the field ``kind``; the control-room schema
    calls it ``reason`` (a narrative choice that fits the interface
    better). Both use the same closed enum. This helper is the one place
    the rename happens so a schema change on either side is a single-file
    change.
    """
    return {
        "at": gap.at,
        "reason": gap.kind,
        "at_line": gap.at_line,
    }


# -- iso helpers ---------------------------------------------------------


def _iso_to_epoch(value: str) -> float:
    if not value or not isinstance(value, str):
        return 0.0
    try:
        return calendar.timegm(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return 0.0


def _epoch_to_iso(value: float) -> str:
    if not value:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))


__all__ = [
    "CONNECTION_STALE_SECONDS",
    "CONNECTION_DISCONNECTED_SECONDS",
    "TOOL_DEADLINE_GRACE_SECONDS",
    "NO_OUTCOME_AFTER_ACTIVITY_SECONDS",
    "NO_OUTCOME_MIN_TOOL_EVENTS",
    "REPEATED_FAILURES_WINDOW_SECONDS",
    "REPEATED_FAILURES_THRESHOLD",
    "SAMPLE_INTERVAL_SECONDS",
    "BOARD_LIMITS",
    "ROW_BASE_LIMITS",
    "Registration",
    "load_registration",
    "ControlRoom",
]
