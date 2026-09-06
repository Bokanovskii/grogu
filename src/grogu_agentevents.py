"""Allowlist normaliser for Copilot session event logs.

Read this whole docstring before touching this file.

# What this module is, and what it is not

`grogu_controlroom` needs to answer three questions that source 1 (the passive
grogu command feed in `activity.jsonl`) cannot answer on its own:

  * what tool an agent is in right now,
  * how many tools failed,
  * whether the agent is waiting on a permission decision.

Source 2 is the Copilot CLI session event log at
``$COPILOT_HOME/session-state/<session>/events.jsonl``. It carries
``subagent.started``/``subagent.completed``, ``tool.execution_start``/
``tool.execution_complete`` and ``permission.requested``/
``permission.completed`` events, which are the shape control-room questions
above map to. Those event types were **observed** in this checkout at plan
time; they are not a published contract, and this module version-checks them.
An unrecognised event vocabulary produces a gap marker and coverage
``unavailable`` -- never a widening of the allowlist to recover coverage.

The same event log also carries assistant message bodies, task descriptions
copied from the parent tool call's ``arguments``, tool arguments and results,
permission intention text, ``reasoningText``, ``reasoningOpaque``, and
whatever prompts the user typed. **None of that ever leaves this file.** This
is not a display filter: the forbidden categories are dropped in the parse
function itself, before any value reaches the returned structure, the cursor,
an exception message, a log line, a cached record or a serialisation.

# The privacy contract, restated so it is not accidentally weakened

Only these fields may appear on a returned :class:`NormalisedEvent`:
``event_id``, ``phase``, ``at``, ``agent_id``, ``tool_call_id``, ``tool_name``,
``request_id``, ``success``, ``error_code`` and ``duration_ms``. The tool name
is validated against :data:`SAFE_NAME_PATTERN`; a value that does not match is
coerced to the empty string. The error code is a short allowlisted machine
token. Everything else in the source event is discarded. The returned record
has ``__slots__`` and no ``__dict__``, so an ``__init__`` that quietly picked
up extra keyword arguments cannot happen.

# What this module WILL NOT DO

  * It will not scan ``$COPILOT_HOME/session-state``. It reads only the exact
    file paths a registration record supplies, and it refuses paths that do
    not sit under a ``session-state/<session-id>/`` directory.
  * It will not follow rotated files by inference. When a source is rotated
    or truncated, that emits a coverage gap and the cursor is reset.
  * It will not expand the allowlist to recover coverage on an unrecognised
    event vocabulary. It emits an ``unknown_event_version`` gap and the
    control-room reports ``tools: unavailable`` for that source.
  * It will not raise on adversarial input. A malformed line, an oversized
    event, a torn first record, a cursor from a different file generation are
    all coverage gaps. A source that cannot be opened is a
    ``source_read_error`` gap. Every path returns a :class:`ReadResult`.

The bounds this module enforces are load-bearing:

  * initial tail  ``<= 4 MiB`` per registered session
  * each refresh  ``<= 1 MiB`` per source
  * a single line ``<= 1 MiB``
  * at most 32 registered sources per reader (enforced by the caller)

These constants are named -- :data:`INITIAL_TAIL_BYTES`, :data:`REFRESH_BYTES`,
:data:`MAX_LINE_BYTES`, :data:`MAX_REGISTERED_SOURCES` -- and the tests assert
them at fixed values.

# Why an allowlist rather than a redactor

A redactor is a blocklist over the *content* of a value, and the content of a
value in ``events.jsonl`` is unbounded: a redactor good enough to catch every
secret in a shell argument is also a redactor that misses the next secret, and
even a perfect redactor still leaks structure (a task description tells the
user what the agent was told to do). The allowlist is over field IDENTITY, not
content: a field the schema does not name is dropped whether or not its
content looks sensitive. That is the property the tests assert.
"""

from __future__ import annotations

import calendar
import io
import json
import os
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

# -- constants ------------------------------------------------------------

INITIAL_TAIL_BYTES = 4 * 1024 * 1024  # 4 MiB
REFRESH_BYTES = 1 * 1024 * 1024  # 1 MiB
MAX_LINE_BYTES = 1 * 1024 * 1024  # 1 MiB
MAX_REGISTERED_SOURCES = 32

# The vocabulary this build recognises. An event whose ``type`` is not on
# this table produces an ``unknown_event_version`` gap for its source. The
# allowlist NEVER widens to recover coverage; that is an amendment.
KNOWN_EVENT_TYPES: frozenset = frozenset({
    "subagent.started",
    "subagent.completed",
    "tool.execution_start",
    "tool.execution_complete",
    "permission.requested",
    "permission.completed",
    # These are known-and-ignored: their existence in the log does not signal
    # an unrecognised vocabulary, but nothing from them is normalised.
    "assistant.message",
    "assistant.turn_start",
    "assistant.turn_end",
    "model.captured_assignment_context",
    "model.message",
    "model.messages_snapshot",
    "model.model_call_started",
    "model.model_call_success",
    "model.response",
    "model.turn_ended",
    "model.turn_started",
    "session.binary_asset",
    "session.error",
    "session.info",
    "session.mode_changed",
    "session.model_change",
    "session.permissions_changed",
    "session.start",
    "session.task_complete",
    "session.usage_checkpoint",
    "session.warning",
    "skill.invoked",
    "subagent.configured",
    "system.message",
    "system.notification",
    "user.message",
})

# The subset that actually contributes to a normalised event. A value in
# :data:`KNOWN_EVENT_TYPES` but not here is silently ignored (its existence is
# not a coverage gap; its body is never read).
NORMALISED_EVENT_TYPES: frozenset = frozenset({
    "subagent.started",
    "subagent.completed",
    "tool.execution_start",
    "tool.execution_complete",
    "permission.requested",
    "permission.completed",
})

# What every ``phase`` value means to the reader. A record whose type is not
# on this map is not emitted.
_PHASE_BY_TYPE: dict = {
    "subagent.started": "subagent_started",
    "subagent.completed": "subagent_completed",
    "tool.execution_start": "tool_started",
    "tool.execution_complete": "tool_completed",
    "permission.requested": "permission_requested",
    "permission.completed": "permission_completed",
}

# The identifier and safe-name grammars. Any value that does not match a
# grammar is coerced to the empty string; no allowlisted field is populated
# from user-controlled bytes without this filter.
SAFE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9._:@/-]{1,80}$")
ERROR_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")

# The gap kinds returned by this module. The schema pins the exact set.
GAP_TORN_LINE = "torn_line"
GAP_MALFORMED_LINE = "malformed_line"
GAP_OVERSIZED_EVENT = "oversized_event"
GAP_OVERSIZED_SOURCE = "oversized_source"
GAP_ROTATED_OR_TRUNCATED = "rotated_or_truncated"
GAP_CURSOR_LOST = "cursor_lost"
GAP_UNKNOWN_EVENT_VERSION = "unknown_event_version"
GAP_UNREGISTERED_SESSION = "unregistered_session"
GAP_SOURCE_READ_ERROR = "source_read_error"


# -- dataclasses -----------------------------------------------------------


@dataclass(frozen=True)
class NormalisedEvent:
    """The only shape that may leave the parser.

    ``__slots__`` and ``frozen`` are deliberate: an ``__init__`` that quietly
    picked up an extra keyword argument would defeat the point of the
    allowlist. Every field here is enumerated in the schema.
    """

    __slots__ = (
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

    event_id: str
    phase: str
    at: str
    agent_id: str
    tool_call_id: str
    tool_name: str
    request_id: str
    success: Optional[bool]
    error_code: Optional[str]
    duration_ms: Optional[int]

    def to_dict(self) -> dict:
        """Deterministic JSON-safe view. Only allowlisted fields are present."""
        return {
            "event_id": self.event_id,
            "phase": self.phase,
            "at": self.at,
            "agent_id": self.agent_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "request_id": self.request_id,
            "success": self.success,
            "error_code": self.error_code,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True)
class CoverageGap:
    """An explicit interruption in coverage.

    Every path in this module that could raise or lie instead returns a gap.
    A gap says the reader knows it does not know.
    """

    kind: str
    at: str
    at_line: Optional[int] = None

    def to_dict(self) -> dict:
        return {"kind": self.kind, "at": self.at, "at_line": self.at_line}


@dataclass(frozen=True)
class Cursor:
    """Advances only over fully processed records.

    ``generation`` is derived from the file's ``st_ino`` and ``st_size``: a
    smaller size than the cursor already advanced past means the file was
    rotated or truncated, and the cursor is discarded with a gap. An inode
    change means the same. This is the only correlation the module makes
    across sample cycles.
    """

    byte_offset: int = 0
    generation: int = 0
    last_event_id: str = ""

    def to_dict(self) -> dict:
        return {
            "byte_offset": self.byte_offset,
            "generation": self.generation,
            "last_event_id": self.last_event_id,
        }


@dataclass
class ReadResult:
    """The only shape that a full read produces.

    Callers that want to combine multiple reads keep only the ``events``,
    the accumulated ``gaps`` and the newest ``cursor``. Nothing else on the
    source may be carried between reads.
    """

    events: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    cursor: Cursor = field(default_factory=Cursor)
    bytes_read: int = 0

    def to_dict(self) -> dict:
        return {
            "events": [event.to_dict() for event in self.events],
            "gaps": [gap.to_dict() for gap in self.gaps],
            "cursor": self.cursor.to_dict(),
            "bytes_read": self.bytes_read,
        }


# -- registration helpers --------------------------------------------------


def is_session_events_path(path: Path) -> bool:
    """A path is only readable when it sits under ``session-state/<id>/events.jsonl``.

    The registration record is the only place a path arrives from, and it
    is provided by the supervisor. That path is still validated: a
    misconfigured registration cannot make this module walk somewhere else,
    and neither can a rogue symlink. See
    :func:`_resolve_session_events_path` for the strict path check that
    :func:`read_source` applies.
    """
    if not isinstance(path, Path):
        return False
    parts = path.parts
    if path.name != "events.jsonl":
        return False
    if "session-state" not in parts:
        return False
    idx = parts.index("session-state")
    # session-state/<session-id>/events.jsonl -- three trailing parts
    return len(parts) - idx == 3


def _resolve_session_events_path(path: Path) -> Optional[Path]:
    """Resolve ``path`` and return it only if it is a genuine
    ``session-state/<id>/events.jsonl`` after every symlink is followed.

    The surface check in :func:`is_session_events_path` cannot see symlinks:
    a ``session-state`` directory that is a symlink, an ``<id>`` directory
    that is a symlink, or an ``events.jsonl`` that is a symlink to a file
    outside the tree all pass the surface check but resolve to somewhere
    else. The privacy contract is that the reader reads *only* the file the
    registration named -- not a target the registration was pointed at.

    Returns ``None`` when the resolved path is not a real
    ``session-state/<id>/events.jsonl`` or when any component of the
    ORIGINAL path from ``session-state`` down is itself a symlink -- an
    attacker who can plant a symlink in the ``session-state`` directory or
    at ``session-state/<id>`` or at the ``events.jsonl`` leaf must not be
    able to redirect the reader.
    """
    if not is_session_events_path(path):
        return None
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    # After resolving all symlinks, the resolved path must ALSO satisfy the
    # surface check: three trailing parts ending in ``events.jsonl``.
    if not is_session_events_path(resolved):
        return None
    # Neither the ORIGINAL path components below ``session-state`` nor the
    # RESOLVED path components below ``session-state`` may be symlinks. This
    # is the layer that stops a plant-a-symlink attack where the attacker
    # controls one directory in the path and points it at a different tree.
    for candidate_path in (path, resolved):
        try:
            parts = candidate_path.parts
            idx = parts.index("session-state")
        except ValueError:
            return None
        for component_index in range(idx, len(parts)):
            candidate = Path(*parts[: component_index + 1])
            try:
                if candidate.is_symlink():
                    return None
            except OSError:
                return None
    return resolved


# -- timestamp helpers -----------------------------------------------------


_ISO_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})$"
)


def _stamp(value: object) -> str:
    """Return an ISO-8601 timestamp verbatim, or the empty string.

    The pattern is strict so that even a controlled events file cannot use
    the ``timestamp`` field as an allowlisted vehicle for arbitrary text.
    A value that does not match is coerced to the empty string.
    """
    if not isinstance(value, str) or not value:
        return ""
    if len(value) > 40:
        return ""
    if _ISO_TIMESTAMP_PATTERN.match(value):
        return value
    return ""


def _epoch(value: object) -> float:
    """Best-effort epoch seconds for internal sorting. Never leaks upstream."""
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return calendar.timegm(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return 0.0


# -- safe scalar coercion --------------------------------------------------


def _safe_name(value: object) -> str:
    """A tool or command name. Coerce to '' on the smallest doubt."""
    if not isinstance(value, str):
        return ""
    return value if SAFE_NAME_PATTERN.match(value) else ""


def _identifier(value: object) -> str:
    """A registered identifier such as ``agentId`` or ``toolCallId``.

    Empty is a valid value; unlike :func:`_safe_name`, a missing identifier
    is not an error condition -- it means the field simply was not present.
    """
    if not isinstance(value, str) or not value:
        return ""
    return value if IDENTIFIER_PATTERN.match(value) else ""


def _error_code(value: object) -> Optional[str]:
    """An allowlisted machine token for an error class."""
    if isinstance(value, str) and ERROR_CODE_PATTERN.match(value):
        return value
    return None


def _boolean(value: object) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _positive_int(value: object) -> Optional[int]:
    if isinstance(value, bool):  # bool is a subclass of int, but not a duration
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


# -- parsing --------------------------------------------------------------


def _normalise(event: object) -> Optional[NormalisedEvent]:
    """Return an allowlisted normalisation, or ``None`` if this event is unwanted.

    A returned :class:`NormalisedEvent` never carries a value the source
    supplied outside the allowlisted fields. In particular:

      * ``data.arguments``, ``data.result``, ``data.content``,
        ``data.reasoningText``, ``data.reasoningOpaque``,
        ``data.encryptedContent``, ``data.toolTelemetry``,
        ``data.permissionRequest.intention``,
        ``data.permissionRequest.promptRequest``,
        ``data.agentDescription``, ``data.agentName``,
        ``data.agentDisplayName`` and every other unlisted key are read only
        long enough to be ignored.
      * Even ``data.model`` is dropped: it would tell an observer which model
        an agent chose, which is a fingerprinting datum the board does not
        need.
    """
    if not isinstance(event, dict):
        return None
    event_type = event.get("type")
    if not isinstance(event_type, str):
        return None
    if event_type not in NORMALISED_EVENT_TYPES:
        return None
    data = event.get("data")
    if not isinstance(data, dict):
        data = {}
    phase = _PHASE_BY_TYPE.get(event_type, "unknown")
    event_id = _identifier(event.get("id"))
    at = _stamp(event.get("timestamp"))
    agent_id = _identifier(event.get("agentId"))
    tool_call_id = _identifier(data.get("toolCallId"))
    tool_name = _safe_name(data.get("toolName"))
    request_id = _identifier(data.get("requestId"))
    success = _boolean(data.get("success"))
    duration_ms = _positive_int(data.get("durationMs"))
    error_code = None
    if event_type == "tool.execution_complete":
        raw_result = data.get("result")
        # A tool result is not a normalised field; the only thing extracted
        # from it is a safe error CLASS if the tool declared success=False.
        # The result body itself is never inspected beyond that.
        if isinstance(raw_result, dict) and success is False:
            error_code = _error_code(raw_result.get("errorCode"))
    elif event_type == "permission.completed":
        result = data.get("result")
        if isinstance(result, dict):
            outcome = result.get("outcome")
            if isinstance(outcome, str):
                error_code = _error_code(outcome)
    return NormalisedEvent(
        event_id=event_id,
        phase=phase,
        at=at,
        agent_id=agent_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        request_id=request_id,
        success=success,
        error_code=error_code,
        duration_ms=duration_ms,
    )


# -- source reading --------------------------------------------------------


def _iter_lines(chunk: bytes) -> Iterable[bytes]:
    """Split a byte chunk on ``\\n``.

    An incomplete tail (no trailing newline) is dropped -- the caller keeps
    the byte offset at the start of that tail so a later read picks it up
    when it is complete.
    """
    if not chunk:
        return
    parts = chunk.split(b"\n")
    # The last part is either empty (chunk ended with \n) or an incomplete
    # tail. Either way, it is not a whole line.
    for line in parts[:-1]:
        yield line


def _split_complete_tail(chunk: bytes) -> tuple:
    """Return ``(processed_bytes, complete_lines)``.

    ``processed_bytes`` is the number of bytes fully accounted for -- the
    caller advances the cursor by exactly that. Bytes past the last newline
    stay unprocessed.
    """
    if not chunk:
        return (0, [])
    idx = chunk.rfind(b"\n")
    if idx < 0:
        return (0, [])
    processed = idx + 1
    return (processed, list(_iter_lines(chunk[:processed])))


def read_source(
    path: Path,
    *,
    cursor: Optional[Cursor] = None,
    now_iso: str = "",
) -> ReadResult:
    """Read a registered source, returning only allowlisted fields.

    ``cursor`` is the value returned by the previous call. A cursor from a
    different file generation, or one whose offset is past the end, is
    discarded with a ``rotated_or_truncated`` gap.

    ``now_iso`` is the timestamp stamped on any gap this call emits, so a
    test can pin gap timestamps deterministically. When empty the wall clock
    is used.

    Path safety: the strict resolver in :func:`_resolve_session_events_path`
    rejects any path that resolves outside a genuine
    ``session-state/<id>/events.jsonl`` shape, including via symlinks. This
    layer is what stops a misconfigured registration from reaching into a
    file the caller was not supposed to name.
    """
    when = now_iso or _current_iso()
    result = ReadResult(cursor=cursor or Cursor())
    resolved = _resolve_session_events_path(path)
    if resolved is None:
        # The surface path or its resolved target is not a genuine
        # session-state/<id>/events.jsonl; the reader refuses without
        # opening it.
        result.gaps.append(CoverageGap(kind=GAP_SOURCE_READ_ERROR, at=when))
        return result
    generation_info = _peek_generation(resolved)
    if generation_info is None:
        result.gaps.append(CoverageGap(kind=GAP_SOURCE_READ_ERROR, at=when))
        return result
    inode, size = generation_info
    start = 0
    prior_generation = result.cursor.generation
    if prior_generation and prior_generation != inode:
        # A new inode is a rotation. The old cursor is meaningless.
        result.gaps.append(CoverageGap(kind=GAP_ROTATED_OR_TRUNCATED, at=when))
        result.cursor = Cursor(byte_offset=0, generation=inode)
    elif result.cursor.byte_offset > size:
        # Same inode, but the file shrank. Treat as truncation.
        result.gaps.append(CoverageGap(kind=GAP_ROTATED_OR_TRUNCATED, at=when))
        result.cursor = Cursor(byte_offset=0, generation=inode)
    else:
        result.cursor = Cursor(
            byte_offset=result.cursor.byte_offset,
            generation=inode,
            last_event_id=result.cursor.last_event_id,
        )
        start = result.cursor.byte_offset
    first_read = start == 0 and result.cursor.last_event_id == ""
    end = size
    if first_read:
        # The initial tail is a lower bound: give the caller a warmup window.
        start = max(start, end - INITIAL_TAIL_BYTES)
    else:
        # Cap each refresh at REFRESH_BYTES; a source that grew faster than
        # that is a coverage gap the caller must show explicitly.
        if end - start > REFRESH_BYTES:
            result.gaps.append(CoverageGap(kind=GAP_OVERSIZED_SOURCE, at=when))
            start = end - REFRESH_BYTES
    if end <= start:
        return result
    chunk = _read_chunk(resolved, start, end - start)
    if chunk is None:
        result.gaps.append(CoverageGap(kind=GAP_SOURCE_READ_ERROR, at=when))
        return result
    processed_bytes, complete_lines = _split_complete_tail(chunk)
    result.bytes_read = processed_bytes
    line_number = 0
    torn_first = False
    if first_read and start > 0:
        # An initial tail almost never starts on a line boundary. Drop the
        # first partial line without calling it malformed; that is exactly
        # what a "tail" is for.
        if complete_lines:
            complete_lines = complete_lines[1:]
            torn_first = True
    for line in complete_lines:
        line_number += 1
        if len(line) > MAX_LINE_BYTES:
            result.gaps.append(
                CoverageGap(kind=GAP_OVERSIZED_EVENT, at=when, at_line=line_number)
            )
            continue
        if not line.strip():
            continue
        parsed = _decode_line(line)
        if parsed is None:
            result.gaps.append(
                CoverageGap(kind=GAP_MALFORMED_LINE, at=when, at_line=line_number)
            )
            continue
        event_type = parsed.get("type") if isinstance(parsed, dict) else None
        if isinstance(event_type, str) and event_type not in KNOWN_EVENT_TYPES:
            result.gaps.append(
                CoverageGap(kind=GAP_UNKNOWN_EVENT_VERSION, at=when, at_line=line_number)
            )
            continue
        normalised = _normalise(parsed)
        if normalised is None:
            # Known type but not one we emit (e.g. session.info). Ignored on
            # purpose; not a coverage gap.
            continue
        result.events.append(normalised)
    result.cursor = Cursor(
        byte_offset=start + processed_bytes,
        generation=inode,
        last_event_id=(
            result.events[-1].event_id if result.events else result.cursor.last_event_id
        ),
    )
    if torn_first:
        result.gaps.insert(
            0, CoverageGap(kind=GAP_TORN_LINE, at=when, at_line=0)
        )
    return result


def _read_chunk(path: Path, start: int, length: int) -> Optional[bytes]:
    """Read a bounded chunk. ``None`` on any I/O error.

    Opened with ``O_NOFOLLOW`` on platforms that support it: even if the
    resolved path was validated by :func:`_resolve_session_events_path`,
    a race between resolve and open cannot substitute a symlink here.
    """
    if length <= 0:
        return b""
    flags = os.O_RDONLY
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags |= nofollow
    try:
        descriptor = os.open(str(path), flags)
    except OSError:
        return None
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            handle.seek(start)
            return handle.read(length)
    except OSError:
        return None


def _peek_generation(path: Path) -> tuple:
    """Return ``(inode, size)`` or ``None`` on I/O error.

    Uses ``lstat`` so that a symlink introduced after
    :func:`_resolve_session_events_path` validated the path is not silently
    resolved here.
    """
    try:
        info = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        # A regular file is the only shape a session events log should have.
        return None
    return (int(info.st_ino), int(info.st_size))


def _decode_line(line: bytes) -> Optional[dict]:
    """Parse a JSON line, returning ``None`` on any decoding error.

    The line is decoded strictly: an object with duplicate keys is refused,
    because a duplicate would put a value in a field that appears legitimate
    but arrived from a shape the schema does not describe.
    """
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_object_pairs_hook,
        )
    except (ValueError, TypeError):
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def _object_pairs_hook(pairs):
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError("duplicate key")
        seen[key] = value
    return seen


def _current_iso() -> str:
    """UTC ISO timestamp used when the caller does not supply one."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# -- public façade --------------------------------------------------------


def unregistered_gap(now_iso: str = "") -> CoverageGap:
    """The one place a caller may synthesise a gap without calling into the
    reader: when it knows a session is unregistered.

    ``grogu_controlroom`` emits this per uncorrelated stream so the interface
    can say "tools: unavailable" rather than "tools: 0".
    """
    return CoverageGap(kind=GAP_UNREGISTERED_SESSION, at=now_iso or _current_iso())


__all__ = [
    "INITIAL_TAIL_BYTES",
    "REFRESH_BYTES",
    "MAX_LINE_BYTES",
    "MAX_REGISTERED_SOURCES",
    "KNOWN_EVENT_TYPES",
    "NORMALISED_EVENT_TYPES",
    "GAP_TORN_LINE",
    "GAP_MALFORMED_LINE",
    "GAP_OVERSIZED_EVENT",
    "GAP_OVERSIZED_SOURCE",
    "GAP_ROTATED_OR_TRUNCATED",
    "GAP_CURSOR_LOST",
    "GAP_UNKNOWN_EVENT_VERSION",
    "GAP_UNREGISTERED_SESSION",
    "GAP_SOURCE_READ_ERROR",
    "SAFE_NAME_PATTERN",
    "IDENTIFIER_PATTERN",
    "ERROR_CODE_PATTERN",
    "NormalisedEvent",
    "CoverageGap",
    "Cursor",
    "ReadResult",
    "is_session_events_path",
    "read_source",
    "unregistered_gap",
]
