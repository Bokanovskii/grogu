"""Local, redacted telemetry and improvement evidence for Grogu."""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

SECRET_KEY = re.compile(r"(token|secret|password|authorization|api[_-]?key|cookie)", re.I)
SECRET_VALUE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]+)\b")


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def redact(value: Any, key: str = "") -> Any:
    if SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(name): redact(item, str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return SECRET_VALUE.sub("[REDACTED]", value)
    return value


def initialize(database: sqlite3.Connection) -> None:
    database.executescript(
        """
        CREATE TABLE IF NOT EXISTS telemetry_events (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            event TEXT NOT NULL,
            outcome TEXT,
            repository_id TEXT,
            session_id TEXT,
            task_id TEXT,
            duration_ms INTEGER,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS telemetry_events_created_at_idx
            ON telemetry_events(created_at DESC);
        CREATE INDEX IF NOT EXISTS telemetry_events_event_idx
            ON telemetry_events(event);
        """
    )


def record(
    database: sqlite3.Connection,
    event: str,
    outcome: str = "",
    payload: Any = None,
    repository_id: str = "",
    session_id: str = "",
    task_id: str = "",
    duration_ms: int = None,
) -> dict:
    if not event.strip():
        raise ValueError("event is required")
    value = {
        "id": str(uuid.uuid4()),
        "created_at": now(),
        "event": event.strip(),
        "outcome": outcome or None,
        "repository_id": repository_id or None,
        "session_id": session_id or None,
        "task_id": task_id or None,
        "duration_ms": duration_ms,
        "payload": redact(payload or {}),
    }
    database.execute(
        """
        INSERT INTO telemetry_events(
            id, created_at, event, outcome, repository_id, session_id,
            task_id, duration_ms, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            value["id"],
            value["created_at"],
            value["event"],
            value["outcome"],
            value["repository_id"],
            value["session_id"],
            value["task_id"],
            value["duration_ms"],
            json.dumps(value["payload"], sort_keys=True),
        ),
    )
    return value


def rows(database: sqlite3.Connection, limit: int = 50) -> list:
    result = database.execute(
        """
        SELECT created_at, event, outcome, repository_id, session_id,
               task_id, duration_ms, payload_json
        FROM telemetry_events
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in result]


def summary(database: sqlite3.Connection) -> list:
    result = database.execute(
        """
        SELECT event, COALESCE(outcome, '') AS outcome, COUNT(*) AS count
        FROM telemetry_events
        GROUP BY event, outcome
        ORDER BY count DESC, event, outcome
        """
    ).fetchall()
    return [dict(row) for row in result]
