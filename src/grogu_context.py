"""Deterministic, bounded context aggregation.

Aggregation operations reduce the amount of raw data a session has to read by
performing the batching, filtering, and aggregation server-side, in Grogu,
before anything reaches Copilot. Every operation returns a bounded,
machine-readable summary rather than an unbounded dump, and the same inputs
always produce the same output shape so results are safe to cache.

This module intentionally has no dependency on argparse or process state; the
CLI wiring in ``grogu_cli`` is a thin adapter so the aggregations here can be
tested and reused directly.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Optional

import grogu_memory
import grogu_tasks
import grogu_telemetry

SCHEMA_VERSION = 1
MAX_ITEMS = 200
SERVICE_MANIFESTS = (
    "package.json",
    "pyproject.toml",
    "go.mod",
    "Cargo.toml",
)
SERVICE_FIELDS = ("name", "version", "description")


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _bound(limit: int) -> int:
    return max(0, min(int(limit), MAX_ITEMS))


def _signature(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf8")).hexdigest()[:24]


def _envelope(op: str, repository_id: str, data: dict) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "grogu.context_summary",
        "op": op,
        "repository_id": repository_id,
        "generated_at": now(),
        "signature": _signature(data),
        "data": data,
    }


def _run_git(root: Path, arguments: list) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def git_summary(root: Path) -> dict:
    """A bounded Git snapshot: branch, upstream drift, and file counts by role.

    Never returns diff content or file bodies, only counts, so the size of the
    working tree cannot make this operation's output unbounded.
    """
    branch = _run_git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    upstream = _run_git(root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    ahead_behind = {"ahead": 0, "behind": 0}
    if upstream:
        counts = _run_git(root, ["rev-list", "--left-right", "--count", f"{upstream}...HEAD"])
        parts = counts.split()
        if len(parts) == 2 and all(part.isdigit() for part in parts):
            ahead_behind = {"behind": int(parts[0]), "ahead": int(parts[1])}
    status_lines = [
        line for line in _run_git(root, ["status", "--porcelain=v1"]).splitlines() if line
    ]
    by_role: dict = {}
    staged = 0
    unstaged = 0
    untracked = 0
    for line in status_lines:
        code, _, relative = line.partition(" ")
        relative = relative.strip()
        role = grogu_memory._role(relative) if relative else "source"
        by_role[role] = by_role.get(role, 0) + 1
        if code.startswith("??"):
            untracked += 1
        else:
            if code[:1] != " ":
                staged += 1
            if len(code) > 1 and code[1] != " ":
                unstaged += 1
    last_commit = _run_git(root, ["log", "-1", "--format=%H %cI %s"])
    commit_sha, _, remainder = last_commit.partition(" ")
    commit_date, _, subject = remainder.partition(" ")
    return {
        "branch": branch,
        "upstream": upstream or None,
        "ahead": ahead_behind["ahead"],
        "behind": ahead_behind["behind"],
        "changed_files": len(status_lines),
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "changed_by_role": by_role,
        "last_commit": (
            {"sha": commit_sha, "date": commit_date, "subject": subject}
            if commit_sha
            else None
        ),
    }


def graph_context(
    store: grogu_memory.MemoryStore,
    query: str = "",
    node_id: str = "",
    depth: int = 1,
    limit: int = 40,
) -> dict:
    """Bounded knowledge-graph neighborhood, unchanged from ``memory context``.

    Exposed here so a single context call can combine it with other bounded
    aggregations instead of issuing separate, larger requests.
    """
    return store.context(_bound(limit), query=query, node_id=node_id, depth=depth)


def tasks_summary(root: Path, limit: int = 20) -> dict:
    """Bounded task overview: counts by status/label and the most recent items.

    Full task bodies are never included; callers that need one task in full
    should use ``grogu task show``.
    """
    store = grogu_tasks.TaskStore(root)
    tasks = store.list_tasks()
    by_status: dict = {}
    by_label: dict = {}
    for task in tasks:
        status = task.get("status", "unknown")
        by_status[status] = by_status.get(status, 0) + 1
        for label in task.get("labels", []) or []:
            by_label[label] = by_label.get(label, 0) + 1
    recent = sorted(
        tasks, key=lambda task: task.get("updated_at", ""), reverse=True
    )[: _bound(limit)]
    return {
        "total": len(tasks),
        "by_status": by_status,
        "by_label": by_label,
        "recent": [
            {
                "id": task.get("id"),
                "title": task.get("title"),
                "status": task.get("status"),
                "labels": task.get("labels", []),
                "updated_at": task.get("updated_at"),
            }
            for task in recent
        ],
    }


def traces_summary(database) -> dict:
    """Aggregated telemetry counts by event and outcome; no payload content."""
    return {"by_event": grogu_telemetry.summary(database)}


def relationships_summary(edges: list, limit: int = 40) -> dict:
    """Bounded cross-repository relationship counts and a capped edge list."""
    by_kind: dict = {}
    for edge in edges:
        kind = edge.get("kind", "unknown")
        by_kind[kind] = by_kind.get(kind, 0) + 1
    return {
        "total": len(edges),
        "by_kind": by_kind,
        "edges": edges[: _bound(limit)],
    }


def service_metadata(root: Path) -> dict:
    """Deterministic, redacted service identity from top-level manifests only.

    Only well-known top-level fields are read; this never walks nested
    directories or executes package tooling, so it cannot become an unbounded
    or unsafe scan.
    """
    found = []
    for name in SERVICE_MANIFESTS:
        path = root / name
        if not path.is_file():
            continue
        entry = {"manifest": name}
        try:
            if name == "package.json":
                payload = json.loads(path.read_text(encoding="utf8"))
                for field in SERVICE_FIELDS:
                    if field in payload:
                        entry[field] = payload[field]
                entry["scripts"] = sorted((payload.get("scripts") or {}).keys())
            else:
                text = path.read_text(encoding="utf8")
                for field in SERVICE_FIELDS:
                    for line in text.splitlines():
                        stripped = line.strip()
                        if stripped.startswith(f"{field} ") or stripped.startswith(f"{field}="):
                            entry[field] = stripped.split("=", 1)[-1].strip().strip('"')
                            break
        except (OSError, json.JSONDecodeError):
            continue
        found.append(grogu_telemetry.redact(entry))
    return {"manifests": found}


def aggregate(op: str, root: Optional[Path] = None, **params) -> dict:
    """Dispatch a context aggregation by name and wrap it in a stable envelope."""
    resolved_root = grogu_memory.repository_root(root)
    store = grogu_memory.MemoryStore(resolved_root)
    repository_id = store.status().get("manifest", {}).get("repository_id", "")

    if op == "git":
        data = git_summary(resolved_root)
    elif op == "graph":
        data = graph_context(
            store,
            query=params.get("query", ""),
            node_id=params.get("node_id", ""),
            depth=params.get("depth", 1),
            limit=params.get("limit", 40),
        )
    elif op == "tasks":
        data = tasks_summary(resolved_root, limit=params.get("limit", 20))
    elif op == "service":
        data = service_metadata(resolved_root)
    else:
        raise ValueError(f"unknown context operation: {op}")
    return _envelope(op, repository_id, data)
