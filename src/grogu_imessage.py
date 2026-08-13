"""Opt-in, least-privilege access to macOS Messages."""

from __future__ import annotations

import datetime as dt
import json
import os
import platform
import sqlite3
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import grogu_mcp

# Name of the local MCP server entry (in ~/.copilot/mcp-config.json) that a
# seaglass installation is expected to register itself under. If a user has
# seaglass installed and configured, `search()` prefers it over the naive
# SQL LIKE scan below -- see README.md's "Messaging skills" section.
SEAGLASS_SERVER_NAME = "seaglass"

# How a message the user sent themselves is labelled; seaglass leaves its
# `sender` null, since there is no contact to resolve.
SELF_HANDLE = "me"

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
    """
    behind = payload.get("n_messages_since_index") or 0
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


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class Recipient:
    identifier: str
    display_name: str = ""


@dataclass
class MessageDraft:
    id: str
    recipient: Recipient
    body: str
    created_at: str
    status: str = "draft"


class DraftStore:
    """Persist drafts locally under the user's Grogu home."""

    def __init__(self, home: Optional[Path] = None) -> None:
        root = Path(home or Path.home() / ".grogu").expanduser()
        self.path = root / "imessage" / "drafts.jsonl"

    def _read(self) -> List[dict]:
        try:
            lines = self.path.read_text(encoding="utf8").splitlines()
        except OSError:
            return []
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            "".join(json.dumps(entry, sort_keys=True) + "\n" for entry in entries),
            encoding="utf8",
        )
        os.replace(temporary, self.path)

    def create(self, recipient: Recipient, body: str) -> MessageDraft:
        if not recipient.identifier.strip():
            raise ValueError("recipient identifier is required")
        if not body.strip():
            raise ValueError("message body is required")
        draft = MessageDraft(str(uuid.uuid4()), recipient, body, now())
        self._write(self._read() + [asdict(draft)])
        return draft

    def get(self, draft_id: str) -> Optional[MessageDraft]:
        for value in self._read():
            if value.get("id") != draft_id:
                continue
            recipient = value.get("recipient", {})
            return MessageDraft(
                value["id"],
                Recipient(
                    recipient.get("identifier", ""),
                    recipient.get("display_name", ""),
                ),
                value.get("body", ""),
                value.get("created_at", ""),
                value.get("status", "draft"),
            )
        return None

    def mark_sent(self, draft_id: str) -> MessageDraft:
        entries = self._read()
        for value in entries:
            if value.get("id") == draft_id:
                value["status"] = "sent"
                self._write(entries)
                result = self.get(draft_id)
                if result is not None:
                    return result
        raise ValueError(f"no iMessage draft with id {draft_id!r}")


class MacOSIMessageAdapter:
    """Read Messages' database and send through Apple's Messages app."""

    def __init__(
        self,
        database_path: Optional[Path] = None,
        runner=subprocess.run,
    ) -> None:
        self.database_path = Path(
            database_path or Path.home() / "Library" / "Messages" / "chat.db"
        ).expanduser()
        self.runner = runner

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

    def send(self, recipient: Recipient, body: str, confirmed: bool = False) -> dict:
        self._require_supported()
        if not confirmed:
            raise ConfirmationRequiredError(
                "sending an iMessage requires explicit confirmation"
            )
        if not recipient.identifier.strip():
            raise ValueError("recipient identifier is required")
        if not body.strip():
            raise ValueError("message body is required")
        script = (
            'tell application "Messages"\n'
            'set targetService to 1st service whose service type = iMessage\n'
            f'set targetBuddy to buddy "{self._escape(recipient.identifier)}" of targetService\n'
            f'send "{self._escape(body)}" to targetBuddy\n'
            "end tell"
        )
        completed = self.runner(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise IMessageError("Messages could not send the message" + (f": {detail}" if detail else ""))
        return {"sent": True, "recipient": recipient.identifier}

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
    def _escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')
