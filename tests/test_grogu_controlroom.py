"""Tests for the planning control room -- the observability read model.

The three big invariants under test:

  * No forbidden field appears in any returned structure, key, exception,
    cache, or serialisation. The adversarial ``events.jsonl`` fixture under
    ``tests/fixtures/controlroom/`` deliberately contains every forbidden
    category (reasoningText, reasoningOpaque, tool arguments/results,
    assistant bodies, task descriptions, permission intention text, secrets).
    Every returned board and drill-in structure is walked with the forbidden
    token list.

  * The three axes -- connection, lifecycle, activity -- are kept
    orthogonal. A terminal lifecycle stays terminal even when the collector
    later disconnects; silence never proves an agent finished; a healthy
    observer is required before ``possibly_stuck`` can fire; a stale
    observer downgrades the connection but not the lifecycle.

  * Correlation is by explicit registration only. A session with no
    registration is either invisible (source 2) or lands in ``uncorrelated``
    (source 1) with no imported role and no imported description.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import _sandbox  # noqa: E402,F401

import grogu_agentevents as ae  # noqa: E402
import grogu_controlroom as cr  # noqa: E402
import grogu_watch  # noqa: E402


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "controlroom"


FORBIDDEN_TOKENS = [
    "FORBIDDEN_PROMPT_TOP_SECRET_QUERY",
    "FORBIDDEN_USER_MESSAGE_ABOUT_PII",
    "FORBIDDEN_AGENT_NAME_SECRET",
    "FORBIDDEN_TASK_DESCRIPTION_DO_NOT_LEAK",
    "FORBIDDEN_DISPLAY_NAME",
    "FORBIDDEN_SHELL_COMMAND",
    "FORBIDDEN_ARGUMENT_DESCRIPTION",
    "FORBIDDEN_ASSISTANT_MESSAGE_BODY",
    "FORBIDDEN_ENCRYPTED_CONTENT_HANDLE",
    "FORBIDDEN_REASONING_TEXT_ABOUT_HOW_TO_EXFILTRATE",
    "FORBIDDEN_OPAQUE_REASONING_BLOB",
    "FORBIDDEN_INTENTION_SUMMARY_PII_HANDLING",
    "FORBIDDEN_TOOL_RESULT_CONTAINING_CREDENTIALS",
    "FORBIDDEN_SECRET_VALUE",
    "FORBIDDEN_TELEMETRY_FIELD",
    "FORBIDDEN_PERMISSION_INTENTION_ABOUT_DEPLOYING_TO_PROD",
    "FORBIDDEN_PROMPT_REQUEST_TEXT",
    "FORBIDDEN_TOOL_OUTPUT_WITH_RATE_LIMIT",
    "FORBIDDEN_DENIAL_REASON_TEXT",
    "FORBIDDEN_SESSION_INFO_LEAKING_HOSTNAME_AND_USER",
    "FORBIDDEN_INTERNAL_SYSTEM",
    "FORBIDDEN_FUTURE_FIELD_TEXT",
    "FORBIDDEN_FILE_CONTENT",
    "FORBIDDEN_GARBAGE",
    "FORBIDDEN_UNSAFE_NAME_TOOL",
    "FORBIDDEN_ARG",
    "FORBIDDEN_ERROR_MESSAGE_LEAKING_PATH",
    "aws-secret-key",
]


def _iter_strings(value: object):
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _iter_strings(key)
            yield from _iter_strings(child)
        return
    if isinstance(value, (list, tuple, set)):
        for child in value:
            yield from _iter_strings(child)
        return


def assert_no_forbidden(test: unittest.TestCase, value: object, *, context: str) -> None:
    for string in _iter_strings(value):
        for token in FORBIDDEN_TOKENS:
            test.assertNotIn(
                token,
                string,
                f"{context}: forbidden token {token!r} appeared: {string!r}",
            )
    forms = []
    try:
        forms.append(repr(value))
    except Exception:
        pass
    try:
        forms.append(json.dumps(value, default=str))
    except Exception:
        pass
    for form in forms:
        for token in FORBIDDEN_TOKENS:
            test.assertNotIn(
                token,
                form,
                f"{context}: forbidden token {token!r} appeared in serialisation: {form!r}",
            )


def _make_registration(session_id: str, **overrides) -> cr.Registration:
    events_path = str(
        FIXTURE_ROOT / "session-state" / session_id / "events.jsonl"
    )
    defaults = {
        "run_id": overrides.get("run_id", f"run-{session_id}"),
        "repository": "/tmp/repo-a",
        "plan": "p-20260905-aab017",
        "agent": "ingest",
        "role": "engineer",
        "workstream": "controlroom",
        "session_id": session_id,
        "agent_id": "agent-ada",
        "registered_at": "2026-09-05T08:59:59Z",
        "events_path": events_path,
    }
    defaults.update(overrides)
    return cr.Registration(**defaults)


class _FakeClock:
    def __init__(self, initial: float = 1_800_000_000.0) -> None:
        self.value = initial

    def advance(self, seconds: float) -> None:
        self.value += seconds

    def __call__(self) -> float:
        return self.value


class SnapshotShape(unittest.TestCase):

    def setUp(self):
        self._watch_home = tempfile.TemporaryDirectory()
        self.addCleanup(self._watch_home.cleanup)

    def test_a_snapshot_has_only_the_documented_top_level_keys(self):
        room = cr.ControlRoom(now=_FakeClock(), watch_home=Path(self._watch_home.name))
        snapshot = room.snapshot()
        self.assertEqual(
            set(snapshot),
            {
                "schema_version",
                "fresh_as_of",
                "sampled_at",
                "observed_through",
                "generation",
                "limits",
                "coverage",
                "source_coverage",
                "agents",
                "uncorrelated",
                "waiting_on_you",
                "topology",
                "recent_events",
                "events_status",
            },
        )
        self.assertEqual(snapshot["schema_version"], 1)

    def test_snapshot_uses_only_explicit_parent_run_lineage(self):
        room = cr.ControlRoom(
            now=_FakeClock(), watch_home=Path(self._watch_home.name)
        )
        parent = cr.Registration(
            run_id="run-parent",
            repository="/tmp/repo",
            plan="p-1",
            agent="parent",
            role="architect",
            workstream="planning",
            session_id="private-parent-session",
            agent_id="parent-agent",
            registered_at="2026-09-10T20:00:00Z",
            root_session_id="private-root-session",
        )
        child = cr.Registration(
            run_id="run-child",
            repository="/tmp/repo",
            plan="p-1",
            agent="child",
            role="engineer",
            workstream="backend",
            session_id="private-child-session",
            agent_id="child-agent",
            registered_at="2026-09-10T20:01:00Z",
            parent_run_id="run-parent",
            root_session_id="private-root-session",
        )
        room.register(parent)
        room.register(child)
        snapshot = room.snapshot()
        self.assertEqual(snapshot["topology"]["coverage"], "complete")
        self.assertEqual(len(snapshot["topology"]["nodes"]), 2)
        self.assertEqual(len(snapshot["topology"]["edges"]), 1)
        edge = snapshot["topology"]["edges"][0]
        self.assertEqual(edge["from"], parent.agent_key)
        self.assertEqual(edge["to"], child.agent_key)
        serialized = json.dumps(snapshot)
        self.assertNotIn("private-child-session", serialized)
        self.assertNotIn("private-root-session", serialized)


class PrivacyOverAdversarialFixture(unittest.TestCase):
    """Load the adversarial fixture through the whole board and drill-in."""

    def setUp(self):
        self._watch_home = tempfile.TemporaryDirectory()
        self.addCleanup(self._watch_home.cleanup)

    def _make_room(self, **kwargs):
        kwargs.setdefault("watch_home", Path(self._watch_home.name))
        return cr.ControlRoom(**kwargs)

    def test_the_board_never_carries_a_forbidden_token(self):
        room = self._make_room(now=_FakeClock())
        room.register(_make_registration("adv-session-001"))
        snapshot = room.snapshot()
        assert_no_forbidden(self, snapshot, context="board snapshot")

    def test_the_drill_in_never_carries_a_forbidden_token(self):
        room = self._make_room(now=_FakeClock())
        registration = _make_registration("adv-session-001")
        room.register(registration)
        room.snapshot()  # ingest source 2
        drill = room.drill_in(registration)
        assert_no_forbidden(self, drill, context="drill-in")

    def test_authored_summary_channel_stays_empty(self):
        room = self._make_room(now=_FakeClock())
        room.register(_make_registration("adv-session-001"))
        snapshot = room.snapshot()
        for row in snapshot["agents"]:
            self.assertEqual(row["basis"], "observed")


class ThreeAxesOrthogonal(unittest.TestCase):
    """connection, lifecycle, activity are orthogonal and must stay so."""

    def setUp(self):
        self._watch_home = tempfile.TemporaryDirectory()
        self.addCleanup(self._watch_home.cleanup)

    def _make_room(self, **kwargs):
        kwargs.setdefault("watch_home", Path(self._watch_home.name))
        return cr.ControlRoom(**kwargs)

    def test_a_terminal_lifecycle_survives_a_later_disconnect(self):
        clock = _FakeClock(initial=_epoch("2026-09-05T09:01:30Z"))
        room = self._make_room(now=clock)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "session-state" / "term-01"
            root.mkdir(parents=True)
            path = root / "events.jsonl"
            # A subagent.completed event lands the agent in a terminal
            # lifecycle. The file will then be removed to simulate the
            # collector losing coverage.
            path.write_text(
                (
                    '{"type":"subagent.started","id":"e-1","timestamp":"2026-09-05T09:00:01Z","agentId":"agent-ada","data":{"toolCallId":"tc","agentType":"general-purpose"}}\n'
                    '{"type":"subagent.completed","id":"e-2","timestamp":"2026-09-05T09:00:30Z","agentId":"agent-ada","data":{"toolCallId":"tc","totalTokens":10,"totalToolCalls":1}}\n'
                ),
                encoding="utf8",
            )
            registration = cr.Registration(
                run_id="run-term",
                repository="/tmp/repo",
                plan="p-term",
                agent="term",
                role="engineer",
                workstream="core",
                session_id="term-01",
                agent_id="agent-ada",
                registered_at="2026-09-05T09:00:00Z",
                events_path=str(path),
            )
            room.register(registration)
            snapshot = room.snapshot()
            row = _find_row(snapshot, registration.agent_key)
            self.assertEqual(row["lifecycle"], "finished")
            # Delete the file to simulate a lost source. Advance the clock
            # past both the sample throttle and the disconnected budget
            # BEFORE calling snapshot again, so the fresh sample fails.
            path.unlink()
            clock.advance(cr.CONNECTION_DISCONNECTED_SECONDS + 60)
            snapshot = room.snapshot()
            row = _find_row(snapshot, registration.agent_key)
            # The three axes stay orthogonal: lifecycle is unchanged (a
            # terminal state cannot regress), but the observer is now
            # disconnected because a fresh sample failed.
            self.assertEqual(row["lifecycle"], "finished")
            self.assertEqual(row["connection"], "disconnected")

    def test_silence_never_proves_finished(self):
        clock = _FakeClock(initial=_epoch("2026-09-05T09:00:15Z"))
        room = self._make_room(now=clock)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "session-state" / "tick-01"
            root.mkdir(parents=True)
            path = root / "events.jsonl"
            path.write_text(
                (
                    '{"type":"subagent.started","id":"e-1","timestamp":"2026-09-05T09:00:01Z","agentId":"agent-ada","data":{"toolCallId":"tc","agentType":"general-purpose"}}\n'
                    '{"type":"tool.execution_start","id":"e-2","timestamp":"2026-09-05T09:00:05Z","agentId":"agent-ada","data":{"toolCallId":"tc-101","toolName":"bash"}}\n'
                ),
                encoding="utf8",
            )
            registration2 = cr.Registration(
                run_id="run-tick2", repository="/tmp/repo", plan="p-tick",
                agent="tick", role="engineer", workstream="core",
                session_id="tick-01", agent_id="agent-ada",
                registered_at="2026-09-05T09:00:00Z",
                events_path=str(path),
            )
            room.register(registration2)
            snapshot = room.snapshot()
            row = _find_row(snapshot, registration2.agent_key)
            # A tool is inflight; nothing terminal. Even when the collector
            # goes stale (source removed), lifecycle must not flip to finished.
            self.assertNotIn(row["lifecycle"], ("finished", "failed", "cancelled"))
            path.unlink()
            clock.advance(cr.CONNECTION_DISCONNECTED_SECONDS + 100)
            snapshot = room.snapshot()
            row = _find_row(snapshot, registration2.agent_key)
            self.assertNotIn(row["lifecycle"], ("finished", "failed", "cancelled"))
            self.assertEqual(row["connection"], "disconnected")

    def test_a_healthy_observer_is_required_for_possibly_stuck(self):
        # A stale observer with no fresh sample must not label an agent
        # possibly_stuck: the reader cannot tell that from silence.
        clock = _FakeClock()
        room = self._make_room(now=clock)
        registration = cr.Registration(
            run_id="run-quiet", repository="/tmp/repo", plan="p-1",
            agent="quiet", role="engineer", workstream="w",
            session_id="", agent_id="", registered_at="",
        )
        room.register(registration)
        snapshot = room.snapshot()
        row = _find_row(snapshot, registration.agent_key)
        # No events observed at all; activity is unknown, not possibly_stuck.
        self.assertNotEqual(row["activity"], "possibly_stuck")


class PossiblyStuckHeuristic(unittest.TestCase):

    def setUp(self):
        self._watch_home = tempfile.TemporaryDirectory()
        self.addCleanup(self._watch_home.cleanup)

    def _make_room(self, **kwargs):
        kwargs.setdefault("watch_home", Path(self._watch_home.name))
        return cr.ControlRoom(**kwargs)

    def test_three_repeated_failures_within_the_window_are_labelled(self):
        clock = _FakeClock()
        room = self._make_room(now=clock)
        # A source-2-backed registration so the observer is HEALTHY.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "session-state" / "fail-1"
            root.mkdir(parents=True)
            path = root / "events.jsonl"
            path.write_text("", encoding="utf8")
            registration = cr.Registration(
                run_id="run-fail", repository="/tmp/repo", plan="p-1",
                agent="fail", role="engineer", workstream="w",
                session_id="fail-1", agent_id="", registered_at="",
                events_path=str(path),
            )
            room.register(registration)
            room.snapshot()  # first sample populates last_healthy_sample
            state = room._ensure_state(registration.agent_key)
            base = clock.value
            state.last_observed_at = base
            room._last_healthy_sample[registration.run_id] = base
            state.failure_log = [
                {"at_epoch": base - 30, "code": "http_429", "tool_name": "gh api"},
                {"at_epoch": base - 20, "code": "http_429", "tool_name": "gh api"},
                {"at_epoch": base - 10, "code": "http_429", "tool_name": "gh api"},
            ]
            state.lifecycle = "running"
            snapshot = room.snapshot()
            row = _find_row(snapshot, registration.agent_key)
            self.assertEqual(row["activity"], "possibly_stuck")
            self.assertIn("repeated_failures", row["possibly_stuck_evidence"])

    def test_a_disconnected_observer_never_labels_an_agent_possibly_stuck(self):
        # This is the invariant from plan-show-controlroom.txt:1318:
        # ``possibly_stuck`` requires a HEALTHY observer. A disconnected
        # observer cannot tell a stuck agent from a collection outage.
        clock = _FakeClock()
        room = self._make_room(now=clock)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "session-state" / "s-1"
            root.mkdir(parents=True)
            path = root / "events.jsonl"
            path.write_text("", encoding="utf8")
            registration = cr.Registration(
                run_id="run-x", repository="/tmp/repo", plan="p-1",
                agent="quiet", role="engineer", workstream="w",
                session_id="s-1", agent_id="", registered_at="",
                events_path=str(path),
            )
            room.register(registration)
            room.snapshot()
            state = room._ensure_state(registration.agent_key)
            base = clock.value
            state.last_observed_at = base
            room._last_healthy_sample[registration.run_id] = base
            state.failure_log = [
                {"at_epoch": base - 30, "code": "http_429", "tool_name": "gh api"},
                {"at_epoch": base - 20, "code": "http_429", "tool_name": "gh api"},
                {"at_epoch": base - 10, "code": "http_429", "tool_name": "gh api"},
            ]
            state.lifecycle = "running"
            # Remove the source and advance past the disconnected budget so
            # the collector goes disconnected.
            path.unlink()
            clock.advance(cr.CONNECTION_DISCONNECTED_SECONDS + 120)
            snapshot = room.snapshot()
            row = _find_row(snapshot, registration.agent_key)
            self.assertEqual(row["connection"], "disconnected")
            self.assertNotEqual(row["activity"], "possibly_stuck")
            self.assertEqual(row["possibly_stuck_evidence"], [])

    def test_a_stale_observer_is_still_healthy_enough_to_label_stuck(self):
        # The plan gates possibly_stuck on "healthy observer", which the
        # implementation reads as ``live`` or ``stale`` -- both are
        # observations close enough to now to distinguish a stuck agent
        # from a collection outage. Only ``disconnected`` is disqualifying.
        clock = _FakeClock()
        room = self._make_room(now=clock)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "session-state" / "s-2"
            root.mkdir(parents=True)
            path = root / "events.jsonl"
            path.write_text("", encoding="utf8")
            registration = cr.Registration(
                run_id="run-stale", repository="/tmp/repo", plan="p-1",
                agent="stale", role="engineer", workstream="w",
                session_id="s-2", agent_id="", registered_at="",
                events_path=str(path),
            )
            room.register(registration)
            room.snapshot()
            state = room._ensure_state(registration.agent_key)
            base = clock.value
            state.last_observed_at = base
            room._last_healthy_sample[registration.run_id] = base
            state.failure_log = [
                {"at_epoch": base - 30, "code": "http_429", "tool_name": "gh api"},
                {"at_epoch": base - 20, "code": "http_429", "tool_name": "gh api"},
                {"at_epoch": base - 10, "code": "http_429", "tool_name": "gh api"},
            ]
            state.lifecycle = "running"
            # Remove the source and advance PAST the stale threshold but
            # BEFORE the disconnected one; a fresh sample fails, so the
            # observer is ``stale``.
            path.unlink()
            clock.advance(cr.CONNECTION_STALE_SECONDS + 2)
            snapshot = room.snapshot()
            row = _find_row(snapshot, registration.agent_key)
            self.assertEqual(row["connection"], "stale")
            self.assertEqual(row["activity"], "possibly_stuck")

    def test_readable_source_without_new_events_keeps_observer_live(self):
        clock = _FakeClock(initial=_epoch("2026-09-05T09:00:02Z"))
        room = self._make_room(now=clock)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "session-state" / "s-idle"
            root.mkdir(parents=True)
            path = root / "events.jsonl"
            path.write_text(
                '{"type":"subagent.started","id":"e-1",'
                '"timestamp":"2026-09-05T09:00:01Z",'
                '"agentId":"agent-ada","data":{"toolCallId":"tc",'
                '"agentType":"general-purpose"}}\n',
                encoding="utf8",
            )
            registration = cr.Registration(
                run_id="run-idle", repository="/tmp/repo", plan="p-1",
                agent="idle", role="engineer", workstream="w",
                session_id="s-idle", agent_id="", registered_at="",
                events_path=str(path),
            )
            room.register(registration)
            self.assertEqual(
                _find_row(room.snapshot(), registration.agent_key)["connection"],
                "live",
            )
            clock.advance(cr.CONNECTION_DISCONNECTED_SECONDS + 1)
            self.assertEqual(
                _find_row(room.snapshot(), registration.agent_key)["connection"],
                "live",
            )

    def test_a_source_one_only_registration_can_still_be_labelled_active(self):
        # A source-1-only registration has no source 2 to make the observer
        # ``unknown`` about, but its grogu activity feed is a healthy
        # observer in its own right. Fresh commands land as ``active``.
        home = Path(self._watch_home.name)
        grogu_watch.record(
            command="plan gate",
            role="engineer",
            agent="fresh",
            plan="p-1",
            cwd="/w",
            home=home,
        )
        # Use the current wall clock so ``time.time()`` inside
        # ``grogu_watch.record`` and the ``FakeClock`` here agree.
        clock = _FakeClock(initial=time.time())
        room = self._make_room(now=clock)
        registration = cr.Registration(
            run_id="run-fresh", repository="/w", plan="p-1",
            agent="fresh", role="engineer", workstream="w",
            session_id="", agent_id="", registered_at="",
        )
        room.register(registration)
        snapshot = room.snapshot()
        row = _find_row(snapshot, registration.agent_key)
        self.assertEqual(row["activity"], "active")

    def test_the_thresholds_have_fixed_named_constants(self):
        # Amending them is an amendment; the tests are the tripwire.
        self.assertEqual(cr.TOOL_DEADLINE_GRACE_SECONDS, 60)
        self.assertEqual(cr.NO_OUTCOME_AFTER_ACTIVITY_SECONDS, 900)
        self.assertEqual(cr.NO_OUTCOME_MIN_TOOL_EVENTS, 100)
        self.assertEqual(cr.REPEATED_FAILURES_WINDOW_SECONDS, 120)
        self.assertEqual(cr.REPEATED_FAILURES_THRESHOLD, 3)
        self.assertEqual(cr.CONNECTION_STALE_SECONDS, 10)
        self.assertEqual(cr.CONNECTION_DISCONNECTED_SECONDS, 30)


class CorrelationRequiresRegistration(unittest.TestCase):

    def setUp(self):
        self._watch_home = tempfile.TemporaryDirectory()
        self.addCleanup(self._watch_home.cleanup)

    def _make_room(self, **kwargs):
        kwargs.setdefault("watch_home", Path(self._watch_home.name))
        return cr.ControlRoom(**kwargs)

    def test_a_source_two_stream_never_appears_without_registration(self):
        # No registration -> nothing to read. The reader does not scan
        # session-state on its own.
        room = self._make_room(now=_FakeClock())
        snapshot = room.snapshot()
        self.assertEqual(snapshot["agents"], [])
        self.assertEqual(snapshot["uncorrelated"], [])

    def test_source_one_activity_without_registration_lands_uncorrelated(self):
        home_path = Path(self._watch_home.name)
        grogu_watch.record(
            command="plan gate",
            role="engineer",
            agent="stranger",
            plan="p-x",
            cwd="/tmp/somewhere",
            home=home_path,
        )
        room = self._make_room(now=_FakeClock())
        snapshot = room.snapshot()
        self.assertEqual(len(snapshot["uncorrelated"]), 1)
        self.assertEqual(snapshot["uncorrelated"][0]["role"], "")
        self.assertEqual(snapshot["agents"], [])

    def test_no_cwd_or_name_inference_ties_a_source_two_stream_to_source_one(self):
        home_path = Path(self._watch_home.name)
        grogu_watch.record(
            command="plan gate",
            role="engineer",
            agent="in-cwd",
            plan="p-1",
            cwd="/w/a",
            home=home_path,
        )
        room = self._make_room(now=_FakeClock())
        registration = cr.Registration(
            run_id="run-1",
            repository="/w/a",  # deliberately the same cwd
            plan="p-1",
            agent="in-cwd",
            role="engineer",
            workstream="w",
            session_id="",
            agent_id="",
            registered_at="",
        )
        room.register(registration)
        snapshot = room.snapshot()
        row = _find_row(snapshot, registration.agent_key)
        self.assertEqual(row["tools"]["started"], 0)


class CoverageDegradesHonestly(unittest.TestCase):

    def setUp(self):
        self._watch_home = tempfile.TemporaryDirectory()
        self.addCleanup(self._watch_home.cleanup)

    def _make_room(self, **kwargs):
        kwargs.setdefault("watch_home", Path(self._watch_home.name))
        return cr.ControlRoom(**kwargs)

    def test_source_two_unavailable_says_tool_activity_is_a_board_limit(self):
        home_path = Path(self._watch_home.name)
        grogu_watch.record(
            command="plan gate",
            role="engineer",
            agent="s1-only",
            plan="p-1",
            cwd="/w/a",
            home=home_path,
        )
        room = self._make_room(now=_FakeClock())
        registration = cr.Registration(
            run_id="run-1", repository="/w/a", plan="p-1",
            agent="s1-only", role="engineer", workstream="w",
            session_id="", agent_id="", registered_at="",
        )
        room.register(registration)
        snapshot = room.snapshot()
        self.assertIn("tool_activity", snapshot["limits"])

    def test_source_two_present_removes_tool_activity_from_board_limits(self):
        clock = _FakeClock(initial=_epoch("2026-09-05T09:01:30Z"))
        room = self._make_room(now=clock)
        registration = _make_registration("adv-session-001")
        room.register(registration)
        snapshot = room.snapshot()
        # source 2 is present, so ``tool_activity`` is not on the *board*
        # limits list (it may still be on individual rows if their events_path
        # is empty).
        self.assertNotIn("tool_activity", snapshot["limits"])


class LimitsAlwaysStated(unittest.TestCase):

    def setUp(self):
        self._watch_home = tempfile.TemporaryDirectory()
        self.addCleanup(self._watch_home.cleanup)

    def test_every_row_states_the_categories_beyond_sight(self):
        clock = _FakeClock(initial=_epoch("2026-09-05T09:01:30Z"))
        room = cr.ControlRoom(now=clock, watch_home=Path(self._watch_home.name))
        registration = _make_registration("adv-session-001")
        room.register(registration)
        snapshot = room.snapshot()
        row = _find_row(snapshot, registration.agent_key)
        for limit in (
            "model_reasoning",
            "command_arguments",
            "tool_arguments",
            "tool_results",
            "file_edits",
            "sealed_stage_content",
            "trace_payload",
        ):
            self.assertIn(limit, row["limits"], f"limit missing: {limit}")


class FeedbackRouting(unittest.TestCase):
    """Feedback goes through PlanStore.steer -- never through a new channel."""

    def setUp(self):
        self._watch_home = tempfile.TemporaryDirectory()
        self.addCleanup(self._watch_home.cleanup)

    def _make_room(self, **kwargs):
        kwargs.setdefault("watch_home", Path(self._watch_home.name))
        return cr.ControlRoom(**kwargs)

    def test_the_receipt_carries_no_note_text(self):
        clock = _FakeClock()
        room = self._make_room(now=clock)
        registration = _make_registration("adv-session-001")
        room.register(registration)
        fake_store = _FakePlanStore()
        receipt = room.route_feedback(
            registration,
            text="prefer decimal for currency columns",
            plan_store=fake_store,
        )
        self.assertEqual(receipt["schema_version"], 1)
        self.assertNotIn("text", receipt)
        # The receipt does say what would close if binding, but nothing binding
        # here.
        self.assertEqual(receipt["gates_closed"], [])
        self.assertEqual(receipt["binding"], False)
        self.assertGreaterEqual(receipt["seq"], 1)
        # PlanStore.steer was called exactly once.
        self.assertEqual(len(fake_store.calls), 1)

    def test_binding_closes_the_gates_and_returns_them(self):
        clock = _FakeClock()
        room = self._make_room(now=clock)
        registration = _make_registration("adv-session-001")
        room.register(registration)
        fake_store = _FakePlanStore()
        receipt = room.route_feedback(
            registration,
            text="the plan cannot work; requires replan",
            binding=True,
            plan_store=fake_store,
        )
        self.assertTrue(receipt["binding"])
        self.assertEqual(
            receipt["gates_closed"],
            ["design", "implement", "test", "evaluate"],
        )
        self.assertEqual(fake_store.calls[0]["requires_replan"], True)

    def test_empty_feedback_is_refused_and_does_not_reach_the_store(self):
        room = self._make_room(now=_FakeClock())
        registration = _make_registration("adv-session-001")
        room.register(registration)
        fake_store = _FakePlanStore()
        with self.assertRaises(ValueError):
            room.route_feedback(registration, text="   ", plan_store=fake_store)
        self.assertEqual(fake_store.calls, [])

    def test_the_relay_command_never_carries_the_note_text(self):
        room = self._make_room(now=_FakeClock())
        registration = _make_registration("adv-session-001")
        room.register(registration)
        fake_store = _FakePlanStore()
        note_text = "FORBIDDEN_STEERING_NOTE_TEXT_that_must_not_appear_in_relay"
        receipt = room.route_feedback(
            registration, text=note_text, plan_store=fake_store
        )
        self.assertIsNotNone(receipt["relay_command"])
        self.assertNotIn(note_text, receipt["relay_command"])


class CachesDropForbiddenContent(unittest.TestCase):
    """A cache the room maintains must not carry forbidden content either."""

    def setUp(self):
        self._watch_home = tempfile.TemporaryDirectory()
        self.addCleanup(self._watch_home.cleanup)

    def test_the_reader_cache_never_carries_forbidden_content(self):
        clock = _FakeClock(initial=_epoch("2026-09-05T09:01:30Z"))
        room = cr.ControlRoom(now=clock, watch_home=Path(self._watch_home.name))
        registration = _make_registration("adv-session-001")
        room.register(registration)
        room.snapshot()
        # Walk every cached object we can reach.
        cached_state = room._agent_state
        assert_no_forbidden(self, {
            key: {
                slot: getattr(state, slot)
                for slot in state.__dataclass_fields__
            }
            for key, state in cached_state.items()
        }, context="agent state cache")
        assert_no_forbidden(self, [
            {"byte_offset": cursor.byte_offset, "generation": cursor.generation,
             "last_event_id": cursor.last_event_id}
            for cursor in room._cursors.values()
        ], context="cursor cache")

    def test_new_control_room_starts_empty_so_role_change_discards_state(self):
        clock = _FakeClock(initial=_epoch("2026-09-05T09:01:30Z"))
        watch_home = Path(self._watch_home.name)
        engineer = cr.ControlRoom(
            now=clock, effective_role="engineer", watch_home=watch_home
        )
        engineer.register(_make_registration("adv-session-001"))
        engineer.snapshot()
        supervisor = cr.ControlRoom(
            now=clock, effective_role="supervisor", watch_home=watch_home
        )
        supervisor_snapshot = supervisor.snapshot()
        # The new room shares no state at all.
        self.assertEqual(supervisor_snapshot["agents"], [])
        self.assertEqual(supervisor_snapshot["uncorrelated"], [])


class TracesArePayloadFree(unittest.TestCase):

    def setUp(self):
        self._watch_home = tempfile.TemporaryDirectory()
        self.addCleanup(self._watch_home.cleanup)

    def test_no_payload_json_column_is_read_or_returned(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "traces.db"
            import sqlite3

            connection = sqlite3.connect(db)
            connection.executescript(
                """
                CREATE TABLE traces (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    provider TEXT,
                    model TEXT,
                    status TEXT,
                    duration_ms INTEGER,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    estimated_cost_usd REAL,
                    payload_json TEXT NOT NULL
                );
                INSERT INTO traces VALUES (
                    't1','r1','2026-09-05T09:00:00Z','call','openai','gpt','ok',
                    100, 500, 200, 0.01,
                    '{"FORBIDDEN_PAYLOAD_JSON_LEAK":"secret"}'
                );
                """
            )
            connection.commit()
            connection.close()
            room = cr.ControlRoom(
                now=_FakeClock(), traces_db=db,
                watch_home=Path(self._watch_home.name),
            )
            snapshot = room.snapshot()
            assert_no_forbidden(self, snapshot, context="traces board")
            # Also assert the token is not in ANY serialisation.
            self.assertNotIn("FORBIDDEN_PAYLOAD_JSON_LEAK", json.dumps(snapshot))


class RegistrationLoading(unittest.TestCase):

    def test_registration_refuses_non_string_fields(self):
        with self.assertRaises(ValueError):
            cr.load_registration({"run_id": 42})

    def test_registration_defaults_missing_fields_to_empty(self):
        reg = cr.load_registration({})
        self.assertEqual(reg.run_id, "")
        self.assertEqual(reg.agent, "")

    def test_registration_round_trips_to_dict(self):
        payload = {
            "schema_version": 1,
            "run_id": "r",
            "repository": "/tmp/x",
            "plan": "p",
            "agent": "a",
            "role": "engineer",
            "workstream": "w",
            "session_id": "s",
            "agent_id": "ai",
            "registered_at": "2026-09-05T00:00:00Z",
            "events_path": "",
        }
        # schema_version is not a dataclass field; drop before loading.
        loadable = {k: v for k, v in payload.items() if k != "schema_version"}
        reg = cr.load_registration(loadable)
        self.assertEqual(reg.to_dict(), payload)


class FreshnessLabels(unittest.TestCase):

    def setUp(self):
        self._watch_home = tempfile.TemporaryDirectory()
        self.addCleanup(self._watch_home.cleanup)

    def test_fresh_as_of_reflects_the_newest_observed_event(self):
        clock = _FakeClock(initial=_epoch("2026-09-05T09:01:30Z"))
        room = cr.ControlRoom(now=clock, watch_home=Path(self._watch_home.name))
        registration = _make_registration("adv-session-001")
        room.register(registration)
        snapshot = room.snapshot()
        self.assertGreaterEqual(snapshot["fresh_as_of"], "2026-09-05")

    def test_generation_advances_monotonically(self):
        room = cr.ControlRoom(now=_FakeClock(), watch_home=Path(self._watch_home.name))
        first = room.snapshot()["generation"]
        second = room.snapshot()["generation"]
        self.assertGreater(second, first)


class TooManyRegisteredSourcesRefused(unittest.TestCase):

    def setUp(self):
        self._watch_home = tempfile.TemporaryDirectory()
        self.addCleanup(self._watch_home.cleanup)

    def test_the_32_source_cap_keeps_registration_with_limited_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "session-state"
            base.mkdir()
            room = cr.ControlRoom(
                now=_FakeClock(), watch_home=Path(self._watch_home.name)
            )
            for i in range(ae.MAX_REGISTERED_SOURCES):
                session_dir = base / f"session-{i:02d}"
                session_dir.mkdir()
                path = session_dir / "events.jsonl"
                path.write_text("", encoding="utf8")
                registration = cr.Registration(
                    run_id=f"r-{i}", repository="/x", plan="p",
                    agent=f"a-{i}", role="engineer", workstream="w",
                    session_id=f"session-{i:02d}", agent_id=f"aid-{i}",
                    registered_at="", events_path=str(path),
                )
                room.register(registration)
            # The 33rd remains visible, but its event source is not opened.
            overflow_dir = base / "session-overflow"
            overflow_dir.mkdir()
            path = overflow_dir / "events.jsonl"
            path.write_text("", encoding="utf8")
            overflow = cr.Registration(
                run_id="r-of", repository="/x", plan="p",
                agent="of", role="engineer", workstream="w",
                session_id="session-overflow", agent_id="aid-of",
                registered_at="", events_path=str(path),
            )
            room.register(overflow)
            row = _find_row(room.snapshot(), overflow.agent_key)
            self.assertEqual(row["events_status"], "unavailable")
            session_coverage = next(
                item
                for item in row["source_coverage"]
                if item["source"] == "session_events"
            )
            self.assertEqual(session_coverage["reason"], "source_limit")


class ObjectRefsAuthorized(unittest.TestCase):
    """The drill-in's evidence panel only shows enumerated refs."""

    def setUp(self):
        self._watch_home = tempfile.TemporaryDirectory()
        self.addCleanup(self._watch_home.cleanup)

    def test_no_objects_are_synthesised_without_evidence(self):
        room = cr.ControlRoom(
            now=_FakeClock(), watch_home=Path(self._watch_home.name)
        )
        registration = cr.Registration(
            run_id="r", repository="/x", plan="p",
            agent="a", role="engineer", workstream="w",
            session_id="", agent_id="", registered_at="",
        )
        room.register(registration)
        drill = room.drill_in(registration)
        self.assertEqual(drill["evidence"], [])


class SchemaVersion(unittest.TestCase):

    def setUp(self):
        self._watch_home = tempfile.TemporaryDirectory()
        self.addCleanup(self._watch_home.cleanup)

    def test_receipts_and_snapshots_carry_schema_version_1(self):
        room = cr.ControlRoom(
            now=_FakeClock(), watch_home=Path(self._watch_home.name)
        )
        snapshot = room.snapshot()
        self.assertEqual(snapshot["schema_version"], 1)


class WatchKeyingByPlanAndAgent(unittest.TestCase):
    """Registered agents' source-1 activity must land on their own row.

    A watch row is joined to a registration by ``(plan, agent)``. Any other
    keying (e.g. by cwd) would either fail to attach source 1 activity at
    all or would treat a name collision across plans as one agent, both of
    which are the plan bugs this test catches.
    """

    def setUp(self):
        self._watch_home = tempfile.TemporaryDirectory()
        self.addCleanup(self._watch_home.cleanup)

    def test_source_one_activity_is_merged_into_the_registered_row(self):
        home = Path(self._watch_home.name)
        for _ in range(3):
            grogu_watch.record(
                command="plan brief",
                role="engineer",
                agent="ada",
                plan="p-x",
                cwd="/tmp/repo",
                home=home,
            )
        room = cr.ControlRoom(now=_FakeClock(), watch_home=home)
        registration = cr.Registration(
            run_id="run-1", repository="/tmp/repo", plan="p-x",
            agent="ada", role="engineer", workstream="w",
            session_id="", agent_id="", registered_at="",
        )
        room.register(registration)
        snapshot = room.snapshot()
        row = _find_row(snapshot, registration.agent_key)
        self.assertEqual(row["grogu_commands"]["calls"], 3)
        # And the uncorrelated list must NOT contain a phantom twin.
        self.assertEqual(snapshot["uncorrelated"], [])

    def test_a_registration_with_no_source_two_still_gets_watch_data(self):
        home = Path(self._watch_home.name)
        grogu_watch.record(
            command="plan gate",
            role="engineer",
            agent="beta",
            plan="p-y",
            cwd="/w/b",
            exit_code=3,
            home=home,
        )
        room = cr.ControlRoom(now=_FakeClock(), watch_home=home)
        registration = cr.Registration(
            run_id="run-2", repository="/w/b", plan="p-y",
            agent="beta", role="engineer", workstream="w",
            session_id="", agent_id="", registered_at="",
        )
        room.register(registration)
        snapshot = room.snapshot()
        row = _find_row(snapshot, registration.agent_key)
        self.assertEqual(row["grogu_commands"]["calls"], 1)
        self.assertEqual(row["grogu_commands"]["failures"], 1)

    def test_same_agent_and_plan_in_another_repository_stays_uncorrelated(self):
        home = Path(self._watch_home.name)
        grogu_watch.record(
            command="plan gate",
            role="engineer",
            agent="ada",
            plan="p-x",
            repository="/tmp/repo-a",
            cwd="/tmp/repo-a",
            home=home,
        )
        grogu_watch.record(
            command="plan write",
            role="engineer",
            agent="ada",
            plan="p-x",
            repository="/tmp/repo-b",
            cwd="/tmp/repo-b",
            home=home,
        )
        room = cr.ControlRoom(now=_FakeClock(), watch_home=home)
        registration = cr.Registration(
            run_id="run-1", repository="/tmp/repo-a", plan="p-x",
            agent="ada", role="engineer", workstream="w",
            session_id="", agent_id="", registered_at="",
        )
        room.register(registration)
        snapshot = room.snapshot()
        row = _find_row(snapshot, registration.agent_key)
        self.assertEqual(row["grogu_commands"]["calls"], 1)
        self.assertEqual(len(snapshot["uncorrelated"]), 1)
        repository = snapshot["uncorrelated"][0]["repository"]
        self.assertRegex(repository, r"^repo-[0-9a-f]{16}$")
        self.assertNotIn("/tmp/repo-b", json.dumps(snapshot))


class SampleThrottleIsEnforced(unittest.TestCase):

    def setUp(self):
        self._watch_home = tempfile.TemporaryDirectory()
        self.addCleanup(self._watch_home.cleanup)

    def test_a_burst_of_snapshot_calls_reuses_the_last_sample(self):
        home = Path(self._watch_home.name)
        clock = _FakeClock()
        room = cr.ControlRoom(now=clock, watch_home=home)
        registration = cr.Registration(
            run_id="run-1", repository="/w", plan="p",
            agent="a", role="engineer", workstream="w",
            session_id="", agent_id="", registered_at="",
        )
        room.register(registration)
        # First call collects; subsequent calls within SAMPLE_INTERVAL do not.
        room.snapshot()
        first_row_calls = room.snapshot()["agents"][0]["grogu_commands"]["calls"]
        # Record a new command; a throttled snapshot must NOT see it.
        grogu_watch.record(
            command="plan gate",
            role="engineer",
            agent="a",
            plan="p",
            cwd="/w",
            home=home,
        )
        throttled = room.snapshot()["agents"][0]["grogu_commands"]["calls"]
        self.assertEqual(throttled, first_row_calls)
        # Advancing past the throttle picks up the new command.
        clock.advance(cr.SAMPLE_INTERVAL_SECONDS + 1)
        refreshed = room.snapshot()["agents"][0]["grogu_commands"]["calls"]
        self.assertEqual(refreshed, first_row_calls + 1)


class BoundedCaches(unittest.TestCase):

    def setUp(self):
        self._watch_home = tempfile.TemporaryDirectory()
        self.addCleanup(self._watch_home.cleanup)

    def test_pending_permissions_are_capped(self):
        room = cr.ControlRoom(
            now=_FakeClock(), watch_home=Path(self._watch_home.name)
        )
        # Feed many permission_requested events straight into the state
        # rather than through the reader, so the cap is exercised directly.
        state = _AgentStateStub(pending_permissions={})
        events = []
        for i in range(cr.MAX_PENDING_PERMISSIONS * 5):
            events.append(
                ae.NormalisedEvent(
                    event_id=f"e-{i}", phase="permission_requested",
                    at="2026-09-05T09:00:00Z", agent_id="a",
                    tool_call_id="", tool_name="",
                    request_id=f"perm-{i}",
                    success=None, error_code=None, duration_ms=None,
                )
            )
        for event in events:
            room._apply_event(state, event)
        self.assertLessEqual(
            len(state.pending_permissions), cr.MAX_PENDING_PERMISSIONS
        )

    def test_failure_log_is_bounded(self):
        room = cr.ControlRoom(
            now=_FakeClock(), watch_home=Path(self._watch_home.name)
        )
        state = _AgentStateStub(pending_permissions={})
        for i in range(cr.MAX_FAILURES_TRACKED * 2):
            room._apply_event(
                state,
                ae.NormalisedEvent(
                    event_id=f"e-{i}", phase="tool_completed",
                    at="2026-09-05T09:00:00Z", agent_id="a",
                    tool_call_id="", tool_name="bash", request_id="",
                    success=False, error_code="http_429", duration_ms=1,
                ),
            )
        self.assertLessEqual(
            len(state.failure_log), cr.MAX_FAILURES_TRACKED
        )

    def test_audit_log_is_bounded_per_agent(self):
        home = Path(self._watch_home.name)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "session-state" / "big"
            root.mkdir(parents=True)
            path = root / "events.jsonl"
            # Write a lot of allowlisted tool events.
            lines = []
            for i in range(cr.MAX_AUDIT_LOG_PER_AGENT * 2):
                lines.append(
                    '{"type":"tool.execution_start","id":"e-%d","timestamp":"2026-09-05T09:00:00Z","agentId":"a","data":{"toolCallId":"tc-%d","toolName":"bash"}}'
                    % (i, i)
                )
            path.write_text("\n".join(lines) + "\n", encoding="utf8")
            room = cr.ControlRoom(now=_FakeClock(), watch_home=home)
            registration = cr.Registration(
                run_id="run-1", repository="/w", plan="p",
                agent="a", role="engineer", workstream="w",
                session_id="big", agent_id="", registered_at="",
                events_path=str(path),
            )
            room.register(registration)
            room.snapshot()
            audit = room._audit_events_for(registration.agent_key)
            self.assertLessEqual(len(audit), cr.MAX_AUDIT_LOG_PER_AGENT)


class DrillInMatchesSchema(unittest.TestCase):
    """The drill-in emits shapes that satisfy the frozen contract."""

    def setUp(self):
        self._watch_home = tempfile.TemporaryDirectory()
        self.addCleanup(self._watch_home.cleanup)

    def test_activity_items_carry_kind_and_no_forbidden_extras(self):
        clock = _FakeClock(initial=_epoch("2026-09-05T09:01:30Z"))
        room = cr.ControlRoom(now=clock, watch_home=Path(self._watch_home.name))
        registration = _make_registration("adv-session-001")
        room.register(registration)
        room.snapshot()
        drill = room.drill_in(registration)
        allowed_keys = {
            "at", "kind", "name", "reference", "success",
            "error_code", "duration_ms",
        }
        for entry in drill["activity"]:
            self.assertIn("kind", entry)
            self.assertIn(
                entry["kind"],
                {
                    "grogu_command",
                    "tool_started",
                    "tool_completed",
                    "permission_requested",
                    "permission_completed",
                    "subagent_started",
                    "subagent_completed",
                    "coverage_gap",
                    "steering_delivered",
                    "steering_acknowledged",
                    "feedback_delivered",
                },
            )
            self.assertTrue(set(entry).issubset(allowed_keys),
                            f"extras: {set(entry) - allowed_keys}")

    def test_gap_items_carry_reason_not_kind(self):
        clock = _FakeClock(initial=_epoch("2026-09-05T09:01:30Z"))
        room = cr.ControlRoom(now=clock, watch_home=Path(self._watch_home.name))
        registration = _make_registration("adv-session-002")
        room.register(registration)
        drill = room.drill_in(registration)
        for gap in drill["gaps"]:
            self.assertIn("reason", gap)
            self.assertNotIn("kind", gap)

    def test_last_command_is_omitted_when_empty_rather_than_violating_pattern(self):
        room = cr.ControlRoom(
            now=_FakeClock(), watch_home=Path(self._watch_home.name)
        )
        registration = cr.Registration(
            run_id="r", repository="/x", plan="p",
            agent="a", role="engineer", workstream="w",
            session_id="", agent_id="", registered_at="",
        )
        room.register(registration)
        snapshot = room.snapshot()
        row = _find_row(snapshot, registration.agent_key)
        # No source-1 activity for this agent, so last_command is empty
        # under the safeName pattern; it must be omitted rather than
        # emitted as "".
        self.assertNotIn("last_command", row["grogu_commands"])


class SchemaValidatedShape(unittest.TestCase):
    """Cross-check the returned board and drill-in against the JSON schema.

    ``jsonschema`` is present in this checkout's virtualenv but is not a
    required import; the test skips gracefully when it is absent so this
    file has no new dependency. When it IS present, board and drill-in
    output is validated end-to-end against
    ``schemas/plan-control-room.schema.json`` so any code/schema drift is
    caught immediately.
    """

    def setUp(self):
        self._watch_home = tempfile.TemporaryDirectory()
        self.addCleanup(self._watch_home.cleanup)
        try:
            import jsonschema  # noqa: F401
        except ImportError:
            self.skipTest("jsonschema not installed")

    def _load_schema(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "schemas"
            / "plan-control-room.schema.json"
        )
        with path.open("r", encoding="utf8") as handle:
            return json.load(handle)

    def test_board_snapshot_with_uncorrelated_rows_satisfies_the_schema(self):
        # `_uncorrelated_row` was previously emitting ``last_command: ""``
        # which violates the safeName pattern. A production `grogu` command
        # is a string like ``"plan gate"`` that fails _safe_name and comes
        # out empty; ``_uncorrelated_row`` must omit the key instead.
        import jsonschema
        schema = self._load_schema()
        validator = jsonschema.Draft202012Validator(
            {"$ref": "#/$defs/boardSnapshot", **schema}
        )
        home = Path(self._watch_home.name)
        grogu_watch.record(
            command="plan gate",
            role="engineer",
            agent="loose",
            plan="p-loose",
            cwd="/w",
            home=home,
        )
        clock = _FakeClock()
        room = cr.ControlRoom(now=clock, watch_home=home)
        snapshot = room.snapshot()
        # Ensure the uncorrelated row is actually present, so the schema
        # check isn't vacuous.
        self.assertEqual(len(snapshot["uncorrelated"]), 1)
        errors = list(validator.iter_errors(snapshot))
        if errors:
            self.fail(
                "uncorrelated-row board snapshot violates schema:\n"
                + "\n".join(str(error) for error in errors)
            )

    def test_board_snapshot_satisfies_the_schema(self):
        import jsonschema
        schema = self._load_schema()
        validator = jsonschema.Draft202012Validator(
            {"$ref": "#/$defs/boardSnapshot", **schema}
        )
        clock = _FakeClock(initial=_epoch("2026-09-05T09:01:30Z"))
        room = cr.ControlRoom(now=clock, watch_home=Path(self._watch_home.name))
        registration = _make_registration("adv-session-001")
        room.register(registration)
        snapshot = room.snapshot()
        errors = list(validator.iter_errors(snapshot))
        if errors:
            self.fail(
                "board snapshot violates schema:\n"
                + "\n".join(str(error) for error in errors)
            )

    def test_drill_in_satisfies_the_schema(self):
        import jsonschema
        schema = self._load_schema()
        validator = jsonschema.Draft202012Validator(
            {"$ref": "#/$defs/drillIn", **schema}
        )
        clock = _FakeClock(initial=_epoch("2026-09-05T09:01:30Z"))
        room = cr.ControlRoom(now=clock, watch_home=Path(self._watch_home.name))
        registration = _make_registration("adv-session-001")
        room.register(registration)
        room.snapshot()
        drill = room.drill_in(registration)
        errors = list(validator.iter_errors(drill))
        if errors:
            self.fail(
                "drill-in violates schema:\n"
                + "\n".join(str(error) for error in errors)
            )


# -- helpers --------------------------------------------------------------


class _AgentStateStub:
    """A stand-in :class:`cr._AgentState` used when a test calls
    :meth:`ControlRoom._apply_event` directly to exercise the bounded
    caches. The stub carries every field :meth:`_apply_event` touches."""

    def __init__(self, pending_permissions):
        self.lifecycle = "running"
        self.lifecycle_at = 0.0
        self.started_at = 0.0
        self.started_basis = "unknown"
        self.last_observed_at = 0.0
        self.current_tool = None
        self.current_tool_call = None
        self.current_tool_started_at = 0.0
        self.tools_started = 0
        self.tools_completed = 0
        self.tools_failed = 0
        self.subagent_completed_seen = False
        self.failure_log: list = []
        self.pending_permissions: dict = pending_permissions
        self.outcome_at = 0.0

    def lifecycle_rank(self) -> int:
        return 2

    def observe_activity(self, at_epoch: float) -> None:
        if at_epoch > self.last_observed_at:
            self.last_observed_at = at_epoch

    def lifecycle_terminal_activity(self) -> str:
        return "quiet"


class _FakePlanStore:
    """A tiny stand-in for :class:`PlanStore` that records the calls we make.

    Reused by the routing tests to assert that ``steer`` and ``steering`` are
    the ONLY methods the control room calls -- no new channel is added.
    """

    def __init__(self) -> None:
        self.calls: list = []
        self._seq = 0

    def steer(
        self,
        *,
        text: str,
        plan_id: str = "",
        role: str = "all",
        requires_replan: bool = False,
    ) -> dict:
        self._seq += 1
        entry = {
            "text": text,
            "plan_id": plan_id,
            "role": role,
            "requires_replan": requires_replan,
        }
        self.calls.append(entry)
        return {
            "seq": self._seq,
            "at": "2026-09-05T09:00:00Z",
            "role": role,
            "text": text,
            "requires_replan": requires_replan,
        }

    def steering(self, *, role: str = "all", plan_id: str = "", unread: bool = False) -> dict:
        plan_notes = [{"seq": call["seq"] if False else index + 1,
                       "at": "2026-09-05T09:00:00Z",
                       "requires_replan": call["requires_replan"]}
                      for index, call in enumerate(self.calls)
                      if call["role"] in ("all", role) or role == "all"]
        return {"role": role, "plan": plan_notes, "repository": [], "plan_id": plan_id}


def _find_row(snapshot: dict, agent_key: str) -> dict:
    for row in snapshot["agents"]:
        if row["agent_key"] == agent_key:
            return row
    for row in snapshot["uncorrelated"]:
        if row["agent_key"] == agent_key:
            return row
    raise AssertionError(f"no row for {agent_key}; agents={snapshot['agents']}")


def _epoch(iso: str) -> float:
    import calendar as _cal
    return _cal.timegm(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S"))


if __name__ == "__main__":
    unittest.main()
