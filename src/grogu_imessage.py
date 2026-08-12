"""Opt-in, least-privilege access to macOS Messages."""

from __future__ import annotations

import datetime as dt
import json
import os
import platform
import sqlite3
import subprocess
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional

import grogu_mcp

# Name of the local MCP server entry (in ~/.copilot/mcp-config.json) that a
# seaglass installation is expected to register itself under. If a user has
# seaglass installed and configured, `search()` prefers it over the naive
# SQL LIKE scan below -- see README.md's "Messaging skills" section.
SEAGLASS_SERVER_NAME = "seaglass"


def seaglass_available() -> bool:
    """Whether a local `seaglass` MCP server is configured for this user.

    Only checks that grogu_mcp can see a matching entry in the MCP config;
    does not start/connect to it (that happens lazily on first real call,
    inside grogu_mcp's own bridge).
    """
    if not grogu_mcp.available():
        return False
    return SEAGLASS_SERVER_NAME in grogu_mcp.load_servers()


def _flatten_seaglass_result(payload: dict) -> List[dict]:
    """Flatten seaglass's ranked session/message payload into the same flat
    `{id, text, date, handle}`-shaped list the SQL LIKE search returns, so
    callers (and existing output formatting) don't need to know which
    backend answered. Session order (best-first, per seaglass's rerank
    score) is preserved; messages within a session keep their own order.
    """
    flattened: List[dict] = []
    for session in payload.get("sessions", []):
        for message in session.get("messages", []):
            flattened.append(
                {
                    "id": message.get("message_id"),
                    "text": message.get("text") or "",
                    "date": message.get("ts"),
                    "handle": message.get("sender") or "",
                }
            )
    return flattened


def search_via_seaglass(query: str, limit: int = 20) -> List[dict]:
    """Search through the configured `seaglass` MCP server and flatten its
    result to this module's plain list-of-dicts shape. Raises whatever
    grogu_mcp.call_tool raises (e.g. RuntimeError/TimeoutError) if the
    server is configured but fails to answer -- callers decide whether to
    fall back to the SQL LIKE search.
    """
    payload = grogu_mcp.call_tool(
        SEAGLASS_SERVER_NAME, "search_messages", query=query, max_sessions=limit
    )
    return _flatten_seaglass_result(payload)[:limit]


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
        return {
            "available": True,
            "platform": platform.system(),
            "database": str(self.database_path),
            "seaglass": seaglass_available(),
        }

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
            except Exception:
                pass  # fall through to the local SQL LIKE scan below
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
                "date": row["date"],
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
