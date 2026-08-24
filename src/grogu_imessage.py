"""Opt-in, least-privilege access to macOS Messages."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterator, List, Optional, Sequence, Tuple

import grogu_mcp
import grogu_platform

# Name of the local MCP server entry (in ~/.copilot/mcp-config.json) that a
# seaglass installation is expected to register itself under. If a user has
# seaglass installed and configured, `search()` prefers it over the naive
# SQL LIKE scan below -- see README.md's "Messaging skills" section.
SEAGLASS_SERVER_NAME = "seaglass"

# How a message the user sent themselves is labelled; seaglass leaves its
# `sender` null, since there is no contact to resolve.
SELF_HANDLE = "me"

DRAFT_STORAGE_VERSION = 2
SUBMISSION_UNKNOWN = "submission_unknown"
ATTACHMENT_SEND_DELAY_SECONDS = 0.5
IMESSAGE_SEND_SCRIPT = f"""on run argv
    set recipientIdentifier to item 1 of argv
    set messageBody to item 2 of argv
    set attachmentAliases to {{}}
    if (count of argv) > 2 then
        repeat with attachmentPath in items 3 thru -1 of argv
            set attachmentAlias to ((POSIX file (attachmentPath as text)) as alias)
            set end of attachmentAliases to attachmentAlias
        end repeat
    end if
    tell application "Messages"
        set targetService to 1st service whose service type = iMessage
        set targetBuddy to buddy recipientIdentifier of targetService
        send messageBody to targetBuddy
        repeat with attachmentAlias in attachmentAliases
            delay {ATTACHMENT_SEND_DELAY_SECONDS:g}
            send (contents of attachmentAlias) to targetBuddy
        end repeat
    end tell
end run"""


def _remove_private_tree(path: Path) -> None:
    if path.is_symlink():
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    if not path.exists():
        return

    def retry(function, value, _error) -> None:
        try:
            target = Path(value)
            target.chmod(0o700 if target.is_dir() else 0o600)
            function(value)
        except OSError:
            pass

    shutil.rmtree(path, ignore_errors=False, onerror=retry)


# Apple's `message.date` column mixes seconds and nanoseconds since
# 2001-01-01 depending on macOS version at write time -- same ambiguity
# seaglass's own imessage.source.apple_to_unix handles. The SQL LIKE
# fallback below reads this column directly, so it must apply the same
# conversion; otherwise its `date` field (Apple-epoch, either unit) is
# silently inconsistent with seaglass's `date` field (always unix
# seconds), depending on which backend happened to answer (BUG-7).
_APPLE_EPOCH_UNIX = 978307200  # 2001-01-01T00:00:00Z, as unix seconds
_NS_VS_S_THRESHOLD = 1e11


def _apple_date_to_unix(value: Optional[int]) -> Optional[float]:
    if value is None:
        return None
    secs = value / 1e9 if value > _NS_VS_S_THRESHOLD else value
    return secs + _APPLE_EPOCH_UNIX


def seaglass_available() -> bool:
    """Whether a local `seaglass` MCP server is configured for this user.

    Only checks that grogu_mcp can see a matching entry in the MCP config;
    does not start/connect to it (that happens lazily on first real call,
    inside grogu_mcp's own bridge).
    """
    if not grogu_mcp.available():
        return False
    return SEAGLASS_SERVER_NAME in grogu_mcp.load_servers()


def _flatten_seaglass_result(payload: dict, limit: Optional[int] = None) -> List[dict]:
    """Flatten seaglass's ranked session/message payload into the same flat
    `{id, text, date, handle, kind}`-shaped list the SQL LIKE search
    returns, so callers (and existing output formatting) don't need to
    know which backend answered.

    Flattening a ranked page into a flat list and then truncating it is
    lossier than it looks, and side-by-side evaluation against the app
    (seaglass's `behavior --compare`) measured the damage over 208
    queries:

    * **Only the first session survived.** Session order was preserved by
      emitting one session's messages before starting the next, so a
      20-message limit was spent entirely inside session 0 -- which alone
      routinely holds 50+ messages once context is expanded. Eight ranked
      sessions were requested, reranked and hydrated; a mean of 1.7
      reached the caller. Budget is now shared: every session gets a
      quota before any session gets a second helping.
    * **Context was indistinguishable from hits.** Surrounding messages
      seaglass expanded a session with are useful, but they are not
      matches -- they are frequently from the *other* participant, or
      from the user. Presented as results, they dropped sender purity
      from 1.00 to 0.23 and precision from 1.00 to 0.51. Hits now fill
      the budget first and every row is labelled `kind`.
    * **A declared ordering was silently discarded.** When seaglass says
      `ordering: "recent"` -- "recent messages from Kaya" -- it means the
      answer is chronological. Per-session emission scrambled that:
      recency order held for 1% of the queries where it applied. It is
      now honoured explicitly.
    * **The same message could appear twice**, as a hit in one session
      and as context in another (372 rows for 249 distinct messages on
      one live query). Deduplicated, first occurrence winning.

    `limit` is applied here rather than by the caller because only this
    function knows which rows are hits, which sessions they came from,
    and what ordering was declared.
    """
    sessions = payload.get("sessions", [])
    # A message can be a match in one session and mere context in a
    # neighbouring one. Deciding its role by whichever session came first
    # demoted real matches to context, and since hits are spent before
    # context those matches then fell off the end of the limit entirely:
    # six of Vamski's newest twenty went missing that way, each one a hit
    # in the very same answer. A match anywhere is a match.
    matched = {
        message.get("message_id")
        for session in sessions
        for message in session.get("messages", [])
    }
    seen: set = set()
    ranked: List[List[dict]] = []
    for session in sessions:
        rows: List[dict] = []
        for kind, messages in (
            ("hit", session.get("messages", [])),
            ("context", session.get("context_messages", [])),
        ):
            for message in messages:
                identifier = message.get("message_id")
                if identifier in seen or (kind == "context" and identifier in matched):
                    continue
                seen.add(identifier)
                rows.append(
                    {
                        "id": identifier,
                        "text": message.get("text") or "",
                        "date": message.get("ts"),
                        "handle": _sender_handle(message),
                        "kind": kind,
                        "score": message.get("match_score") or 0,
                    }
                )
        if rows:
            ranked.append(rows)

    if not ranked:
        return []
    if payload.get("ordering") == "recent":
        return _newest_first(ranked, limit)
    return _share_budget(ranked, limit)


def _newest_first(ranked: List[List[dict]], limit: Optional[int]) -> List[dict]:
    """"Latest from Vamski" wants the newest messages, not a sample.

    When seaglass declares `ordering: "recent"` the sessions are days and
    the answer is chronological, so sharing the budget across them is
    exactly backwards: it returned two or three messages from each of
    eight days instead of the twenty most recent, and Grogu -- which
    cannot ask for a second page -- had no way back to the rest. Recall
    of the true newest messages sat at 0.65 against the app's 1.00.

    Hits are still spent before context, so the budget is not consumed by
    surrounding conversation, and the result is one contiguous run of the
    newest matches.
    """
    def by_date(rows):
        return sorted(rows, key=lambda row: (row["date"] is not None, row["date"]), reverse=True)

    hits = by_date([r for rows in ranked for r in rows if r["kind"] == "hit"])
    context = by_date([r for rows in ranked for r in rows if r["kind"] == "context"])
    if limit is None:
        return by_date(hits + context)
    chosen = hits[:limit]
    return chosen + context[: max(0, limit - len(chosen))]


def _share_budget(ranked: List[List[dict]], limit: Optional[int]) -> List[dict]:
    """Spread `limit` rows across ranked sessions: breadth first, then depth.

    Every session contributes one row before any session contributes a
    second, so the eighth-ranked conversation still reaches the caller
    rather than being truncated away with the seven above it. What is
    left over then follows the reranker's order -- session 1 takes as
    much of the surplus as it has, then session 2, and so on -- because
    the top session is the best answer and a caller usually wants to read
    it, not sample it.

    Sharing the surplus evenly instead was measurably worse: with eight
    sessions and a twenty-message limit each one got three rows, and
    verbatim phrases sitting at hit 4 and hit 13 of the *top* session --
    exactly where a good match lives -- never shipped.

    Hits are spent before context everywhere, so surrounding conversation
    never displaces a match. Session grouping is preserved in the output,
    so a caller reading top to bottom sees each conversation together.
    """
    chosen = {"hit": [[] for _ in ranked], "context": [[] for _ in ranked]}
    budget = None if limit is None else max(0, limit)
    for kind in ("hit", "context"):
        # A session's `messages` are the whole matched stretch of
        # conversation, and only some of them actually matched --
        # `match_score` is 0 for the rest. Taking them in the order they
        # were sent spent a small limit on whatever the session happened
        # to open with: "what did kaya say about the boat" led with a
        # winking emoji while the message about the boat ranked below it.
        queues = [
            sorted(
                (row for row in rows if row["kind"] == kind),
                key=lambda row: row["score"],
                reverse=True,
            )
            for rows in ranked
        ]
        cursors = [0] * len(ranked)
        target = chosen[kind]
        # Breadth: one row each, in rank order.
        for index, queue in enumerate(queues):
            if budget == 0:
                break
            if queue:
                target[index].append(queue[0])
                cursors[index] = 1
                if budget is not None:
                    budget -= 1
        # Depth: the surplus follows the ranking.
        for index, queue in enumerate(queues):
            while (budget is None or budget > 0) and cursors[index] < len(queue):
                target[index].append(queue[cursors[index]])
                cursors[index] += 1
                if budget is not None:
                    budget -= 1
    # Every session's hits before any session's context. Grouping the
    # output by session instead let session 1's *context* -- often a
    # message the user sent themselves, matching nothing -- outrank
    # session 2's actual match, which is the whole defect this function
    # exists to prevent.
    return [row for rows in chosen["hit"] for row in rows] + [
        row for rows in chosen["context"] for row in rows
    ]


SEAGLASS_SESSIONS = 8
SEAGLASS_MAX_PAGES = 2


def _sender_handle(message: dict) -> str:
    """The sender, as a handle string.

    seaglass reports the user's own messages with a null `sender` (there is
    no contact to resolve), which flattened to an empty handle -- so half a
    conversation looked like it came from nobody, and a caller could not
    tell "sent by the user" apart from "sender unknown". `is_from_me` is
    the field that actually carries that, so use it.
    """
    sender = message.get("sender")
    if sender:
        return sender
    return SELF_HANDLE if message.get("is_from_me") else ""


def search_via_seaglass(query: str, limit: int = 20) -> List[dict]:
    """Search through the configured `seaglass` MCP server and flatten its
    result to this module's plain list-of-dicts shape. Raises whatever
    grogu_mcp.call_tool raises (e.g. RuntimeError/TimeoutError) if the
    server is configured but fails to answer -- callers decide whether to
    fall back to the SQL LIKE search.

    `limit` is a *message* count (matching the SQL LIKE path's semantics
    and this module's own public API), not a *session* count -- seaglass's
    `max_sessions` parameter is sessions, not individual messages
    (typically several messages each). Previously `limit` was passed
    straight through as `max_sessions` (IMPROVEMENT-8), so
    `imessage search --limit 20` (the default) asked seaglass for 20
    *sessions* (~2.5x its own default of 8), overfetching and paying
    unnecessary rerank/hydrate cost.

    A chronological answer -- "latest from Sam" -- is asked again, wider.
    Seaglass orders whole *days*, and eight of them are simply not enough
    to hold the twenty newest messages of a contact who texts in bursts
    across two handles: measured against the true newest twenty, the
    narrow ask reached 0.77 of them, and the messages it dropped were
    matches sitting in days it never requested. Widening is close to free
    because a chronological answer is a filter, not a search: seaglass
    skips the models entirely and answered sixteen days faster than it
    had answered eight.

    Re-asking rather than paging is deliberate. Stitching page two onto
    page one gave a *locally* sorted answer with holes in it, because the
    two pages were ranked separately; one wider request is ordered once,
    globally. A page is still fetched if even the wide answer came back
    short of matches, which is the sparse-contact case paging is actually
    for.

    Relevance answers are neither widened nor paged: page two is by
    definition less relevant than the page the reranker already chose,
    and hydrating sessions nobody asked for is the one cost this path
    cannot make back.
    """
    payload = _seaglass_call(query, sessions=SEAGLASS_SESSIONS)
    _warn_if_stale(payload)
    if not isinstance(payload, dict):
        return []
    if payload.get("ordering") != "recent":
        return _flatten_seaglass_result(payload, limit=limit)

    if payload.get("has_more") and limit > SEAGLASS_SESSIONS:
        wider = _seaglass_call(query, sessions=limit)
        if isinstance(wider, dict):
            payload = wider

    hits: List[dict] = []
    context: List[dict] = []
    seen: set = set()
    pages = 0
    while True:
        for row in _flatten_seaglass_result(payload):
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            (hits if row["kind"] == "hit" else context).append(row)
        pages += 1
        if len(hits) >= limit or not payload.get("has_more") or pages >= SEAGLASS_MAX_PAGES:
            break
        payload = _seaglass_call(
            query,
            sessions=max(SEAGLASS_SESSIONS, limit),
            offset=payload.get("next_offset") or pages * max(SEAGLASS_SESSIONS, limit),
        )
        if not isinstance(payload, dict):
            break

    def newest(rows):
        return sorted(rows, key=lambda row: (row["date"] is not None, row["date"]), reverse=True)

    # Context only ever tops up an answer that has run out of matches.
    # Counting it toward the limit used to declare the answer full and
    # leave real matches unread.
    chosen = newest(hits)[:limit]
    return chosen + newest(context)[: max(0, limit - len(chosen))]


def _seaglass_call(query: str, *, sessions: int, offset: int = 0) -> object:
    kwargs = {"query": query, "max_sessions": sessions}
    if offset:
        kwargs["offset"] = offset
    return grogu_mcp.call_tool(SEAGLASS_SERVER_NAME, "search_messages", **kwargs)


def _warn_if_stale(payload: dict) -> None:
    """Say so when the answer came from an index missing recent messages.

    A stale result is indistinguishable from a complete one, so "what did
    she just say" answers confidently with yesterday's conversation. The
    count rides along in the search payload, so this costs no extra call.

    Silent when seaglass served the gap from chat.db directly. A
    filters-only query needs no models and no ranking, so seaglass answers
    it from the live database and the index being behind costs the answer
    nothing. Warning anyway would send the user off to sync to fix a
    result that is already complete -- and a warning that fires when
    nothing is wrong is one the user learns to scroll past, which is how
    it gets missed on the query where it mattered.
    """
    behind = payload.get("n_messages_since_index") or 0
    if payload.get("unindexed_included"):
        return
    if payload.get("index_stale") and behind:
        print(
            f"warning: seaglass index is {behind} message(s) behind; "
            "run `grogu imessage sync` for the newest messages",
            file=sys.stderr,
        )


def sync_seaglass_index(wait: bool = True) -> dict:
    """Bring the seaglass index up to date with the live Messages db."""
    if not seaglass_available():
        raise IMessageError("seaglass is not configured for this user")
    return grogu_mcp.call_tool(SEAGLASS_SERVER_NAME, "sync_index", wait=wait)


def seaglass_index_status() -> dict:
    """seaglass's own view of its index: size, freshness, and whether the
    live Messages db is readable at all."""
    return grogu_mcp.call_tool(SEAGLASS_SERVER_NAME, "index_status")


class IMessageError(RuntimeError):
    """Base error for unavailable or unsafe iMessage operations."""


class UnsupportedPlatformError(IMessageError):
    """Raised when an operation requires macOS."""


class PermissionError(IMessageError):
    """Raised when macOS privacy permissions block Messages access."""


class ConfirmationRequiredError(IMessageError):
    """Raised when an outbound message lacks explicit confirmation."""


class RecipientResolutionError(IMessageError):
    """Raised when a display name cannot resolve to one direct chat."""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class Recipient:
    identifier: str
    display_name: str = ""


@dataclass(frozen=True)
class MessageAttachment:
    index: int
    filename: str
    source_path: str
    snapshot_path: str
    size_bytes: int
    sha256: str

    def review(self) -> dict:
        return {
            "filename": self.filename,
            "index": self.index,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "source_path": self.source_path,
        }


@dataclass(frozen=True)
class MessageDraft:
    id: str
    recipient: Recipient
    body: str
    created_at: str
    status: str = "draft"
    attachments: Tuple[MessageAttachment, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "attachments", tuple(self.attachments))

    def review(self) -> dict:
        return {
            "attachments": [attachment.review() for attachment in self.attachments],
            "body": self.body,
            "created_at": self.created_at,
            "id": self.id,
            "recipient": {
                "display_name": self.recipient.display_name,
                "identifier": self.recipient.identifier,
            },
            "status": self.status,
        }


@dataclass(frozen=True)
class StagedAttachments:
    directory: Optional[Path] = None
    paths: Tuple[str, ...] = ()

    def discard(self) -> None:
        if self.directory is not None:
            _remove_private_tree(self.directory)


class DraftStore:
    """Persist drafts locally under the user's Grogu home."""

    def __init__(self, home: Optional[Path] = None) -> None:
        root = Path(home or Path.home() / ".grogu").expanduser().resolve()
        self.state_root = root / "imessage"
        self.path = self.state_root / "drafts.jsonl"
        self.lock_path = self.state_root / "drafts.lock"
        self.drafts_root = self.state_root / "drafts"

    def _read(self) -> List[dict]:
        try:
            lines = self.path.read_text(encoding="utf8").splitlines()
        except FileNotFoundError:
            return []
        except OSError as error:
            raise IMessageError("local iMessage drafts could not be read") from error
        entries = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                entries.append(value)
        return entries

    def _write(self, entries: List[dict]) -> None:
        try:
            self._private_directory(self.state_root)
            temporary = self.path.with_name(
                f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            )
            descriptor = os.open(
                temporary,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf8") as handle:
                    handle.write(
                        "".join(
                            json.dumps(entry, sort_keys=True) + "\n"
                            for entry in entries
                        )
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.chmod(0o600)
                os.replace(temporary, self.path)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
        except OSError as error:
            raise IMessageError("local iMessage drafts could not be saved") from error

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._private_directory(self.state_root)
        if self.path.is_symlink() or self.lock_path.is_symlink():
            raise IMessageError("private iMessage storage cannot use a symbolic link")
        try:
            if self.path.exists():
                self.path.chmod(0o600)
            descriptor = os.open(
                self.lock_path,
                os.O_CREAT | os.O_RDWR,
                0o600,
            )
            os.chmod(self.lock_path, 0o600)
        except OSError as error:
            raise IMessageError(
                "local iMessage drafts could not be protected"
            ) from error
        try:
            with grogu_platform.exclusive_lock(descriptor):
                yield
        finally:
            os.close(descriptor)

    def create(
        self,
        recipient: Recipient,
        body: str,
        attachments: Optional[Sequence[str]] = None,
    ) -> MessageDraft:
        if not recipient.identifier.strip():
            raise ValueError("recipient identifier is required")
        if not body.strip():
            raise ValueError("message body is required")
        draft_id = str(uuid.uuid4())
        captured: Tuple[MessageAttachment, ...] = ()
        draft = MessageDraft(
            draft_id,
            recipient,
            body,
            now(),
        )
        try:
            captured = self._capture_attachments(draft_id, attachments or ())
            draft = replace(draft, attachments=captured)
            with self._locked():
                self._write(self._read() + [self._serialize(draft)])
            return draft
        except Exception:
            if captured or (self.drafts_root / draft_id).exists():
                _remove_private_tree(self.drafts_root / draft_id)
            raise

    def get(self, draft_id: str) -> Optional[MessageDraft]:
        with self._locked():
            return self._get(draft_id, self._read())

    def require_draft_status(self, draft: MessageDraft) -> None:
        if draft.status == "draft":
            return
        if draft.status == SUBMISSION_UNKNOWN:
            raise IMessageError(
                f"iMessage draft '{draft.id}' is submission_unknown and cannot "
                "be retried; next: inspect the conversation in Messages, then "
                "create and review a replacement draft only for content still unsent"
            )
        raise IMessageError(
            f"iMessage draft '{draft.id}' is {draft.status} and cannot be retried; "
            "next: create and review a replacement draft"
        )

    def prepare_submission(
        self,
        draft_id: str,
        stage: Callable[[MessageDraft], StagedAttachments],
    ) -> Tuple[MessageDraft, StagedAttachments]:
        staged: Optional[StagedAttachments] = None
        with self._locked():
            entries = self._read()
            draft = self._get(draft_id, entries)
            if draft is None:
                raise ValueError(f"no iMessage draft with id {draft_id!r}")
            self.require_draft_status(draft)
            self._validate_attachments(draft)
            try:
                staged = stage(draft)
                claimed = replace(draft, status=SUBMISSION_UNKNOWN)
                self._replace(entries, claimed)
                self._write(entries)
            except Exception:
                if staged is not None:
                    staged.discard()
                raise
        return draft, staged

    def mark_submitted(self, draft_id: str) -> MessageDraft:
        with self._locked():
            entries = self._read()
            draft = self._get(draft_id, entries)
            if draft is None:
                raise ValueError(f"no iMessage draft with id {draft_id!r}")
            if draft.status != SUBMISSION_UNKNOWN:
                self.require_draft_status(draft)
                raise IMessageError(
                    f"iMessage draft '{draft.id}' has not been claimed for submission"
                )
            submitted = replace(draft, status="submitted")
            self._replace(entries, submitted)
            self._write(entries)
            return submitted

    def _get(
        self, draft_id: str, entries: Sequence[dict]
    ) -> Optional[MessageDraft]:
        for value in entries:
            if value.get("id") == draft_id:
                return self._deserialize(value)
        return None

    def _replace(self, entries: List[dict], draft: MessageDraft) -> None:
        for index, value in enumerate(entries):
            if value.get("id") == draft.id:
                entries[index] = self._serialize(draft)
                return
        raise ValueError(f"no iMessage draft with id {draft.id!r}")

    def _serialize(self, draft: MessageDraft) -> dict:
        return {
            "attachments": [
                {
                    "filename": attachment.filename,
                    "index": attachment.index,
                    "sha256": attachment.sha256,
                    "size_bytes": attachment.size_bytes,
                    "snapshot_path": attachment.snapshot_path,
                    "source_path": attachment.source_path,
                }
                for attachment in draft.attachments
            ],
            "body": draft.body,
            "created_at": draft.created_at,
            "id": draft.id,
            "recipient": {
                "display_name": draft.recipient.display_name,
                "identifier": draft.recipient.identifier,
            },
            "status": draft.status,
            "storage_version": DRAFT_STORAGE_VERSION,
        }

    def _deserialize(self, value: dict) -> MessageDraft:
        draft_id = value.get("id")
        if not isinstance(draft_id, str) or not draft_id:
            raise ValueError("invalid local iMessage draft record")
        raw_attachments = value.get("attachments")
        version = value.get("storage_version")
        if version is None and (raw_attachments is None or raw_attachments == []):
            attachments: Tuple[MessageAttachment, ...] = ()
        elif version != DRAFT_STORAGE_VERSION or not isinstance(
            raw_attachments, list
        ):
            raise self._unsupported_attachment_record(draft_id)
        else:
            if Path(draft_id).name != draft_id or draft_id in {".", ".."}:
                raise self._unsupported_attachment_record(draft_id)
            attachments = tuple(
                self._deserialize_attachment(draft_id, index, attachment)
                for index, attachment in enumerate(raw_attachments, start=1)
            )
        recipient = value.get("recipient")
        if not isinstance(recipient, dict):
            recipient = {}
        return MessageDraft(
            id=draft_id,
            recipient=Recipient(
                str(recipient.get("identifier", "")),
                str(recipient.get("display_name", "")),
            ),
            body=str(value.get("body", "")),
            created_at=str(value.get("created_at", "")),
            status=str(value.get("status", "draft")),
            attachments=attachments,
        )

    def _deserialize_attachment(
        self, draft_id: str, expected_index: int, value: object
    ) -> MessageAttachment:
        if not isinstance(value, dict):
            raise self._unsupported_attachment_record(draft_id)
        index = value.get("index")
        filename = value.get("filename")
        source_path = value.get("source_path")
        snapshot_path = value.get("snapshot_path")
        size_bytes = value.get("size_bytes")
        digest = value.get("sha256")
        expected_snapshot = (
            self.drafts_root
            / draft_id
            / "attachments"
            / f"{expected_index:04d}"
            / str(filename)
        )
        if (
            index != expected_index
            or not isinstance(filename, str)
            or not filename
            or Path(filename).name != filename
            or not isinstance(source_path, str)
            or not Path(source_path).is_absolute()
            or not isinstance(snapshot_path, str)
            or Path(snapshot_path) != expected_snapshot
            or not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 0
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise self._unsupported_attachment_record(draft_id)
        return MessageAttachment(
            index=index,
            filename=filename,
            source_path=source_path,
            snapshot_path=snapshot_path,
            size_bytes=size_bytes,
            sha256=digest,
        )

    @staticmethod
    def _unsupported_attachment_record(draft_id: str) -> ValueError:
        return ValueError(
            f"iMessage draft '{draft_id}' uses unsupported attachment metadata; "
            "create and review a replacement draft"
        )

    def _capture_attachments(
        self, draft_id: str, attachments: Sequence[str]
    ) -> Tuple[MessageAttachment, ...]:
        captured = []
        for index, value in enumerate(attachments, start=1):
            selected = Path(value).expanduser()
            filename = selected.name
            try:
                source = selected.resolve(strict=True)
                source_size, source_digest = self._file_identity(source)
            except (OSError, RuntimeError):
                raise self._invalid_attachment(index, value) from None
            if not source.is_file() or not filename:
                raise self._invalid_attachment(index, value)
            destination = (
                self.drafts_root
                / draft_id
                / "attachments"
                / f"{index:04d}"
                / filename
            )
            try:
                draft_root = self.drafts_root / draft_id
                attachments_root = draft_root / "attachments"
                for directory in (
                    self.state_root,
                    self.drafts_root,
                    draft_root,
                    attachments_root,
                    destination.parent,
                ):
                    self._private_directory(directory)
                snapshot_size, snapshot_digest = self._atomic_copy(
                    source, destination, 0o400
                )
                current_size, current_digest = self._file_identity(source)
            except (OSError, RuntimeError):
                raise self._invalid_attachment(index, value) from None
            if (
                (source_size, source_digest)
                != (snapshot_size, snapshot_digest)
                or (current_size, current_digest)
                != (snapshot_size, snapshot_digest)
            ):
                raise ValueError(
                    f"attachment {index} changed while it was being snapshotted: "
                    f"{value}; no draft was created; next: rerun the same "
                    "`grogu imessage draft` command"
                )
            captured.append(
                MessageAttachment(
                    index=index,
                    filename=filename,
                    source_path=str(source),
                    snapshot_path=str(destination),
                    size_bytes=snapshot_size,
                    sha256=snapshot_digest,
                )
            )
        return tuple(captured)

    @staticmethod
    def _invalid_attachment(index: int, value: str) -> ValueError:
        return ValueError(
            f"attachment {index} is not a readable regular file: {value}; "
            "no draft was created; next: fix the file and rerun the same "
            "`grogu imessage draft` command"
        )

    def _validate_attachments(self, draft: MessageDraft) -> None:
        for attachment in draft.attachments:
            try:
                source_identity = self._canonical_file_identity(
                    Path(attachment.source_path)
                )
            except (OSError, RuntimeError):
                source_identity = None
            expected = (attachment.size_bytes, attachment.sha256)
            if source_identity != expected:
                raise IMessageError(
                    f"iMessage draft '{draft.id}' was not submitted because "
                    f"attachment {attachment.index} no longer matches the reviewed "
                    f"source {attachment.source_path}; no staging or Messages "
                    "action occurred; next: create and review a replacement "
                    "draft, then confirm the new draft id"
                )
            snapshot = Path(attachment.snapshot_path)
            try:
                snapshot_identity = self._canonical_file_identity(snapshot)
            except (OSError, RuntimeError):
                snapshot_identity = None
            if snapshot_identity != expected:
                raise IMessageError(
                    f"iMessage draft '{draft.id}' was not submitted because its "
                    f"immutable snapshot for attachment {attachment.index} is "
                    "missing or changed; no staging or Messages action occurred; "
                    "next: create and review a replacement draft, then confirm "
                    "the new draft id"
                )

    @staticmethod
    def _file_identity(path: Path) -> Tuple[int, str]:
        if path.is_symlink() or not path.is_file():
            raise OSError("not a readable regular file")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
        return size, digest.hexdigest()

    @classmethod
    def _canonical_file_identity(cls, path: Path) -> Tuple[int, str]:
        if path.resolve(strict=True) != path:
            raise OSError("file path is no longer canonical")
        return cls._file_identity(path)

    @classmethod
    def _atomic_copy(
        cls, source: Path, destination: Path, mode: int
    ) -> Tuple[int, str]:
        cls._private_directory(destination.parent)
        temporary = destination.parent / f".snapshot.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(
            temporary,
            os.O_CREAT
            | os.O_EXCL
            | os.O_WRONLY
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as destination_handle, source.open(
                "rb"
            ) as source_handle:
                shutil.copyfileobj(source_handle, destination_handle)
                destination_handle.flush()
                os.fsync(destination_handle.fileno())
            size, digest = cls._file_identity(temporary)
            temporary.chmod(mode)
            os.replace(temporary, destination)
            return size, digest
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _private_directory(path: Path) -> None:
        if path.is_symlink():
            raise IMessageError("private iMessage storage cannot use a symbolic link")
        try:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)
        except OSError as error:
            raise IMessageError(
                "private iMessage storage could not be prepared"
            ) from error
        if path.is_symlink() or not path.is_dir():
            raise IMessageError("private iMessage storage is not a directory")


class MacOSIMessageAdapter:
    """Read Messages' database and send through Apple's Messages app."""

    def __init__(
        self,
        database_path: Optional[Path] = None,
        runner=subprocess.run,
        staging_root: Optional[Path] = None,
    ) -> None:
        self.database_path = Path(
            database_path or Path.home() / "Library" / "Messages" / "chat.db"
        ).expanduser()
        self.runner = runner
        configured_staging_root = (
            staging_root
            or os.environ.get("GROGU_IMESSAGE_STAGING_ROOT")
            or Path.home() / "Library" / "Messages" / ".grogu-send-staging"
        )
        self.staging_root = Path(configured_staging_root).expanduser()

    @staticmethod
    def supported() -> bool:
        return platform.system() == "Darwin"

    def status(self) -> dict:
        if not self.supported():
            return {
                "available": False,
                "platform": platform.system(),
                "reason": "iMessage integration is only supported on macOS",
            }
        if not self.database_path.is_file():
            return {
                "available": False,
                "platform": platform.system(),
                "reason": "Messages database was not found",
                "database": str(self.database_path),
            }
        try:
            with self._connect():
                pass
        except PermissionError as error:
            return {
                "available": False,
                "platform": platform.system(),
                "reason": str(error),
                "database": str(self.database_path),
            }
        status = {
            "available": True,
            "platform": platform.system(),
            "database": str(self.database_path),
            "seaglass": seaglass_available(),
        }
        if status["seaglass"]:
            # Whether the index is current decides whether a search can
            # answer about the last hour at all, so it belongs in status
            # rather than only in a warning nobody asked for.
            try:
                index = seaglass_index_status()
            except Exception as error:  # noqa: BLE001 - status must never fail
                status["seaglass_index"] = {"error": str(error)}
            else:
                status["seaglass_index"] = {
                    key: index.get(key)
                    for key in (
                        "n_chunks",
                        "n_messages_since_index",
                        "stale",
                        "live_chat_readable",
                        "served_by",
                    )
                    if key in index
                }
        return status

    def search(self, query: str, limit: int = 20, use_seaglass: bool = True) -> List[dict]:
        """Search message history for `query`.

        When a `seaglass` MCP server is configured (see
        `seaglass_available()`), it is preferred: it does semantic/ranked
        retrieval rather than a raw substring scan, and returns citable
        message_ids drawn from full conversation context. If seaglass is
        unavailable or its call fails for any reason, this transparently
        falls back to the local SQL LIKE scan below, so `imessage search`
        always returns *something* even on a machine without seaglass set
        up. Pass `use_seaglass=False` to force the SQL LIKE path (e.g. for
        the fallback comparison, or when the caller wants raw substring
        semantics).
        """
        self._require_supported()
        if not query.strip():
            raise ValueError("search query is required")
        if use_seaglass and seaglass_available():
            try:
                return search_via_seaglass(query, limit=limit)
            except Exception as error:
                # Say so. The fallback is a substring scan, which answers a
                # different question entirely: a semantic query returns
                # nothing, and a literal one returns something that looks
                # like it worked. A stale index path in the MCP config
                # degraded every search this way, invisibly, for as long as
                # nobody thought to compare the two backends by hand.
                print(
                    f"grogu: seaglass search failed ({error}); "
                    "falling back to a plain substring scan",
                    file=sys.stderr,
                )
        with self._connect() as database:
            rows = database.execute(
                """
                SELECT message.ROWID AS id, message.text, message.date,
                       handle.id AS handle
                FROM message
                LEFT JOIN handle ON handle.ROWID = message.handle_id
                WHERE message.text LIKE ? OR handle.id LIKE ?
                ORDER BY message.date DESC
                LIMIT ?
                """,
                (f"%{query}%", f"%{query}%", max(0, limit)),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "text": row["text"] or "",
                "date": _apple_date_to_unix(row["date"]),
                "handle": row["handle"] or "",
            }
            for row in rows
        ]

    def resolve_recipient(
        self, requested: str, display_name: str = ""
    ) -> Recipient:
        """Resolve a display name to the handle of one direct conversation.

        Concrete phone numbers and email-style Apple IDs pass through.
        Names are resolved by asking seaglass for that person's recent
        conversation, then mapping the returned chat id to the private
        Messages database. This keeps contact matching in seaglass while
        ensuring drafts persist a real sendable identifier.
        """
        requested = requested.strip()
        if not requested:
            raise ValueError("recipient identifier is required")
        if self._looks_like_handle(requested):
            return Recipient(requested, display_name.strip())

        self._require_supported()
        if not seaglass_available():
            raise RecipientResolutionError(
                "recipient names require a configured seaglass server; "
                "otherwise pass an iMessage phone number or Apple ID"
            )
        try:
            payload = _seaglass_call(
                f"latest messages from {requested}", sessions=SEAGLASS_SESSIONS
            )
        except Exception as error:
            raise RecipientResolutionError(
                f"seaglass could not resolve recipient {requested!r}: {error}"
            ) from error
        if not isinstance(payload, dict):
            raise RecipientResolutionError(
                f"seaglass could not resolve recipient {requested!r}"
            )

        matches = {}
        for session in payload.get("sessions", []):
            chat_id = session.get("chat_id")
            if not isinstance(chat_id, int):
                continue
            names = {
                message.get("sender", "").strip()
                for message in session.get("messages", [])
                if not message.get("is_from_me")
                and isinstance(message.get("sender"), str)
                and message.get("sender", "").strip()
            }
            if names:
                matches.setdefault(chat_id, set()).update(names)

        if not matches:
            raise RecipientResolutionError(
                f"no iMessage conversation resolved for {requested!r}"
            )
        if len(matches) != 1:
            names = sorted({name for values in matches.values() for name in values})
            detail = f": {', '.join(names)}" if names else ""
            raise RecipientResolutionError(
                f"recipient {requested!r} is ambiguous{detail}"
            )

        chat_id, names = next(iter(matches.items()))
        handles = self._chat_handles(chat_id)
        if len(handles) != 1:
            if handles:
                raise RecipientResolutionError(
                    f"recipient {requested!r} resolved to a group conversation"
                )
            raise RecipientResolutionError(
                f"recipient {requested!r} has no sendable iMessage handle"
            )
        resolved_name = display_name.strip() or sorted(names)[0]
        return Recipient(handles[0], resolved_name)

    def send(
        self,
        recipient: Recipient,
        body: str,
        confirmed: bool = False,
        attachments: Optional[Sequence[str]] = None,
    ) -> dict:
        self.validate_submission(recipient, body, confirmed=confirmed)
        staged_paths = self._validated_staged_paths(attachments or ())
        completed = self.runner(
            [
                "osascript",
                "-e",
                IMESSAGE_SEND_SCRIPT,
                "--",
                recipient.identifier,
                body,
                *staged_paths,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise IMessageError("Messages could not accept the iMessage submission")
        result = {
            "submitted": True,
            "delivery_confirmed": False,
            "recipient": recipient.identifier,
        }
        if staged_paths:
            result["attachment_count"] = len(staged_paths)
        return result

    def validate_submission(
        self,
        recipient: Recipient,
        body: str,
        confirmed: bool = False,
    ) -> None:
        if not confirmed:
            raise ConfirmationRequiredError(
                "sending an iMessage requires explicit confirmation"
            )
        self._require_supported()
        if not recipient.identifier.strip():
            raise ValueError("recipient identifier is required")
        if not self._looks_like_handle(recipient.identifier):
            raise RecipientResolutionError(
                "draft recipient is not a resolved iMessage phone number or Apple ID; "
                "create a replacement draft"
            )
        if not body.strip():
            raise ValueError("message body is required")

    def stage_attachments(self, draft: MessageDraft) -> StagedAttachments:
        if not draft.attachments:
            return StagedAttachments()
        root = self._prepare_staging_root(draft.id)
        attempt = root / uuid.uuid4().hex
        staged_paths = []
        try:
            try:
                self._private_staging_directory(attempt, root)
            except Exception as error:
                raise self._staging_root_error(draft.id) from error
            for attachment in draft.attachments:
                try:
                    directory = attempt / f"{attachment.index:04d}"
                    self._private_staging_directory(directory, root)
                    destination = directory / attachment.filename
                    if (
                        destination.parent != directory
                        or destination.name != attachment.filename
                    ):
                        raise ValueError("attachment filename escaped staging")
                    snapshot = Path(attachment.snapshot_path)
                    size, digest = DraftStore._atomic_copy(
                        snapshot, destination, 0o600
                    )
                    if (size, digest) != (
                        attachment.size_bytes,
                        attachment.sha256,
                    ):
                        raise ValueError("staged attachment identity changed")
                    self._validate_private_staged_file(destination, root)
                except Exception as error:
                    raise self._staging_copy_error(
                        draft.id, attachment.index
                    ) from error
                staged_paths.append(str(destination))
        except Exception:
            _remove_private_tree(attempt)
            raise
        return StagedAttachments(attempt, tuple(staged_paths))

    def _prepare_staging_root(self, draft_id: str) -> Path:
        if self.staging_root.is_symlink():
            raise self._staging_root_error(draft_id)
        try:
            try:
                self.staging_root.mkdir(parents=True, mode=0o700)
            except FileExistsError:
                created = False
            else:
                created = True
            if created:
                self.staging_root.chmod(0o700)
        except (OSError, RuntimeError) as error:
            raise self._staging_root_error(draft_id) from error
        if not self._is_private_directory(self.staging_root):
            raise self._staging_root_error(draft_id)
        try:
            return self.staging_root.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise self._staging_root_error(draft_id) from error

    def _validated_staged_paths(
        self, attachments: Sequence[str]
    ) -> Tuple[str, ...]:
        if not attachments:
            return ()
        if not self._is_private_directory(self.staging_root):
            raise IMessageError("the private iMessage staging root is unavailable")
        try:
            root = self.staging_root.resolve(strict=True)
        except (OSError, RuntimeError):
            raise IMessageError(
                "the private iMessage staging root is unavailable"
            ) from None
        staged_paths = []
        for value in attachments:
            candidate = Path(value)
            try:
                resolved = candidate.resolve(strict=True)
                relative = resolved.relative_to(root)
            except (OSError, ValueError, RuntimeError):
                raise IMessageError(
                    "a staged iMessage attachment escaped its private root"
                ) from None
            if (
                candidate != resolved
                or len(relative.parts) != 3
                or not self._is_private_directory(resolved.parent)
                or not self._is_private_directory(resolved.parent.parent)
                or not self._is_private_file(candidate, 0o600)
            ):
                raise IMessageError(
                    "a staged iMessage attachment is unavailable or not private"
                )
            staged_paths.append(str(resolved))
        return tuple(staged_paths)

    @classmethod
    def _private_staging_directory(cls, path: Path, root: Path) -> None:
        path.mkdir(mode=0o700)
        path.chmod(0o700)
        resolved = path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            _remove_private_tree(path)
            raise IMessageError(
                "an iMessage attachment staging path escaped its private root"
            ) from None
        if path != resolved or path.is_symlink() or not path.is_dir():
            raise IMessageError(
                "an iMessage attachment staging path is not a private directory"
            )
        if not cls._is_private_directory(path):
            raise IMessageError(
                "an iMessage attachment staging path is not a private directory"
            )

    @classmethod
    def _validate_private_staged_file(cls, path: Path, root: Path) -> None:
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError, RuntimeError):
            raise IMessageError(
                "an iMessage attachment staging path escaped its private root"
            ) from None
        if path != resolved or not cls._is_private_file(path, 0o600):
            raise IMessageError(
                "an iMessage attachment staging file is not private"
            )

    @classmethod
    def _is_private_directory(cls, path: Path) -> bool:
        try:
            metadata = path.stat()
        except OSError:
            return False
        return (
            not path.is_symlink()
            and path.is_dir()
            and os.access(
                path,
                os.W_OK if os.name == "nt" else os.W_OK | os.X_OK,
            )
            and (
                os.name == "nt"
                or (
                    stat.S_IMODE(metadata.st_mode) == 0o700
                    and cls._owned_by_current_user(metadata.st_uid)
                )
            )
        )

    @classmethod
    def _is_private_file(cls, path: Path, mode: int) -> bool:
        try:
            metadata = path.stat()
        except OSError:
            return False
        return (
            not path.is_symlink()
            and path.is_file()
            and (
                os.name == "nt"
                or (
                    stat.S_IMODE(metadata.st_mode) == mode
                    and cls._owned_by_current_user(metadata.st_uid)
                )
            )
        )

    @staticmethod
    def _owned_by_current_user(owner: int) -> bool:
        return not hasattr(os, "geteuid") or owner == os.geteuid()

    def _staging_root_error(self, draft_id: str) -> IMessageError:
        return IMessageError(
            f"iMessage draft '{draft_id}' was not submitted because "
            "GROGU_IMESSAGE_STAGING_ROOT is not a private writable directory: "
            f"{self.staging_root}; no Messages action occurred; next: run "
            '`GROGU_IMESSAGE_STAGING_ROOT="<private-messages-root>" grogu '
            f"imessage send {draft_id} --confirm`"
        )

    @staticmethod
    def _staging_copy_error(draft_id: str, index: int) -> IMessageError:
        return IMessageError(
            f"iMessage draft '{draft_id}' was not submitted because attachment "
            f"{index} could not be staged and verified; no Messages action "
            "occurred and incomplete staging was removed; next: fix "
            "GROGU_IMESSAGE_STAGING_ROOT, then rerun `grogu imessage send "
            f"{draft_id} --confirm`"
        )

    def _chat_handles(self, chat_id: int) -> List[str]:
        with self._connect() as database:
            rows = database.execute(
                """
                SELECT DISTINCT handle.id
                FROM chat_handle_join
                JOIN handle ON handle.ROWID = chat_handle_join.handle_id
                WHERE chat_handle_join.chat_id = ?
                ORDER BY handle.id
                """,
                (chat_id,),
            ).fetchall()
        return [row["id"] for row in rows if row["id"]]

    def _connect(self) -> sqlite3.Connection:
        try:
            database = sqlite3.connect(
                f"file:{self.database_path}?mode=ro", uri=True, timeout=2
            )
            database.row_factory = sqlite3.Row
            database.execute("SELECT 1 FROM message LIMIT 1")
            return database
        except (OSError, sqlite3.Error) as error:
            raise PermissionError(
                "Messages access is unavailable; grant Full Disk Access to Grogu"
            ) from error

    def _require_supported(self) -> None:
        if not self.supported():
            raise UnsupportedPlatformError(
                "iMessage integration is only supported on macOS"
            )

    @staticmethod
    def _looks_like_handle(value: str) -> bool:
        value = value.strip()
        if "@" in value and not any(character.isspace() for character in value):
            return True
        digits = re.sub(r"\D", "", value)
        return len(digits) >= 7 and re.fullmatch(r"[+\d().\-\s]+", value) is not None
