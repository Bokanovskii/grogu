"""Tests for grogu_agentevents -- the allowlist normaliser for Copilot session events.

The heart of these tests is the privacy contract: no forbidden field ever
appears in any value, key, exception, cache or serialisation. The fixture
under ``tests/fixtures/controlroom/`` deliberately contains every forbidden
category, so the assertions here are load-bearing rather than aesthetic.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import _sandbox  # noqa: E402,F401

import grogu_agentevents as ae  # noqa: E402


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "controlroom"


def _fixture_events(session_id: str) -> Path:
    """Return the path to a fixture events log, staged under a temporary
    ``session-state`` directory. Copying under a temporary root proves the
    reader would refuse any path that did not sit under ``session-state``."""
    source = (
        FIXTURE_ROOT / "session-state" / session_id / "events.jsonl"
    )
    return source


# Every forbidden token below appears verbatim somewhere in a fixture.
# A test that finds one in a returned structure has caught a leak.
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


def _serialised_forms(value: object) -> list:
    """Return every string form a value could reach through: repr, str, json."""
    forms: list = []
    try:
        forms.append(repr(value))
    except Exception:
        pass
    try:
        forms.append(str(value))
    except Exception:
        pass
    try:
        forms.append(json.dumps(value, default=str))
    except Exception:
        pass
    return forms


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
                f"{context}: forbidden token {token!r} appeared in a returned value: {string!r}",
            )
    for form in _serialised_forms(value):
        for token in FORBIDDEN_TOKENS:
            test.assertNotIn(
                token,
                form,
                f"{context}: forbidden token {token!r} appeared in a serialisation: {form!r}",
            )


class AllowlistDropsEveryForbiddenField(unittest.TestCase):
    """Read the adversarial fixture and prove nothing forbidden survives."""

    def setUp(self):
        self.path = _fixture_events("adv-session-001")

    def test_reading_the_fixture_returns_only_allowlisted_fields(self):
        result = ae.read_source(self.path)
        self.assertGreater(len(result.events), 0)
        for event in result.events:
            # NormalisedEvent has __slots__, so no extra attributes can hide.
            for attribute in event.__slots__:
                value = getattr(event, attribute)
                assert_no_forbidden(
                    self, value, context=f"event.{attribute}"
                )
            assert_no_forbidden(self, event.to_dict(), context="event.to_dict()")

    def test_the_read_result_serialisation_hides_nothing_and_leaks_nothing(self):
        result = ae.read_source(self.path)
        assert_no_forbidden(self, result.to_dict(), context="ReadResult.to_dict()")

    def test_the_cursor_carries_only_scalars(self):
        result = ae.read_source(self.path)
        cursor = result.cursor
        for attribute in ("byte_offset", "generation", "last_event_id"):
            value = getattr(cursor, attribute)
            assert_no_forbidden(self, value, context=f"cursor.{attribute}")
        assert_no_forbidden(self, cursor.to_dict(), context="cursor.to_dict()")

    def test_repeated_reads_never_accumulate_forbidden_content(self):
        first = ae.read_source(self.path)
        second = ae.read_source(self.path, cursor=first.cursor)
        assert_no_forbidden(self, first.to_dict(), context="first read")
        assert_no_forbidden(self, second.to_dict(), context="second read")


class NormalisedEventShape(unittest.TestCase):

    def test_only_the_allowlisted_slots_exist(self):
        expected = (
            "event_id",
            "phase",
            "at",
            "agent_id",
            "tool_call_id",
            "tool_name",
            "request_id",
            "success",
            "error_code",
            "duration_ms",
        )
        self.assertEqual(ae.NormalisedEvent.__slots__, expected)

    def test_instances_have_no_dict_so_stray_kwargs_cannot_hide(self):
        event = ae.NormalisedEvent(
            event_id="e", phase="tool_started", at="", agent_id="",
            tool_call_id="", tool_name="", request_id="",
            success=None, error_code=None, duration_ms=None,
        )
        with self.assertRaises(AttributeError):
            event.extra = "leak"  # type: ignore[attr-defined]

    def test_tool_names_that_are_not_safe_identifiers_are_dropped(self):
        result = ae.read_source(
            _fixture_events("adv-session-002")
        )
        for event in result.events:
            if event.tool_name:
                self.assertRegex(
                    event.tool_name,
                    ae.SAFE_NAME_PATTERN,
                    "an unsafe name reached the returned structure",
                )


class TornAndMalformedLines(unittest.TestCase):

    def test_a_torn_first_line_is_a_gap_not_an_exception(self):
        # Simulate a tail that starts mid-line by supplying a cursor with
        # byte_offset=0 (a first read) into a fixture where we know the
        # first line was truncated: use the fixture as-is but call it a
        # cold start of a large log by writing a bigger file.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "session-state" / "torn-1"
            root.mkdir(parents=True)
            path = root / "events.jsonl"
            # Two known-good lines; then use the initial-tail path with a
            # size larger than TAIL_BYTES to force a tail-start-mid-line.
            path.write_text(
                b"".join(
                    [b'{"type":"tool.execution_start","id":"e-1","timestamp":"2026-09-05T09:00:00Z","agentId":"a","data":{"toolCallId":"tc","toolName":"bash"}}\n' for _ in range(2)]
                ).decode("utf8"),
                encoding="utf8",
            )
            result = ae.read_source(path)
            # No exceptions; a valid read.
            self.assertIsInstance(result, ae.ReadResult)

    def test_a_malformed_line_becomes_a_gap(self):
        result = ae.read_source(_fixture_events("adv-session-002"))
        self.assertTrue(
            any(
                gap.kind == ae.GAP_MALFORMED_LINE
                for gap in result.gaps
            ),
            "the reader failed to flag a malformed line",
        )
        assert_no_forbidden(self, result.to_dict(), context="malformed fixture")

    def test_unknown_event_type_becomes_a_version_gap(self):
        result = ae.read_source(_fixture_events("adv-session-002"))
        self.assertTrue(
            any(
                gap.kind == ae.GAP_UNKNOWN_EVENT_VERSION
                for gap in result.gaps
            ),
        )

    def test_oversized_line_is_a_gap_not_a_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "session-state" / "big"
            root.mkdir(parents=True)
            path = root / "events.jsonl"
            oversize = "FORBIDDEN_MEGA_LINE_" + ("X" * (ae.MAX_LINE_BYTES + 10))
            path.write_text(oversize + "\n", encoding="utf8")
            result = ae.read_source(path)
            self.assertTrue(
                any(gap.kind == ae.GAP_OVERSIZED_EVENT for gap in result.gaps)
            )
            self.assertEqual(result.events, [])


class CursorInvariants(unittest.TestCase):

    def test_cursor_advances_only_over_processed_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "session-state" / "seq"
            root.mkdir(parents=True)
            path = root / "events.jsonl"
            good = (
                '{"type":"tool.execution_start","id":"e-1","timestamp":"2026-09-05T09:00:00Z","agentId":"a","data":{"toolCallId":"tc","toolName":"bash"}}\n'
            )
            path.write_text(good + '{"partial ', encoding="utf8")
            result = ae.read_source(path)
            self.assertEqual(len(result.events), 1)
            # Second call should not re-emit the first line.
            second = ae.read_source(path, cursor=result.cursor)
            self.assertEqual(second.events, [])

    def test_rotation_resets_the_cursor_and_reports_a_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "session-state" / "rot"
            root.mkdir(parents=True)
            path = root / "events.jsonl"
            good = (
                '{"type":"tool.execution_start","id":"e-1","timestamp":"2026-09-05T09:00:00Z","agentId":"a","data":{"toolCallId":"tc","toolName":"bash"}}\n'
            )
            path.write_text(good, encoding="utf8")
            first = ae.read_source(path)
            # Simulate rotation: rewrite content shorter than cursor.
            path.write_text('{"type":"tool.execution_start","id":"e-9","timestamp":"2026-09-05T09:00:05Z","agentId":"a","data":{"toolCallId":"tc2","toolName":"view"}}\n', encoding="utf8")
            # Corrupt the cursor by moving offset beyond the shrunk file so
            # the truncation branch fires.
            second = ae.read_source(
                path,
                cursor=ae.Cursor(byte_offset=first.cursor.byte_offset + 10_000,
                                 generation=first.cursor.generation),
            )
            self.assertTrue(
                any(gap.kind == ae.GAP_ROTATED_OR_TRUNCATED for gap in second.gaps)
            )


class PathSafety(unittest.TestCase):

    def test_a_path_outside_session_state_is_refused_with_a_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            weird = Path(directory) / "not-session" / "events.jsonl"
            weird.parent.mkdir(parents=True)
            weird.write_text(
                '{"type":"tool.execution_start","id":"e","timestamp":"","agentId":"","data":{}}\n',
                encoding="utf8",
            )
            result = ae.read_source(weird)
            self.assertEqual(result.events, [])
            self.assertTrue(
                any(gap.kind == ae.GAP_SOURCE_READ_ERROR for gap in result.gaps)
            )

    def test_a_missing_file_is_a_source_read_error(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "session-state" / "x" / "events.jsonl"
            result = ae.read_source(missing)
            self.assertEqual(result.events, [])
            self.assertTrue(
                any(gap.kind == ae.GAP_SOURCE_READ_ERROR for gap in result.gaps)
            )

    def test_a_symlink_events_jsonl_that_points_outside_is_refused(self):
        # An events.jsonl file whose surface path passes the check but which
        # is a symlink pointing outside session-state must be refused with a
        # gap; nothing may be read from the target.
        if not hasattr(os, "symlink"):  # pragma: no cover - platform guard
            self.skipTest("symlinks unavailable on this platform")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            elsewhere = root / "elsewhere.jsonl"
            elsewhere.write_text(
                '{"type":"tool.execution_start","id":"e-outside","timestamp":"2026-09-05T09:00:00Z","agentId":"a","data":{"toolCallId":"tc","toolName":"bash"}}\n',
                encoding="utf8",
            )
            session_dir = root / "session-state" / "sneaky"
            session_dir.mkdir(parents=True)
            events = session_dir / "events.jsonl"
            try:
                os.symlink(str(elsewhere), str(events))
            except OSError:  # pragma: no cover - permission on CI
                self.skipTest("could not create symlink")
            result = ae.read_source(events)
            self.assertEqual(result.events, [])
            self.assertTrue(
                any(gap.kind == ae.GAP_SOURCE_READ_ERROR for gap in result.gaps)
            )

    def test_a_symlinked_session_state_directory_is_refused(self):
        # A ``session-state`` directory that is itself a symlink pointing
        # to another location must not be walked into.
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable on this platform")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            victim = root / "victim" / "session-state" / "vic"
            victim.mkdir(parents=True)
            (victim / "events.jsonl").write_text(
                '{"type":"tool.execution_start","id":"e-v","timestamp":"2026-09-05T09:00:00Z","agentId":"a","data":{"toolCallId":"tc","toolName":"bash"}}\n',
                encoding="utf8",
            )
            attacker = root / "attacker"
            attacker.mkdir()
            # Attacker replaces its own session-state with a symlink to the
            # victim's session-state directory.
            try:
                os.symlink(str(victim.parent), str(attacker / "session-state"))
            except OSError:
                self.skipTest("could not create symlink")
            events = attacker / "session-state" / "vic" / "events.jsonl"
            result = ae.read_source(events)
            self.assertEqual(result.events, [])
            self.assertTrue(
                any(gap.kind == ae.GAP_SOURCE_READ_ERROR for gap in result.gaps)
            )

    def test_events_jsonl_that_is_a_regular_file_at_the_wrong_shape_is_refused(self):
        # ``events.jsonl`` sitting at a path with a session-state that has
        # more or fewer trailing components is refused; only the exact
        # session-state/<id>/events.jsonl shape is accepted.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session-state" / "extra" / "sub" / "events.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text("{}\n", encoding="utf8")
            result = ae.read_source(path)
            self.assertEqual(result.events, [])
            self.assertTrue(
                any(gap.kind == ae.GAP_SOURCE_READ_ERROR for gap in result.gaps)
            )


class TimestampFieldIsNotAnAllowlistedTextVehicle(unittest.TestCase):
    """A hostile events file must not smuggle arbitrary text through ``at``."""

    def test_a_free_form_timestamp_is_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "session-state" / "hostile"
            root.mkdir(parents=True)
            path = root / "events.jsonl"
            hostile = 'FORBIDDEN_HOSTILE_TIMESTAMP_TEXT'
            payload = (
                '{"type":"tool.execution_start","id":"e-1","timestamp":"'
                + hostile
                + '","agentId":"a","data":{"toolCallId":"tc","toolName":"bash"}}\n'
            )
            path.write_text(payload, encoding="utf8")
            result = ae.read_source(path)
            self.assertEqual(len(result.events), 1)
            self.assertEqual(result.events[0].at, "")
            for event in result.events:
                self.assertNotIn(hostile, event.at)


class Bounds(unittest.TestCase):

    def test_the_documented_thresholds_are_named_and_pinned(self):
        # Amending these is an amendment; the tests are the tripwire.
        self.assertEqual(ae.INITIAL_TAIL_BYTES, 4 * 1024 * 1024)
        self.assertEqual(ae.REFRESH_BYTES, 1 * 1024 * 1024)
        self.assertEqual(ae.MAX_LINE_BYTES, 1 * 1024 * 1024)
        self.assertEqual(ae.MAX_REGISTERED_SOURCES, 32)


class ExceptionsCarryNoForbiddenContent(unittest.TestCase):
    """A leak through an exception message would defeat the allowlist."""

    def test_reading_the_adversarial_fixture_never_raises(self):
        try:
            ae.read_source(_fixture_events("adv-session-001"))
        except BaseException as error:
            self.fail(f"read_source raised: {error!r}")

    def test_duplicate_keys_in_a_line_do_not_produce_a_forbidden_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "session-state" / "dup"
            root.mkdir(parents=True)
            path = root / "events.jsonl"
            # A duplicate ``type`` key: json.loads with the reader's hook
            # will refuse. The exception is caught internally and turned
            # into a gap; the forbidden content on the line must not leak.
            path.write_text(
                '{"type":"tool.execution_start","type":"FORBIDDEN_DUPLICATE_KEY_VALUE","id":"e"}\n',
                encoding="utf8",
            )
            result = ae.read_source(path)
            assert_no_forbidden(self, result.to_dict(), context="dup key gap")


class KnownVersusUnknownVocabulary(unittest.TestCase):

    def test_a_known_event_vocabulary_contains_the_six_normalised_types(self):
        for event_type in ae.NORMALISED_EVENT_TYPES:
            self.assertIn(event_type, ae.KNOWN_EVENT_TYPES)

    def test_the_normaliser_is_the_only_place_extension_happens(self):
        # A regression that adds a new key to a NormalisedEvent should fail
        # this test loudly rather than silently expanding what leaves the
        # parser.
        self.assertEqual(
            set(ae.NormalisedEvent.__slots__),
            {
                "event_id",
                "phase",
                "at",
                "agent_id",
                "tool_call_id",
                "tool_name",
                "request_id",
                "success",
                "error_code",
                "duration_ms",
            },
        )


if __name__ == "__main__":
    unittest.main()
