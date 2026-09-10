#!/usr/bin/env python3
"""Lightweight Grogu launcher and local state utilities."""

from __future__ import annotations

import argparse
import copy
import dataclasses
import datetime as dt
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import grogu_banner
import grogu_capabilities
import grogu_codemode
import grogu_context
import grogu_design
import grogu_mcp
import grogu_memory
import grogu_personal_memory
import grogu_platform
import grogu_plan_server
import grogu_plans
import grogu_plandoc
import grogu_plandoc_canon
import grogu_plandoc_patch
import grogu_plandoc_schema
import grogu_privacy
import grogu_review
import grogu_review_server
import grogu_skills
import grogu_telemetry
import grogu_tasks
import grogu_watch
import grogu_worktrees

VERSION = "0.1.0"
MIN_PYTHON = (3, 10)
ROOT = Path(__file__).resolve().parent.parent
GROGU_HOME = Path(os.environ.get("GROGU_HOME", Path.home() / ".grogu"))
TRACE_DB = GROGU_HOME / "traces.db"
CATALOG_DB = GROGU_HOME / "catalog.db"
TAB_COLOR = (30, 118, 58)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(path, timeout=10)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA journal_mode=WAL")
    database.execute("PRAGMA busy_timeout=10000")
    return database


def initialize_trace_db() -> None:
    with connect(TRACE_DB) as database:
        database.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT OR IGNORE INTO schema_metadata(key, value)
                VALUES ('schema_version', '1');
            CREATE TABLE IF NOT EXISTS traces (
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
            CREATE INDEX IF NOT EXISTS traces_created_at_idx
                ON traces(created_at DESC);
            CREATE INDEX IF NOT EXISTS traces_run_id_idx
                ON traces(run_id);
            """
        )
        grogu_telemetry.initialize(database)


def initialize_catalog_db() -> None:
    with connect(CATALOG_DB) as database:
        database.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT OR IGNORE INTO schema_metadata(key, value)
                VALUES ('schema_version', '1');
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                slug TEXT NOT NULL UNIQUE,
                path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS projects_path_idx ON projects(path);
            CREATE TABLE IF NOT EXISTS project_relationships (
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source_id, target_id, kind)
            );
            CREATE TABLE IF NOT EXISTS repositories (
                repository_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                path TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS repositories_path_idx ON repositories(path);
            """
        )


def initialize_state() -> None:
    initialize_trace_db()
    initialize_catalog_db()


# Set before dispatch, cleared by whoever delivers it. A notice that only ever
# went to stderr was dropped by agent harnesses that capture stdout alone, and a
# steering note the agent never sees was not delivered.
_PENDING_NOTICE = ""


def print_json(value: object) -> None:
    """Print a JSON payload, carrying any pending notice inside it.

    An agent that only ever calls `--json` commands has no other channel: a
    trailer appended after the document would break the parse it is asking for,
    so the notice becomes a `grogu_notice` field of the document instead. It can
    appear on any JSON object Grogu prints — that is the delivery mechanism, not
    a quirk of the plan commands, and every JSON payload is therefore an object
    rather than a bare array so that there is always somewhere to put it.
    """
    global _PENDING_NOTICE
    if _PENDING_NOTICE and isinstance(value, dict) and "grogu_notice" not in value:
        value = dict(value, grogu_notice=_PENDING_NOTICE)
        _PENDING_NOTICE = ""
        grogu_plans.mark_delivered()
    print(json.dumps(value, indent=2, sort_keys=True))


def doctor(_: argparse.Namespace) -> int:
    initialize_state()
    copilot = shutil.which("copilot")
    instructions = ROOT / ".github" / "AGENTS.md"
    settings = grogu_banner.settings_path()
    store = grogu_tasks.TaskStore()
    try:
        stale = grogu_worktrees.stale_worktrees(ROOT)
        stale_worktree_paths = [str(entry.worktree.path) for entry in stale]
    except Exception:  # pragma: no cover - best-effort diagnostic only
        stale_worktree_paths = []
    try:
        main_behind_origin = grogu_worktrees.main_behind_origin(ROOT)
    except Exception:  # pragma: no cover - best-effort diagnostic only
        main_behind_origin = None
    try:
        capability_repositories = grogu_capabilities.CapabilityStore(
            GROGU_HOME
        ).list()
        capability_error = ""
    except grogu_capabilities.CapabilityError as error:
        capability_repositories = []
        capability_error = str(error)
    checks = {
        "python": sys.version.split()[0],
        "python_min_required": ".".join(str(part) for part in MIN_PYTHON),
        "python_meets_minimum": sys.version_info >= MIN_PYTHON,
        "mcp_available": grogu_mcp.available(),
        "grogu_version": VERSION,
        "copilot_path": copilot,
        "copilot_available": copilot is not None,
        "instructions": str(instructions),
        "instructions_available": instructions.is_file(),
        "trace_db": str(TRACE_DB),
        "catalog_db": str(CATALOG_DB),
        "personal_memory_dir": str(grogu_personal_memory.PersonalMemoryStore(GROGU_HOME).directory),
        "capability_store": str(GROGU_HOME / "capabilities.json"),
        "capability_repositories": capability_repositories,
        "capability_config_valid": not capability_error,
        "capability_error": capability_error,
        "azure_enabled": os.environ.get("GROGU_AZURE", "0") == "1",
        "autopilot_default": autopilot_default_enabled(),
        "banner_enabled": banner_enabled(),
        "banner_settings": str(settings),
        "banner_settings_writable": os.access(
            settings if settings.exists() else settings.parent, os.W_OK
        ),
        "task_store": str(store.tasks_dir),
        "open_tasks": sum(
            1
            for task in store.list_tasks()
            if task["status"] not in grogu_tasks.CLOSED_STATUSES
        ),
        "pending_updates": store.pending_counts(),
        "stale_worktrees": stale_worktree_paths,
        "main_behind_origin": main_behind_origin,
    }
    print_json(checks)
    return 0 if (
        checks["copilot_available"]
        and checks["instructions_available"]
        and checks["capability_config_valid"]
    ) else 1


def trace_record(args: argparse.Namespace) -> int:
    initialize_trace_db()
    payload = json.loads(args.payload) if args.payload else {}
    with connect(TRACE_DB) as database:
        database.execute(
            """
            INSERT INTO traces(
                id, run_id, created_at, kind, provider, model, status,
                duration_ms, input_tokens, output_tokens,
                estimated_cost_usd, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                args.run_id or str(uuid.uuid4()),
                now(),
                args.kind,
                args.provider,
                args.model,
                args.status,
                args.duration_ms,
                args.input_tokens,
                args.output_tokens,
                args.estimated_cost_usd,
                json.dumps(payload, sort_keys=True),
            ),
        )
    return 0


def trace_list(args: argparse.Namespace) -> int:
    initialize_trace_db()
    with connect(TRACE_DB) as database:
        rows = database.execute(
            """
            SELECT created_at, kind, provider, model, status, duration_ms,
                   input_tokens, output_tokens, estimated_cost_usd, run_id
            FROM traces
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
    for row in rows:
        print(json.dumps(dict(row), sort_keys=True))
    return 0


def trace_failures(_: argparse.Namespace) -> int:
    initialize_trace_db()
    with connect(TRACE_DB) as database:
        rows = database.execute(
            """
            SELECT created_at, kind, provider, model, status, run_id
            FROM traces
            WHERE status IN ('error', 'failed', 'timeout')
            ORDER BY created_at DESC
            """
        ).fetchall()
    for row in rows:
        print(json.dumps(dict(row), sort_keys=True))
    return 0


def slugify(value: str) -> str:
    slug = "".join(char.lower() if char.isalnum() else "-" for char in value)
    return "-".join(part for part in slug.split("-") if part)


def project_init(args: argparse.Namespace) -> int:
    initialize_catalog_db()
    path = Path(args.path).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    project_dir = path / ".grogu"
    project_dir.mkdir(exist_ok=True)
    slug = slugify(args.name)
    manifest_path = project_dir / "project.json"
    if manifest_path.exists() and not args.force:
        print(f"refusing to overwrite {manifest_path}; use --force", file=sys.stderr)
        return 2
    repository_manifest = grogu_memory.MemoryStore(path).initialize(args.name)
    manifest = {
        "schema_version": 1,
        "project_id": str(uuid.uuid4()),
        "name": args.name,
        "slug": slug,
        "repository_id": repository_manifest["repository_id"],
        "created_at": now(),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    timestamp = now()
    with connect(CATALOG_DB) as database:
        database.execute(
            """
            INSERT INTO projects(id, name, slug, path, created_at, updated_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                name=excluded.name, path=excluded.path,
                updated_at=excluded.updated_at, metadata_json=excluded.metadata_json
            """,
            (
                manifest["project_id"],
                args.name,
                slug,
                str(path),
                timestamp,
                timestamp,
                json.dumps(manifest, sort_keys=True),
            ),
        )
    register_repository(repository_manifest, path)
    print(manifest_path)
    return 0


def project_list(_: argparse.Namespace) -> int:
    initialize_catalog_db()
    with connect(CATALOG_DB) as database:
        rows = database.execute(
            "SELECT name, slug, path, updated_at FROM projects ORDER BY name"
        ).fetchall()
    for row in rows:
        print(json.dumps(dict(row), sort_keys=True))


def project_relate(args: argparse.Namespace) -> int:
    initialize_catalog_db()
    timestamp = now()
    evidence = json.loads(args.evidence) if args.evidence else {}
    with connect(CATALOG_DB) as database:
        database.execute(
            """
            INSERT INTO project_relationships(
                source_id, target_id, kind, evidence_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, target_id, kind) DO UPDATE SET
                evidence_json=excluded.evidence_json,
                updated_at=excluded.updated_at
            """,
            (
                args.source,
                args.target,
                args.kind,
                json.dumps(evidence, sort_keys=True),
                timestamp,
                timestamp,
            ),
        )
    print_json(
        {
            "source_id": args.source,
            "target_id": args.target,
            "kind": args.kind,
            "evidence": evidence,
            "updated_at": timestamp,
        }
    )
    return 0


def project_graph(_: argparse.Namespace) -> int:
    initialize_catalog_db()
    with connect(CATALOG_DB) as database:
        rows = database.execute(
            """
            SELECT source_id, target_id, kind, evidence_json, updated_at
            FROM project_relationships
            ORDER BY source_id, target_id, kind
            """
        ).fetchall()
    for row in rows:
        value = dict(row)
        value["evidence"] = json.loads(value.pop("evidence_json"))
        print(json.dumps(value, sort_keys=True))
    return 0


def register_repository(manifest: dict, path: Path) -> None:
    initialize_catalog_db()
    with connect(CATALOG_DB) as database:
        database.execute(
            """
            INSERT INTO repositories(repository_id, name, path, updated_at, metadata_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(repository_id) DO UPDATE SET
                name=excluded.name, path=excluded.path,
                updated_at=excluded.updated_at, metadata_json=excluded.metadata_json
            """,
            (
                manifest["repository_id"],
                manifest["name"],
                str(path),
                now(),
                json.dumps(manifest, sort_keys=True),
            ),
        )


def related_repository_context(repository_id: str, limit: int) -> list:
    initialize_catalog_db()
    with connect(CATALOG_DB) as database:
        relationships = database.execute(
            """
            SELECT source_id, target_id, kind
            FROM project_relationships
            WHERE source_id = ? OR target_id = ?
            ORDER BY source_id, target_id, kind
            """,
            (repository_id, repository_id),
        ).fetchall()
        related_ids = {
            row["target_id"] if row["source_id"] == repository_id else row["source_id"]
            for row in relationships
        }
        result = []
        for related_id in sorted(related_ids):
            row = database.execute(
                "SELECT path, name FROM repositories WHERE repository_id = ?",
                (related_id,),
            ).fetchone()
            if not row:
                continue
            store = grogu_memory.MemoryStore(Path(row["path"]))
            result.append(
                {
                    "repository_id": related_id,
                    "name": row["name"],
                    "context": store.context(limit=limit),
                    "relationships": [
                        {
                            "source": item["source_id"],
                            "target": item["target_id"],
                            "kind": item["kind"],
                        }
                        for item in relationships
                        if item["source_id"] == related_id or item["target_id"] == related_id
                    ],
                }
            )
        return result


def memory_store(args: argparse.Namespace) -> grogu_memory.MemoryStore:
    return grogu_memory.MemoryStore(
        Path(args.repo).expanduser() if getattr(args, "repo", None) else None
    )


def memory_index(args: argparse.Namespace) -> int:
    store = memory_store(args)
    result = store.index()
    register_repository(result["manifest"], store.root)
    initialize_trace_db()
    with connect(TRACE_DB) as database:
        grogu_telemetry.record(
            database,
            event="memory_index",
            outcome="ok",
            repository_id=result["manifest"]["repository_id"],
            payload={
                "changed": len(result["changed"]),
                "removed": len(result["removed"]),
                "summary": result["index"]["summary"],
            },
        )
    print_json(
        {
            "repository": result["manifest"],
            "index": result["index"]["summary"],
            "changed": result["changed"],
            "removed": result["removed"],
        }
    )
    return 0


def memory_status(args: argparse.Namespace) -> int:
    print_json(memory_store(args).status())
    return 0


def memory_context(args: argparse.Namespace) -> int:
    store = memory_store(args)
    result = store.context(
        args.limit, query=args.query, node_id=args.node, depth=args.depth
    )
    if args.related:
        result["related_repositories"] = related_repository_context(
            result["repository"]["repository_id"], args.limit
        )
    if not result.get("nodes") and not result.get("edges"):
        result["note"] = (
            "nothing recorded for this repository yet; an empty graph and an "
            "unindexed one look the same, so write to it with `grogu memory "
            "remember` before reading anything into this"
        )
    print_json(result)
    return 0


def memory_remember(args: argparse.Namespace) -> int:
    node = memory_store(args).remember(
        args.type,
        args.name,
        args.summary,
        paths=args.path,
        tags=args.tag,
        confidence=args.confidence,
        provenance={"kind": args.provenance},
    )
    print_json(node)
    return 0


def memory_link(args: argparse.Namespace) -> int:
    edge = memory_store(args).link(
        args.source,
        args.target,
        args.kind,
        confidence=args.confidence,
        provenance={"kind": args.provenance},
    )
    print_json(edge)
    return 0


def context_repo(args: argparse.Namespace) -> Optional[Path]:
    return Path(args.repo).expanduser() if getattr(args, "repo", None) else None


def context_git(args: argparse.Namespace) -> int:
    print_json(grogu_context.aggregate("git", context_repo(args)))
    return 0


def context_graph(args: argparse.Namespace) -> int:
    print_json(
        grogu_context.aggregate(
            "graph",
            context_repo(args),
            query=args.query,
            node_id=args.node,
            depth=args.depth,
            limit=args.limit,
        )
    )
    return 0


def context_tasks(args: argparse.Namespace) -> int:
    print_json(grogu_context.aggregate("tasks", context_repo(args), limit=args.limit))
    return 0


def context_service(args: argparse.Namespace) -> int:
    print_json(grogu_context.aggregate("service", context_repo(args)))
    return 0


def codemode_repo(args: argparse.Namespace) -> Path:
    given = Path(args.repo).expanduser() if getattr(args, "repo", None) else None
    return grogu_memory.repository_root(given)


def codemode_tools(_: argparse.Namespace) -> int:
    print_json({"tools": grogu_codemode.list_tools()})
    return 0


def codemode_search(args: argparse.Namespace) -> int:
    print_json({"query": args.query, "tools": grogu_codemode.search_tools(args.query)})
    return 0


def codemode_generate(args: argparse.Namespace) -> int:
    directory = grogu_codemode.generate_tool_tree(codemode_repo(args))
    print_json({"tools_directory": str(directory)})
    return 0


def codemode_exec(args: argparse.Namespace) -> int:
    root = codemode_repo(args)
    if args.file:
        code = Path(args.file).expanduser().read_text(encoding="utf8")
    elif args.code:
        code = args.code
    else:
        code = sys.stdin.read()
    result = grogu_codemode.execute(root, code, timeout=args.timeout)
    print_json(result)
    return 0 if result["returncode"] == 0 and not result["timed_out"] else 1


def codemode_mcp_servers(_: argparse.Namespace) -> int:
    print_json({"servers": grogu_mcp.list_servers()})
    return 0


def codemode_mcp_tools(args: argparse.Namespace) -> int:
    if not grogu_mcp.available():
        print_json(
            {
                "error": "the 'mcp' package is not installed for this Python "
                "interpreter; run `python3 -m pip install mcp` (or re-run "
                "./setup.sh) to list MCP tools"
            }
        )
        return 1
    print_json({"server": args.server, "tools": grogu_mcp.list_tools(args.server)})
    return 0


def context_traces(args: argparse.Namespace) -> int:
    root = grogu_memory.repository_root(context_repo(args))
    store = grogu_memory.MemoryStore(root)
    repository_id = store.status().get("manifest", {}).get("repository_id", "")
    initialize_trace_db()
    with connect(TRACE_DB) as database:
        grogu_telemetry.initialize(database)
        data = grogu_context.traces_summary(database)
    print_json(grogu_context._envelope("traces", repository_id, data))
    return 0


def context_relationships(args: argparse.Namespace) -> int:
    root = grogu_memory.repository_root(context_repo(args))
    store = grogu_memory.MemoryStore(root)
    repository_id = store.status().get("manifest", {}).get("repository_id", "")
    initialize_catalog_db()
    with connect(CATALOG_DB) as database:
        rows = database.execute(
            """
            SELECT source_id, target_id, kind, evidence_json, updated_at
            FROM project_relationships
            WHERE source_id = ? OR target_id = ?
            ORDER BY source_id, target_id, kind
            """,
            (repository_id, repository_id),
        ).fetchall()
    edges = []
    for row in rows:
        value = dict(row)
        value["evidence"] = json.loads(value.pop("evidence_json"))
        edges.append(value)
    data = grogu_context.relationships_summary(edges, limit=args.limit)
    print_json(grogu_context._envelope("relationships", repository_id, data))
    return 0


def personal_store(_: argparse.Namespace) -> grogu_personal_memory.PersonalMemoryStore:
    return grogu_personal_memory.PersonalMemoryStore(GROGU_HOME)


def personal_status(args: argparse.Namespace) -> int:
    print_json(personal_store(args).status())
    return 0


def personal_remember(args: argparse.Namespace) -> int:
    node = personal_store(args).remember(
        args.type,
        args.name,
        args.summary,
        tags=args.tag,
        confidence=args.confidence,
        provenance={"kind": args.provenance},
    )
    print_json(node)
    return 0


def personal_link(args: argparse.Namespace) -> int:
    edge = personal_store(args).link(
        args.source,
        args.target,
        args.kind,
        confidence=args.confidence,
        provenance={"kind": args.provenance},
    )
    print_json(edge)
    return 0


def personal_forget(args: argparse.Namespace) -> int:
    removed = personal_store(args).forget(args.node)
    if not removed:
        print(f"grogu: no personal memory node {args.node!r}", file=sys.stderr)
        return 2
    print_json({"forgotten": args.node})
    return 0


def personal_list(args: argparse.Namespace) -> int:
    for node in personal_store(args).list(node_type=args.type, limit=args.limit):
        print(json.dumps(node, sort_keys=True))
    return 0


def personal_recall(args: argparse.Namespace) -> int:
    result = personal_store(args).recall(
        args.limit, query=args.query, node_id=args.node, depth=args.depth
    )
    print_json(result)
    return 0


def personal_suggest(args: argparse.Namespace) -> int:
    candidate = personal_store(args).suggest(
        args.type,
        args.name,
        args.summary,
        args.source,
        tags=args.tag,
        confidence=args.confidence,
    )
    print_json(candidate)
    return 0


def personal_review(args: argparse.Namespace) -> int:
    for candidate in personal_store(args).review(limit=args.limit):
        print(json.dumps(candidate, sort_keys=True))
    return 0


def personal_confirm(args: argparse.Namespace) -> int:
    try:
        node = personal_store(args).confirm(args.candidate)
    except ValueError as error:
        print(f"grogu: {error}", file=sys.stderr)
        return 2
    print_json(node)
    return 0


def personal_reject(args: argparse.Namespace) -> int:
    removed = personal_store(args).reject(args.candidate)
    if not removed:
        print(f"grogu: no pending candidate {args.candidate!r}", file=sys.stderr)
        return 2
    print_json({"rejected": args.candidate})
    return 0


def capability_store() -> grogu_capabilities.CapabilityStore:
    return grogu_capabilities.CapabilityStore(GROGU_HOME)


def capability_list(_: argparse.Namespace) -> int:
    print_json({"repositories": capability_store().list()})
    return 0


def capability_add(args: argparse.Namespace) -> int:
    print_json(capability_store().add(args.repository))
    return 0


def capability_remove(args: argparse.Namespace) -> int:
    print_json(capability_store().remove(args.repository))
    return 0


def telemetry_record(args: argparse.Namespace) -> int:
    initialize_trace_db()
    payload = json.loads(args.payload) if args.payload else {}
    with connect(TRACE_DB) as database:
        event = grogu_telemetry.record(
            database,
            event=args.event,
            outcome=args.outcome,
            payload=payload,
            repository_id=args.repository_id,
            session_id=args.session_id,
            task_id=args.task_id,
            duration_ms=args.duration_ms,
        )
    print_json(event)
    return 0


def telemetry_list(args: argparse.Namespace) -> int:
    initialize_trace_db()
    with connect(TRACE_DB) as database:
        for row in grogu_telemetry.rows(database, args.limit):
            print(json.dumps(row, sort_keys=True))
    return 0


def telemetry_summary(_: argparse.Namespace) -> int:
    initialize_trace_db()
    with connect(TRACE_DB) as database:
        print_json(grogu_telemetry.summary(database))
    return 0
    return 0


def mark_grogu_terminal() -> None:
    if (
        os.environ.get("GROGU_TAB_COLOR", "1") != "1"
        or os.environ.get("TERM_PROGRAM") != "iTerm.app"
        or not sys.stdout.isatty()
    ):
        return
    red, green, blue = TAB_COLOR
    for channel, value in (("red", red), ("green", green), ("blue", blue)):
        sys.stdout.write(f"\033]6;1;bg;{channel};brightness;{value}\a")
    sys.stdout.write("\033]2;Grogu\a")
    sys.stdout.flush()


def sync_grogu_main_checkout() -> None:
    """Best-effort startup fast-forward of the primary checkout to `origin/main`.

    Never blocks or fails a launch: any error leaves the checkout untouched
    and is swallowed, since this is a convenience, not a correctness
    requirement. Runs before worktree pruning so staleness is judged against
    a fresh `main`, not whatever happened to be checked out last session.
    """
    if os.environ.get("GROGU_SYNC_MAIN", "1") == "0":
        return
    try:
        result = grogu_worktrees.sync_main_with_origin(ROOT)
    except Exception:  # pragma: no cover - never let sync break a launch
        return
    if result:
        print(f"grogu: {result}", file=sys.stderr)


def prune_stale_grogu_worktrees() -> None:
    """Best-effort startup cleanup of Grogu's own self-modification worktrees.

    Never blocks or fails a launch: any error prunes nothing and is
    swallowed, since this is a convenience, not a correctness requirement.

    Passes the launching process's own current directory as an
    `active_paths` guard (friction #46): `ROOT` always resolves to wherever
    the installed `grogu` launcher's own script lives -- the primary
    checkout -- regardless of which worktree this process was actually
    started from, so without this, an ordinary launch from inside a
    session's own dedicated worktree could prune the very worktree it is
    running in out from under it.
    """
    if os.environ.get("GROGU_PRUNE_WORKTREES", "1") == "0":
        return
    try:
        pruned = grogu_worktrees.prune_stale_worktrees(
            ROOT, active_paths=[Path.cwd()]
        )
    except Exception:  # pragma: no cover - never let cleanup break a launch
        return
    for entry in pruned:
        print(
            f"grogu: removed stale worktree {entry.worktree.path} "
            f"({entry.reason})",
            file=sys.stderr,
        )


def banner_enabled() -> bool:
    return os.environ.get("GROGU_BANNER", "1") != "0"


def status_line_enabled() -> bool:
    return os.environ.get("GROGU_STATUS_LINE", "1") != "0"


def banner_show(args: argparse.Namespace) -> int:
    for line in grogu_banner.render_mark(args.frame, VERSION):
        print(line)
    return 0


def banner_status_line(_: argparse.Namespace) -> int:
    print(grogu_banner.status_line_frame())
    return 0


def banner_restore(_: argparse.Namespace) -> int:
    grogu_banner.restore(GROGU_HOME, pid=-1)
    return 0


def worktree_list(_: argparse.Namespace) -> int:
    for entry in grogu_worktrees.list_worktrees(ROOT):
        marker = "*" if entry.is_main else " "
        print(f"{marker} {entry.path}  [{entry.branch or 'detached'}]")
    return 0


def worktree_prune(args: argparse.Namespace) -> int:
    # Never prune the worktree this very command is running from (friction
    # #46) -- explicit or not, that one is active by definition.
    active_paths = [Path.cwd()]
    if args.dry_run:
        stale = grogu_worktrees.stale_worktrees(ROOT, active_paths=active_paths)
        for entry in stale:
            print(f"{entry.worktree.path}  ({entry.reason})")
        if not stale:
            print("no stale worktrees")
        return 0
    pruned = grogu_worktrees.prune_stale_worktrees(ROOT, active_paths=active_paths)
    for entry in pruned:
        print(f"removed {entry.worktree.path}  ({entry.reason})")
    if not pruned:
        print("no stale worktrees")
    return 0


# Copilot CLI subcommands; none of them start an agent session.
COPILOT_SUBCOMMANDS = frozenset(
    {
        "completion",
        "help",
        "init",
        "login",
        "logout",
        "mcp",
        "plugin",
        "plugins",
        "skill",
        "update",
        "version",
    }
)

# `--autopilot` is rejected by Copilot when combined with these.
CONFLICTING_MODE_FLAGS = frozenset({"--autopilot", "--mode", "--plan"})

# The user already chose how this invocation runs; do not change its mode.
EXPLICIT_LAUNCH_FLAGS = frozenset({"-i", "--interactive", "-p", "--prompt"})

# These launches are not a fresh local session: they either restore a session
# that carries its own mode or do not start an agent at all.
NON_DEFAULTABLE_FLAGS = frozenset(
    {
        "--acp",
        "--cloud",
        "--connect",
        "--continue",
        "--help",
        "-h",
        "--resume",
        "-r",
        "--version",
        "-v",
    }
)


def autopilot_default_enabled() -> bool:
    return os.environ.get("GROGU_AUTOPILOT", "1") != "0"


# The model every pipeline role already runs on (see `.github/agents/*.md`);
# a bare launch used to fall through to whatever Copilot itself defaults to,
# which is a different, weaker model than the one doing the actual work.
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_MODEL_CONTEXT = "long_context"
DEFAULT_MODEL_EFFORT = "high"

# Flags that mean the user, not Grogu, is choosing what the session runs on.
MODEL_CHOICE_FLAGS = frozenset({"--model", "--context", "--effort", "--reasoning-effort"})


def model_default_enabled() -> bool:
    return os.environ.get("GROGU_MODEL_DEFAULT", "1") != "0"


def wants_model_default(arguments: list[str]) -> bool:
    """True when Grogu should supply its own default model, context and effort.

    Mirrors `wants_autopilot_default`: skip subcommands, resumed or connected
    sessions, and anything where the user already named a model, context tier
    or reasoning effort, so an explicit choice is never overridden or
    duplicated.
    """
    if not model_default_enabled():
        return False
    if any(argument in COPILOT_SUBCOMMANDS for argument in arguments):
        return False
    names = _flags(arguments)
    if names & NON_DEFAULTABLE_FLAGS:
        return False
    return not (names & MODEL_CHOICE_FLAGS)


def _flags(arguments: list[str]) -> set[str]:
    """Option names in `arguments`, with `--name=value` reduced to `--name`."""
    names = set()
    for argument in arguments:
        if not argument.startswith("-") or argument == "-":
            continue
        names.add(argument.split("=", 1)[0] if argument.startswith("--") else argument)
    return names


def wants_autopilot_default(arguments: list[str]) -> bool:
    """True when Grogu should add `--autopilot` to a Copilot invocation.

    Grogu only supplies the default for a normal session launch. Anything the
    user made explicit -- a mode, a prompt, a resumed session, a subcommand --
    is passed through untouched, so Copilot never sees a duplicate or a
    combination it rejects.
    """
    if not autopilot_default_enabled():
        return False
    if any(argument in COPILOT_SUBCOMMANDS for argument in arguments):
        return False
    names = _flags(arguments)
    return not (names & (CONFLICTING_MODE_FLAGS | EXPLICIT_LAUNCH_FLAGS | NON_DEFAULTABLE_FLAGS))


def copilot_arguments(arguments: list[str]) -> list[str]:
    """The Copilot argument vector for a Grogu launch."""
    prepared = list(arguments)
    if wants_autopilot_default(arguments):
        # Leading position keeps user arguments, including any trailing `--`
        # separator, exactly as they were typed.
        prepared = ["--autopilot", *prepared]
    if wants_model_default(arguments):
        prepared = [
            "--model", DEFAULT_MODEL,
            "--context", DEFAULT_MODEL_CONTEXT,
            "--effort", DEFAULT_MODEL_EFFORT,
            *prepared,
        ]
    plugins = ["--plugin-dir", str(ROOT)]
    for path in capability_store().plugin_paths():
        plugins.extend(("--plugin-dir", str(path)))
    return [*plugins, *prepared]


def task_store(args: argparse.Namespace) -> grogu_tasks.TaskStore:
    return grogu_tasks.TaskStore(Path(args.repo).expanduser() if args.repo else None)


def _task_line(task: dict, store: grogu_tasks.TaskStore) -> str:
    lease = store.lease(task["id"])
    holder = ""
    if lease:
        state = "held" if store.lease_is_live(lease) else "stale"
        holder = f" [{state}: {lease.get('owner')} until {lease.get('expires_at')}]"
    issue = f" #{task['issue']}" if task.get("issue") else ""
    return f"{task['id']}  {task['status']:<9}{issue} {task['title']}{holder}"


def task_new(args: argparse.Namespace) -> int:
    store = task_store(args)
    task = store.create(
        args.title,
        body=args.body or "",
        labels=args.label,
        priority=args.priority,
        issue=args.issue,
    )
    print(task["id"] if not args.json else json.dumps(task, indent=2, sort_keys=True))
    return 0


def task_adopt(args: argparse.Namespace) -> int:
    store = task_store(args)
    payload = grogu_tasks.issue_payload(args.number, args.repository)
    labels = [label["name"] for label in payload.get("labels", []) if label.get("name")]
    existing = [t for t in store.list_tasks() if t.get("issue") == payload["number"]]
    if existing and not args.force:
        print(existing[0]["id"])
        return 0
    task = store.create(
        payload.get("title", f"issue #{payload['number']}"),
        body=payload.get("body") or "",
        labels=labels,
        issue=payload["number"],
    )
    print(task["id"])
    return 0


def task_list(args: argparse.Namespace) -> int:
    store = task_store(args)
    tasks = store.list_tasks()
    if not args.all:
        tasks = [t for t in tasks if t["status"] not in grogu_tasks.CLOSED_STATUSES]
    if args.status:
        tasks = [t for t in tasks if t["status"] == args.status]
    if args.mine:
        tasks = [t for t in tasks if t.get("assignee") == grogu_tasks.actor()]
    tasks.sort(key=lambda task: (task["status"], task["id"]))
    if args.json:
        print_json({"tasks": [store.view(task["id"]) for task in tasks]})
        return 0
    for task in tasks:
        print(_task_line(task, store))
    return 0


def task_show(args: argparse.Namespace) -> int:
    store = task_store(args)
    task = store.view(store.resolve(args.id))
    if args.json:
        print_json(task)
        return 0
    print(_task_line(task, store))
    if task.get("body"):
        print()
        print(task["body"])
    for entry in task.get("log", []):
        text = f": {entry['text']}" if entry.get("text") else ""
        print(f"  {entry['at']}  {entry['by']}  {entry['event']}{text}")
    return 0


def task_claim(args: argparse.Namespace) -> int:
    store = task_store(args)
    task = store.claim(store.resolve(args.id), ttl=args.ttl, force=args.force)
    print(f"claimed {task['id']} until {task['lease']['expires_at']}")
    return 0


def task_heartbeat(args: argparse.Namespace) -> int:
    store = task_store(args)
    lease = store.heartbeat(store.resolve(args.id), ttl=args.ttl)
    print(f"renewed {lease['task_id']} until {lease['expires_at']}")
    return 0


def task_release(args: argparse.Namespace) -> int:
    store = task_store(args)
    task = store.release(store.resolve(args.id), status=args.status, note=args.note or "")
    print(f"released {task['id']} as {task['status']}")
    return 0


def task_update(args: argparse.Namespace) -> int:
    store = task_store(args)
    task = store.update(
        store.resolve(args.id),
        status=args.status,
        title=args.title,
        body=args.body,
        note=args.note,
        issue=args.issue,
        priority=args.priority,
        labels=args.label or None,
    )
    print(f"{task['id']} {task['status']} r{task['revision']}")
    return 0


def task_gc(args: argparse.Namespace) -> int:
    store = task_store(args)
    for task_id in store.collect_expired():
        print(f"released expired lease on {task_id}")
    return 0


def task_tell(args: argparse.Namespace) -> int:
    store = task_store(args)
    task_id = store.resolve(args.id)
    message = store.tell(task_id, " ".join(args.text))
    print(f"queued {message['id']} for {task_id}")
    return 0


def task_inbox(args: argparse.Namespace) -> int:
    store = task_store(args)
    if args.id is None:
        counts = store.pending_counts()
        if args.json:
            print_json(counts)
            return 0
        for task_id, count in sorted(counts.items()):
            print(f"{task_id}  {count} pending update(s)")
        return 0
    messages = store.inbox(
        store.resolve(args.id), consume=args.consume, include_delivered=args.all
    )
    if args.json:
        print_json(messages)
        return 0
    for message in messages:
        print(f"{message['at']}  {message['from']}: {message['text']}")
    return 0


# Commands only the user may run. Running one is evidence the caller is not an
# agent, regardless of which role last bound this working directory.
# `steer` is here because steering is the user's channel by contract -- the
# architect is told to commission rather than steer. Without it, the user's own
# `plan steer` inherited whatever role last fetched a brief in this directory
# and printed that agent's unread notes back at the person who wrote them,
# under an instruction addressed to somebody else ("fold this in now").
USER_ONLY_COMMANDS = frozenset({"approve", "steer"})

# Commands where `--role` names *whose* steering is being asked about, not who
# is asking. A person checking that their note landed was appearing on the
# watch board as the agent they had steered, working.
SUBJECT_ROLE_COMMANDS = frozenset({"steer", "steering", "commission"})

# Top-level commands that exist for the person supervising, not for an agent
# doing work. An agent that declares itself in the environment is still shown.
USER_SURFACE_COMMANDS = frozenset({"watch"})


def _notice_for(parsed: argparse.Namespace) -> str:
    """The unsolicited notice this command should carry, if any.

    Computed before the handler runs so a `--json` command can fold it into its
    payload; whatever is left over is printed afterwards. Not on the friction
    report itself: it already shows these notes, and spending the once-a-day
    reminder on the one command that did not need it wastes the only prompt the
    user gets.
    """
    # The banner exists to push steering into commands that were about
    # something else. `plan steering` already prints the notes and marks them
    # read, so adding the banner printed every note twice in one response --
    # doubling the tokens of the one path built to be token-efficient, and
    # making one note look like two agents had said the same thing.
    if getattr(parsed, "command", "") == "plan" and getattr(
        parsed, "plan_command", ""
    ) in {"friction", "steering"}:
        return ""
    hint = ""
    if getattr(parsed, "command", "") == "plan":
        hint = getattr(parsed, "plan", "") or getattr(parsed, "id", "") or ""
    try:
        return grogu_plans.pending_banner(
            Path(parsed.repo).expanduser() if getattr(parsed, "repo", None) else None,
            plan_hint=hint if isinstance(hint, str) else "",
            user_command=getattr(parsed, "plan_command", "") in USER_ONLY_COMMANDS,
        )
    except Exception:  # a notice must never be why a command fails
        return ""


def _emit_notice(banner: str, parsed: argparse.Namespace) -> None:
    """Deliver an unsolicited notice where its reader will actually see it.

    A human sees both streams in a terminal, so stderr is right there. An agent
    usually sees only what its tool call captured, and many harnesses capture
    stdout alone — a steering note the agent never sees is a steering note that
    was not delivered. So when stdout is redirected, the notice goes there
    instead, except when the caller asked for JSON and would have to parse it.
    """
    try:
        redirected = not sys.stdout.isatty()
    except (ValueError, AttributeError):
        redirected = False
    wants_json = bool(getattr(parsed, "json", False))
    print(banner, file=sys.stdout if (redirected and not wants_json) else sys.stderr)
    global _PENDING_NOTICE
    _PENDING_NOTICE = ""
    grogu_plans.mark_delivered()


def _record_activity(parsed: argparse.Namespace) -> None:
    """Log that a grogu command ran, so `grogu watch` can show who is working.

    Subcommand name only. The arguments are exactly where the private text
    lives, and this file is never read by anyone until something has gone wrong.
    """
    try:
        command = getattr(parsed, "command", "") or ""
        sub = ""
        for attribute in ("plan_command", "design_command", "task_command", "skill_command"):
            sub = getattr(parsed, attribute, "") or ""
            if sub:
                break
        # `--role` usually names the caller, but on the steering commands it
        # names the *target* — reading it there would report the user's own
        # steering as the steered agent doing work, which is a board that lies.
        claimed_role = "" if sub in SUBJECT_ROLE_COMMANDS else (getattr(parsed, "role", "") or "")
        role = grogu_plans.current_role() or claimed_role
        plan = os.environ.get("GROGU_PLAN", "").strip()
        # A plan named on the command line identifies the agent just as well as
        # the environment variable, and subagents pass it far more often.
        for attribute in ("plan", "id"):
            candidate = getattr(parsed, attribute, "") or ""
            if isinstance(candidate, str) and candidate.startswith("p-"):
                plan = plan or candidate
                break
        cwd = str(Path.cwd())
        # The session binding says "an agent is working in this directory". It
        # does not say that *this* process is that agent. The user steering
        # from the same shell was showing up on the board as the agent they
        # were steering, doing work, one line under the note they had just
        # written. A command that only a person runs never inherits a binding.
        user_shaped = (
            sub in SUBJECT_ROLE_COMMANDS
            or sub in USER_ONLY_COMMANDS
            or getattr(parsed, "command", "") in USER_SURFACE_COMMANDS
        )
        agent = os.environ.get("GROGU_AGENT", "").strip()
        activity_store = None
        repository = str(Path(cwd).resolve())
        try:
            activity_store = grogu_plans.PlanStore(
                Path(parsed.repo).expanduser()
                if getattr(parsed, "repo", None)
                else None
            )
            repository = str(activity_store.root)
        except (grogu_plans.PlanError, OSError):
            pass
        if user_shaped and not grogu_plans.current_role():
            role = ""
            agent = ""
        else:
            try:
                bound = (
                    activity_store.session_binding()
                    if activity_store is not None
                    else {}
                )
                if not role:
                    role, plan = bound.get("role", ""), plan or bound.get("plan", "")
                agent = agent or bound.get("agent", "")
            except (grogu_plans.PlanError, OSError):
                pass
        grogu_watch.record(
            command=f"{command} {sub}".strip(),
            role=role,
            agent=agent,
            plan=plan,
            repository=repository,
            cwd=cwd,
            exit_code=0,
        )
    except Exception:
        return  # watching must never be the reason a command fails


def _skill_proposals_safely() -> tuple:
    """Proposals for the board, and why there are none if there are none.

    The board must never be the reason a command fails, so this swallows. But
    swallowing silently meant a store the skill commands were refusing to touch
    showed on the board as "nothing waiting" -- which is the answer you get
    when there is genuinely nothing, and the user reads the board precisely to
    find out whether anything needs them.
    """
    try:
        return grogu_skills.proposals(), ""
    except (grogu_skills.SkillError, OSError) as error:
        return [], str(error).splitlines()[0]


def _plan_summaries(args: argparse.Namespace) -> dict:
    summaries: dict = {}
    try:
        store = grogu_plans.PlanStore(
            Path(args.repo).expanduser() if getattr(args, "repo", None) else None
        )
        for plan in store.list_plans():
            if plan.get("status") == grogu_plans.SUPERSEDED:
                continue
            summaries[plan["id"]] = store.summary(plan["id"])
    except (grogu_plans.PlanError, OSError):
        return {}
    return summaries


def watch(args: argparse.Namespace) -> int:
    proposals, store_error = _skill_proposals_safely()
    state = grogu_watch.board(
        window_minutes=args.window,
        plan_summaries=_plan_summaries(args),
        skill_proposals=proposals,
        skill_store_error=store_error,
    )
    if args.json:
        print_json(state)
        return 0
    if not args.follow:
        print(grogu_watch.render(state, window_minutes=args.window))
        return 0
    try:
        while True:
            # Recomputed every pass: a board that shows the plan state from when
            # you started watching is worse than no board.
            live, live_error = _skill_proposals_safely()
            state = grogu_watch.board(
                window_minutes=args.window,
                plan_summaries=_plan_summaries(args),
                skill_proposals=live,
                skill_store_error=live_error,
            )
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.write(grogu_watch.render(state, window_minutes=args.window))
            sys.stdout.write("\n")
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def guard_scan(args: argparse.Namespace) -> int:
    targets = args.paths or []
    findings: list = []
    if not targets:
        text = sys.stdin.read()
        findings = grogu_privacy.scan(text, path="<stdin>", personal=not args.secrets_only)
    expanded = []
    for target in targets:
        path = Path(target).expanduser()
        if path.is_dir():
            # "Scan this" almost always means a tree. Refusing a directory
            # pushed every caller into hand-rolling `find`, and a guard you
            # have to build a pipeline around is a guard that gets skipped.
            expanded.extend(
                sorted(
                    child
                    for child in path.rglob("*")
                    if child.is_file() and ".git" not in child.parts
                )
            )
            continue
        if not path.exists():
            print(f"grogu: no such file or directory: {target}", file=sys.stderr)
            return 2
        expanded.append(path)
    for path in expanded:
        if not path.is_file():
            print(f"grogu: not a file: {path}", file=sys.stderr)
            return 2
        if grogu_privacy.dangerous_path(str(path)):
            findings.append(
                grogu_privacy.Finding(
                    grogu_privacy.SECRET, "credential file", 0, path.name, str(path)
                )
            )
        findings.extend(
            grogu_privacy.scan(
                path.read_text(encoding="utf8", errors="replace"),
                path=str(path),
                personal=not args.secrets_only,
            )
        )
    return _guard_verdict(findings, destination=args.destination, quiet=args.quiet)


def guard_staged(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser() if args.repo else Path.cwd()
    findings = grogu_privacy.scan_staged(repo, personal=args.personal)
    destination = args.destination or _destination_for(repo)
    return _guard_verdict(findings, destination=destination, quiet=args.quiet)


def _destination_for(repo: Path) -> str:
    """A commit in a repository with a remote is on its way somewhere.

    Personal data used to be non-blocking on the reasoning that a working
    repository is private. Grogu's own repository is public, and the guard had
    no way to know that -- so the one thing it let through was a machine
    hostname, into a public commit, which is exactly the accident it exists to
    stop. Having a remote is the cheap, offline, honest version of the
    question "is this going to leave the machine".
    """
    try:
        remotes = subprocess.run(
            ["git", "remote"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        ).stdout.split()
    except OSError:
        remotes = []
    return grogu_privacy.PUBLISHED if remotes else grogu_privacy.REPOSITORY


def _guard_verdict(findings: list, *, destination: str, quiet: bool) -> int:
    blocking = grogu_privacy.blocking(findings, destination=destination)
    if not findings:
        if not quiet:
            print("clean")
        return 0
    stream = sys.stderr if blocking else sys.stdout
    print(
        f"grogu guard: {len(findings)} finding(s), {len(blocking)} blocking",
        file=stream,
    )
    print(grogu_privacy.report(findings), file=stream)
    if blocking:
        print(
            "\nRefusing to continue. Move the value to the environment or a "
            "secret store, or remove it from the change. If a line genuinely "
            f"needs to contain this — a test fixture, a documentation example — "
            f"mark that one line `{grogu_privacy.ALLOW_MARKER_TEXT}` in a "
            "comment, which stays visible in review. Reach for "
            "`git commit --no-verify` only when the pattern itself is wrong, "
            "and say so, so it gets fixed rather than routed around.",
            file=stream,
        )
        return 4
    return 0


def guard_install(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser() if args.repo else Path.cwd()
    path = grogu_privacy.install_hook(
        repo, python=sys.executable, script=str(Path(__file__).resolve())
    )
    print(f"installed {path}")
    return 0


def _plan_id_argument(parser: argparse.ArgumentParser) -> None:
    """Take the plan id from the argument or from GROGU_PLAN.

    Every role prompt tells an agent to export GROGU_PLAN, and steering
    delivery has always honoured it, but the commands that need a plan id most
    -- `gate`, `brief`, `show` -- did not. An engineer that followed its own
    setup instructions got a usage error from `plan gate implement` and a brief
    with no plan in it, and had to work out from the near-empty output that the
    variable it had exported was being ignored.
    """
    parser.add_argument("id", nargs="?", default="")
    # `plan brief`, `plan steering` and `plan friction` take `--plan <id>`, so
    # every agent learns `--plan` as the convention and then spends a failed
    # call per command discovering that `plan status`, `plan gate` and
    # `plan workstreams` want it positionally. Both spellings work everywhere.
    # `--id` is here because it is what the spawn prompts kept saying. An
    # architect's very first command failed on it, which is the least
    # recoverable moment there is: a fresh agent with no context, whose one
    # instruction was wrong. Guessing a name for the same value is not a
    # judgement worth making an agent make.
    parser.add_argument(
        "--plan", "--id", dest="plan_flag", default="", help=argparse.SUPPRESS
    )
    parser.set_defaults(_plan_id_required=True)


#: Words that appear where a plan id goes but name a stage or a gate instead.
_STAGE_WORDS = frozenset(
    set(grogu_plans.GATES)
    | {
        grogu_plans.DESIGN,
        grogu_plans.IMPLEMENTATION,
        grogu_plans.TESTING,
        grogu_plans.EVALUATION,
    }
)


def _resolve_plan_id(parsed: argparse.Namespace) -> bool:
    """Fill in the plan id from the environment; False when there is none."""
    if not hasattr(parsed, "id"):
        return True
    # The commands that spell the id as a flag also accept it positionally, for
    # the same reason the flag has two names: an agent that has just learned
    # `plan status <id>` should not have to unlearn it one subcommand later.
    # A plan id has a shape, and these words are not it. `plan gate test
    # --plan <id>` is what a tester types -- the brief teaches both halves --
    # and it was answered with "two plans given, 'test' and 'p-...'", because
    # the stage word landed in the id positional and the conflict check fired
    # before the subcommand ever got to recognise it.
    if getattr(parsed, "id", "") in _STAGE_WORDS:
        parsed.stage_positional = parsed.id
        # `plan gate test <id>` puts the stage in the first slot and the plan
        # in the second, so promote it here rather than in the handler: the
        # missing-id check below runs first and would refuse a complete
        # command.
        parsed.id = (getattr(parsed, "gate_positional", "") or "").strip()
    positional = (getattr(parsed, "plan_positional", "") or "").strip()
    if positional and not parsed.id:
        parsed.id = positional
    elif positional and parsed.id and positional != parsed.id:
        print(
            f"grogu: two plans given, {parsed.id!r} and {positional!r}; name "
            "it once",
            file=sys.stderr,
        )
        return False
    flagged = (getattr(parsed, "plan_flag", "") or "").strip()
    if flagged:
        if parsed.id and parsed.id != flagged:
            print(
                f"grogu: two plans given, {parsed.id!r} and {flagged!r}; name "
                "it once",
                file=sys.stderr,
            )
            return False
        parsed.id = flagged
    if not parsed.id:
        parsed.id = os.environ.get("GROGU_PLAN", "").strip()
    if not getattr(parsed, "_plan_id_required", False):
        return True
    if not parsed.id:
        print(
            "no plan id: pass one, or export GROGU_PLAN for this shell",
            file=sys.stderr,
        )
        return False
    return True


def plan_store(args: argparse.Namespace) -> grogu_plans.PlanStore:
    return grogu_plans.PlanStore(Path(args.repo).expanduser() if args.repo else None)


def _plan_role(args: argparse.Namespace) -> str:
    role = getattr(args, "role", "") or grogu_plans.current_role()
    if role:
        return role
    # Every other command falls back to the session binding, so being refused
    # here looks like an inconsistency. It is not, and the message says why:
    # the binding records that *a* role is working in this directory, which is
    # enough to route steering to it and nowhere near enough to authorise
    # reading a sealed stage. The user shares that directory with the agent.
    message = (
        "pass --role, or export GROGU_ROLE. Plan access is role-scoped: "
        "who is asking decides what may be read."
    )
    try:
        bound = (
            grogu_plans.PlanStore(
                Path(args.repo).expanduser() if getattr(args, "repo", None) else None
            )
            .session_binding()
            .get("role", "")
        )
    except (grogu_plans.PlanError, OSError):
        bound = ""
    if bound:
        message += (
            f"\nA {bound} is bound to this directory, and that is deliberately "
            "not enough: the binding routes steering to whoever works here, "
            "including you sharing the shell with them. Claim the role you are "
            f"reading as -- `--role {bound}` if that is you."
        )
    raise grogu_plans.PlanError(message)


def _read_body(args: argparse.Namespace) -> str:
    if getattr(args, "file", None):
        if args.file == "-":
            return sys.stdin.read()
        path = Path(args.file).expanduser()
        # An agent that mistypes the path to a plan stage it spent ten minutes
        # writing got a Python traceback, which reads like the harness broke
        # rather than like the file is not there.
        try:
            return path.read_text(encoding="utf8")
        except IsADirectoryError:
            raise grogu_plans.PlanError(f"{path} is a directory, not a file")
        except FileNotFoundError:
            raise grogu_plans.PlanError(f"no such file: {path}")
        except OSError as error:
            raise grogu_plans.PlanError(f"cannot read {path}: {error}")
        except UnicodeDecodeError:
            raise grogu_plans.PlanError(f"{path} is not text")
    return args.body or ""


def plan_new(args: argparse.Namespace) -> int:
    store = plan_store(args)
    plan = store.create(
        args.title,
        task_id=args.task or "",
        design=args.design,
        evaluation=args.eval,
        review_required=args.review_required,
    )
    print(plan["id"] if not args.json else json.dumps(plan, indent=2, sort_keys=True))
    return 0


def plan_list(args: argparse.Namespace) -> int:
    store = plan_store(args)
    plans = store.list_plans()
    if args.status:
        plans = [plan for plan in plans if plan.get("status") == args.status]
    elif not args.all:
        plans = [plan for plan in plans if plan.get("status") != grogu_plans.SUPERSEDED]
    plans.sort(key=lambda plan: plan["id"])
    if args.json:
        print_json({"plans": [store.summary(plan["id"]) for plan in plans]})
        return 0
    for plan in plans:
        review = " [awaiting review]" if plan.get("review_required") and plan.get("status") != grogu_plans.APPROVED else ""
        task = f" ({plan['task_id']})" if plan.get("task_id") else ""
        print(f"{plan['id']}  {plan.get('status', '?'):<12}{task} {plan.get('title', '')}{review}")
    return 0


def plan_status(args: argparse.Namespace) -> int:
    store = plan_store(args)
    plan_id = store.resolve(args.id)
    summary = store.summary(plan_id)
    review_line = ""
    review = grogu_review.ReviewStore(store)
    if store.is_document_plan(plan_id) or review.path(plan_id).exists():
        rsummary = review.summary(plan_id)
        if rsummary.get("threads"):
            review_line = f"review: round {rsummary.get('round')}, {rsummary.get('open', 0)} open"
            if rsummary.get("orphaned"):
                review_line += f", {rsummary['orphaned']} orphaned"
            summary = dict(summary, review=rsummary)
    if args.json:
        print_json(summary)
        return 0
    print(f"{summary['id']}  {summary['status']}  {summary['title']}")
    print(f"  stages: " + ", ".join(
        f"{stage}={summary['stage_state'].get(stage, '?')}"
        + ("" if summary.get("stage_written", {}).get(stage, True) else " (unwritten)")
        for stage in summary["stages"]
    ))
    for stage, decision in sorted(summary.get("declined_stages", {}).items()):
        print(f"  no {stage} stage, by decision: {decision.get('why', '')}")
    print(f"  amendment rounds: {summary['rounds']}   engineer/tester rounds: {summary['defect_rounds']}")
    if summary.get("escalated"):
        print("  escalated to the architect: the engineer/tester loop stopped converging")
    if summary["review_required"] and summary["status"] != grogu_plans.APPROVED:
        # The designer starts before approval by construction -- the spec is
        # part of what the user reviews -- so "before work starts" read to the
        # one role that must go first as "do not go".
        if "design" in summary.get("stages", []) and not summary.get(
            "stage_written", {}
        ).get("design"):
            print(
                "  the user asked for this plan; the designer writes the spec "
                "first, then `grogu plan approve` releases everything else"
            )
        else:
            print(
                "  the user asked for this plan; it needs `grogu plan approve` "
                "before anything downstream of the design spec moves"
            )
    for stream in summary["workstreams"]:
        depends = f" after {', '.join(stream['depends_on'])}" if stream["depends_on"] else ""
        state = stream.get("state", grogu_plans.PENDING)
        print(
            f"  workstream {stream['name']} [{state}]: "
            f"{', '.join(stream['paths'])}{depends}"
        )
    for amendment in summary["open_amendments"]:
        print(f"  amendment {amendment['id']} from {amendment['raised_by']}: {amendment['claim']}")
    for defect in summary["open_defects"]:
        print(f"  defect {defect['id']} -> {defect['owner']} ({defect['route']}): {defect['report']}")
    # `steering_pending` answers "do I have unread notes", and it is computed
    # against whoever is asking. Printing it to the user meant `plan status`
    # said "2 unread steering note(s) for the reviewer" forever -- the count
    # never fell when the reviewer read them, because what it was really
    # reporting was that the *user's own shell* had not read them. The user's
    # question is whether the note landed, which is the undelivered list below.
    if grogu_plans.current_role():
        pending = {
            role: count for role, count in summary["steering_pending"].items() if count
        }
        for role, count in sorted(pending.items()):
            print(f"  {count} unread steering note(s) for the {role}")
    # This list is the supervisor's view: which notes have not landed yet. It
    # is clipped to 90 characters because it is a summary of many notes. An
    # agent running `plan status` was shown its *own* pending note through this
    # clipped line, so the architect read 87 characters of the note that
    # invalidated its architecture and believed it had read the note. Delivery
    # is the banner, in full, once. Supervision is this list. Never both.
    if not grogu_plans.current_role():
        for note in summary.get("steering_undelivered", []):
            text = note["text"]
            if len(text) > 90:
                text = text[:87] + "... (`grogu plan steering --all` for the rest)"
            who = ", ".join(note.get("unread_by") or [note["role"]])
            print(f"  steering #{note['seq']} has not reached {who}: {text}")
    governance = summary.get("governance", {})
    for warning in governance.get("warnings", []):
        print(f"  governance warning: {warning}")
    for blocker in governance.get("blockers", []):
        print(f"  governance blocker: {blocker}")
    if review_line:
        print(f"  {review_line}")
    return 0


def plan_complete(args: argparse.Namespace) -> int:
    """`plan complete <id> <stage>` -- the spelling every role tries first."""
    stage = (getattr(args, "stage_positional", "") or args.stage or "").strip()
    if not stage:
        print(
            "grogu: which stage? " + ", ".join(grogu_plans.STAGES),
            file=sys.stderr,
        )
        return 2
    if stage not in grogu_plans.STAGES:
        print(
            f"grogu: no stage called {stage!r}; expected one of "
            + ", ".join(grogu_plans.STAGES),
            file=sys.stderr,
        )
        return 2
    args.stage = stage
    args.state = grogu_plans.COMPLETE
    return plan_stage(args)


def plan_shape(args: argparse.Namespace) -> int:
    store = plan_store(args)
    plan_id = store.resolve(args.id)
    role = getattr(args, "role", "") or ""
    if args.as_user and not args.clear_review:
        raise grogu_plans.PlanError(
            "plan shape --as-user is only valid with --clear-review"
        )
    if args.add:
        store.add_stage(plan_id, args.add, role=role)
        print(f"{plan_id}: added a {args.add} stage")
        return 0
    if getattr(args, "reset", ""):
        store.reset_stage(plan_id, args.reset, role=role)
        print(f"{plan_id}: reset the {args.reset} stage to unwritten")
        return 0
    if args.decline:
        store.decline_stage(plan_id, args.decline, args.why or "", role=role)
        print(f"{plan_id}: recorded that no {args.decline} stage is warranted")
        return 0
    if args.clear_review:
        store.clear_review_requirement(
            plan_id, args.why or "", role=role, as_user=args.as_user
        )
        print(
            f"{plan_id}: cleared the unapproved review requirement; "
            "other plan state is unchanged"
        )
        return 0
    manifest = store.require_review(plan_id, role=role)
    for warning in manifest.get("warnings", []):
        print(f"grogu: {warning}", file=sys.stderr)
    print(f"{plan_id}: held for user review; work is blocked until `grogu plan approve`")
    return 0


def plan_write(args: argparse.Namespace) -> int:
    store = plan_store(args)
    plan_id = store.resolve(args.id)
    if getattr(args, "file", None) and args.file != "-" and store.is_document_plan(plan_id):
        source = Path(args.file).expanduser().resolve()
        package = store.plan_dir(plan_id).resolve()
        if source == package or package in source.parents:
            raise grogu_plans.PlanError(
                "refusing --file from inside the .plan package; stage Markdown "
                "there is compiled output, not a writable source"
            )
    body = _read_body(args)
    manifest = store.write_stage(
        plan_id,
        args.stage,
        body,
        role=getattr(args, "role", "") or "",
        replace=getattr(args, "replace", False),
        base=getattr(args, "base", None),
    )
    for warning in manifest.get("warnings", []):
        print(f"grogu: {warning}", file=sys.stderr)
    print(f"wrote {args.stage} plan for {plan_id} ({len(body)} bytes)")
    last = manifest.get("last_write") or {}
    if last.get("digest"):
        print(f"  base: {last['digest']}")
    if last.get("revision"):
        print(
            f"  replaced {last['was']} bytes, kept as revision {last['revision']}: "
            f"`grogu plan show {plan_id} --stage {args.stage} "
            f"--revision {last['revision']}`"
        )
    return 0


def plan_show(args: argparse.Namespace) -> int:
    store = plan_store(args)
    # `plan write` takes the stage positionally and `plan show` demanded
    # `--stage`, so the architect typed `plan show <id> testing` -- the shape
    # the harness had just taught it -- and got an argparse error.
    positional = (getattr(args, "stage_positional", "") or "").strip()
    if positional:
        if positional not in grogu_plans.STAGES:
            print(
                f"grogu: no stage called {positional!r}; expected one of "
                + ", ".join(grogu_plans.STAGES),
                file=sys.stderr,
            )
            return 2
        if args.stage and args.stage != positional:
            print(
                f"grogu: two stages given, {positional!r} and {args.stage!r}; "
                "name it once",
                file=sys.stderr,
            )
            return 2
        args.stage = positional
    args.stage = args.stage or grogu_plans.IMPLEMENTATION
    plan_id = store.resolve(args.id)
    if getattr(args, "revisions", False):
        history = store.revisions(plan_id, args.stage)
        if not history:
            print(f"{args.stage} has never been rewritten")
            return 0
        for item in history:
            print(
                f"revision {item['revision']}  {item['bytes']} bytes  "
                f"{item['at']}  by {item['by']}"
            )
        return 0
    if getattr(args, "revision", 0):
        # Reading an old revision is a read of that stage, so it goes through
        # the same role check the current text does.
        store.read_stage(plan_id, args.stage, role=_plan_role(args))
        sys.stdout.write(store.revision_body(plan_id, args.stage, args.revision))
        return 0
    sys.stdout.write(store.read_stage(plan_id, args.stage, role=_plan_role(args)))
    return 0


def plan_approve(args: argparse.Namespace) -> int:
    store = plan_store(args)
    plan = store.approve(store.resolve(args.id), note=args.note or "")
    print(f"approved {plan['id']}")
    return 0


def plan_stage(args: argparse.Namespace) -> int:
    store = plan_store(args)
    plan = store.set_stage_state(
        store.resolve(args.id),
        args.stage,
        args.state,
        note=args.note or "",
        role=getattr(args, "role", "") or "",
        as_user=getattr(args, "as_user", False),
        workstream=getattr(args, "workstream", "") or "",
    )
    state = plan.get("stage_state", {}).get(args.stage, args.state)
    print(f"{plan['id']} {args.stage}={state}")
    streams = [stream["name"] for stream in plan.get("workstreams", [])]
    if len(streams) > 1 and args.stage == grogu_plans.IMPLEMENTATION:
        done = plan.get("workstream_state", {})
        outstanding = [name for name in streams if done.get(name) != grogu_plans.COMPLETE]
        if outstanding:
            print(f"  still open: {', '.join(outstanding)}")
    return 0


def plan_supersede(args: argparse.Namespace) -> int:
    store = plan_store(args)
    plan = store.set_status(
        store.resolve(args.id), grogu_plans.SUPERSEDED, note=args.note or ""
    )
    print(f"superseded {plan['id']}")
    return 0


def plan_finalize(args: argparse.Namespace) -> int:
    store = plan_store(args)
    result = store.finalize(
        store.resolve(args.id),
        note=args.note or "",
        force=args.force,
        role=getattr(args, "role", "") or "",
        as_user=getattr(args, "as_user", False),
    )
    for blocker in result.get("shipped_incomplete", []):
        print(f"grogu: shipped incomplete: {blocker}", file=sys.stderr)
    print(f"finalized {result['plan']}")
    staged = list(result.get("staged") or [])
    for path in result["emitted"]:
        print(f"  unsealed {path}")
    for path in staged:
        print(f"  staged {path}")
    if staged:
        print(
            "  the plans are in the index; commit them with the diff they justify"
        )
    else:
        print(
            "  not a git repository: copy the plan directory into the pull "
            "request by hand"
        )
    return 0


_GATE_ALIASES = {
    "implementation": grogu_plans.GATE_IMPLEMENT,
    "testing": grogu_plans.GATE_TEST,
    "evaluation": grogu_plans.GATE_EVALUATE,
}


def plan_gate(args: argparse.Namespace) -> int:
    # `grogu plan gate implement` is what agents type, every time, because it
    # is what a gate sounds like. Stage names cannot be confused with plan ids,
    # so accept it rather than answering a correct question with a usage error.
    # `plan status` prints stage names (implementation, testing) and the gate
    # took verbs (implement, test), so the vocabulary the harness taught was
    # rejected by the harness.
    stage = _GATE_ALIASES.get(args.stage, args.stage)
    # Either order: the stage word may arrive in the id slot or the extra one,
    # depending on whether the plan id was given as a flag or positionally.
    recovered = getattr(args, "stage_positional", "")
    if recovered and not stage:
        stage = _GATE_ALIASES.get(recovered, recovered)
    if _GATE_ALIASES.get(args.id, args.id) in grogu_plans.GATES and not stage:
        stage, args.id = (
            _GATE_ALIASES.get(args.id, args.id),
            os.environ.get("GROGU_PLAN", "").strip(),
        )
    if not stage:
        print(
            "which gate? " + "|".join(grogu_plans.GATES),
            file=sys.stderr,
        )
        return 2
    if not args.id:
        print("no plan id: pass one, or export GROGU_PLAN", file=sys.stderr)
        return 2
    store = plan_store(args)
    result = store.gate(store.resolve(args.id), stage)
    if args.json:
        print_json(result)
    else:
        verdict = "allowed" if result["allowed"] else "blocked"
        print(f"{result['plan']} {result['gate']}: {verdict} (status {result['status']})")
        for blocker in result["blockers"]:
            print(f"  - {blocker}")
    return 0 if result["allowed"] else 3


def plan_design_review(args: argparse.Namespace) -> int:
    store = plan_store(args)
    manifest = store.design_review(
        store.resolve(args.id),
        args.verdict,
        notes=args.notes or "",
        evidence=args.evidence or [],
        role=args.role or "",
    )
    review = manifest["design_review"]
    print(f"design review recorded: {review['verdict']}")
    return 0


def plan_amend(args: argparse.Namespace) -> int:
    store = plan_store(args)
    plan_id = store.resolve(args.id)
    amendment = store.amend(
        plan_id,
        claim=args.claim,
        evidence=args.evidence or "",
        stage=args.stage,
        raised_by=getattr(args, "role", "") or "",
    )
    print(f"raised {amendment['id']} on {plan_id}; the architect must verify and resolve it")
    return 0


def plan_amendments(args: argparse.Namespace) -> int:
    store = plan_store(args)
    manifest = store.load(store.resolve(args.id))
    amendments = manifest.get("amendments", [])
    if not args.all:
        amendments = [item for item in amendments if item.get("status") == grogu_plans.PENDING]
    if args.json:
        print_json(amendments)
        return 0
    if not amendments:
        # Silence here reads as a bad plan id, which is the one thing it is
        # not: `resolve` already refused those.
        scope = "" if args.all else " open"
        print(f"{manifest['id']} has no{scope} amendments")
        # The engineer that raised one runs this command to find out what the
        # architect decided, and got "no open amendments" -- true, and the
        # opposite of the answer it wanted. The ruling is the news.
        if not args.all:
            recent = [
                item
                for item in manifest.get("amendments", [])
                if item.get("resolved_at")
            ][-3:]
            for item in recent:
                print(
                    f"  {item['id']} was {item['status']} by the architect: "
                    f"{item.get('reason', '')}"
                )
            if recent:
                print("  `--all` for the full history")
        return 0
    for amendment in amendments:
        print(f"{amendment['id']}  {amendment['status']:<9} {amendment['raised_by']}: {amendment['claim']}")
        if amendment.get("evidence"):
            print(f"    evidence: {amendment['evidence']}")
        if amendment.get("reason"):
            print(f"    resolution: {amendment['reason']}")
    return 0


def plan_resolve(args: argparse.Namespace) -> int:
    store = plan_store(args)
    plan_id = store.resolve(args.id)
    if args.guidance:
        outcome, reason = grogu_plans.GUIDED, args.guidance
    elif args.accept:
        outcome, reason = grogu_plans.ACCEPTED, args.reason or ""
    else:
        outcome, reason = grogu_plans.REJECTED, args.reason or ""
    amendment = store.resolve_amendment(
        plan_id,
        args.amendment,
        outcome=outcome,
        reason=reason,
        verified=args.verified,
        role=getattr(args, "role", "") or "",
    )
    print(f"{amendment['id']} {amendment['status']}")
    if outcome == grogu_plans.GUIDED:
        print("  guidance queued as steering for the engineer and the tester")
    return 0


def plan_defect(args: argparse.Namespace) -> int:
    if getattr(args, "resolve", ""):
        # An engineer handed a routed-back defect reached for `--resolve`
        # first, because that is what every other command in the pipeline
        # would have called it. Being right about the name is not worth a
        # failed call.
        if not args.note:
            print(
                "grogu: closing a defect needs --note saying what you changed",
                file=sys.stderr,
            )
            return 2
        args.defect = args.resolve
        return plan_defect_resolve(args)
    if not args.report or not args.route:
        print(
            "grogu: filing a defect needs --report and --route "
            "(or --resolve DEFECT to close one)",
            file=sys.stderr,
        )
        return 2
    store = plan_store(args)
    plan_id = store.resolve(args.id)
    defect = store.report_defect(
        plan_id,
        report=args.report,
        route=args.route,
        evidence=args.evidence or "",
        raised_by=getattr(args, "role", "") or "",
    )
    print(f"raised {defect['id']} on {plan_id}, routed to the {defect['owner']}")
    return 0


def plan_defects(args: argparse.Namespace) -> int:
    store = plan_store(args)
    manifest = store.load(store.resolve(args.id))
    defects = manifest.get("defects", [])
    if not args.all:
        defects = [item for item in defects if item.get("status") == grogu_plans.PENDING]
    if args.json:
        print_json(defects)
        return 0
    for defect in defects:
        print(f"{defect['id']}  {defect['status']:<9} -> {defect.get('owner')} ({defect['route']}): {defect['report']}")
        if defect.get("evidence"):
            print(f"    evidence: {defect['evidence']}")
    return 0


def plan_defect_resolve(args: argparse.Namespace) -> int:
    store = plan_store(args)
    defect = store.resolve_defect(store.resolve(args.id), args.defect, note=args.note)
    print(f"{defect['id']} {defect['status']}")
    return 0


def plan_workstream(args: argparse.Namespace) -> int:
    store = plan_store(args)
    if getattr(args, "drop", False):
        if args.path:
            print(
                "grogu: --drop removes a workstream; it takes no --path",
                file=sys.stderr,
            )
            return 2
        stream = store.drop_workstream(store.resolve(args.id), args.name)
        print(f"dropped workstream {stream['name']}")
        return 0
    if not args.path:
        print(
            "grogu: a workstream needs at least one --path glob so parallel "
            "work can be checked for overlap",
            file=sys.stderr,
        )
        return 2
    stream = store.add_workstream(
        store.resolve(args.id),
        name=args.name,
        paths=args.path,
        depends_on=args.depends_on or [],
        model=args.model,
        required_reviews=args.review,
        brief=args.brief,
        replace=getattr(args, "replace", False),
    )
    detail = "".join(
        [
            f" model={stream['model']}" if stream["model"] else "",
            (
                f" reviews={','.join(stream['required_reviews'])}"
                if stream.get("required_reviews")
                else ""
            ),
        ]
    )
    print(f"workstream {stream['name']}: {', '.join(stream['paths'])}{detail}")
    return 0


def plan_workstreams(args: argparse.Namespace) -> int:
    store = plan_store(args)
    plan_id = store.resolve(args.id)
    conflicts = store.workstream_conflicts(plan_id)
    batches = store.parallel_batches(plan_id)
    # Read-only: this reports whichever dedicated worktrees already exist
    # (via `plan workstream-worktree`) without ever creating one itself, so a
    # supervisor or engineer can see at a glance which workstreams still need
    # `grogu plan workstream-worktree <id> --name <name>` run for them.
    worktrees = {
        entry["name"]: entry for entry in store.list_workstream_worktrees(plan_id)
    }

    def worktree_path(name: str) -> str:
        existing = worktrees.get(name)
        if existing:
            return existing["path"]
        return str(grogu_worktrees.workstream_worktree_path(store.root, plan_id, name))

    if args.json:
        # An orchestrator fans out from this. Returning only the wave names
        # meant the one caller that has to honour --model, --brief and
        # --review had to scrape them out of the pretty output.
        streams = store.summary(plan_id)["workstreams"]
        for stream in streams:
            stream["worktree"] = worktree_path(stream["name"])
            stream["worktree_ready"] = stream["name"] in worktrees
        print_json(
            {
                "batches": batches,
                "conflicts": conflicts,
                "workstreams": streams,
            }
        )
    else:
        streams = {
            stream["name"]: stream
            for stream in store.load(plan_id).get("workstreams", [])
        }
        for index, batch in enumerate(batches, start=1):
            print(f"wave {index}: {', '.join(batch)}")
            for name in batch:
                stream = streams.get(name, {})
                bits = [f"paths {' '.join(stream.get('paths', []))}"]
                if stream.get("model"):
                    bits.append(f"model {stream['model']}")
                for required in stream.get("required_reviews", []):
                    reviewed = any(
                        review.get("verdict") == "pass"
                        and review.get("kind") == required
                        for review in stream.get("reviews", [])
                    )
                    bits.append(
                        f"review {required}"
                        + (" (done)" if reviewed else " (outstanding)")
                    )
                print(f"    {name}: {'; '.join(bits)}")
                if stream.get("brief"):
                    print(f"      {stream['brief']}")
                if name in worktrees:
                    print(f"      worktree: {worktrees[name]['path']}")
                else:
                    print(
                        f"      worktree: not created yet -- grogu plan "
                        f"workstream-worktree {plan_id} --name {shlex.quote(name)}"
                    )
        for conflict in conflicts:
            left, right = conflict["workstreams"]
            print(f"conflict: {left} and {right} both claim {' / '.join(conflict['paths'])}")
        if not conflicts and len(batches) and max(len(batch) for batch in batches) > 1:
            print(
                "file sets are disjoint; these waves may run in parallel -- "
                "`grogu plan workstream-worktree <id> --name <name>` gives each "
                "one its own worktree"
            )
    return 3 if (conflicts and args.check) else 0


def plan_writer(args: argparse.Namespace) -> int:
    store = plan_store(args)
    plan_id = store.resolve(args.id)
    if not args.takeover:
        print_json(store.stage_version(plan_id, args.stage))
        return 0
    writer = store.supersede_stage_writer(
        plan_id,
        args.stage,
        role=getattr(args, "role", "") or "",
        agent=args.agent or "",
    )
    print(
        f"{plan_id} {args.stage}: active writer {writer['agent']}"
        + (
            f" (supersedes {writer['supersedes']})"
            if writer.get("supersedes")
            else ""
        )
    )
    return 0


def plan_agent_budget(args: argparse.Namespace) -> int:
    store = plan_store(args)
    result = store.configure_agent_governance(
        store.resolve(args.id),
        agent=args.agent or "",
        role=args.agent_role or "",
        workstream=args.workstream,
        tool_calls=args.tool_calls,
        elapsed_seconds=args.elapsed_seconds,
        ai_credits=args.ai_credits,
        checkpoint_tool_calls=args.checkpoint_tool_calls,
    )
    print_json(result) if args.json else print(
        f"{result['agent']}: governance limits recorded"
    )
    return 0


def plan_agent_usage(args: argparse.Namespace) -> int:
    store = plan_store(args)
    result = store.record_agent_usage(
        store.resolve(args.id),
        agent=args.agent or "",
        tool_calls=args.tool_calls,
        elapsed_seconds=args.elapsed_seconds,
        ai_credits=args.ai_credits,
    )
    print_json(result) if args.json else print(
        f"{result['agent']}: usage recorded"
    )
    return 0


def plan_checkpoint(args: argparse.Namespace) -> int:
    store = plan_store(args)
    result = store.record_checkpoint(
        store.resolve(args.id),
        agent=args.agent or "",
        commit=args.commit or "",
        note=args.note or "",
    )
    print_json(result) if args.json else print(
        f"{args.id}: checkpoint {result['id']} recorded"
    )
    return 0


def plan_checkpoint_recovery(args: argparse.Namespace) -> int:
    store = plan_store(args)
    result = store.record_checkpoint_recovery(
        store.resolve(args.id),
        args.checkpoint,
        agent=args.agent or "",
        status=args.status,
        note=args.note or "",
    )
    print_json(result) if args.json else print(
        f"{args.id}: checkpoint {result['checkpoint']} {result['status']}"
    )
    return 0


def plan_governance(args: argparse.Namespace) -> int:
    store = plan_store(args)
    result = store.governance_status(store.resolve(args.id))
    if args.json:
        print_json(result)
        return 0
    if not result["agents"]:
        print("no agent governance configured")
        return 0
    for name, item in result["agents"].items():
        print(
            f"{name}  role={item['role'] or '?'}"
            f" workstream={item['workstream'] or '-'}"
        )
        if item["usage"]:
            print(
                "  usage: "
                + ", ".join(
                    f"{key}={value}" for key, value in item["usage"].items()
                )
            )
        for warning in item["warnings"]:
            print(f"  warning: {warning}")
        for blocker in item["blockers"]:
            print(f"  blocker: {blocker}")
        if item["checkpoint_due"]:
            print(
                "  cancel/recover: use Copilot `/tasks`; "
                "restore the latest checkpoint"
            )
    return 0


def plan_workstream_worktree(args: argparse.Namespace) -> int:
    store = plan_store(args)
    plan_id = store.resolve(args.id)
    if getattr(args, "list", False):
        entries = store.list_workstream_worktrees(plan_id)
        if args.json:
            print_json({"plan": plan_id, "worktrees": entries})
            return 0
        if not entries:
            print(f"no workstream worktrees created yet for {plan_id}")
            return 0
        for entry in entries:
            flag = "" if entry["declared"] else "  (workstream no longer declared)"
            print(f"{entry['name']}: {entry['path']}  [{entry['branch']}]{flag}")
        return 0
    if not args.name:
        print(
            "grogu: --name is required unless you pass --list",
            file=sys.stderr,
        )
        return 2
    if getattr(args, "remove", False):
        result = store.remove_workstream_worktree(
            plan_id,
            args.name,
            force=args.force,
            delete_branch=args.delete_branch,
            base=args.base,
        )
        if args.json:
            print_json(result)
            return 0
        if not result["removed"]:
            print(f"no worktree to remove for workstream {args.name!r}")
            return 0
        detail = (
            " and deleted its branch"
            if result["branch_deleted"]
            else f" (branch kept: {result['branch_kept_reason']})"
            if result["branch_kept_reason"]
            else ""
        )
        print(f"removed {result['removed']}{detail}")
        return 0
    result = store.workstream_worktree(plan_id, args.name, base=args.base)
    if args.json:
        print_json(result)
        return 0
    state = "created" if result["created"] else "already there"
    print(f"workstream {args.name}: worktree {state} at {result['path']} (branch {result['branch']})")
    # The whole point is that this is directly usable: the caller -- an
    # engineer or the supervisor spawning one -- runs this line rather than
    # `cd`-ing into the shared checkout the way friction #40 described.
    print(f"cd {result['path']}")
    return 0


def plan_review(args: argparse.Namespace) -> int:
    store = plan_store(args)
    record = store.record_review(
        store.resolve(args.id),
        args.workstream,
        verdict=args.verdict,
        model=args.model or "",
        findings=" ".join(args.findings) if args.findings else "",
        kind=args.kind or "",
    )
    print(f"{record['kind']} review of {args.workstream}: {record['verdict']}")
    return 0


def plan_commission(args: argparse.Namespace) -> int:
    store = plan_store(args)
    plan_id = store.resolve(args.id)
    manifest = store.commission(
        plan_id,
        args.for_role,
        args.brief,
        by=getattr(args, "role", "") or "",
        replace=args.replace,
    )
    for warning in manifest.get("warnings", []):
        print(f"grogu: {warning}", file=sys.stderr)
    print(
        f"{plan_id}: commissioned the {args.for_role}; it arrives in "
        f"`grogu plan brief --role {args.for_role} --plan {plan_id}`"
    )
    return 0


_PLAN_ID = re.compile(r"^p-\d{8}-[0-9a-f]{6}$")


def plan_steer(args: argparse.Namespace) -> int:
    store = plan_store(args)
    plan_id = store.resolve(args.id) if args.id else ""
    if getattr(args, "retract", 0):
        result = store.retract_steering(args.retract, plan_id=plan_id)
        print(f"retracted steering #{result['seq']}")
        if result["delivered"]:
            print(
                "  it had already been delivered; the agent that read it still "
                "has it, so say so directly if it matters"
            )
        return 0
    words = list(args.text)
    # Every other plan command takes the id positionally. This one takes
    # `--plan`, so `grogu plan steer p-... --note "..."` parsed the id as the
    # note, recorded a plan id as the steering, and dropped the real note
    # without a word. A leading token shaped like a plan id is a scope.
    if words and _PLAN_ID.match(words[0]):
        if not plan_id:
            plan_id = store.resolve(words[0])
        words = words[1:]
    positional = " ".join(words).strip()
    supplied = (getattr(args, "note", "") or "").strip()
    if positional and supplied:
        print(
            "grogu: two notes given, one positionally and one with --note; "
            "pass the note once",
            file=sys.stderr,
        )
        return 2
    text = positional or supplied
    if not text:
        print("grogu: nothing to steer with; pass the note as text or --note", file=sys.stderr)
        return 2
    note = store.steer(
        text,
        plan_id=plan_id,
        role=args.role or "all",
        requires_replan=args.requires_replan,
        relayed=getattr(args, "relayed", False),
    )
    scope = plan_id or "repository"
    whose = " in the user's words" if note.get("relayed_by") else ""
    print(f"steering #{note['seq']} recorded for {note['role']} on {scope}{whose}")
    if note["requires_replan"]:
        print("plan moved to needs_review: the architect must fold this in before work continues")
    _relay_hint(note, plan_id)
    return 0


def _relay_hint(note: dict, plan_id: str) -> None:
    """Tell the spawner to push the note now rather than wait for a poll.

    A queued message lands at the agent's next turn boundary, which is seconds
    to minutes away; the banner lands whenever it next happens to run `grogu`,
    which may be much longer. So the fast path is the spawner relaying with
    `write_agent`, and this is the reminder, printed where the user's own
    session will read it.

    The reminder is unconditional. The activity log only knows agents that have
    already run a `grogu` command, and an engineer spawned a minute ago has not
    — exactly the agent most likely to be steered. Naming who we know about is
    a floor, never the list.
    """
    role = note.get("role", "all")
    # Scoped to this working tree on purpose. The activity feed is machine-wide,
    # so an unscoped read offered up an engineer working in an unrelated
    # repository and told the user to relay this note into it -- steering for
    # one project pushed into another project's agent.
    here = str(Path.cwd())
    try:
        known = [
            row
            for row in grogu_watch.sessions(window_minutes=30)
            if row.get("role")
            and role in ("all", row["role"])
            and (not plan_id or row.get("plan") in ("", plan_id))
            and row.get("state") != "gone"
            and str(row.get("cwd", "")) == here
        ]
    except Exception:  # a hint must never be why steering fails to record
        known = []
    target = "" if role == "all" else f" --role {role}"
    plan_part = f" --plan {plan_id}" if plan_id else ""
    # Name each agent separately. A fan-out is the case where relaying matters
    # most and the case this hint used to handle worst: two engineers on one
    # plan printed as one word, "engineer", with no way to tell how many there
    # were or what to pass to --agent for each.
    named = [row for row in known if row.get("agent") and not row["agent"].startswith("/")]
    if known:
        print(
            "  seen recently: "
            + ", ".join(
                (f"{row['role']}@{row['agent']}" if row in named else row["role"])
                + f" (idle {int(row['idle_seconds'] // 60)}m)"
                for row in known
            )
        )
    print(
        f"  relay this into any running {role} agent with write_agent now, "
        "then ack it for each one so it is not delivered twice:"
    )
    if named:
        for row in named:
            print(
                f"    grogu plan steering{target}{plan_part} --ack "
                f"--agent {row['agent']}"
            )
        print(
            "  and once more for any agent above that has not run a grogu "
            "command yet, naming the GROGU_AGENT you spawned it with."
        )
    else:
        print(
            f"    grogu plan steering{target}{plan_part} --ack --agent <GROGU_AGENT>"
        )
        print(
            "  where <GROGU_AGENT> is the value you set in that agent's "
            "environment when you spawned it."
        )


def plan_steering(args: argparse.Namespace) -> int:
    store = plan_store(args)
    reference = args.id or getattr(args, "plan", "") or ""
    plan_id = store.resolve(reference) if reference else ""
    role = getattr(args, "role", "") or "all"
    if args.ack:
        acked = store.ack_steering(
            role=_plan_role(args), plan_id=plan_id, agent=getattr(args, "agent", "") or ""
        )
        who = f" for {args.agent}" if getattr(args, "agent", "") else ""
        print(f"acked steering for {acked['role']}{who} (repo {acked['repository_seq']}, plan {acked['plan_seq']})")
        return 0
    # A declared role is an agent asking "is there anything new for me",
    # and answering with the whole history every time is how a poll-at-
    # decision-points instruction turns into a context leak.
    if getattr(args, "audit", 0):
        note = store.audit_note(args.audit, plan_id=plan_id)
        if args.json:
            print_json(note)
            return 0
        state = " (retracted)" if note["retracted"] else ""
        print(f"{note['source']} #{note['seq']} ->{note['role']}{state}: {note['text']}")
        seen = note.get("last_seen") or {}

        def describe(keys: list) -> str:
            parts = []
            for key in keys:
                stamp = seen.get(key, "")
                parts.append(f"{key} (last seen {stamp})" if stamp else key)
            return ", ".join(parts)

        print("  read by: " + (describe(note["read_by"]) or "nobody yet"))
        if note["unread_by"]:
            print("  not yet read by: " + describe(note["unread_by"]))
            print(
                "  agents that have gone quiet stay on this list; it is every "
                "agent this plan has ever seen, not only the running ones."
            )
        return 0
    unread_only = args.unread or (role != "all" and not args.all)
    # Who is *asking* is not the same as whose steering is being asked about.
    # The user checking that a note landed was acking it on the agent's behalf,
    # so the agent's own first poll came back empty and the steering was lost
    # in the one direction that matters most. A person looking is a peek.
    caller_is_agent = bool(
        grogu_plans.current_role() or (getattr(args, "agent", "") or "")
    )
    result = store.steering(role=role, plan_id=plan_id, unread=unread_only)
    if args.json:
        print_json(result)
        if unread_only and caller_is_agent:
            store.ack_steering(role=role, plan_id=plan_id)
        return 0
    shown = 0
    for scope in ("repository", "plan"):
        for note in result.get(scope, []):
            binding = " [requires replan]" if note.get("requires_replan") else ""
            print(f"{scope} #{note['seq']}  {note['at']}  ->{note['role']}{binding}: {note['text']}")
            shown += 1
    if unread_only:
        if not shown:
            # An engineer that was relayed a note and then polled saw the same
            # blank answer it would get if the note had never been recorded,
            # and could not tell the two apart. The count is the cheapest
            # possible way to say "it is here, you have had it".
            history = store.steering(role=role, plan_id=plan_id, unread=False)
            seen = len(history.get("repository", [])) + len(history.get("plan", []))
            if seen:
                print(
                    f"no unread steering for the {role} "
                    f"({seen} already read; `--all` replays them, "
                    "`--audit <n>` says who read note n without consuming it)"
                )
            else:
                print(f"no steering for the {role} on this plan")
        elif caller_is_agent:
            # An agent told in conversation that it had already been acked for
            # a note could not check that claim: nothing named a note number
            # and said who had read it. It acted on faith, then polled again to
            # see whether the note resurfaced, which is a round trip spent on
            # distrust. `--audit` answers it directly, and was undiscoverable.
            print(
                f"({shown} shown once and marked read; `--all` replays the "
                "history, `--audit <n>` says who has read note n)"
            )
        else:
            print(
                f"({shown} unread by the {role}; you are looking, not "
                "consuming, so it is still waiting for them. "
                "`--audit <n>` names who has read note n.)"
            )
        if caller_is_agent:
            store.ack_steering(role=role, plan_id=plan_id)
    return 0


def plan_brief(args: argparse.Namespace) -> int:
    store = plan_store(args)
    plan_id = store.resolve(args.id) if args.id else ""
    brief = store.brief(args.role, plan_id=plan_id, base_dir=ROOT / ".github" / "agents")
    if args.json:
        print_json(brief)
        return 0
    # The role prompt itself is already the agent's system prompt; reprinting it
    # here would spend a hundred lines of context restating what the agent was
    # instantiated from. --full exists for inspecting a brief from outside.
    if args.full and brief["base"]:
        print(brief["base"].rstrip())
    elif brief["base"]:
        # Not printing it is deliberate -- it is already the agent's system
        # prompt, and reprinting costs a hundred lines of context. But saying
        # nothing at all reads as "there is no contract", which an architect
        # reported as the brief's biggest gap.
        print(
            f"contract: {ROOT / '.github' / 'agents' / (args.role + '.md')} "
            f"({len(brief['base'])} bytes, `--full` to print it here)"
        )
    if brief["overlay"]:
        print(f"\n## Repository specifics ({brief['overlay_path']})\n")
        print(brief["overlay"].rstrip())
    else:
        print(
            f"\n(no repository overlay at {brief['overlay_path']}; "
            "write one to give this role repository-specific context)"
        )
    principles = brief.get("design_principles") or []
    if principles:
        print("\n## The user's design principles\n")
        for principle in principles:
            print(f"- [{principle['scope']}] {principle['statement']}")
    commission = brief.get("commission") or {}
    if commission.get("brief"):
        print("\n## What the architect is asking you for\n")
        print(commission["brief"].rstrip())
    notes = brief["steering"].get("repository", []) + brief["steering"].get("plan", [])
    if notes:
        print("\n## Standing steering\n")
        for note in notes:
            binding = " [requires replan]" if note.get("requires_replan") else ""
            # Attributing an architect's note to the user misleads exactly the
            # role that is supposed to weigh whose opinion it is.
            source = (
                "the harness"
                if note.get("automatic")
                else "the user"
                if note.get("from") in ("", None, "user")
                else f"the {note['from']}"
            )
            if note.get("relayed_by"):
                source += f", relayed by the {note['relayed_by']}"
            print(f"- ({source}) {note['text']}{binding}")
    attached = brief.get("attachments") or []
    if attached:
        print("\n## Artifacts attached to this plan\n")
        for item in attached:
            where = f" [{item['stage']}]" if item.get("stage") else ""
            reason = f" -- {item['note']}" if item.get("note") else ""
            print(
                f"- {item['name']}{where} ({item['bytes']} bytes, "
                f"from the {item.get('role') or '?'}){reason}"
            )
        print("  read them under .grogu/plans/<id>/attachments/")
    summary = brief.get("summary") or {}
    if summary:
        print(
            f"\nPlan {summary.get('id')}: status {summary.get('status')}, "
            f"{len(summary.get('open_amendments') or [])} open amendment(s), "
            f"{len(summary.get('open_defects') or [])} open defect(s)"
        )
    # The user reviews the plan through `grogu review`; the architect reads the
    # round back here, without a browser. Assembled in the CLI from the review
    # store so the module dependency stays one-way (grogu_plans never imports
    # grogu_review).
    if plan_id and args.role == grogu_plans.ARCHITECT:
        review = grogu_review.ReviewStore(store)
        if review.path(plan_id).exists():
            rsummary = review.summary(plan_id)
            open_threads = review.threads(plan_id, status="open")
            if open_threads:
                print(
                    f"\n## Open review comments ({rsummary.get('open', len(open_threads))})\n"
                )
                for thread in open_threads:
                    target = _thread_target(thread)
                    comments = thread.get("comments", [])
                    body = comments[0].get("body", "") if comments else ""
                    print(f"- [{thread['id']} {thread['stage']} {_thread_state_word(thread)}] {target}")
                    if body:
                        print(f"    {body}")
    return 0


def plan_attach(args: argparse.Namespace) -> int:
    store = plan_store(args)
    body = _read_body(args)
    if not body.strip():
        print("nothing to attach: pass --file or --body", file=sys.stderr)
        return 2
    try:
        result = store.attach(
            args.id,
            args.name or (os.path.basename(args.file) if args.file and args.file != "-" else ""),
            body,
            stage=args.stage or "",
            role=args.role or os.environ.get("GROGU_ROLE", ""),
            note=args.note or "",
            verifier=getattr(args, "verifier", False),
        )
    except grogu_plans.PlanError as error:
        print(str(error), file=sys.stderr)
        return 3
    verb = "replaced" if result["replaced"] else "attached"
    print(f"{verb} {result['name']} ({result['bytes']} bytes)")
    print("every role reading `grogu plan brief` for this plan will be told it exists")
    return 0


def plan_verify(args: argparse.Namespace) -> int:
    store = plan_store(args)
    result = store.run_verifiers(args.id, role=getattr(args, "role", "") or "")
    for item in result["results"]:
        mark = "pass" if item["passed"] else "FAIL"
        print(f"{mark}  {item['name']}")
        if not item["passed"]:
            print("    " + (item["output"].strip().splitlines() or [""])[-1])
    return 0 if result["passed"] else 1


def plan_triage(args: argparse.Namespace) -> int:
    result = grogu_plans.triage(" ".join(args.text))
    if args.json:
        print_json(result)
        return 0
    print(f"{result['decision']}: {result['explanation']}")
    for reason in result["reasons"]:
        print(f"  - {reason}")
    return 0


def plan_retro(args: argparse.Namespace) -> int:
    store = plan_store(args)
    report = store.retro(store.resolve(args.id))
    if args.json:
        print_json(report)
        return 0
    print(f"{report['plan']}  {report['status']}  {report['title']}")
    print(
        f"  amendment rounds {report['amendment_rounds']}, "
        f"engineer/tester rounds {report['defect_rounds']}, "
        f"steering notes {report['steering_notes']}"
    )
    if report["clean"]:
        print("  no friction signals: the plan held")
        return 0
    for finding in report["findings"]:
        print(f"  [{finding['target']}] {finding['signal']} x{finding['count']}: {finding['detail']}")
        for example in finding.get("examples", []):
            print(f"      - {example}")
    # A retro that names the target and not the file is a suggestion nobody
    # acts on. Every target here has an address.
    overlays = sorted(
        {
            finding["target"][: -len("_overlay")]
            for finding in report["findings"]
            if finding["target"].endswith("_overlay")
        }
    )
    for role in overlays:
        print(f"  write it down: {store.overlay_path(role)}")
    if any(finding["target"] == "harness" for finding in report["findings"]):
        print("  file the harness gaps: grogu plan friction --note \"...\"")
    if not overlays:
        print("  fix the overlay or the harness, not just this plan")
    return 0


def plan_friction(args: argparse.Namespace) -> int:
    store = plan_store(args)
    target = grogu_plans.TARGET_HARNESS if args.harness else grogu_plans.TARGET_REPO
    if getattr(args, "repo_only", False):
        target = grogu_plans.TARGET_REPO_ONLY
    if args.note:
        rerouted = (
            target == grogu_plans.TARGET_REPO
            and not getattr(args, "repo_only", False)
            and store.names_the_harness(args.note)
        )
        entry = store.note_friction(
            args.note,
            plan_id=store.resolve(args.id) if args.id else "",
            role=getattr(args, "role", "") or "",
            target=target,
        )
        harness = args.harness or rerouted
        where = "about Grogu itself" if harness else "about this repository"
        print(f"recorded friction #{entry['seq']} {where} from the {entry['role']}")
        if rerouted:
            print(
                "  (it named a grogu command, so it was pooled across "
                "repositories; `--repo-only` to keep it here)"
            )
        return 0
    if args.resolve:
        if args.harness:
            done = grogu_plans.resolve_harness_friction(
                args.resolve, resolution=args.resolution or "addressed"
            )
            print("resolved" if done else "no such pending harness friction")
            return 0 if done else 2
        entry = store.resolve_friction(args.resolve, note=args.resolution or "addressed")
        print(f"friction #{entry['seq']} resolved")
        return 0
    report = store.friction(include_resolved=args.all)
    if args.json:
        print_json(report)
        return 0
    if args.claim:
        result = grogu_plans.claim_harness_friction(args.claim, reference=args.reference or "")
        print(f"{result['cluster']} claimed by {result['claim']}")
        return 0
    if args.ripe:
        clusters = grogu_plans.cluster_harness_friction()
        if args.json:
            print_json(clusters)
            return 0
        shown = [
            cluster
            for cluster in clusters
            if args.all or cluster["ripe"] or cluster["stale"]
        ]
        if not shown:
            print("nothing ripe; friction is still accumulating")
            return 0
        for cluster in shown:
            mark = "ripe" if cluster["ripe"] else "stale" if cluster["stale"] else "-"
            print(f"{cluster['id']}  [{mark}: {cluster['reason']}]  {cluster['title']}")
            for note in cluster["notes"]:
                print(f"    - {note}")
            print(
                f"    seen {cluster['count']}x in {', '.join(cluster['repositories']) or '?'}"
                f"; raised by {', '.join(cluster['roles'])}; open {cluster['age_days']}d"
            )
            if cluster["claim"]:
                print(f"    claimed: {cluster['claim']}")
        print(
            "\nFix these in the Grogu repository. Claim one with "
            "`grogu plan friction --claim f1 --reference <pr>` so it stops being "
            "proposed, and `--harness --resolve <seq>` when it ships."
        )
        return 0
    if args.harness:
        for entry in report["harness"]:
            print(f"#{entry['seq']}  [{entry.get('repository', '?')}] {entry['note']}")
        if not report["harness"]:
            print("no unreviewed friction with the harness")
        else:
            # These stay open until someone says they are shut, and an open
            # note is counted forever: twenty-one fixed complaints sitting
            # here would dilute every cluster computed afterwards.
            print(
                "\nClose each one as it ships: "
                "`grogu plan friction --harness --resolve <seq> "
                '--resolution "<commit or PR>"`.'
            )
        return 0
    if report["harness"]:
        print(
            f"({len(report['harness'])} note(s) about the harness itself; "
            "`--harness` to see them)"
        )
    for entry in report["notes"]:
        plan = f" ({entry['plan']})" if entry.get("plan") else ""
        print(f"#{entry['seq']}  {entry['role']}{plan}: {entry['note']}")
    for bucket in report["signals"]:
        mark = "*" if bucket["plans"] > 1 else " "
        print(
            f"{mark} {bucket['signal']}: {bucket['count']} across {bucket['plans']} plan(s)"
            f" -> {bucket['target']}"
        )
    print(report["verdict"])
    return 0


def _doc_role(args: argparse.Namespace) -> str:
    return (
        (getattr(args, "role", "") or "").strip()
        or grogu_plans.current_role()
        or grogu_plans.REVIEWER
    )


def _doc_store(
    args: argparse.Namespace,
    *,
    auto_migrate: bool = False,
) -> tuple[grogu_plans.PlanStore, grogu_plans.PlanDocumentStore]:
    store = plan_store(args)
    plan_id = store.resolve(args.id)
    if not store.is_document_plan(plan_id):
        if not auto_migrate:
            raise grogu_plans.PlanError(
                f"plan {plan_id} uses the legacy layout; run "
                f"`grogu plan doc migrate {plan_id}`"
            )
        result = grogu_plans.PlanDocumentStore.migrate(
            store, plan_id, role=_doc_role(args)
        )
        if not getattr(args, "json", False):
            print(
                f"migrated {plan_id} to {result['to']} "
                "(the verbatim legacy copy is retained)"
            )
    return store, grogu_plans.PlanDocumentStore.for_plan(store, plan_id)


def _stdin_value(value: str) -> str:
    return sys.stdin.read() if value == "-" else value


def _json_input(
    filename: str,
    *,
    service: Optional[grogu_plans.PlanDocumentStore] = None,
) -> object:
    if not filename:
        raise grogu_plans.PlanError("a JSON --file is required")
    if filename == "-":
        raw = sys.stdin.read()
    else:
        path = Path(filename).expanduser().resolve()
        if service is not None:
            package = service.package.resolve()
            if path == package or package in path.parents:
                raise grogu_plans.PlanError(
                    "refusing a patch file inside the .plan package; that "
                    "would make compiled or private package state an input to "
                    "its own rewrite"
                )
        try:
            raw = path.read_text(encoding="utf8")
        except (OSError, UnicodeDecodeError) as error:
            raise grogu_plans.PlanError(f"cannot read {path}: {error}") from error
    if len(raw.encode("utf8")) > grogu_plans.PlanDocumentStore.MAX_REQUEST_BYTES:
        raise grogu_plans.PlanError("JSON input exceeds 256 KB")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise grogu_plans.PlanError(
            f"invalid JSON at line {error.lineno}, column {error.colno}"
        ) from error


def _patch_ops(value: object) -> list[dict]:
    if isinstance(value, dict):
        value = value.get("ops")
    if not isinstance(value, list) or not all(
        isinstance(item, dict) for item in value
    ):
        raise grogu_plans.PlanError(
            "patch input must be an array of operations or an object with ops"
        )
    return value


def plan_doc_create(args: argparse.Namespace) -> int:
    store = plan_store(args)
    manifest = grogu_plans.PlanDocumentStore.create(
        store,
        args.title,
        task_id=args.task or "",
        design=args.design,
        evaluation=args.eval,
        review_required=args.review_required,
    )
    if args.json:
        print_json(manifest)
    else:
        print(manifest["id"])
    return 0


def plan_doc_open(args: argparse.Namespace) -> int:
    store, documents = _doc_store(args)
    plan_id = documents.plan_id

    def on_ready(info: dict) -> None:
        launch_url = f"{info['url']}/?t={info['token']}"
        if args.json:
            print_json({**info, "launch_url": launch_url})
        elif args.no_open:
            print(f"open {launch_url}")
        else:
            _open_in_browser(launch_url)
            print(f"{info['url']}  opened in your browser")

    grogu_plan_server.serve(
        plan_id,
        role=_doc_role(args),
        stage=args.stage or "",
        mode=args.mode,
        port=args.port,
        timeout=args.timeout,
        store=store,
        on_ready=on_ready,
    )
    return 0


def plan_doc_show(args: argparse.Namespace) -> int:
    _store, documents = _doc_store(args)
    result = documents.query(
        role=_doc_role(args),
        stage=args.stage or "",
        kind=args.kind or "",
        text=args.text or "",
        identifier=args.node or "",
    )
    if args.json:
        print_json(result)
        return 0
    for node in result["nodes"]:
        print(f"{node['id']}  {node['kind']:<12} {node['stage'] or 'plan'}  {node['title']}")
        if args.body and node.get("body"):
            print(node["body"])
    for edge in result["edges"]:
        print(f"{edge['id']}  {edge['kind']:<12} {edge['from']} -> {edge['to']}")
    return 0


def plan_doc_projection(args: argparse.Namespace) -> int:
    _store, documents = _doc_store(args)
    stages = [args.stage] if args.stage else None
    result = documents.projection(
        role=_doc_role(args),
        stages=stages,
        include=args.include,
        budget=args.budget,
        since=args.since or "",
        format=args.format,
    )
    if args.json:
        print_json(result)
    elif args.format == "md":
        sys.stdout.write(str(result["payload"]))
    else:
        print_json(result["payload"])
    return 0


def plan_doc_context(args: argparse.Namespace) -> int:
    return plan_doc_projection(args)


def plan_doc_query(args: argparse.Namespace) -> int:
    return plan_doc_show(args)


def plan_doc_lint(args: argparse.Namespace) -> int:
    _store, documents = _doc_store(args)
    result = documents.verify(role=_doc_role(args))
    if args.json:
        print_json(result)
    else:
        print(
            f"{result['plan']} {result['head']}: package, log, partitions and "
            f"{len(result['projections'])} readable projection(s) verified"
        )
    return 0


def plan_doc_compile(args: argparse.Namespace) -> int:
    _store, documents = _doc_store(args)
    result = documents.compile(
        role=_doc_role(args),
        stages=[args.stage] if args.stage else None,
        check=args.check,
    )
    if args.json:
        print_json(result)
    else:
        verb = "would rewrite" if args.check else "compiled"
        if result["changed"]:
            print(f"{verb}: {', '.join(result['changed'])}")
        else:
            print(f"{result['plan']} {result['revision']}: compiled artifacts match")
    return 0 if result["ok"] else 3


def plan_doc_patch(args: argparse.Namespace) -> int:
    _store, documents = _doc_store(args)
    operations = _patch_ops(_json_input(args.file, service=documents))
    base = args.base or documents.head()
    result = documents.patch(
        role=_doc_role(args),
        base=base,
        operations=operations,
        intent=_stdin_value(args.intent or ""),
        origin="cli",
        dry_run=args.dry_run,
    )
    if args.json:
        print_json(result)
    else:
        suffix = " (dry run)" if args.dry_run else ""
        print(f"{result['revision']}  {len(result['changed'])} object(s) changed{suffix}")
    return 0


def _attrs(values: list[str]) -> dict:
    result = {}
    for value in values or []:
        key, separator, raw = value.partition("=")
        if not separator or not key:
            raise grogu_plans.PlanError("--attr values must be key=value")
        try:
            result[key] = json.loads(raw)
        except json.JSONDecodeError:
            result[key] = raw
    return result


def plan_doc_node_add(args: argparse.Namespace) -> int:
    _store, documents = _doc_store(args)
    role = _doc_role(args)
    document = documents.load(role=role, record=False)
    manifest = documents.plans.load(documents.plan_id)
    counter_manifest = {
        "plandoc": {
            "counters": copy.deepcopy(
                manifest.get("plandoc", {}).get(
                    "counters", documents._counter_state(document)
                )
            )
        }
    }
    node_id = args.node or grogu_plandoc.allocate_id(
        counter_manifest, args.kind, existing_ids=document["nodes"]
    )
    body = _stdin_value(args.body or "")
    node = grogu_plandoc.make_node(
        node_id,
        args.kind,
        args.title,
        stage=args.stage or "",
        body=body,
        attrs=_attrs(args.attr),
        order=args.order,
        revision=document["revision"],
    )
    result = documents.patch(
        role=role,
        base=document["revision"],
        operations=[{"op": "add", "path": f"/nodes/{node_id}", "value": node}],
        intent=args.intent or f"add {node_id}",
    )
    print_json(result | {"node": node_id}) if args.json else print(
        f"added {node_id} in {result['revision']}"
    )
    return 0


def plan_doc_node_set(args: argparse.Namespace) -> int:
    _store, documents = _doc_store(args)
    role = _doc_role(args)
    document = documents.load(role=role, record=False)
    if args.node not in document["nodes"]:
        raise grogu_plans.PlanError(f"no node {args.node!r}")
    operations = []
    for field, value in (
        ("title", args.title),
        ("body", _stdin_value(args.body) if args.body is not None else None),
        ("order", args.order),
    ):
        if value is not None:
            operations.append(
                {
                    "op": "replace",
                    "path": f"/nodes/{args.node}/{field}",
                    "value": value,
                }
            )
    for key, value in _attrs(args.attr).items():
        op = "replace" if key in document["nodes"][args.node]["attrs"] else "add"
        operations.append(
            {
                "op": op,
                "path": f"/nodes/{args.node}/attrs/{key}",
                "value": value,
            }
        )
    if not operations:
        raise grogu_plans.PlanError("node set needs a field to change")
    result = documents.patch(
        role=role,
        base=document["revision"],
        operations=operations,
        intent=args.intent or f"update {args.node}",
    )
    print_json(result) if args.json else print(
        f"updated {args.node} in {result['revision']}"
    )
    return 0


def plan_doc_node_rm(args: argparse.Namespace) -> int:
    _store, documents = _doc_store(args)
    role = _doc_role(args)
    document = documents.load(role=role, record=False)
    if args.node not in document["nodes"]:
        raise grogu_plans.PlanError(f"no node {args.node!r}")
    operations = [
        {"op": "remove", "path": f"/edges/{edge_id}"}
        for edge_id, edge in document["edges"].items()
        if args.node in {edge["from"], edge["to"]}
    ]
    operations.append({"op": "remove", "path": f"/nodes/{args.node}"})
    result = documents.patch(
        role=role,
        base=document["revision"],
        operations=operations,
        intent=args.intent or f"remove {args.node}",
    )
    print_json(result) if args.json else print(
        f"removed {args.node} in {result['revision']}"
    )
    return 0


def plan_doc_link(args: argparse.Namespace) -> int:
    _store, documents = _doc_store(args)
    role = _doc_role(args)
    document = documents.load(role=role, record=False)
    manifest = documents.plans.load(documents.plan_id)
    counter_manifest = {
        "plandoc": {
            "counters": copy.deepcopy(
                manifest.get("plandoc", {}).get(
                    "counters", documents._counter_state(document)
                )
            )
        }
    }
    edge_id = args.edge or grogu_plandoc.allocate_id(
        counter_manifest, "edge", existing_ids=document["edges"]
    )
    edge = grogu_plandoc.make_edge(
        edge_id,
        args.kind,
        args.source,
        args.target,
        attrs=_attrs(args.attr),
        revision=document["revision"],
    )
    result = documents.patch(
        role=role,
        base=document["revision"],
        operations=[{"op": "add", "path": f"/edges/{edge_id}", "value": edge}],
        intent=args.intent or f"link {args.source} to {args.target}",
    )
    print_json(result | {"edge": edge_id}) if args.json else print(
        f"added {edge_id} in {result['revision']}"
    )
    return 0


def plan_doc_unlink(args: argparse.Namespace) -> int:
    _store, documents = _doc_store(args)
    role = _doc_role(args)
    document = documents.load(role=role, record=False)
    if args.edge not in document["edges"]:
        raise grogu_plans.PlanError(f"no edge {args.edge!r}")
    result = documents.patch(
        role=role,
        base=document["revision"],
        operations=[{"op": "remove", "path": f"/edges/{args.edge}"}],
        intent=args.intent or f"unlink {args.edge}",
    )
    print_json(result) if args.json else print(
        f"removed {args.edge} in {result['revision']}"
    )
    return 0


def plan_doc_revisions(args: argparse.Namespace) -> int:
    _store, documents = _doc_store(args)
    values = documents.revisions(role=_doc_role(args))
    if args.json:
        print_json({"revisions": values})
    else:
        for value in reversed(values):
            print(
                f"{value['revision']}  {value['at']}  "
                f"{value['origin']:<20} {value['intent']}"
            )
    return 0


def plan_doc_diff(args: argparse.Namespace) -> int:
    _store, documents = _doc_store(args)
    result = documents.diff(
        role=_doc_role(args), before=args.from_revision, after=args.to_revision
    )
    if args.json:
        print_json(result)
    else:
        print(json.dumps(result["ops"], indent=2, ensure_ascii=False))
    return 0


def plan_doc_propose(args: argparse.Namespace) -> int:
    _store, documents = _doc_store(args)
    operations = _patch_ops(_json_input(args.file, service=documents))
    proposal = documents.propose(
        role=_doc_role(args),
        operations=operations,
        why=_stdin_value(args.why),
        base=args.base or "",
        from_thread=args.from_thread or "",
    )
    print_json(proposal) if args.json else print(proposal["id"])
    return 0


def plan_doc_revise(args: argparse.Namespace) -> int:
    return plan_doc_propose(args)


def plan_doc_proposals(args: argparse.Namespace) -> int:
    _store, documents = _doc_store(args)
    values = documents.proposals(role=_doc_role(args))
    if args.json:
        print_json({"proposals": values})
    else:
        for value in values:
            print(f"{value['id']}  {value['status']:<9} {value['why']}")
    return 0


def plan_doc_accept(args: argparse.Namespace) -> int:
    _store, documents = _doc_store(args)
    result = documents.accept_proposal(args.proposal, role=_doc_role(args))
    print_json(result) if args.json else print(result["revision"])
    return 0


def plan_doc_reject(args: argparse.Namespace) -> int:
    _store, documents = _doc_store(args)
    result = documents.reject_proposal(
        args.proposal,
        role=_doc_role(args),
        why_not=_stdin_value(args.why),
    )
    print_json(result) if args.json else print(
        f"{args.proposal} rejected"
    )
    return 0


def plan_doc_impact(args: argparse.Namespace) -> int:
    _store, documents = _doc_store(args)
    if args.select.startswith("{"):
        selector = json.loads(args.select)
    else:
        selector = {"type": "node", "id": args.select}
    result = documents.impact(
        role=_doc_role(args), selector=selector, depth=args.depth
    )
    if args.json:
        print_json(result)
    else:
        for item in result["direct"]:
            print(f"direct       {item['id']}  {item['title']}")
        for item in result["transitive"]:
            print(f"transitive   {item['id']}  {item['reason']}")
    return 0


def plan_doc_export(args: argparse.Namespace) -> int:
    _store, documents = _doc_store(args)
    result = documents.export(
        role=_doc_role(args),
        stages=[args.stage] if args.stage else None,
        include=args.include,
        budget=args.budget,
        since=args.since or "",
        format=args.format,
        output=Path(args.output).expanduser() if args.output else None,
    )
    if args.json:
        print_json(result)
    elif not args.output:
        if isinstance(result["payload"], str):
            sys.stdout.write(result["payload"])
        else:
            print_json(result["payload"])
    else:
        print(result["output"])
    return 0


def plan_doc_migrate(args: argparse.Namespace) -> int:
    store = plan_store(args)
    result = grogu_plans.PlanDocumentStore.migrate(
        store,
        store.resolve(args.id),
        role=_doc_role(args),
        dry_run=args.dry_run,
    )
    print_json(result) if args.json else print(
        f"{result['plan']}: {'would migrate' if args.dry_run else 'migrated'} "
        f"{result['from']} -> {result['to']}"
    )
    return 0


def plan_doc_revert(args: argparse.Namespace) -> int:
    store = plan_store(args)
    result = grogu_plans.PlanDocumentStore.revert(
        store,
        store.resolve(args.id),
        role=_doc_role(args),
        dry_run=args.dry_run,
    )
    print_json(result) if args.json else print(
        f"{result['plan']}: {'would restore' if args.dry_run else 'restored'} "
        f"{result['to']}"
    )
    return 0


def plan_doc_control(args: argparse.Namespace) -> int:
    _store, documents = _doc_store(args)
    role = _doc_role(args)
    if args.feedback is not None:
        if not args.agent:
            raise grogu_plans.PlanError("--feedback requires --agent")
        result = documents.route_feedback(
            role=role,
            scope={
                "kind": "agent",
                "agent_key": args.agent,
                "label": f"agent {args.agent}",
            },
            text=_stdin_value(args.feedback),
            binding=args.binding,
        )
    elif args.agent:
        result = documents.control_drill(args.agent, role=role)
    else:
        result = documents.control_snapshot(
            role=role,
            plan=args.filter_plan or "",
            filter_role=args.filter_role or "",
            workstream=args.workstream or "",
            state=args.state or "",
            window_minutes=args.window,
        )
    if args.json:
        print_json(result)
    else:
        if args.feedback is not None:
            print(f"{result['seq']} -> {result['delivered_to']}")
        elif args.agent:
            print(f"{result['agent']['agent']}  {result['agent']['badge']}")
            for event in result["activity"]:
                print(f"  {event['type']:<20} {event['summary']}")
        else:
            for agent in result["agents"]:
                print(
                    f"{agent['badge']:<12} {agent['role']:<10} "
                    f"{agent['agent']}  {agent['plan']}"
                )
    return 0


def plan_doc_register(args: argparse.Namespace) -> int:
    _store, documents = _doc_store(args)
    value = documents.register_session(
        {
            "run_id": args.run_id,
            "repository": str(documents.plans.root),
            "plan": documents.plan_id,
            "agent": args.agent,
            "role": args.session_role,
            "workstream": args.workstream or "",
            "session_id": args.session_id,
            "agent_id": args.agent_id,
            "registered_at": args.registered_at or now(),
            "events_path": args.events or "",
        }
    )
    print_json(value) if args.json else print(
        f"registered {value['run_id']} for {value['agent']}"
    )
    return 0


def _skill_repo(args: argparse.Namespace) -> Path:
    """The working tree the skill belongs to, not the primary one.

    `PlanStore.root` deliberately resolves to the primary worktree so several
    worktrees share one set of plans. Skills are the opposite: they are files
    on a branch, reviewed in that branch's diff. Resolving them the plan way
    hid every skill added on this branch from `skill list`, and `skill accept`
    would have written the new file into whatever branch the primary worktree
    happened to have checked out.
    """
    if getattr(args, "repo", None):
        return Path(args.repo).expanduser().resolve()
    return Path(grogu_tasks.repository_root())


BINDING_STALE_HOURS = 12


def _stale(stamp: Optional[str]) -> bool:
    """Whether a session binding is too old to refuse on.

    A binding is written once and never cleared -- the shell that made it just
    stops existing. Refusing forever on a role that finished last week turns
    the user out of his own checkout with a message about an agent that is not
    there, and the only way back is to guess at `--role supervisor`. Steering
    still honours old bindings, because delivering a note to a shell that has
    gone costs nothing; refusing on one costs the user his commands.
    """
    if not stamp:
        return True
    try:
        when = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    age = dt.datetime.now(dt.timezone.utc) - when
    return age > dt.timedelta(hours=BINDING_STALE_HOURS)


def _skill_decider(action: str, args: argparse.Namespace) -> Optional[str]:
    """Who may turn a proposal into a standing instruction.

    A skill is read by every future agent before it starts thinking, which is
    the same authority a role contract has. An engineer that can install one
    can rewrite the instructions the next engineer works under, from inside a
    single task, with nobody reading the diff. So the pipeline roles propose
    and the user or the supervisor decides -- the same split as `plan approve`,
    for the same reason.

    Like every other role boundary here this is trust-on-assert: an agent that
    simply never sets `GROGU_ROLE` is the user as far as any of this can tell.
    What is closable is the case that actually happens, which is an agent that
    has already said who it is and then drops the variable on one command --
    the session binding remembers, and remembering is enough to refuse.
    """
    role = (getattr(args, "role", "") or "").strip() or grogu_plans.current_role()
    if not role:
        binding = {}
        try:
            root = _skill_repo(args)
            binding = grogu_plans.PlanStore(root).binding_covering(root)
        except (grogu_plans.PlanError, OSError):
            binding = {}
        bound = binding.get("role", "")
        if bound and bound != grogu_plans.SUPERVISOR and not _stale(binding.get("at")):
            article = "an" if bound[:1] in "aeiou" else "a"
            when = binding.get("at") or "recently"
            return (
                f"{article} {bound} declared itself in this tree at {when} and "
                "this command arrived without a role, so it is either that "
                f"{bound} having dropped GROGU_ROLE or the user sharing its "
                "shell. Deciding a skill is the user's call, so it is refused "
                "either way: run it from your own shell, or say so with "
                "`--role supervisor`."
            )
        return None
    if role != grogu_plans.SUPERVISOR:
        return (
            f"the {role} may propose a skill but not {action} one. A skill is a "
            "standing instruction to every agent that comes after you, so it is "
            "the user's call (or the supervisor's). Yours is recorded and "
            "waiting in `grogu skill proposals`."
        )
    return None


def skill_list(args: argparse.Namespace) -> int:
    installed = grogu_skills.installed_skills(_skill_repo(args))
    if args.json:
        print_json(installed)
        return 0
    if not installed:
        print("no skills in this repository yet")
        return 0
    for skill in installed:
        print(f"{skill['name']}: {skill['description']}")
    return 0


def skill_propose(args: argparse.Namespace) -> int:
    root = _skill_repo(args)
    # `_read_body` silently prefers `--file`, which elsewhere in this CLI is
    # already treated as a bug worth an error rather than a guess: an agent
    # that passed both wrote one of them for nothing and is not told which.
    if args.file and args.body:
        print(
            "grogu: --body and --file both given; the skill body comes from "
            "one of them, so name it once",
            file=sys.stderr,
        )
        return 2
    if not args.file and not args.body:
        print(
            "grogu: no skill body. Pass --body \"...\", or --file <path> "
            "(or --file - to read it from stdin). The body is the procedure: "
            "what to do, in what order, and how the result is checked.",
            file=sys.stderr,
        )
        return 2
    body = _read_body(args)
    entry = grogu_skills.propose(
        args.name,
        description=args.description,
        body=body,
        why=args.why or "",
        role=getattr(args, "role", "") or grogu_plans.current_role(),
        plan=getattr(args, "id", "") or "",
        repository=root.name,
        repository_path=str(root),
        actor=grogu_plans.actor(),
        installed=grogu_skills.installed_skills(root),
        overrode=getattr(args, "not_the_same", None) or [],
        liked=[int(seq) for seq in (getattr(args, "like", None) or [])],
    )
    print(f"skill proposal #{entry['seq']} {entry['name']}: {entry['description']}")
    related = entry.get("related_to") or []
    if related:
        listed = ", ".join(f"#{seq}" for seq in related)
        print(
            f"  this looks close to {listed}, so they are linked for whoever "
            "decides. Both are kept: if they are one lesson, one gets declined "
            "with the other named."
        )
    nearby = entry.get("nearby") or []
    if nearby:
        # Word counting cannot see a paraphrase, so the agent that just wrote
        # the lesson is told what is closest and left to judge it. It is the
        # only party with both texts and the context they came from.
        print("  nearest existing proposals, in case one of them is this lesson:")
        for other in grogu_skills.proposals():
            if other["seq"] in nearby:
                print(f"    #{other['seq']} {other['name']}: {other['description']}")
        print(
            f"    if one of them is, link it: `grogu skill link {entry['seq']} <n>` "
            "(or leave it; nothing is lost)"
        )
    if entry.get("amends"):
        print("  this amends an installed skill; the change will show up in a diff")
    print("  waiting on the user or the supervisor: grogu skill proposals")
    return 0


def skill_link(args: argparse.Namespace) -> int:
    entry = grogu_skills.link(args.seq, args.other)
    listed = ", ".join(f"#{seq}" for seq in entry.get("related_to") or [])
    print(f"#{entry['seq']} {entry['name']} is now linked to {listed}")
    print("  both are kept; whoever decides reads them side by side")
    return 0


def skill_proposals(args: argparse.Namespace) -> int:
    entries = grogu_skills.proposals(include_decided=args.all)
    if args.json:
        print_json(entries)
        return 0
    if not entries:
        print("no skill proposals")
        return 0
    everything = {
        other["seq"]: other for other in grogu_skills.proposals(include_decided=True)
    }
    for entry in entries:
        related = entry.get("related_to") or []
        # A bare "(close to #4)" reads as an open question the reviewer still
        # has to settle, when #4 may already have been declined as the
        # duplicate -- so the state travels with the link, and a link to
        # nothing says so rather than pointing at a number that is not there.
        marks = []
        for seq in related:
            other = everything.get(seq)
            marks.append(f"#{seq} ({other['status']})" if other else f"#{seq} (missing)")
        suffix = f" (close to {', '.join(marks)})" if marks else ""
        origin = entry.get("repository") or "?"
        role = entry.get("role") or "unknown role"
        print(
            f"#{entry['seq']} {entry['name']} [{entry.get('status')}] "
            f"from the {role} in {origin}{suffix}"
        )
        print(f"    {entry.get('description','')}")
        if entry.get("why"):
            print(f"    why: {entry['why']}")
        if entry.get("overrode"):
            # Recorded but never shown is the same as not recorded. This is an
            # agent saying it read something and disagreed, which is precisely
            # the judgement the person deciding is here to check.
            print(
                "    the agent was told this was already known or already "
                "declined, read " + ", ".join(entry["overrode"]) + ", and said "
                "this is a different lesson"
            )
        if entry.get("contested"):
            contested = entry["contested"]
            print(
                f"    contested by the {contested.get('role') or 'unknown role'}: "
                f"{contested.get('note','')}"
            )
            print(f"    (you declined it for: {contested.get('declined_for','')})")
        elif entry.get("decision"):
            print(f"    decision: {entry['decision']}")
    print("\ngrogu skill show <n> for the body; accept <n> or decline <n> --note ...")
    return 0


def skill_show(args: argparse.Namespace) -> int:
    for entry in grogu_skills.proposals(include_decided=True):
        if entry.get("seq") == args.seq:
            sys.stdout.write(
                grogu_skills.render(entry["name"], entry["description"], entry["body"])
            )
            # Whoever decides this is deciding on behalf of every agent that
            # was folded into it, so they get to see what was folded in rather
            # than a count claiming agreement they cannot check.
            if entry.get("overrode"):
                print(
                    "\nThe agent was told this was already known or already "
                    "declined, read " + ", ".join(entry["overrode"]) + ", and "
                    "judged this to be a different lesson. That judgement is "
                    "recorded, not trusted."
                )
            everything = {
                other["seq"]: other
                for other in grogu_skills.proposals(include_decided=True)
            }
            for seq in entry.get("related_to") or []:
                if seq not in everything:
                    print(f"\n--- #{seq}, linked from this one, is missing ---")
                for other in grogu_skills.proposals(include_decided=True):
                    if other.get("seq") != seq:
                        continue
                    state = other.get("status")
                    print(
                        f"\n--- #{seq} [{state}], filed separately and possibly "
                        f"the same lesson, by the "
                        f"{other.get('role') or 'unknown role'} "
                        f"in {other.get('repository') or '?'} ---"
                    )
                    if other.get("decision"):
                        print(f"decision: {other['decision']}")
                    sys.stdout.write(
                        grogu_skills.render(
                            other["name"], other["description"], other["body"]
                        )
                    )
            for echo in entry.get("echoes", []):
                print(
                    f"\n--- also proposed by the {echo.get('role') or 'unknown role'} "
                    f"in {echo.get('repository') or '?'} as "
                    f"{echo.get('name') or 'the same skill'} ---"
                )
                if echo.get("description"):
                    print(echo["description"])
                if echo.get("why"):
                    print(f"why: {echo['why']}")
            return 0
    print(f"grogu: no skill proposal #{args.seq}", file=sys.stderr)
    return 2


def skill_accept(args: argparse.Namespace) -> int:
    refusal = _skill_decider("accept", args)
    if refusal:
        print(f"grogu: {refusal}", file=sys.stderr)
        return 3
    root = _skill_repo(args)
    entry = grogu_skills.accept(args.seq, root=root, note=args.note or "")
    print(f"installed {entry['installed_at']}")
    # Proposals are pooled across repositories on purpose, so the one you are
    # accepting was often learned somewhere else. Installing it here is usually
    # right and occasionally a mistake, and the only way to tell is to be told.
    if entry.get("repository") and entry["repository"] != root.name:
        print(
            f"  note: the {entry.get('role') or 'agent'} learned this in "
            f"{entry['repository']}; you have installed it in {root.name}"
        )
    print("  commit it: a skill nobody reviewed in a diff is a rule nobody agreed to")
    return 0


def skill_decline(args: argparse.Namespace) -> int:
    refusal = _skill_decider("decline", args)
    if refusal:
        print(f"grogu: {refusal}", file=sys.stderr)
        return 3
    entry = grogu_skills.decline(args.seq, note=args.note or "")
    print(f"declined skill proposal #{entry['seq']}: {entry['decision']}")
    return 0


def skill_contest(args: argparse.Namespace) -> int:
    root = _skill_repo(args)
    entry = grogu_skills.contest(
        args.seq,
        note=args.note,
        role=getattr(args, "role", "") or grogu_plans.current_role(),
        repository=root.name,
    )
    print(f"skill proposal #{entry['seq']} {entry['name']} is back in the queue")
    print(f"  it was declined for: {entry['contested'].get('declined_for') or 'no reason recorded'}")
    print("  waiting on the user or the supervisor: grogu skill proposals")
    return 0


def skill_suggest(args: argparse.Namespace) -> int:
    root = _skill_repo(args)
    installed = grogu_skills.installed_skills(root)
    pending = grogu_skills.proposals()
    lessons = grogu_skills.unwritten_lessons(
        grogu_plans.cluster_harness_friction(), installed=installed
    )
    if args.json:
        print_json({"ripe": grogu_skills.ripe(pending), "unwritten": lessons})
        return 0
    ripe = grogu_skills.ripe(pending)
    if ripe:
        print("lessons more than one agent arrived at:")
        for entry in ripe:
            reached = len(entry.get("echoes") or []) + len(entry.get("related_to") or []) + 1
            print(f"  #{entry['seq']} {entry['name']} ({reached} agents)")
    if lessons:
        print("recurring friction nobody has written down or fixed:")
        for lesson in lessons:
            where = ", ".join(lesson.get("repositories") or []) or "one repository"
            print(f"  {lesson['id']} x{lesson['count']} in {where}: {lesson['title']}")
        print(
            "\nEach of these is either a harness bug to fix or a procedure to "
            "write: grogu skill propose <name> --description ... --file <body>"
        )
    if not ripe and not lessons:
        print("nothing repeating yet; keep collecting")
    return 0


def design_store(args: argparse.Namespace) -> grogu_design.DesignStore:
    return grogu_design.DesignStore(GROGU_HOME)


def design_status(args: argparse.Namespace) -> int:
    status = design_store(args).status()
    if args.json:
        print_json(status)
        return 0
    print(f"{status['principles']} principle(s) in force")
    if status.get("adopted"):
        # The user adopted Apple's set on purpose and was then told nothing
        # had been learned from them, which read as an unfinished chore.
        # Adopting a set is an answer, not a placeholder.
        sets = " and ".join(status.get("sets") or ["a set you adopted"])
        print(f"  {status['adopted']} you adopted from {sets}. These apply now; nothing is owed.")
    if status.get("stated"):
        print(f"  {status['stated']} you stated in your own words")
    else:
        print(
            "  none in your own words yet. Say a preference to Grogu in "
            "conversation and it gets recorded, or run "
            '`grogu design remember "<preference>"` yourself.'
        )
    if status["pending"]:
        # "3 pending" meant nothing to the person it was addressed to, who had
        # seeded a set of principles and never asked for a review queue. It is
        # a queue of guesses, it is optional, and saying so costs one line.
        print(
            f"  the {status['pending']} pending are guesses a designer agent made "
            "while working, waiting on your yes or no. Nothing uses them until "
            "you say so, and ignoring them is fine: `grogu design review` to "
            "look, `confirm`/`reject` to answer."
        )
    print(f"  {status['directory']}")
    if status["scopes"]:
        print(f"  scopes: {', '.join(status['scopes'])}")
    # The designer's complaint: this command answers "what taste is on file"
    # when the question from that seat is "is my stage done". It has the plan
    # id in its environment either way, so it can answer both.
    plan_id = os.environ.get("GROGU_PLAN", "")
    if plan_id:
        try:
            store = grogu_plans.PlanStore()
            summary = store.summary(store.resolve(plan_id))
        except Exception:
            summary = {}
        if summary and "design" in (summary.get("stages") or []):
            state = (summary.get("stage_state") or {}).get("design", "?")
            written = (summary.get("stage_written") or {}).get("design")
            print(
                f"\n  plan {summary.get('id')}: design {state}, "
                f"{'spec written' if written else 'no spec written yet'}"
            )
        elif summary:
            print(f"\n  plan {summary.get('id')} has no design stage")
    return 0


def design_remember(args: argparse.Namespace) -> int:
    principle = design_store(args).remember(
        " ".join(args.statement),
        scope=args.scope,
        rationale=args.rationale or "",
        examples=args.example or [],
        anti_examples=args.anti_example or [],
    )
    print(principle["id"])
    return 0


def design_suggest(args: argparse.Namespace) -> int:
    candidate = design_store(args).suggest(
        " ".join(args.statement),
        scope=args.scope,
        rationale=args.rationale or "",
        evidence=args.evidence or "",
        source=args.source,
        confidence=args.confidence,
    )
    print(f"{candidate['id']} (pending; needs `grogu design confirm`)")
    return 0


def design_review(args: argparse.Namespace) -> int:
    candidates = design_store(args).review(limit=args.limit)
    if args.json:
        print_json(candidates)
        return 0
    for candidate in candidates:
        print(f"{candidate['id']}  [{candidate['scope']}] {candidate['statement']}")
        if candidate.get("evidence"):
            print(f"    observed: {candidate['evidence']}")
    return 0


def design_confirm(args: argparse.Namespace) -> int:
    principle = design_store(args).confirm(args.id)
    print(f"confirmed {principle['id']}")
    return 0


def design_reject(args: argparse.Namespace) -> int:
    print("rejected" if design_store(args).reject(args.id) else "no such candidate")
    return 0


def design_forget(args: argparse.Namespace) -> int:
    print("forgotten" if design_store(args).forget(args.id) else "no such principle")
    return 0


def design_recall(args: argparse.Namespace) -> int:
    principles = design_store(args).recall(
        query=" ".join(args.query) if args.query else "",
        scope=args.scope or "",
        limit=args.limit,
    )
    if args.json:
        print_json(principles)
        return 0
    for principle in principles:
        print(f"[{principle['scope']}] {principle['statement']}")
        if principle.get("rationale"):
            print(f"    why: {principle['rationale']}")
    return 0


def design_template(args: argparse.Namespace) -> int:
    sys.stdout.write(grogu_plans.design_template(" ".join(args.title) if args.title else "<change>"))
    return 0


def design_html_template(args: argparse.Namespace) -> int:
    title = " ".join(args.title) if args.title else "<report title>"
    hero_stats = None
    if args.hero_stats:
        hero_stats = []
        for raw in args.hero_stats:
            value, _, label = raw.partition(":")
            hero_stats.append((value.strip(), label.strip()))
    sys.stdout.write(
        grogu_plans.html_report_template(
            title,
            subtitle=args.subtitle,
            eyebrow=args.eyebrow or "",
            headline=args.headline or "",
            dek=args.dek or "",
            logo=args.logo or "",
            footnote=args.footnote,
            sections=args.sections,
            hero_stats=hero_stats,
        )
    )
    return 0


def design_seed(args: argparse.Namespace) -> int:
    added = design_store(args).seed_apple()
    print(f"recorded {len(added)} principle(s)")
    for principle in added:
        print(f"  [{principle['scope']}] {principle['statement']}")
    return 0


def session_new(args: argparse.Namespace) -> int:
    """Start a new Grogu session that can be opened from GitHub."""
    arguments = list(args.copilot_arguments)
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    remote_flags = {"--remote", "--remote-export", "--no-remote", "--no-remote-export"}
    if args.remote_export:
        arguments.insert(0, "--remote-export")
    elif args.remote:
        arguments.insert(0, "--remote")
    elif args.no_remote:
        arguments.insert(0, "--no-remote")
    elif not remote_flags.intersection(_flags(arguments)):
        arguments.insert(0, "--remote")
    return launch_copilot(arguments)


class _SubcommandAwareParser(argparse.ArgumentParser):
    """Show the usage of the command that was actually run.

    Argparse hands unrecognised arguments back to the top-level parser, so
    `grogu plan stage <id> --stage X` printed the usage for the whole binary
    -- a wall of {doctor,watch,guard,...} that never mentions that `stage`
    and `state` are positional. An architect lost a call to this and filed it
    as friction. The parser knows which subcommand was typed; it can say so.
    """

    def error(self, message: str):  # pragma: no cover - exercised via CLI tests
        target = self._deepest_subparser(sys.argv[1:])
        if target is not None and target is not self:
            target.print_usage(sys.stderr)
            self.exit(2, f"grogu {target.prog.split(' ', 1)[-1]}: error: {message}\n")
        return super().error(message)

    def _deepest_subparser(self, arguments: list):
        parser = self
        for word in arguments:
            if word.startswith("-"):
                break
            actions = [
                action
                for action in parser._actions
                if isinstance(action, argparse._SubParsersAction)
            ]
            if not actions or word not in actions[0].choices:
                break
            parser = actions[0].choices[word]
        return parser


def _confidence(value: str) -> float:
    """Accept the words an agent actually reaches for.

    `--confidence` was an undocumented float, and a designer that passed
    `high` got a raw argparse type error. The words are what a model writes;
    the number is what the store wants.
    """
    words = {"certain": 0.95, "high": 0.8, "medium": 0.5, "low": 0.25, "guess": 0.1}
    if value.strip().lower() in words:
        return words[value.strip().lower()]
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected a number between 0 and 1, or one of "
            f"{', '.join(sorted(words))}; got {value!r}"
        )
    if not 0.0 <= number <= 1.0:
        raise argparse.ArgumentTypeError("confidence is between 0 and 1")
    return number


# -- the `review` command family ---------------------------------------------


def _review_effective_role() -> str:
    return grogu_review_server.effective_role()


def _review_store(args: argparse.Namespace):
    store = plan_store(args)
    return store, grogu_review.ReviewStore(store)


def _open_in_browser(url: str) -> bool:
    try:
        import webbrowser

        return webbrowser.open(url, new=1)
    except Exception:  # noqa: BLE001 -- a browser that will not open is not fatal
        return False


def review_open(args: argparse.Namespace) -> int:
    store = plan_store(args)
    plan_id = store.resolve(args.id)
    role = _review_effective_role()
    if store.is_document_plan(plan_id):
        def on_document_ready(info: dict) -> None:
            launch_url = f"{info['url']}/?t={info['token']}"
            if args.json:
                print_json({**info, "launch_url": launch_url})
            elif args.no_open:
                print(f"  open {launch_url}")
            else:
                _open_in_browser(launch_url)
                print(f"  {info['url']}  opened in your browser")
            if not args.json:
                print("  ctrl-c ends the session")

        grogu_plan_server.serve(
            plan_id,
            role=role,
            stage=args.stage or "",
            mode="document",
            port=args.port,
            timeout=args.timeout,
            store=store,
            on_ready=on_document_ready,
        )
        return 0
    manifest = store.load(plan_id)
    readable = [
        stage
        for stage in grogu_plans.STAGES
        if stage in grogu_plans.ROLE_READABLE_STAGES[role]
    ]
    assets = grogu_review_server.assets_status()

    def on_ready(info: dict) -> None:
        if args.json:
            print_json(
                {
                    "url": info["url"],
                    "host": info["host"],
                    "port": info["port"],
                    "plan": info["plan"],
                    "role": info["role"],
                    "assets": {"mermaid": assets["mermaid"], "version": assets["version"]},
                    "token": info["token"],
                    "launch_url": f"{info['url']}/?t={info['token']}",
                }
            )
            return
        print(f"{plan_id}  {manifest.get('status')}  {manifest.get('title', '')}")
        print(f"  reading as {role}: {', '.join(readable)}")
        if not assets["mermaid"]:
            print(
                "  diagrams: mermaid not installed; "
                "`grogu review assets --install`"
            )
        launch_url = f"{info['url']}/?t={info['token']}"
        if args.no_open:
            print(f"  open {launch_url}")
        else:
            _open_in_browser(launch_url)
            print(f"  {info['url']}  opened in your browser")
        print("  ctrl-c ends the session")

    grogu_review_server.serve(
        plan_id,
        role=role,
        stage=args.stage or "",
        port=args.port,
        timeout=args.timeout,
        store=store,
        on_ready=on_ready,
    )
    return 0


def _thread_state_word(thread: dict) -> str:
    state = thread.get("anchor_state", "anchored")
    return "moved" if state == "shifted" else state


def _thread_target(thread: dict) -> str:
    anchor = thread.get("anchor", {}) or {}
    if anchor.get("kind") == "mermaid":
        target = anchor.get("target", "diagram")
        name = anchor.get("node_id", "")
        label = anchor.get("label", "")
        head = f"{target}:{name}".rstrip(":")
        return f'{head} "{label}"' if label else head
    return f'"{anchor.get("exact", "")}"'


def review_list(args: argparse.Namespace) -> int:
    store, review = _review_store(args)
    plan_id = store.resolve(args.id)
    role = _review_effective_role()
    review.sync(plan_id, role=role)
    threads = review.threads(
        plan_id,
        stage=args.stage or "",
        status="open" if getattr(args, "open_only", False) else "",
    )
    if args.json:
        print_json({"plan": plan_id, "threads": threads})
        return 0
    for thread in threads:
        target = _thread_target(thread)
        print(
            f"{thread['id']:<3} {thread['status']:<9} {thread['stage']:<15} "
            f"{_thread_state_word(thread):<9} {target}"
        )
        comments = thread.get("comments", [])
        if comments:
            first = comments[0]
            author = first.get("author") or "reviewer"
            print(f"{'':<40}{author}: {first.get('body', '')}")
    return 0


def review_comment(args: argparse.Namespace) -> int:
    store, review = _review_store(args)
    plan_id = store.resolve(args.id)
    role = _review_effective_role()
    view = review.stage_view(plan_id, args.stage, role=role)
    body_md = view.get("markdown", "")
    revision = view.get("revision", 0)
    chosen = [name for name in ("quote", "node", "edge") if getattr(args, name)]
    if len(chosen) != 1:
        raise grogu_review.ReviewError(
            "comment on exactly one of --quote, --node or --edge"
        )
    if args.quote:
        occurrences = body_md.count(args.quote)
        if occurrences == 0:
            raise grogu_review.ReviewError(
                f"the quoted text is not in the {args.stage} stage body"
            )
        if occurrences > 1:
            raise grogu_review.ReviewError(
                f"the quoted text occurs {occurrences} times in the {args.stage} "
                "stage body; quote more context so it is unambiguous"
            )
        start = body_md.index(args.quote)
        anchor = grogu_review.text_anchor(
            body_md, start, start + len(args.quote), stage=args.stage, revision=revision
        )
    else:
        blocks = view.get("mermaid", [])
        if not blocks:
            raise grogu_review.ReviewError(
                f"the {args.stage} stage body has no diagrams to comment on"
            )
        if args.diagram < 0 or args.diagram >= len(blocks):
            raise grogu_review.ReviewError(
                f"diagram {args.diagram} is out of range (0..{len(blocks) - 1})"
            )
        block = blocks[args.diagram]
        parsed = block.get("parsed", {})
        if args.node:
            if not any(node.get("id") == args.node for node in parsed.get("nodes", [])):
                raise grogu_review.ReviewError(
                    f"no node {args.node!r} in diagram {args.diagram}"
                )
            anchor = grogu_review.mermaid_anchor(
                stage=args.stage,
                revision=revision,
                body=body_md,
                block=block,
                parsed=parsed,
                target="node",
                node_id=args.node,
            )
        else:
            if ">" not in args.edge:
                raise grogu_review.ReviewError("edge is written as A>B")
            source_id, target_id = (part.strip() for part in args.edge.split(">", 1))
            edge = next(
                (
                    e
                    for e in parsed.get("edges", [])
                    if e.get("from") == source_id and e.get("to") == target_id
                ),
                None,
            )
            if edge is None:
                raise grogu_review.ReviewError(
                    f"no edge {source_id}>{target_id} in diagram {args.diagram}"
                )
            anchor = grogu_review.mermaid_anchor(
                stage=args.stage,
                revision=revision,
                body=body_md,
                block=block,
                parsed=parsed,
                target="edge",
                edge=edge,
            )
    thread = review.add_thread(
        plan_id, stage=args.stage, anchor=anchor, body=args.body, author=grogu_plans.actor()
    )
    if args.json:
        print_json(thread)
    else:
        print(f"{thread['id']} on {args.stage}: {_thread_target(thread)}")
    return 0


def review_reply(args: argparse.Namespace) -> int:
    store, review = _review_store(args)
    plan_id = store.resolve(args.id)
    thread = review.reply(plan_id, args.thread, args.body, author=grogu_plans.actor())
    if args.json:
        print_json(thread)
    else:
        print(f"replied to {args.thread}")
    return 0


def review_resolve(args: argparse.Namespace) -> int:
    store, review = _review_store(args)
    plan_id = store.resolve(args.id)
    thread = review.resolve_thread(
        plan_id, args.thread, note=args.note or "", author=grogu_plans.actor()
    )
    if args.json:
        print_json(thread)
    else:
        print(f"resolved {args.thread}")
    return 0


def review_request_changes(args: argparse.Namespace) -> int:
    store, review = _review_store(args)
    plan_id = store.resolve(args.id)
    role = _review_effective_role()
    result = review.request_changes(plan_id, note=args.note or "", role=role)
    if args.json:
        print_json(result)
    else:
        summary = review.summary(plan_id)
        print(
            f"requested changes on {plan_id}: round {summary.get('round')} sent "
            "to the architect as one steering note"
        )
    return 0


def review_status(args: argparse.Namespace) -> int:
    store, review = _review_store(args)
    plan_id = store.resolve(args.id)
    role = _review_effective_role()
    review.sync(plan_id, role=role)
    manifest = store.load(plan_id)
    summary = review.summary(plan_id)
    data = review.load(plan_id)
    if args.json:
        print_json({"plan": plan_id, "summary": summary, "rounds": data.get("rounds", [])})
        return 0
    print(f"{plan_id}  {manifest.get('status')}  {manifest.get('title', '')}")
    if summary.get("threads"):
        line = f"  review: round {summary.get('round')}, {summary.get('open', 0)} open"
        if summary.get("orphaned"):
            line += f", {summary['orphaned']} orphaned"
        print(line)
    else:
        print("  review: no comments yet")
    for entry in data.get("rounds", []):
        stamp = entry.get("requested_at") or entry.get("opened_at") or ""
        print(f"  rounds: {entry.get('number')} {entry.get('state')} at {stamp}")
    by_stage: dict = {}
    for thread in review.threads(plan_id):
        by_stage[thread["stage"]] = by_stage.get(thread["stage"], 0) + 1
    if by_stage:
        print(
            "  threads: "
            + ", ".join(f"{stage} {count}" for stage, count in sorted(by_stage.items()))
        )
    return 0


def review_assets(args: argparse.Namespace) -> int:
    if args.install:
        result = grogu_review_server.install_asset(source=args.from_path or "")
        if args.json:
            print_json(result)
        else:
            print(
                f"installed mermaid {result['version']} "
                f"({result['bytes']} bytes) at {result['installed']}"
            )
        return 0
    status = grogu_review_server.assets_status()
    if args.json:
        print_json(status)
        return 0
    if status["mermaid"]:
        print(f"mermaid {status['version']} installed at {status['path']}")
    else:
        print(
            f"mermaid {status['version']} not installed; "
            "`grogu review assets --install` (or set GROGU_REVIEW_MERMAID)"
        )
    return 0


def _review_no_subcommand(args: argparse.Namespace) -> int:
    print(
        "grogu review <plan-id> [open|list|comment|reply|resolve|"
        "request-changes|status|assets]",
        file=sys.stderr,
    )
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = _SubcommandAwareParser(prog="grogu")
    parser.add_argument("--version", action="version", version=f"grogu {VERSION}")
    subparsers = parser.add_subparsers(dest="command")

    subparser = subparsers.add_parser("doctor")
    subparser.set_defaults(handler=doctor)

    watch_parser = subparsers.add_parser(
        "watch", help="see which agents are running and what is blocking them"
    )
    watch_parser.add_argument("--repo")
    watch_parser.add_argument(
        "--window", type=int, default=grogu_watch.DEFAULT_WINDOW_MINUTES,
        help="how many minutes of activity to consider (default 120)",
    )
    watch_parser.add_argument(
        "-f", "--follow", action="store_true", help="redraw until interrupted"
    )
    watch_parser.add_argument("--interval", type=float, default=5.0)
    watch_parser.add_argument("--json", action="store_true")
    watch_parser.set_defaults(handler=watch)

    guard = subparsers.add_parser(
        "guard",
        help="keep secrets and personal data out of what Grogu publishes",
        description=(
            "An accident guard, not a security boundary. It checks staged "
            "commits, plans about to be published, and harness friction. It "
            "does NOT check pull request or issue bodies, commit messages, web "
            "search or fetch arguments, agent transcripts, or history already "
            "committed, and it only recognises credentials with a familiar "
            "shape. Read what Grogu is about to publish; this is defence in "
            "depth beneath that, not a replacement for it. Exit codes: 0 "
            "nothing blocking, 4 something blocking was found, 2 the arguments "
            "were wrong."
        ),
    )
    guard_subparsers = guard.add_subparsers(dest="guard_command", required=True)

    guard_scan_parser = guard_subparsers.add_parser(
        "scan", help="scan files, directories, or stdin"
    )
    guard_scan_parser.add_argument(
        "paths", nargs="*", help="files or directories; stdin when omitted"
    )
    guard_scan_parser.add_argument(
        "--destination",
        choices=(grogu_privacy.LOCAL, grogu_privacy.REPOSITORY, grogu_privacy.PUBLISHED),
        default=grogu_privacy.REPOSITORY,
        help="how public the destination is; 'published' also blocks personal data",
    )
    guard_scan_parser.add_argument("--secrets-only", action="store_true")
    guard_scan_parser.add_argument("--quiet", action="store_true")
    guard_scan_parser.set_defaults(handler=guard_scan)

    guard_staged_parser = guard_subparsers.add_parser("staged")
    guard_staged_parser.add_argument("--repo")
    guard_staged_parser.add_argument(
        "--destination",
        choices=(grogu_privacy.LOCAL, grogu_privacy.REPOSITORY, grogu_privacy.PUBLISHED),
        default=None,
        help=(
            "default: published when the repository has a remote, since the "
            "commit is on its way off the machine"
        ),
    )
    guard_staged_parser.add_argument(
        "--no-personal",
        dest="personal",
        action="store_false",
        help=(
            "only look for credentials. Personal data is checked by default: "
            "a commit is the most common way it leaves the machine"
        ),
    )
    guard_staged_parser.set_defaults(personal=True)
    guard_staged_parser.add_argument("--quiet", action="store_true")
    guard_staged_parser.set_defaults(handler=guard_staged)

    guard_install_parser = guard_subparsers.add_parser("install")
    guard_install_parser.add_argument("--repo")
    guard_install_parser.set_defaults(handler=guard_install)

    trace = subparsers.add_parser("trace")
    trace_subparsers = trace.add_subparsers(dest="trace_command", required=True)
    record = trace_subparsers.add_parser("record")
    record.add_argument("--kind", required=True)
    record.add_argument("--run-id")
    record.add_argument("--provider")
    record.add_argument("--model")
    record.add_argument("--status", default="ok")
    record.add_argument("--duration-ms", type=int)
    record.add_argument("--input-tokens", type=int)
    record.add_argument("--output-tokens", type=int)
    record.add_argument("--estimated-cost-usd", type=float)
    record.add_argument("--payload")
    record.set_defaults(handler=trace_record)
    listing = trace_subparsers.add_parser("list")
    listing.add_argument("--limit", type=int, default=20)
    listing.set_defaults(handler=trace_list)
    failures = trace_subparsers.add_parser("failures")
    failures.set_defaults(handler=trace_failures)

    telemetry = subparsers.add_parser(
        "telemetry", help="record and audit redacted Grogu improvement evidence"
    )
    telemetry_subparsers = telemetry.add_subparsers(
        dest="telemetry_command", required=True
    )
    event = telemetry_subparsers.add_parser("record")
    event.add_argument("--event", required=True)
    event.add_argument("--outcome", default="")
    event.add_argument("--repository-id", default="")
    event.add_argument("--session-id", default="")
    event.add_argument("--task-id", default="")
    event.add_argument("--duration-ms", type=int)
    event.add_argument("--payload")
    event.set_defaults(handler=telemetry_record)
    telemetry_listing = telemetry_subparsers.add_parser("list")
    telemetry_listing.add_argument("--limit", type=int, default=50)
    telemetry_listing.set_defaults(handler=telemetry_list)
    telemetry_summary_parser = telemetry_subparsers.add_parser("summary")
    telemetry_summary_parser.set_defaults(handler=telemetry_summary)

    project = subparsers.add_parser("project")
    project_subparsers = project.add_subparsers(dest="project_command", required=True)
    init = project_subparsers.add_parser("init")
    init.add_argument("name")
    init.add_argument("--path", default=".")
    init.add_argument("--force", action="store_true")
    init.set_defaults(handler=project_init)
    listing = project_subparsers.add_parser("list")
    listing.set_defaults(handler=project_list)
    relate = project_subparsers.add_parser(
        "relate", help="record an explicit cross-project relationship"
    )
    relate.add_argument("source")
    relate.add_argument("target")
    relate.add_argument("kind")
    relate.add_argument("--evidence")
    relate.set_defaults(handler=project_relate)
    graph = project_subparsers.add_parser(
        "graph", help="list the cross-project relationship catalog"
    )
    graph.set_defaults(handler=project_graph)

    memory = subparsers.add_parser(
        "memory", help="build and inspect repository-local intelligence"
    )
    memory_subparsers = memory.add_subparsers(
        dest="memory_command", required=True
    )
    memory_common = argparse.ArgumentParser(add_help=False)
    memory_common.add_argument(
        "--repo", help="repository root (default: the enclosing Git work tree)"
    )
    indexing = memory_subparsers.add_parser("index", parents=[memory_common])
    indexing.set_defaults(handler=memory_index)
    memory_status_parser = memory_subparsers.add_parser(
        "status", parents=[memory_common]
    )
    memory_status_parser.set_defaults(handler=memory_status)
    context = memory_subparsers.add_parser("context", parents=[memory_common])
    context.add_argument("--limit", type=int, default=40)
    context.add_argument("--query", default="")
    context.add_argument("--node", default="")
    context.add_argument("--depth", type=int, default=1)
    context.add_argument("--related", action="store_true")
    context.set_defaults(handler=memory_context)
    remember = memory_subparsers.add_parser("remember", parents=[memory_common])
    remember.add_argument("--type", required=True)
    remember.add_argument("--name", required=True)
    remember.add_argument("--summary", required=True)
    remember.add_argument("--path", action="append")
    remember.add_argument("--tag", action="append")
    remember.add_argument("--confidence", type=_confidence, default=0.8, help="0-1, or certain/high/medium/low/guess")
    remember.add_argument("--provenance", default="user")
    remember.set_defaults(handler=memory_remember)
    link = memory_subparsers.add_parser("link", parents=[memory_common])
    link.add_argument("source")
    link.add_argument("target")
    link.add_argument("--kind", required=True)
    link.add_argument("--confidence", type=_confidence, default=0.8, help="0-1, or certain/high/medium/low/guess")
    link.add_argument("--provenance", default="user")
    link.set_defaults(handler=memory_link)

    aggregate = subparsers.add_parser(
        "aggregate",
        help="bounded, cacheable context aggregations for Git, the knowledge "
        "graph, tasks, telemetry, relationships, and service metadata",
    )
    context_subparsers = aggregate.add_subparsers(
        dest="context_command", required=True
    )
    context_common = argparse.ArgumentParser(add_help=False)
    context_common.add_argument(
        "--repo", help="repository root (default: the enclosing Git work tree)"
    )
    context_git_parser = context_subparsers.add_parser(
        "git", parents=[context_common]
    )
    context_git_parser.set_defaults(handler=context_git)
    context_graph_parser = context_subparsers.add_parser(
        "graph", parents=[context_common]
    )
    context_graph_parser.add_argument("--limit", type=int, default=40)
    context_graph_parser.add_argument("--query", default="")
    context_graph_parser.add_argument("--node", default="")
    context_graph_parser.add_argument("--depth", type=int, default=1)
    context_graph_parser.set_defaults(handler=context_graph)
    context_tasks_parser = context_subparsers.add_parser(
        "tasks", parents=[context_common]
    )
    context_tasks_parser.add_argument("--limit", type=int, default=20)
    context_tasks_parser.set_defaults(handler=context_tasks)
    context_traces_parser = context_subparsers.add_parser(
        "traces", parents=[context_common]
    )
    context_traces_parser.set_defaults(handler=context_traces)
    context_relationships_parser = context_subparsers.add_parser(
        "relationships", parents=[context_common]
    )
    context_relationships_parser.add_argument("--limit", type=int, default=40)
    context_relationships_parser.set_defaults(handler=context_relationships)
    context_service_parser = context_subparsers.add_parser(
        "service", parents=[context_common]
    )
    context_service_parser.set_defaults(handler=context_service)

    codemode = subparsers.add_parser(
        "codemode",
        help="generate code bindings for Grogu's tools and run agent code "
        "against them in a bounded sandbox",
    )
    codemode_subparsers = codemode.add_subparsers(
        dest="codemode_command", required=True
    )
    codemode_common = argparse.ArgumentParser(add_help=False)
    codemode_common.add_argument(
        "--repo", help="repository root (default: the enclosing Git work tree)"
    )
    codemode_tools_parser = codemode_subparsers.add_parser(
        "tools", parents=[codemode_common], help="list all available tools"
    )
    codemode_tools_parser.set_defaults(handler=codemode_tools)
    codemode_search_parser = codemode_subparsers.add_parser(
        "search", parents=[codemode_common], help="search tools by name/summary"
    )
    codemode_search_parser.add_argument("query")
    codemode_search_parser.set_defaults(handler=codemode_search)
    codemode_generate_parser = codemode_subparsers.add_parser(
        "generate",
        parents=[codemode_common],
        help="write one documentation file per tool for filesystem discovery",
    )
    codemode_generate_parser.set_defaults(handler=codemode_generate)
    codemode_exec_parser = codemode_subparsers.add_parser(
        "exec",
        parents=[codemode_common],
        help="run code with every tool bound as a plain function call",
    )
    codemode_exec_parser.add_argument("--code", help="inline code to execute")
    codemode_exec_parser.add_argument("--file", help="path to a script to execute")
    codemode_exec_parser.add_argument(
        "--timeout",
        type=int,
        default=grogu_codemode.DEFAULT_TIMEOUT_SECONDS,
        help="seconds before the sandboxed run is killed",
    )
    codemode_exec_parser.set_defaults(handler=codemode_exec)
    codemode_mcp_servers_parser = codemode_subparsers.add_parser(
        "mcp-servers",
        parents=[codemode_common],
        help="list configured local MCP servers callable from exec scripts",
    )
    codemode_mcp_servers_parser.set_defaults(handler=codemode_mcp_servers)
    codemode_mcp_tools_parser = codemode_subparsers.add_parser(
        "mcp-tools",
        parents=[codemode_common],
        help="list a configured MCP server's tools (connects to it once)",
    )
    codemode_mcp_tools_parser.add_argument("server")
    codemode_mcp_tools_parser.set_defaults(handler=codemode_mcp_tools)

    personal = subparsers.add_parser(
        "personal",
        help="build and inspect user-scoped personal memory (never repository state)",
    )
    personal_subparsers = personal.add_subparsers(
        dest="personal_command", required=True
    )
    personal_status_parser = personal_subparsers.add_parser("status")
    personal_status_parser.set_defaults(handler=personal_status)
    personal_remember_parser = personal_subparsers.add_parser(
        "remember", help="explicitly record a confirmed personal memory"
    )
    personal_remember_parser.add_argument(
        "--type", required=True, choices=sorted(grogu_personal_memory.NODE_TYPES)
    )
    personal_remember_parser.add_argument("--name", required=True)
    personal_remember_parser.add_argument("--summary", required=True)
    personal_remember_parser.add_argument("--tag", action="append")
    personal_remember_parser.add_argument("--confidence", type=_confidence, default=0.8, help="0-1, or certain/high/medium/low/guess")
    personal_remember_parser.add_argument("--provenance", default="user")
    personal_remember_parser.set_defaults(handler=personal_remember)
    personal_link_parser = personal_subparsers.add_parser("link")
    personal_link_parser.add_argument("source")
    personal_link_parser.add_argument("target")
    personal_link_parser.add_argument("--kind", required=True)
    personal_link_parser.add_argument("--confidence", type=_confidence, default=0.8, help="0-1, or certain/high/medium/low/guess")
    personal_link_parser.add_argument("--provenance", default="user")
    personal_link_parser.set_defaults(handler=personal_link)
    personal_forget_parser = personal_subparsers.add_parser(
        "forget", help="delete a confirmed personal memory node and its edges"
    )
    personal_forget_parser.add_argument("node")
    personal_forget_parser.set_defaults(handler=personal_forget)
    personal_list_parser = personal_subparsers.add_parser("list")
    personal_list_parser.add_argument(
        "--type", default="", choices=[""] + sorted(grogu_personal_memory.NODE_TYPES)
    )
    personal_list_parser.add_argument("--limit", type=int, default=100)
    personal_list_parser.set_defaults(handler=personal_list)
    personal_recall_parser = personal_subparsers.add_parser(
        "recall", help="bounded, machine-readable personal context"
    )
    personal_recall_parser.add_argument("--limit", type=int, default=40)
    personal_recall_parser.add_argument("--query", default="")
    personal_recall_parser.add_argument("--node", default="")
    personal_recall_parser.add_argument("--depth", type=int, default=1)
    personal_recall_parser.set_defaults(handler=personal_recall)
    personal_suggest_parser = personal_subparsers.add_parser(
        "suggest",
        help="queue a passively observed candidate fact; never persisted without confirm",
    )
    personal_suggest_parser.add_argument(
        "--type", required=True, choices=sorted(grogu_personal_memory.NODE_TYPES)
    )
    personal_suggest_parser.add_argument("--name", required=True)
    personal_suggest_parser.add_argument("--summary", required=True)
    personal_suggest_parser.add_argument(
        "--source",
        required=True,
        help="where this candidate was observed, e.g. a capability plugin name",
    )
    personal_suggest_parser.add_argument("--tag", action="append")
    personal_suggest_parser.add_argument("--confidence", type=_confidence, default=0.5, help="0-1, or certain/high/medium/low/guess")
    personal_suggest_parser.set_defaults(handler=personal_suggest)
    personal_review_parser = personal_subparsers.add_parser(
        "review", help="list candidate facts awaiting confirmation"
    )
    personal_review_parser.add_argument("--limit", type=int, default=50)
    personal_review_parser.set_defaults(handler=personal_review)
    personal_confirm_parser = personal_subparsers.add_parser(
        "confirm", help="persist a pending candidate into confirmed personal memory"
    )
    personal_confirm_parser.add_argument("candidate")
    personal_confirm_parser.set_defaults(handler=personal_confirm)
    personal_reject_parser = personal_subparsers.add_parser(
        "reject", help="discard a pending candidate without persisting it"
    )
    personal_reject_parser.add_argument("candidate")
    personal_reject_parser.set_defaults(handler=personal_reject)

    capability = subparsers.add_parser(
        "capability",
        help="manage user-scoped Copilot plugin repositories",
    )
    capability_subparsers = capability.add_subparsers(
        dest="capability_command", required=True
    )
    capability_list_parser = capability_subparsers.add_parser("list")
    capability_list_parser.set_defaults(handler=capability_list)
    capability_add_parser = capability_subparsers.add_parser("add")
    capability_add_parser.add_argument(
        "repository", help="local repository containing plugin.json"
    )
    capability_add_parser.set_defaults(handler=capability_add)
    capability_remove_parser = capability_subparsers.add_parser("remove")
    capability_remove_parser.add_argument("repository")
    capability_remove_parser.set_defaults(handler=capability_remove)

    banner = subparsers.add_parser("banner")
    banner_subparsers = banner.add_subparsers(dest="banner_command", required=True)
    show = banner_subparsers.add_parser("show")
    show.add_argument(
        "--frame", choices=sorted(grogu_banner.EYE_FRAMES), default="open"
    )
    show.set_defaults(handler=banner_show)
    status = banner_subparsers.add_parser("status-line")
    status.set_defaults(handler=banner_status_line)
    restore = banner_subparsers.add_parser("restore")
    restore.set_defaults(handler=banner_restore)

    task = subparsers.add_parser(
        "task", help="repository-backed tasks shared by people and sessions"
    )
    task_subparsers = task.add_subparsers(dest="task_command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--repo", help="repository root (default: the enclosing Git work tree)"
    )

    created = task_subparsers.add_parser("new", parents=[common])
    created.add_argument("title")
    created.add_argument("--body", default="")
    created.add_argument("--label", action="append")
    created.add_argument("--priority", default="normal")
    created.add_argument("--issue", type=int)
    created.add_argument("--json", action="store_true")
    created.set_defaults(handler=task_new)

    adopt = task_subparsers.add_parser(
        "adopt", help="create a task from a GitHub issue", parents=[common]
    )
    adopt.add_argument("number", type=int)
    adopt.add_argument("--repository", help="owner/name, when not the current repository")
    adopt.add_argument("--force", action="store_true")
    adopt.set_defaults(handler=task_adopt)

    listing = task_subparsers.add_parser("list", parents=[common])
    listing.add_argument("--status", choices=grogu_tasks.STATUSES)
    listing.add_argument("--mine", action="store_true")
    listing.add_argument("--all", action="store_true", help="include done and cancelled")
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(handler=task_list)

    showing = task_subparsers.add_parser("show", parents=[common])
    showing.add_argument("id")
    showing.add_argument("--json", action="store_true")
    showing.set_defaults(handler=task_show)

    claim = task_subparsers.add_parser("claim", parents=[common])
    claim.add_argument("id")
    claim.add_argument("--ttl", type=int, default=grogu_tasks.DEFAULT_LEASE_SECONDS)
    claim.add_argument("--force", action="store_true")
    claim.set_defaults(handler=task_claim)

    heartbeat = task_subparsers.add_parser("heartbeat", parents=[common])
    heartbeat.add_argument("id")
    heartbeat.add_argument("--ttl", type=int)
    heartbeat.set_defaults(handler=task_heartbeat)

    release = task_subparsers.add_parser("release", parents=[common])
    release.add_argument("id")
    release.add_argument("--status", choices=grogu_tasks.STATUSES)
    release.add_argument("--note")
    release.set_defaults(handler=task_release)

    updating = task_subparsers.add_parser("update", parents=[common])
    updating.add_argument("id")
    updating.add_argument("--status", choices=grogu_tasks.STATUSES)
    updating.add_argument("--title")
    updating.add_argument("--body")
    updating.add_argument("--note")
    updating.add_argument("--issue", type=int)
    updating.add_argument("--priority")
    updating.add_argument("--label", action="append")
    updating.set_defaults(handler=task_update)

    collect = task_subparsers.add_parser(
        "gc", help="release leases whose holder is gone", parents=[common]
    )
    collect.set_defaults(handler=task_gc)

    tell = task_subparsers.add_parser(
        "tell",
        help="queue an update for the session working on a task",
        parents=[common],
    )
    tell.add_argument("id")
    tell.add_argument("text", nargs="+")
    tell.set_defaults(handler=task_tell)

    inbox = task_subparsers.add_parser(
        "inbox", help="read queued updates", parents=[common]
    )
    inbox.add_argument("id", nargs="?")
    inbox.add_argument("--consume", action="store_true", help="mark updates delivered")
    inbox.add_argument("--all", action="store_true", help="include delivered updates")
    inbox.add_argument("--json", action="store_true")
    inbox.set_defaults(handler=task_inbox)

    plan = subparsers.add_parser(
        "plan", help="architect/engineer/tester plan artifacts and stage gates"
    )
    plan_subparsers = plan.add_subparsers(dest="plan_command", required=True)
    plan_common = argparse.ArgumentParser(add_help=False)
    plan_common.add_argument(
        "--repo", help="repository root (default: the enclosing Git work tree)"
    )
    role_common = argparse.ArgumentParser(add_help=False)
    role_common.add_argument(
        "--role",
        choices=grogu_plans.ROLES,
        help="role making the call (default: $GROGU_ROLE)",
    )

    plan_new_parser = plan_subparsers.add_parser(
        "new", help="create a plan with implementation and testing stages",
        parents=[plan_common],
    )
    plan_new_parser.add_argument("title")
    plan_new_parser.add_argument("--task", help="task id this plan serves")
    plan_new_parser.add_argument(
        "--design",
        action="store_true",
        help="add a design stage for user-visible surfaces",
    )
    plan_new_parser.add_argument(
        "--eval",
        action="store_true",
        help="add an evaluation stage for end-to-end or non-deterministic behavior",
    )
    plan_new_parser.add_argument(
        "--review-required",
        action="store_true",
        help="the user asked for this plan; block work until they approve it",
    )
    plan_new_parser.add_argument("--json", action="store_true")
    plan_new_parser.set_defaults(handler=plan_new)

    plan_doc = plan_subparsers.add_parser(
        "doc",
        help="create, inspect, revise, compile, and serve typed .plan packages",
    )
    plan_doc_subparsers = plan_doc.add_subparsers(
        dest="plan_doc_command", required=True
    )

    def doc_parser(
        name: str,
        *,
        help: str = "",
        needs_id: bool = True,
    ) -> argparse.ArgumentParser:
        parser = plan_doc_subparsers.add_parser(
            name,
            help=help,
            parents=[plan_common, role_common],
        )
        if needs_id:
            _plan_id_argument(parser)
        parser.add_argument("--json", action="store_true")
        return parser

    doc_create = doc_parser(
        "create", help="create a new typed .plan package", needs_id=False
    )
    doc_create.add_argument("title")
    doc_create.add_argument("--task")
    doc_create.add_argument("--design", action="store_true")
    doc_create.add_argument("--eval", action="store_true")
    doc_create.add_argument("--review-required", action="store_true")
    doc_create.set_defaults(handler=plan_doc_create)

    doc_open = doc_parser("open", help="open the secured local workspace")
    doc_open.add_argument(
        "--mode",
        choices=["control", "document", "canvas", "dependencies", "revision"],
        default="document",
    )
    doc_open.add_argument("--stage", choices=grogu_plans.STAGES, default="")
    doc_open.add_argument("--port", type=int, default=0)
    doc_open.add_argument("--no-open", action="store_true")
    doc_open.add_argument(
        "--timeout", type=int, default=grogu_plan_server.DEFAULT_TIMEOUT
    )
    doc_open.set_defaults(handler=plan_doc_open)

    for name, handler, help_text in (
        ("show", plan_doc_show, "show role-visible graph objects"),
        ("query", plan_doc_query, "query role-visible graph objects"),
    ):
        item = doc_parser(name, help=help_text)
        item.add_argument("--stage", choices=grogu_plans.STAGES, default="")
        item.add_argument(
            "--kind",
            choices=(
                list(grogu_plandoc_schema.NODE_KINDS)
                + list(grogu_plandoc_schema.EDGE_KINDS)
            ),
            default="",
        )
        item.add_argument("--node", default="")
        item.add_argument("--text", default="")
        item.add_argument("--body", action="store_true")
        item.set_defaults(handler=handler)

    for name, handler, help_text in (
        ("projection", plan_doc_projection, "compile a role projection"),
        ("context", plan_doc_context, "emit bounded role context"),
    ):
        item = doc_parser(name, help=help_text)
        item.add_argument("--stage", choices=grogu_plans.STAGES, default="")
        item.add_argument(
            "--include", choices=["normative", "all"], default="normative"
        )
        item.add_argument("--budget", type=int)
        item.add_argument("--since", default="")
        item.add_argument("--format", choices=["md", "json"], default="md")
        item.set_defaults(handler=handler)

    for name, handler in (
        ("lint", plan_doc_lint),
        ("verify", plan_doc_lint),
    ):
        item = doc_parser(name, help="verify package and compiler invariants")
        item.set_defaults(handler=handler)

    doc_compile = doc_parser(
        "compile", help="compile or check derived stage artifacts"
    )
    doc_compile.add_argument("--stage", choices=grogu_plans.STAGES, default="")
    doc_compile.add_argument("--check", action="store_true")
    doc_compile.set_defaults(handler=plan_doc_compile)

    doc_patch = doc_parser("patch", help="apply one atomic JSON Patch")
    doc_patch.add_argument("--file", required=True)
    doc_patch.add_argument("--base", default="")
    doc_patch.add_argument("--dry-run", action="store_true")
    doc_patch.add_argument("--intent", default="")
    doc_patch.set_defaults(handler=plan_doc_patch)

    doc_node = plan_doc_subparsers.add_parser(
        "node", help="add, update, or remove graph nodes"
    )
    doc_node_subparsers = doc_node.add_subparsers(
        dest="plan_doc_node_command", required=True
    )

    def node_parser(name: str) -> argparse.ArgumentParser:
        parser = doc_node_subparsers.add_parser(
            name, parents=[plan_common, role_common]
        )
        _plan_id_argument(parser)
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--intent", default="")
        return parser

    node_add = node_parser("add")
    node_add.add_argument("--node", default="")
    node_add.add_argument(
        "--kind", required=True, choices=list(grogu_plandoc_schema.NODE_KINDS)
    )
    node_add.add_argument("--title", required=True)
    node_add.add_argument("--body", default="")
    node_add.add_argument("--stage", choices=("", *grogu_plans.STAGES), default="")
    node_add.add_argument("--attr", action="append", default=[])
    node_add.add_argument("--order", type=int, default=1000)
    node_add.set_defaults(handler=plan_doc_node_add)

    node_set = node_parser("set")
    node_set.add_argument("node")
    node_set.add_argument("--title")
    node_set.add_argument("--body")
    node_set.add_argument("--attr", action="append", default=[])
    node_set.add_argument("--order", type=int)
    node_set.set_defaults(handler=plan_doc_node_set)

    node_rm = node_parser("rm")
    node_rm.add_argument("node")
    node_rm.set_defaults(handler=plan_doc_node_rm)

    doc_link = doc_parser("link", help="add a typed graph edge")
    doc_link.add_argument("source")
    doc_link.add_argument("kind", choices=grogu_plandoc_schema.EDGE_KINDS)
    doc_link.add_argument("target")
    doc_link.add_argument("--edge", default="")
    doc_link.add_argument("--attr", action="append", default=[])
    doc_link.add_argument("--intent", default="")
    doc_link.set_defaults(handler=plan_doc_link)

    doc_unlink = doc_parser("unlink", help="remove a graph edge")
    doc_unlink.add_argument("edge")
    doc_unlink.add_argument("--intent", default="")
    doc_unlink.set_defaults(handler=plan_doc_unlink)

    doc_revisions = doc_parser("revisions", help="list append-only revisions")
    doc_revisions.set_defaults(handler=plan_doc_revisions)

    doc_diff = doc_parser("diff", help="diff two role-visible revisions")
    doc_diff.add_argument("--from", dest="from_revision", required=True)
    doc_diff.add_argument("--to", dest="to_revision", required=True)
    doc_diff.set_defaults(handler=plan_doc_diff)

    for name, handler in (
        ("propose", plan_doc_propose),
        ("revise", plan_doc_revise),
    ):
        item = doc_parser(name, help="validate and store a proposed revision")
        item.add_argument("--file", required=True)
        item.add_argument("--why", required=True)
        item.add_argument("--base", default="")
        item.add_argument("--from-thread", default="")
        item.set_defaults(handler=handler)

    doc_proposals = doc_parser("proposals", help="list revision proposals")
    doc_proposals.set_defaults(handler=plan_doc_proposals)

    doc_accept = doc_parser("accept", help="atomically accept a proposal")
    doc_accept.add_argument("proposal")
    doc_accept.set_defaults(handler=plan_doc_accept)

    doc_reject = doc_parser("reject", help="reject a proposal with a reason")
    doc_reject.add_argument("proposal")
    doc_reject.add_argument("--why", required=True)
    doc_reject.set_defaults(handler=plan_doc_reject)

    doc_impact = doc_parser("impact", help="show dependency impact")
    doc_impact.add_argument("--select", required=True)
    doc_impact.add_argument("--depth", type=int)
    doc_impact.set_defaults(handler=plan_doc_impact)

    doc_export = doc_parser("export", help="export a role-bounded projection")
    doc_export.add_argument("--stage", choices=grogu_plans.STAGES, default="")
    doc_export.add_argument(
        "--include", choices=["normative", "all"], default="normative"
    )
    doc_export.add_argument("--budget", type=int)
    doc_export.add_argument("--since", default="")
    doc_export.add_argument("--format", choices=["md", "json"], default="md")
    doc_export.add_argument("--output")
    doc_export.set_defaults(handler=plan_doc_export)

    doc_migrate = doc_parser(
        "migrate", help="losslessly convert a legacy plan directory"
    )
    doc_migrate.add_argument("--dry-run", action="store_true")
    doc_migrate.set_defaults(handler=plan_doc_migrate)

    doc_revert = doc_parser(
        "revert", help="restore the verbatim pre-migration directory"
    )
    doc_revert.add_argument("--dry-run", action="store_true")
    doc_revert.set_defaults(handler=plan_doc_revert)

    doc_control = doc_parser(
        "control", help="read registered-agent control-room state"
    )
    doc_control.add_argument("--agent", default="")
    doc_control.add_argument("--feedback")
    doc_control.add_argument("--binding", action="store_true")
    doc_control.add_argument("--filter-plan", default="")
    doc_control.add_argument("--filter-role", default="")
    doc_control.add_argument("--workstream", default="")
    doc_control.add_argument("--state", default="")
    doc_control.add_argument("--window", type=int, default=120)
    doc_control.set_defaults(handler=plan_doc_control)

    doc_register = doc_parser(
        "register", help="explicitly register one Copilot session source"
    )
    doc_register.add_argument("--run-id", required=True)
    doc_register.add_argument("--session-id", required=True)
    doc_register.add_argument("--agent-id", required=True)
    doc_register.add_argument("--agent", required=True)
    doc_register.add_argument(
        "--session-role", required=True, choices=grogu_plans.ROLES
    )
    doc_register.add_argument("--workstream", default="")
    doc_register.add_argument("--events", default="")
    doc_register.add_argument("--registered-at", default="")
    doc_register.set_defaults(handler=plan_doc_register)

    plan_list_parser = plan_subparsers.add_parser("list", parents=[plan_common])
    plan_list_parser.add_argument("--status", choices=grogu_plans.PLAN_STATUSES)
    plan_list_parser.add_argument("--all", action="store_true", help="include superseded")
    plan_list_parser.add_argument("--json", action="store_true")
    plan_list_parser.set_defaults(handler=plan_list)

    plan_status_parser = plan_subparsers.add_parser(
        "status", help="bounded plan summary with no plan prose", parents=[plan_common]
    )
    plan_status_parser.add_argument("id", nargs="?", default="")
    plan_status_parser.add_argument("--plan", "--id", dest="plan_flag", default="", help=argparse.SUPPRESS)
    plan_status_parser.add_argument("--json", action="store_true")
    plan_status_parser.set_defaults(handler=plan_status)

    plan_shape_parser = plan_subparsers.add_parser(
        "shape",
        help="change stages or the review hold of an existing plan (architect only)",
        parents=[plan_common, role_common],
    )
    plan_shape_parser.add_argument("id", nargs="?", default="")
    plan_shape_parser.add_argument("--plan", "--id", dest="plan_flag", default="", help=argparse.SUPPRESS)
    plan_shape_group = plan_shape_parser.add_mutually_exclusive_group(required=True)
    plan_shape_group.add_argument(
        "--add",
        choices=[grogu_plans.DESIGN, grogu_plans.EVALUATION],
        help="add an optional stage this plan turns out to need",
    )
    plan_shape_group.add_argument(
        "--decline",
        choices=[grogu_plans.DESIGN, grogu_plans.EVALUATION],
        help="record that this stage was considered and is not warranted",
    )
    plan_shape_group.add_argument(
        "--require-review",
        action="store_true",
        help="hold work until the user approves the plan",
    )
    plan_shape_group.add_argument(
        "--clear-review",
        action="store_true",
        help=(
            "clear an unapproved review requirement "
            "(declared architect or --as-user; requires --why)"
        ),
    )
    plan_shape_parser.add_argument(
        "--why", help="reason, required with --decline or --clear-review"
    )
    plan_shape_parser.add_argument(
        "--as-user",
        action="store_true",
        help=(
            "you are the user, not an agent; permits --clear-review without "
            "an architect role"
        ),
    )
    plan_shape_parser.add_argument(
        "--reset",
        choices=grogu_plans.STAGES,
        help="throw away a stage body and mark it unwritten again",
    )
    plan_shape_parser.set_defaults(handler=plan_shape)

    plan_write_parser = plan_subparsers.add_parser(
        "write", help="write a plan stage (architect only)",
        parents=[plan_common, role_common],
    )
    _plan_id_argument(plan_write_parser)
    plan_write_parser.add_argument("stage", choices=grogu_plans.STAGES)
    plan_write_parser.add_argument("--body", default="")
    plan_write_parser.add_argument("--file", help="read the body from a file, or - for stdin")
    plan_write_parser.add_argument(
        "--replace",
        action="store_true",
        help="rewrite a stage that is already complete, reopening it",
    )
    plan_write_parser.add_argument(
        "--base",
        help=(
            "expected plaintext stage digest from `grogu plan writer`; "
            "refuse if another writer changed the stage"
        ),
    )
    plan_write_parser.set_defaults(handler=plan_write)

    plan_show_parser = plan_subparsers.add_parser(
        "show", help="read a plan stage the role is allowed to read",
        parents=[plan_common, role_common],
    )
    _plan_id_argument(plan_show_parser)
    plan_show_parser.add_argument(
        "stage_positional",
        nargs="?",
        default="",
        metavar="stage",
        help="the stage to read; `--stage` also works",
    )
    plan_show_parser.add_argument("--stage", choices=grogu_plans.STAGES, default="")
    plan_show_parser.add_argument(
        "--revision", type=int, default=0, help="an earlier version of this stage"
    )
    plan_show_parser.add_argument(
        "--revisions", action="store_true", help="list what this stage used to say"
    )
    plan_show_parser.set_defaults(handler=plan_show)

    plan_approve_parser = plan_subparsers.add_parser("approve", parents=[plan_common])
    _plan_id_argument(plan_approve_parser)
    plan_approve_parser.add_argument("--note")
    plan_approve_parser.add_argument(
        "--as-user",
        action="store_true",
        help=(
            "accepted for symmetry with stage and finalize; approval is "
            "already refused to any caller with a role, and this does not "
            "lift that"
        ),
    )
    plan_approve_parser.set_defaults(handler=plan_approve)

    plan_stage_parser = plan_subparsers.add_parser(
        "stage", help="record stage progress", parents=[plan_common, role_common]
    )
    _plan_id_argument(plan_stage_parser)
    plan_stage_parser.add_argument("stage", choices=grogu_plans.STAGES)
    plan_stage_parser.add_argument("state", choices=grogu_plans.STAGE_STATES)
    plan_stage_parser.add_argument("--note")
    plan_stage_parser.add_argument(
        "--workstream",
        default="",
        help=(
            "which workstream you finished (default: $GROGU_WORKSTREAM); "
            "required once a plan is split across more than one"
        ),
    )
    plan_stage_parser.add_argument(
        "--as-user",
        action="store_true",
        help="you are the user, not an agent (needed to complete a sealed stage "
        "without a role)",
    )
    plan_stage_parser.set_defaults(handler=plan_stage)

    # Roles reach for `plan complete <id> <stage>` because that is what
    # finishing sounds like, and the real spelling inverts it into
    # `plan stage <id> <stage> complete`. Same reasoning as the gate aliases:
    # answer the correct question rather than printing a choice list at it.
    plan_complete_parser = plan_subparsers.add_parser(
        "complete",
        help="mark a stage complete (same as `plan stage <id> <stage> complete`)",
        parents=[plan_common, role_common],
    )
    _plan_id_argument(plan_complete_parser)
    plan_complete_parser.add_argument(
        "stage_positional", nargs="?", default="", metavar="stage"
    )
    plan_complete_parser.add_argument("--stage", default="")
    plan_complete_parser.add_argument("--note")
    plan_complete_parser.add_argument("--workstream", default="")
    plan_complete_parser.add_argument("--as-user", action="store_true")
    plan_complete_parser.set_defaults(handler=plan_complete)

    plan_supersede_parser = plan_subparsers.add_parser("supersede", parents=[plan_common])
    _plan_id_argument(plan_supersede_parser)
    plan_supersede_parser.add_argument("--note")
    plan_supersede_parser.set_defaults(handler=plan_supersede)

    plan_finalize_parser = plan_subparsers.add_parser(
        "finalize",
        help="unseal every stage so the finished plan ships in the pull request",
        parents=[plan_common, role_common],
    )
    _plan_id_argument(plan_finalize_parser)
    plan_finalize_parser.add_argument("--note")
    plan_finalize_parser.add_argument(
        "--force",
        action="store_true",
        help="finalize despite open defects, amendments or incomplete stages",
    )
    plan_finalize_parser.add_argument(
        "--as-user",
        action="store_true",
        help="you are the user, not an agent; finalizing unseals the test plan",
    )
    plan_finalize_parser.set_defaults(handler=plan_finalize)

    plan_gate_parser = plan_subparsers.add_parser(
        "gate", help="may the pipeline enter a stage (exit 3 when blocked)",
        parents=[plan_common],
    )
    _plan_id_argument(plan_gate_parser)
    # `plan gate test <id>` is the shape a tester reaches for: the gate, then
    # the thing it is about. One positional slot could hold only one of them.
    plan_gate_parser.add_argument(
        "gate_positional", nargs="?", default="", help=argparse.SUPPRESS
    )
    plan_gate_parser.add_argument(
        "--stage", choices=list(grogu_plans.GATES) + sorted(_GATE_ALIASES)
    )
    plan_gate_parser.add_argument("--json", action="store_true")
    plan_gate_parser.set_defaults(handler=plan_gate)

    plan_amend_parser = plan_subparsers.add_parser(
        "amend", help="ask the architect to change the plan",
        parents=[plan_common, role_common],
    )
    _plan_id_argument(plan_amend_parser)
    plan_amend_parser.add_argument("--claim", required=True)
    plan_amend_parser.add_argument("--evidence", default="")
    plan_amend_parser.add_argument("--stage", choices=grogu_plans.STAGES, default=grogu_plans.IMPLEMENTATION)
    plan_amend_parser.set_defaults(handler=plan_amend)

    plan_amendments_parser = plan_subparsers.add_parser("amendments", parents=[plan_common])
    _plan_id_argument(plan_amendments_parser)
    # Read commands accept `--role` uniformly except this one, which rejected
    # it. An engineer checking whether its amendment had been answered had to
    # work out that this one command wanted the role dropped -- and the role is
    # what every other command in the same sequence had just required.
    plan_amendments_parser.add_argument("--role", default="")
    plan_amendments_parser.add_argument("--all", action="store_true")
    plan_amendments_parser.add_argument("--json", action="store_true")
    plan_amendments_parser.set_defaults(handler=plan_amendments)

    plan_resolve_parser = plan_subparsers.add_parser(
        "resolve", help="architect decision on an amendment or escalation",
        parents=[plan_common, role_common],
    )
    _plan_id_argument(plan_resolve_parser)
    plan_resolve_parser.add_argument("amendment")
    outcome_group = plan_resolve_parser.add_mutually_exclusive_group(required=True)
    outcome_group.add_argument("--accept", action="store_true")
    outcome_group.add_argument("--reject", action="store_true")
    outcome_group.add_argument(
        "--guidance", help="break the deadlock with direction instead of a plan change"
    )
    plan_resolve_parser.add_argument("--reason", default="")
    plan_resolve_parser.add_argument(
        "--verified",
        action="store_true",
        help="the architect checked the claim against the code itself",
    )
    plan_resolve_parser.set_defaults(handler=plan_resolve)

    plan_defect_parser = plan_subparsers.add_parser(
        "defect", help="report a failure and route it to whoever owns it",
        parents=[plan_common, role_common],
    )
    _plan_id_argument(plan_defect_parser)
    plan_defect_parser.add_argument(
        "--resolve",
        default="",
        metavar="DEFECT",
        help="close a defect instead of filing one (same as `plan defect-resolve`)",
    )
    plan_defect_parser.add_argument("--report", default="")
    plan_defect_parser.add_argument("--route", choices=grogu_plans.DEFECT_ROUTES)
    plan_defect_parser.add_argument("--evidence", default="")
    plan_defect_parser.add_argument(
        "--note",
        "--resolution",
        default="",
        dest="note",
        help="what you changed, when closing with --resolve",
    )
    plan_defect_parser.set_defaults(handler=plan_defect)

    plan_defects_parser = plan_subparsers.add_parser("defects", parents=[plan_common])
    _plan_id_argument(plan_defects_parser)
    plan_defects_parser.add_argument("--all", action="store_true")
    plan_defects_parser.add_argument("--json", action="store_true")
    plan_defects_parser.set_defaults(handler=plan_defects)

    plan_defect_resolve_parser = plan_subparsers.add_parser(
        "defect-resolve", parents=[plan_common]
    )
    _plan_id_argument(plan_defect_resolve_parser)
    plan_defect_resolve_parser.add_argument("defect")
    plan_defect_resolve_parser.add_argument("--note", required=True)
    plan_defect_resolve_parser.set_defaults(handler=plan_defect_resolve)

    plan_workstream_parser = plan_subparsers.add_parser(
        "workstream", help="declare a parallelisable unit and the files it owns",
        parents=[plan_common],
    )
    _plan_id_argument(plan_workstream_parser)
    plan_workstream_parser.add_argument(
        "--replace",
        action="store_true",
        help="redefine a workstream that is already declared but not finished",
    )
    plan_workstream_parser.add_argument(
        "--drop",
        action="store_true",
        help="withdraw a workstream this plan no longer wants",
    )
    plan_workstream_parser.add_argument("--name", required=True)
    plan_workstream_parser.add_argument("--path", action="append")
    plan_workstream_parser.add_argument("--depends-on", action="append")
    plan_workstream_parser.add_argument(
        "--model", help="model this workstream should be implemented on"
    )
    plan_workstream_parser.add_argument(
        "--review",
        action="append",
        choices=list(grogu_plans.REVIEW_KINDS),
        help="a required review kind; repeat for multiple reviews",
    )
    plan_workstream_parser.add_argument(
        "--brief", help="what this engineer should know that the others need not"
    )
    plan_workstream_parser.set_defaults(handler=plan_workstream)

    plan_workstreams_parser = plan_subparsers.add_parser(
        "workstreams", help="parallel waves, and any file-set conflicts between them",
        parents=[plan_common],
    )
    _plan_id_argument(plan_workstreams_parser)
    plan_workstreams_parser.add_argument(
        "--check", action="store_true", help="exit 3 when workstreams overlap"
    )
    plan_workstreams_parser.add_argument("--json", action="store_true")
    plan_workstreams_parser.set_defaults(handler=plan_workstreams)

    plan_writer_parser = plan_subparsers.add_parser(
        "writer",
        help="inspect or take over the active writer for a plan stage",
        parents=[plan_common, role_common],
    )
    _plan_id_argument(plan_writer_parser)
    plan_writer_parser.add_argument("stage", choices=grogu_plans.STAGES)
    plan_writer_parser.add_argument("--takeover", action="store_true")
    plan_writer_parser.add_argument("--agent", default="")
    plan_writer_parser.set_defaults(handler=plan_writer)

    plan_budget_parser = plan_subparsers.add_parser(
        "agent-budget",
        help="declare observable limits and checkpoint cadence for an agent",
        parents=[plan_common],
    )
    _plan_id_argument(plan_budget_parser)
    plan_budget_parser.add_argument("--agent", default="")
    plan_budget_parser.add_argument("--agent-role", choices=grogu_plans.ROLES)
    plan_budget_parser.add_argument("--workstream")
    plan_budget_parser.add_argument("--tool-calls", type=int)
    plan_budget_parser.add_argument("--elapsed-seconds", type=float)
    plan_budget_parser.add_argument("--ai-credits", type=float)
    plan_budget_parser.add_argument("--checkpoint-tool-calls", type=int)
    plan_budget_parser.add_argument("--json", action="store_true")
    plan_budget_parser.set_defaults(handler=plan_agent_budget)

    plan_usage_parser = plan_subparsers.add_parser(
        "agent-usage",
        help="record observable agent usage counters",
        parents=[plan_common],
    )
    _plan_id_argument(plan_usage_parser)
    plan_usage_parser.add_argument("--agent", default="")
    plan_usage_parser.add_argument("--tool-calls", type=int)
    plan_usage_parser.add_argument("--elapsed-seconds", type=float)
    plan_usage_parser.add_argument("--ai-credits", type=float)
    plan_usage_parser.add_argument("--json", action="store_true")
    plan_usage_parser.set_defaults(handler=plan_agent_usage)

    plan_checkpoint_parser = plan_subparsers.add_parser(
        "checkpoint",
        help="record a recoverable agent checkpoint",
        parents=[plan_common],
    )
    _plan_id_argument(plan_checkpoint_parser)
    plan_checkpoint_parser.add_argument("--agent", default="")
    plan_checkpoint_parser.add_argument("--commit", default="")
    plan_checkpoint_parser.add_argument("--note")
    plan_checkpoint_parser.add_argument("--json", action="store_true")
    plan_checkpoint_parser.set_defaults(handler=plan_checkpoint)

    plan_recovery_parser = plan_subparsers.add_parser(
        "checkpoint-recovery",
        help="record whether a checkpoint can be or was restored",
        parents=[plan_common],
    )
    _plan_id_argument(plan_recovery_parser)
    plan_recovery_parser.add_argument("checkpoint")
    plan_recovery_parser.add_argument("--agent", default="")
    plan_recovery_parser.add_argument(
        "--status", required=True, choices=["available", "restored", "failed"]
    )
    plan_recovery_parser.add_argument("--note")
    plan_recovery_parser.add_argument("--json", action="store_true")
    plan_recovery_parser.set_defaults(handler=plan_checkpoint_recovery)

    plan_governance_parser = plan_subparsers.add_parser(
        "governance",
        help="show agent budgets, usage, checkpoints and recovery blockers",
        parents=[plan_common],
    )
    _plan_id_argument(plan_governance_parser)
    plan_governance_parser.add_argument("--json", action="store_true")
    plan_governance_parser.set_defaults(handler=plan_governance)

    plan_workstream_worktree_parser = plan_subparsers.add_parser(
        "workstream-worktree",
        help="get, create, list or remove a workstream's dedicated git worktree",
        parents=[plan_common],
    )
    _plan_id_argument(plan_workstream_worktree_parser)
    plan_workstream_worktree_parser.add_argument(
        "--name", help="the declared workstream (required unless --list)"
    )
    plan_workstream_worktree_parser.add_argument(
        "--base",
        help="branch or commit the worktree forks from when created (default: "
        "whatever branch this checkout is currently on)",
    )
    plan_workstream_worktree_parser.add_argument(
        "--list",
        action="store_true",
        help="report every workstream worktree already created for this plan",
    )
    plan_workstream_worktree_parser.add_argument(
        "--remove",
        action="store_true",
        help="remove this workstream's dedicated worktree",
    )
    plan_workstream_worktree_parser.add_argument(
        "--force",
        action="store_true",
        help="remove even if the worktree has uncommitted changes",
    )
    plan_workstream_worktree_parser.add_argument(
        "--delete-branch",
        action="store_true",
        help="with --remove, also delete the branch, only if it looks merged or closed",
    )
    plan_workstream_worktree_parser.add_argument("--json", action="store_true")
    plan_workstream_worktree_parser.set_defaults(handler=plan_workstream_worktree)

    plan_review_parser = plan_subparsers.add_parser(
        "review", help="record a review the architect asked for"
    )
    _plan_id_argument(plan_review_parser)
    plan_review_parser.add_argument("--repo")
    plan_review_parser.add_argument("--workstream", required=True)
    plan_review_parser.add_argument(
        "--verdict", required=True, choices=list(grogu_plans.DESIGN_VERDICTS)
    )
    plan_review_parser.add_argument("--kind", choices=list(grogu_plans.REVIEW_KINDS))
    plan_review_parser.add_argument("--model")
    plan_review_parser.add_argument("--findings", nargs="*")
    plan_review_parser.set_defaults(handler=plan_review)

    plan_steer_parser = plan_subparsers.add_parser(
        "steer", help="record steering that reaches agents spawned later",
        parents=[plan_common],
    )
    plan_steer_parser.add_argument("text", nargs="*")
    plan_steer_parser.add_argument(
        "--note", default="", help="the note, if you would rather not quote it positionally"
    )
    plan_steer_parser.add_argument("--plan", "--id", dest="id", help="scope to one plan")
    plan_steer_parser.add_argument(
        "--relayed",
        action="store_true",
        help="supervisor only: these are the user's words, not yours",
    )
    plan_steer_parser.add_argument(
        "--retract", type=int, metavar="SEQ", help="take back a note you sent"
    )
    plan_steer_parser.add_argument(
        "--role", choices=(*grogu_plans.ROLES, "all"), default="all"
    )
    plan_steer_parser.add_argument(
        "--requires-replan",
        action="store_true",
        help="block the gates until the architect folds this into the plan",
    )
    plan_steer_parser.set_defaults(handler=plan_steer)

    plan_commission_parser = plan_subparsers.add_parser(
        "commission",
        help="tell a role what the architect wants from it (architect only)",
        parents=[plan_common, role_common],
    )
    _plan_id_argument(plan_commission_parser)
    plan_commission_parser.add_argument("for_role", metavar="ROLE", choices=grogu_plans.ROLES)
    plan_commission_parser.add_argument("--brief", required=True)
    plan_commission_parser.add_argument(
        "--replace", action="store_true", help="overwrite an existing commission"
    )
    plan_commission_parser.set_defaults(handler=plan_commission)

    plan_attach_parser = plan_subparsers.add_parser(
        "attach",
        help="carry a file alongside the plan for the roles that come after",
        parents=[plan_common, role_common],
    )
    _plan_id_argument(plan_attach_parser)
    plan_attach_parser.add_argument("--name", help="file name (default: the --file basename)")
    plan_attach_parser.add_argument("--file", help="read the artifact from here, or - for stdin")
    plan_attach_parser.add_argument("--body")
    plan_attach_parser.add_argument("--stage", choices=list(grogu_plans.STAGES))
    plan_attach_parser.add_argument("--note", help="what this artifact is for")
    plan_attach_parser.add_argument(
        "--verifier",
        action="store_true",
        help="this script checks the plan; the test gate will require it to pass",
    )
    plan_attach_parser.set_defaults(handler=plan_attach)

    plan_verify_parser = plan_subparsers.add_parser(
        "verify",
        help="run the checks attached to this plan",
        parents=[plan_common, role_common],
    )
    _plan_id_argument(plan_verify_parser)
    plan_verify_parser.set_defaults(handler=plan_verify)

    plan_steering_parser = plan_subparsers.add_parser(
        "steering", help="steering visible to a role", parents=[plan_common]
    )
    plan_steering_parser.add_argument(
        "--role",
        choices=list(grogu_plans.ROLES) + ["all"],
        help="role making the call (default: $GROGU_ROLE)",
    )
    plan_steering_parser.add_argument("plan", nargs="?", default="", help="plan id")
    plan_steering_parser.add_argument("--plan", "--id", dest="id")
    plan_steering_parser.add_argument("plan_positional", nargs="?", default="", help=argparse.SUPPRESS)
    plan_steering_parser.add_argument(
        "--audit",
        type=int,
        default=0,
        metavar="N",
        help="who has read note N; never consumes it",
    )
    plan_steering_parser.add_argument(
        "--unread", action="store_true", help="only notes this role has not acked"
    )
    plan_steering_parser.add_argument(
        "--all",
        action="store_true",
        help="replay every note, including ones this role has already read",
    )
    plan_steering_parser.add_argument(
        "--ack", action="store_true", help="mark everything visible as seen"
    )
    plan_steering_parser.add_argument(
        "--agent",
        default="",
        help=(
            "ack for the named agent instead of this one, after relaying the "
            "note into it with write_agent"
        ),
    )
    plan_steering_parser.add_argument("--json", action="store_true")
    plan_steering_parser.set_defaults(handler=plan_steering)

    plan_brief_parser = plan_subparsers.add_parser(
        "brief",
        help="assemble a role's prompt: shared contract, repository overlay, steering",
        parents=[plan_common],
    )
    plan_brief_parser.add_argument("--role", choices=grogu_plans.ROLES, required=True)
    plan_brief_parser.add_argument("--plan", "--id", dest="id")
    plan_brief_parser.add_argument("plan_positional", nargs="?", default="", help=argparse.SUPPRESS)
    plan_brief_parser.add_argument(
        "--full",
        action="store_true",
        help="include the shared role contract (already the agent's own prompt)",
    )
    plan_brief_parser.add_argument("--json", action="store_true")
    plan_brief_parser.set_defaults(handler=plan_brief)

    plan_triage_parser = plan_subparsers.add_parser(
        "triage", help="does this request warrant a plan at all",
    )
    plan_triage_parser.add_argument("text", nargs="+")
    plan_triage_parser.add_argument("--json", action="store_true")
    plan_triage_parser.set_defaults(handler=plan_triage)

    plan_retro_parser = plan_subparsers.add_parser(
        "retro", help="what this plan cost beyond the work, and what to change",
        parents=[plan_common],
    )
    _plan_id_argument(plan_retro_parser)
    plan_retro_parser.add_argument("--json", action="store_true")
    plan_retro_parser.set_defaults(handler=plan_retro)

    plan_friction_parser = plan_subparsers.add_parser(
        "friction", help="recorded friction and signals recurring across plans",
        parents=[plan_common, role_common],
    )
    plan_friction_parser.add_argument("--note", help="record friction you just hit")
    plan_friction_parser.add_argument(
        "--repo-only",
        dest="repo_only",
        action="store_true",
        help="keep the note in this repository even if it names a grogu command",
    )
    plan_friction_parser.add_argument("--plan", "--id", dest="id")
    plan_friction_parser.add_argument("plan_positional", nargs="?", default="", help=argparse.SUPPRESS)
    plan_friction_parser.add_argument("--resolve", type=int, metavar="SEQ")
    plan_friction_parser.add_argument("--resolution")
    plan_friction_parser.add_argument("--all", action="store_true")
    plan_friction_parser.add_argument(
        "--ripe",
        action="store_true",
        help="harness friction grouped into clusters worth a pull request",
    )
    plan_friction_parser.add_argument("--claim", metavar="CLUSTER")
    plan_friction_parser.add_argument(
        "--reference", help="the PR, branch or issue taking on a claimed cluster"
    )
    plan_friction_parser.add_argument(
        "--harness",
        action="store_true",
        help="friction with Grogu itself, pooled across every repository",
    )
    plan_friction_parser.add_argument("--json", action="store_true")
    plan_friction_parser.set_defaults(handler=plan_friction)

    plan_design_review_parser = plan_subparsers.add_parser(
        "design-review", help="the designer's verdict on the built interface"
    )
    _plan_id_argument(plan_design_review_parser)
    plan_design_review_parser.add_argument("--repo")
    plan_design_review_parser.add_argument(
        "--verdict", required=True, choices=list(grogu_plans.DESIGN_VERDICTS)
    )
    plan_design_review_parser.add_argument("--notes")
    plan_design_review_parser.add_argument(
        "--evidence",
        action="append",
        help="screenshot path, recording or captured output (required to pass)",
    )
    plan_design_review_parser.add_argument("--role")
    plan_design_review_parser.set_defaults(handler=plan_design_review)

    review = subparsers.add_parser(
        "review", help="read a plan and comment on it in a local browser workspace"
    )
    review_subparsers = review.add_subparsers(dest="review_command")
    review.set_defaults(handler=_review_no_subcommand, _plan_id_required=False)
    review_common = argparse.ArgumentParser(add_help=False)
    review_common.add_argument(
        "--repo", help="repository root (default: the enclosing Git work tree)"
    )

    def _review_plan_id(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("id", nargs="?", default="")
        parser.add_argument(
            "--plan", "--id", dest="plan_flag", default="", help=argparse.SUPPRESS
        )
        parser.set_defaults(_plan_id_required=True)

    review_open_parser = review_subparsers.add_parser("open", parents=[review_common])
    _review_plan_id(review_open_parser)
    review_open_parser.add_argument("--stage", default="")
    review_open_parser.add_argument("--port", type=int, default=0)
    review_open_parser.add_argument("--no-open", dest="no_open", action="store_true")
    review_open_parser.add_argument(
        "--timeout", type=int, default=grogu_review_server.DEFAULT_TIMEOUT
    )
    review_open_parser.add_argument("--json", action="store_true")
    review_open_parser.set_defaults(handler=review_open)

    review_list_parser = review_subparsers.add_parser("list", parents=[review_common])
    _review_plan_id(review_list_parser)
    review_list_parser.add_argument("--stage", default="")
    review_list_parser.add_argument("--open", dest="open_only", action="store_true")
    review_list_parser.add_argument("--json", action="store_true")
    review_list_parser.set_defaults(handler=review_list)

    review_comment_parser = review_subparsers.add_parser("comment", parents=[review_common])
    _review_plan_id(review_comment_parser)
    review_comment_parser.add_argument("--stage", required=True)
    review_comment_parser.add_argument("--quote")
    review_comment_parser.add_argument("--node")
    review_comment_parser.add_argument("--edge")
    review_comment_parser.add_argument("--diagram", type=int, default=0)
    review_comment_parser.add_argument("--body", required=True)
    review_comment_parser.add_argument("--json", action="store_true")
    review_comment_parser.set_defaults(handler=review_comment)

    review_reply_parser = review_subparsers.add_parser("reply", parents=[review_common])
    _review_plan_id(review_reply_parser)
    review_reply_parser.add_argument("thread")
    review_reply_parser.add_argument("--body", required=True)
    review_reply_parser.add_argument("--json", action="store_true")
    review_reply_parser.set_defaults(handler=review_reply)

    review_resolve_parser = review_subparsers.add_parser("resolve", parents=[review_common])
    _review_plan_id(review_resolve_parser)
    review_resolve_parser.add_argument("thread")
    review_resolve_parser.add_argument("--note", default="")
    review_resolve_parser.add_argument("--json", action="store_true")
    review_resolve_parser.set_defaults(handler=review_resolve)

    review_rc_parser = review_subparsers.add_parser(
        "request-changes", parents=[review_common]
    )
    _review_plan_id(review_rc_parser)
    review_rc_parser.add_argument("--note", default="")
    review_rc_parser.add_argument("--json", action="store_true")
    review_rc_parser.set_defaults(handler=review_request_changes)

    review_status_parser = review_subparsers.add_parser("status", parents=[review_common])
    _review_plan_id(review_status_parser)
    review_status_parser.add_argument("--json", action="store_true")
    review_status_parser.set_defaults(handler=review_status)

    review_assets_parser = review_subparsers.add_parser("assets", parents=[review_common])
    review_assets_parser.add_argument("--install", action="store_true")
    review_assets_parser.add_argument("--from", dest="from_path", default="")
    review_assets_parser.add_argument("--json", action="store_true")
    review_assets_parser.set_defaults(handler=review_assets, _plan_id_required=False)

    skill = subparsers.add_parser(
        "skill", help="skills the agents write for the agents that come after them"
    )
    skill.add_argument("--repo")
    skill_subparsers = skill.add_subparsers(dest="skill_command", required=True)

    skill_list_parser = skill_subparsers.add_parser(
        "list", help="skills installed in this repository"
    )
    skill_list_parser.add_argument("--json", action="store_true")
    skill_list_parser.set_defaults(handler=skill_list)

    skill_propose_parser = skill_subparsers.add_parser(
        "propose", help="write down a lesson the next agent should not have to rediscover"
    )
    skill_propose_parser.add_argument("name")
    skill_propose_parser.add_argument("--description", required=True)
    skill_propose_parser.add_argument("--body")
    skill_propose_parser.add_argument("--file", help="the skill body, or - for stdin")
    skill_propose_parser.add_argument("--why", help="what happened that made this worth writing")
    skill_propose_parser.add_argument(
        "--not-the-same",
        action="append",
        dest="not_the_same",
        help="an installed skill name or a declined proposal number you have "
        "read and judged to be a different lesson",
    )
    skill_propose_parser.add_argument(
        "--like",
        action="append",
        type=int,
        help="a pending proposal number this is the same lesson as",
    )
    skill_propose_parser.add_argument("--role")
    skill_propose_parser.add_argument("--id", nargs="?", default="")
    skill_propose_parser.set_defaults(handler=skill_propose)

    skill_proposals_parser = skill_subparsers.add_parser(
        "proposals", help="skills waiting on a decision"
    )
    skill_proposals_parser.add_argument("--all", action="store_true")
    skill_proposals_parser.add_argument("--json", action="store_true")
    skill_proposals_parser.set_defaults(handler=skill_proposals)

    skill_link_parser = skill_subparsers.add_parser(
        "link", help="say two filed proposals are the same lesson"
    )
    skill_link_parser.add_argument("seq", type=int)
    skill_link_parser.add_argument("other", type=int)
    skill_link_parser.set_defaults(handler=skill_link)

    skill_show_parser = skill_subparsers.add_parser("show", help="the body of a proposal")
    skill_show_parser.add_argument("seq", type=int)
    skill_show_parser.set_defaults(handler=skill_show)

    skill_accept_parser = skill_subparsers.add_parser(
        "accept", help="install a proposal into this repository"
    )
    skill_accept_parser.add_argument("seq", type=int)
    skill_accept_parser.add_argument("--note")
    skill_accept_parser.add_argument("--role")
    skill_accept_parser.set_defaults(handler=skill_accept)

    skill_decline_parser = skill_subparsers.add_parser("decline")
    skill_decline_parser.add_argument("seq", type=int)
    skill_decline_parser.add_argument("--note", required=True)
    skill_decline_parser.add_argument("--role")
    skill_decline_parser.set_defaults(handler=skill_decline)

    skill_contest_parser = skill_subparsers.add_parser(
        "contest", help="argue with a decline rather than re-proposing it"
    )
    skill_contest_parser.add_argument("seq", type=int)
    skill_contest_parser.add_argument("--note", required=True)
    skill_contest_parser.add_argument("--role")
    skill_contest_parser.set_defaults(handler=skill_contest)

    skill_suggest_parser = skill_subparsers.add_parser(
        "suggest", help="lessons that have repeated and nobody wrote down"
    )
    skill_suggest_parser.add_argument("--json", action="store_true")
    skill_suggest_parser.set_defaults(handler=skill_suggest)

    design = subparsers.add_parser(
        "design", help="design taste the designer works from, and the spec skeleton"
    )
    design_subparsers = design.add_subparsers(dest="design_command", required=True)

    design_status_parser = design_subparsers.add_parser("status")
    design_status_parser.add_argument("--json", action="store_true")
    design_status_parser.set_defaults(handler=design_status)

    design_remember_parser = design_subparsers.add_parser(
        "remember", help="record a principle the user stated"
    )
    design_remember_parser.add_argument("statement", nargs="+")
    design_remember_parser.add_argument("--scope", default="all", choices=sorted(grogu_design.SCOPES))
    design_remember_parser.add_argument("--rationale")
    design_remember_parser.add_argument("--example", action="append")
    design_remember_parser.add_argument("--anti-example", action="append", dest="anti_example")
    design_remember_parser.set_defaults(handler=design_remember)

    design_suggest_parser = design_subparsers.add_parser(
        "suggest", help="queue an inferred preference for the user to confirm"
    )
    design_suggest_parser.add_argument("statement", nargs="+")
    design_suggest_parser.add_argument("--scope", default="all", choices=sorted(grogu_design.SCOPES))
    design_suggest_parser.add_argument("--rationale")
    design_suggest_parser.add_argument("--evidence")
    design_suggest_parser.add_argument("--source", default="observed")
    design_suggest_parser.add_argument("--confidence", type=_confidence, default=0.5, help="0-1, or certain/high/medium/low/guess")
    design_suggest_parser.set_defaults(handler=design_suggest)

    design_review_parser = design_subparsers.add_parser(
        "review", help="list pending inferred preferences"
    )
    design_review_parser.add_argument("--limit", type=int, default=50)
    design_review_parser.add_argument("--json", action="store_true")
    design_review_parser.set_defaults(handler=design_review)

    design_confirm_parser = design_subparsers.add_parser("confirm")
    design_confirm_parser.add_argument("id")
    design_confirm_parser.set_defaults(handler=design_confirm)

    design_reject_parser = design_subparsers.add_parser("reject")
    design_reject_parser.add_argument("id")
    design_reject_parser.set_defaults(handler=design_reject)

    design_forget_parser = design_subparsers.add_parser("forget")
    design_forget_parser.add_argument("id")
    design_forget_parser.set_defaults(handler=design_forget)

    design_recall_parser = design_subparsers.add_parser(
        "recall", help="the principles that apply to a surface"
    )
    design_recall_parser.add_argument("query", nargs="*")
    design_recall_parser.add_argument("--scope")
    design_recall_parser.add_argument("--limit", type=int, default=20)
    design_recall_parser.add_argument("--json", action="store_true")
    design_recall_parser.set_defaults(handler=design_recall)

    design_template_parser = design_subparsers.add_parser(
        "template", help="print the required design spec skeleton"
    )
    design_template_parser.add_argument("title", nargs="*")
    design_template_parser.set_defaults(handler=design_template)

    design_html_template_parser = design_subparsers.add_parser(
        "html-template",
        help="print the standing chrome for a standalone HTML report or guide",
    )
    design_html_template_parser.add_argument("title", nargs="*")
    design_html_template_parser.add_argument("--subtitle", default="Review guide")
    design_html_template_parser.add_argument("--eyebrow")
    design_html_template_parser.add_argument("--headline")
    design_html_template_parser.add_argument("--dek")
    design_html_template_parser.add_argument("--logo")
    design_html_template_parser.add_argument("--footnote", default="")
    design_html_template_parser.add_argument(
        "--section",
        action="append",
        dest="sections",
        help="a table-of-contents entry, in order (can be used multiple times)",
    )
    design_html_template_parser.add_argument(
        "--hero-stat",
        action="append",
        dest="hero_stats",
        metavar="VALUE:LABEL",
        help='a hero stat as "value:label", e.g. "94pct:Hit rate" (can be used multiple times, up to four reads best)',
    )
    design_html_template_parser.set_defaults(handler=design_html_template)

    design_seed_parser = design_subparsers.add_parser(
        "seed", help="record the baseline Apple-leaning principles"
    )
    design_seed_parser.add_argument("--apple", action="store_true")
    design_seed_parser.set_defaults(handler=design_seed)

    session = subparsers.add_parser(
        "session", help="start and manage Grogu sessions"
    )
    session_subparsers = session.add_subparsers(
        dest="session_command", required=True
    )
    new_session = session_subparsers.add_parser(
        "new",
        help="start a new remotely accessible Grogu session",
    )
    remote = new_session.add_mutually_exclusive_group()
    remote.add_argument("--remote", action="store_true")
    remote.add_argument("--remote-export", action="store_true")
    remote.add_argument(
        "--no-remote",
        action="store_true",
        help="start locally without GitHub remote access",
    )
    new_session.add_argument(
        "copilot_arguments",
        nargs=argparse.REMAINDER,
        help="additional Copilot options; separate them with -- when needed",
    )
    new_session.set_defaults(handler=session_new)

    worktree = subparsers.add_parser(
        "worktree",
        help="inspect and clean up Grogu's own self-modification git worktrees",
    )
    worktree_subparsers = worktree.add_subparsers(
        dest="worktree_command", required=True
    )
    worktree_list_parser = worktree_subparsers.add_parser(
        "list", help="list git worktrees in the Grogu checkout"
    )
    worktree_list_parser.set_defaults(handler=worktree_list)
    worktree_prune_parser = worktree_subparsers.add_parser(
        "prune",
        help="remove worktrees whose branch has merged or was deleted upstream",
    )
    worktree_prune_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report stale worktrees without removing them",
    )
    worktree_prune_parser.set_defaults(handler=worktree_prune)

    return parser


_WINDOWS_CMD_META = re.compile(r'([()\[\]%!^"`<>&|;, *?])')


def _escape_windows_cmd(value: str) -> str:
    return _WINDOWS_CMD_META.sub(r"^\1", value)


def _escape_windows_cmd_argument(value: str, escape_depth: int) -> str:
    value = re.sub(
        r'(\\*)"',
        lambda match: match.group(1) * 2 + r"\"",
        value,
    )
    value = re.sub(r"(\\*)$", lambda match: match.group(1) * 2, value)
    value = f'"{value}"'
    for _ in range(escape_depth):
        value = _WINDOWS_CMD_META.sub(r"^\1", value)
    return value


def _windows_batch_forwards_all_arguments(copilot: str) -> bool:
    try:
        with open(copilot, "rb") as shim:
            content = shim.read()
    except OSError:
        return False
    for raw_line in content.splitlines():
        line = raw_line.lstrip()
        lowered = line.lower()
        if (
            not line
            or line.startswith(b":")
            or re.match(br"@?rem(?:[ \t]|$)", lowered)
        ):
            continue
        if any(
            (len(match.group(0)) - 1) % 2
            for match in re.finditer(br"%+\*", line)
        ):
            return True
    return False


def _windows_powershell_shim(copilot: str) -> str | None:
    shim = f"{os.path.splitext(copilot)[0]}.ps1"
    return shim if os.path.isfile(shim) else None


def _windows_powershell(environment: dict[str, str]) -> str | None:
    path = (
        environment.get("PATH")
        or environment.get("Path")
        or environment.get("path")
    )
    for executable in ("pwsh.exe", "powershell.exe"):
        resolved = shutil.which(executable, path=path)
        if resolved:
            return resolved
    return None


def _windows_batch_invocation(
    copilot: str, arguments: list[str], environment: dict[str, str]
) -> tuple[str, str]:
    interpreter = (
        environment.get("COMSPEC") or os.environ.get("COMSPEC") or "cmd.exe"
    )
    escape_depth = 2 if _windows_batch_forwards_all_arguments(copilot) else 1
    shell_command = " ".join(
        [
            _escape_windows_cmd(copilot),
            *(
                _escape_windows_cmd_argument(argument, escape_depth)
                for argument in arguments
            ),
        ]
    )
    command_line = (
        f"{subprocess.list2cmdline([interpreter])} /d /v:off /s /c "
        f'"{shell_command}"'
    )
    return interpreter, command_line


def _run_copilot(copilot: str, arguments: list[str], environment: dict[str, str]) -> int:
    """Run Copilot as a child that owns the terminal directly.

    stdin/stdout/stderr are inherited untouched, so Copilot's TUI is never
    piped, buffered or rewritten by Grogu. Terminal-generated signals reach
    Copilot through the foreground process group; `restore_signals` resets the
    handlers Grogu ignores here before Copilot is executed.
    """
    if os.name == "nt":
        # Parent and child inherit the same console and standard handles.
        # Windows has no POSIX process-group or controlling-terminal APIs;
        # Ctrl+C is delivered by the console to both processes.
        try:
            if os.path.splitext(copilot)[1].lower() in {".cmd", ".bat"}:
                powershell_shim = _windows_powershell_shim(copilot)
                if powershell_shim is not None:
                    powershell = _windows_powershell(environment)
                    if powershell is None:
                        print(
                            "grogu: a PowerShell sibling exists for the Windows "
                            "Copilot shim, but no PowerShell executable was found",
                            file=sys.stderr,
                        )
                        return 127
                    return subprocess.run(
                        [
                            powershell,
                            "-NoLogo",
                            "-NoProfile",
                            "-ExecutionPolicy",
                            "Bypass",
                            "-File",
                            powershell_shim,
                            *arguments,
                        ],
                        env=environment,
                    ).returncode
                if any("%" in value for value in [copilot, *arguments]):
                    print(
                        "grogu: refusing to pass a percent-bearing command or "
                        "argument through a .cmd/.bat shim without a sibling "
                        "PowerShell shim; cmd.exe would expand or corrupt it",
                        file=sys.stderr,
                    )
                    return 2
                interpreter, command_line = _windows_batch_invocation(
                    copilot, arguments, environment
                )
                return subprocess.run(
                    command_line,
                    executable=interpreter,
                    env=environment,
                ).returncode
            return subprocess.run([copilot, *arguments], env=environment).returncode
        except KeyboardInterrupt:
            return 130

    previous = {
        number: signal.signal(number, signal.SIG_IGN)
        for number in (signal.SIGINT, signal.SIGQUIT)
    }
    process = None
    terminal_fd = None
    parent_pgrp = None
    if sys.stdin.isatty():
        terminal_fd = sys.stdin.fileno()
        parent_pgrp = os.getpgrp()

    def prepare_child() -> None:
        os.setpgrp()
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGQUIT, signal.SIG_DFL)

    try:
        process = subprocess.Popen(
            [copilot, *arguments],
            env=environment,
            preexec_fn=prepare_child if terminal_fd is not None else None,
        )
        if terminal_fd is not None:
            os.tcsetpgrp(terminal_fd, process.pid)

        def forward(signal_number: int, _frame: object) -> None:
            try:
                process.send_signal(signal_number)
            except (ProcessLookupError, ValueError):
                pass

        for number in (signal.SIGTERM, signal.SIGHUP):
            previous[number] = signal.signal(number, forward)
        while True:
            try:
                status = process.wait()
                break
            except KeyboardInterrupt:  # pragma: no cover - parent ignores SIGINT
                continue
    finally:
        if terminal_fd is not None and parent_pgrp is not None:
            os.tcsetpgrp(terminal_fd, parent_pgrp)
        for number, handler in previous.items():
            signal.signal(number, handler)
    return 128 - status if status < 0 else status


def launch_copilot(arguments: list[str]) -> int:
    copilot = shutil.which("copilot")
    if copilot is None:
        print("grogu: copilot CLI was not found on PATH", file=sys.stderr)
        return 127
    if "--plain" in arguments:
        # `--plain` is the escape hatch: Copilot exactly as it ships, without
        # Grogu instructions, banner, terminal marks or the autopilot default.
        arguments = [argument for argument in arguments if argument != "--plain"]
        if os.name == "nt":
            return _run_copilot(copilot, arguments, os.environ.copy())
        os.execvpe(copilot, [copilot, *arguments], os.environ.copy())
        return 127

    try:
        arguments = copilot_arguments(arguments)
    except grogu_capabilities.CapabilityError as error:
        print(f"grogu: {error}", file=sys.stderr)
        return 2
    mark_grogu_terminal()
    sync_grogu_main_checkout()
    prune_stale_grogu_worktrees()
    environment = os.environ.copy()
    environment.setdefault("GROGU_SESSION_ID", str(uuid.uuid4()))
    # Task leases taken inside this session end when this launcher ends.
    environment["GROGU_SESSION_PID"] = str(os.getpid())
    memory = grogu_memory.MemoryStore()
    try:
        memory_result = memory.index()
    except (OSError, ValueError) as error:
        print(f"grogu: repository intelligence update failed: {error}", file=sys.stderr)
        memory_result = {"manifest": {}, "index": {}}
    if memory_result.get("manifest"):
        register_repository(memory_result["manifest"], memory.root)
    environment["GROGU_MEMORY_DIR"] = str(memory.directory)
    if memory_result.get("manifest", {}).get("repository_id"):
        environment["GROGU_REPOSITORY_ID"] = memory_result["manifest"]["repository_id"]
    environment["GROGU_PERSONAL_MEMORY_DIR"] = str(
        grogu_personal_memory.PersonalMemoryStore(GROGU_HOME).directory
    )
    initialize_trace_db()
    with connect(TRACE_DB) as database:
        grogu_telemetry.record(
            database,
            event="session_started",
            outcome="ok",
            repository_id=memory_result.get("manifest", {}).get("repository_id", ""),
            session_id=environment["GROGU_SESSION_ID"],
            payload={"grogu_version": VERSION},
        )
    instruction_dirs = environment.get("COPILOT_CUSTOM_INSTRUCTIONS_DIRS", "")
    additions = str(ROOT / ".github")
    environment["COPILOT_CUSTOM_INSTRUCTIONS_DIRS"] = (
        f"{instruction_dirs},{additions}" if instruction_dirs else additions
    )

    if not banner_enabled():
        if os.name == "nt":
            return _run_copilot(copilot, arguments, environment)
        os.execvpe(copilot, [copilot, *arguments], environment)
        return 127

    installed = False
    try:
        installed = grogu_banner.install(
            GROGU_HOME,
            VERSION,
            environment,
            status_line=status_line_enabled(),
        )
    except OSError:
        installed = False
    try:
        return _run_copilot(copilot, arguments, environment)
    finally:
        if installed:
            try:
                grogu_banner.restore(GROGU_HOME)
            except OSError:
                pass


GROGU_COMMANDS = frozenset(
    {
        "doctor",
        "trace",
        "telemetry",
        "project",
        "memory",
        "aggregate",
        "codemode",
        "personal",
        "capability",
        "banner",
        "task",
        "plan",
        "review",
        "design",
        "session",
        "worktree",
        "watch",
        "guard",
        "skill",
    }
)


def _role_claim_is_honest(parsed: argparse.Namespace) -> bool:
    """Fast-path a role contradiction still visible in this process.

    PlanStore also binds the claim to the agent identity, which covers later
    commands after a fresh shell has dropped GROGU_ROLE. Keeping this check
    gives the immediate mismatch the CLI's concise usage-error exit.
    """
    declared = grogu_plans.current_role()
    if not declared:
        return True
    sub = ""
    for attribute in ("plan_command", "design_command", "task_command"):
        sub = getattr(parsed, attribute, "") or ""
        if sub:
            break
    if sub in SUBJECT_ROLE_COMMANDS:
        return True
    claimed = (getattr(parsed, "role", "") or "").strip().lower()
    if not claimed or claimed == declared:
        return True
    print(
        f"grogu: this session is the {declared}; it cannot act as the "
        f"{claimed}. If the {claimed} should do this, spawn one -- reading a "
        "stage as a role you are not is how an engineer ends up writing to "
        "the test rather than to the plan.",
        file=sys.stderr,
    )
    return False


def _normalize_review_args(arguments: list[str]) -> list[str]:
    """`grogu review <plan-id>` means `grogu review open <plan-id>`.

    A bare plan id in the first slot is not a subcommand name, so argparse would
    reject it. Insert the implicit `open` so both spellings work, exactly as
    `grogu review open` and `grogu review` are documented to be the same thing.
    """
    known = {
        "open", "list", "comment", "reply", "resolve",
        "request-changes", "status", "assets",
    }
    if arguments and arguments[0] == "review":
        if len(arguments) == 1:
            return ["review", "open"]
        first = arguments[1]
        if first not in known and not first.startswith("-"):
            return ["review", "open", *arguments[1:]]
        if first in ("-h", "--help"):
            return arguments
    return arguments


def main(arguments: list[str]) -> int:
    if arguments[:2] == ["banner", "status-line"]:
        sys.stdout.write(grogu_banner.status_line_frame() + "\n")
        return 0
    arguments = _normalize_review_args(arguments)
    is_grogu_command = bool(arguments) and (
        arguments[0] in GROGU_COMMANDS
        or (len(arguments) == 1 and arguments[0] in {"--version", "-h", "--help"})
    )
    if not is_grogu_command:
        # Everything else, including bare `grogu`, belongs to Copilot.
        return launch_copilot(arguments)
    parser = build_parser()
    parsed = parser.parse_args(arguments)
    if parsed.command is None:
        return launch_copilot(arguments)
    if not _resolve_plan_id(parsed):
        return 2
    if not _role_claim_is_honest(parsed):
        return 2
    global _PENDING_NOTICE
    _PENDING_NOTICE = _notice_for(parsed)
    try:
        return parsed.handler(parsed)
    except grogu_skills.SkillError as error:
        print(f"grogu: {error}", file=sys.stderr)
        return 2
    except grogu_design.DesignError as error:
        print(f"grogu: {error}", file=sys.stderr)
        return 2
    except grogu_plans.PlanError as error:
        print(f"grogu: {error}", file=sys.stderr)
        return 3
    except grogu_review.ReviewError as error:
        print(f"grogu: {error}", file=sys.stderr)
        return 3
    except grogu_review_server.ReviewServerError as error:
        print(f"grogu: {error}", file=sys.stderr)
        return 2
    except grogu_plan_server.PlanServerError as error:
        print(f"grogu: {error}", file=sys.stderr)
        return 2
    except grogu_tasks.TaskError as error:
        print(f"grogu: {error}", file=sys.stderr)
        return 2
    except (grogu_capabilities.CapabilityError, ValueError) as error:
        print(f"grogu: {error}", file=sys.stderr)
        return 2
    finally:
        # Steering rides out on whatever the agent already ran, so nobody has to
        # remember to poll for it.
        _record_activity(parsed)
        if _PENDING_NOTICE:
            # Still pending means no JSON payload carried it out.
            _emit_notice(_PENDING_NOTICE, parsed)


if __name__ == "__main__":
    grogu_platform.configure_standard_streams()
    raise SystemExit(main(sys.argv[1:]))
