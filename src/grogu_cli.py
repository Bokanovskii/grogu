#!/usr/bin/env python3
"""Lightweight Grogu launcher and local state utilities."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import grogu_banner
import grogu_codemode
import grogu_context
import grogu_gmail
import grogu_imessage
import grogu_mcp
import grogu_memory
import grogu_personal_memory
import grogu_plans
import grogu_telemetry
import grogu_tasks
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


def print_json(value: object) -> None:
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
    return 0 if checks["copilot_available"] and checks["instructions_available"] else 1


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


def imessage_adapter(_: argparse.Namespace) -> grogu_imessage.MacOSIMessageAdapter:
    return grogu_imessage.MacOSIMessageAdapter()


def imessage_status(args: argparse.Namespace) -> int:
    print_json(imessage_adapter(args).status())
    return 0


def imessage_search(args: argparse.Namespace) -> int:
    messages = imessage_adapter(args).search(
        args.query, limit=args.limit, use_seaglass=not args.no_seaglass
    )
    for message in messages:
        print(json.dumps(message, sort_keys=True))
    return 0


def imessage_draft(args: argparse.Namespace) -> int:
    draft = grogu_imessage.DraftStore(GROGU_HOME).create(
        grogu_imessage.Recipient(args.recipient, args.display_name),
        args.message,
    )
    print_json(dataclasses.asdict(draft))
    return 0


def imessage_send(args: argparse.Namespace) -> int:
    store = grogu_imessage.DraftStore(GROGU_HOME)
    draft = store.get(args.draft)
    if draft is None:
        print(f"grogu: no iMessage draft with id {args.draft!r}", file=sys.stderr)
        return 2
    if draft.status != "draft":
        print(
            f"grogu: iMessage draft {args.draft!r} is already {draft.status}",
            file=sys.stderr,
        )
        return 2
    result = imessage_adapter(args).send(
        draft.recipient, draft.body, confirmed=args.confirm
    )
    store.mark_sent(draft.id)
    print_json(result)
    return 0


def gmail_adapter(_: argparse.Namespace) -> grogu_gmail.GmailAdapter:
    return grogu_gmail.GmailAdapter()


def gmail_status(args: argparse.Namespace) -> int:
    print_json(gmail_adapter(args).status())
    return 0


def gmail_search(args: argparse.Namespace) -> int:
    for message in gmail_adapter(args).search(args.query, limit=args.limit):
        print(json.dumps(message, sort_keys=True))
    return 0


def gmail_draft(args: argparse.Namespace) -> int:
    draft = grogu_gmail.DraftStore(GROGU_HOME).create(
        args.to, args.subject, args.message
    )
    print_json(dataclasses.asdict(draft))
    return 0


def gmail_send(args: argparse.Namespace) -> int:
    store = grogu_gmail.DraftStore(GROGU_HOME)
    draft = store.get(args.draft)
    if draft is None:
        print(f"grogu: no Gmail draft with id {args.draft!r}", file=sys.stderr)
        return 2
    if draft.status != "draft":
        print(
            f"grogu: Gmail draft {args.draft!r} is already {draft.status}",
            file=sys.stderr,
        )
        return 2
    result = gmail_adapter(args).send(draft, confirmed=args.confirm)
    store.mark_sent(draft.id)
    print_json(result)
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
    """
    if os.environ.get("GROGU_PRUNE_WORKTREES", "1") == "0":
        return
    try:
        pruned = grogu_worktrees.prune_stale_worktrees(ROOT)
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
    if args.dry_run:
        stale = grogu_worktrees.stale_worktrees(ROOT)
        for entry in stale:
            print(f"{entry.worktree.path}  ({entry.reason})")
        if not stale:
            print("no stale worktrees")
        return 0
    pruned = grogu_worktrees.prune_stale_worktrees(ROOT)
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
    if not wants_autopilot_default(arguments):
        return list(arguments)
    # Leading position keeps user arguments, including any trailing `--`
    # separator, exactly as they were typed.
    return ["--autopilot", *arguments]


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
        print_json([store.view(task["id"]) for task in tasks])
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


def plan_store(args: argparse.Namespace) -> grogu_plans.PlanStore:
    return grogu_plans.PlanStore(Path(args.repo).expanduser() if args.repo else None)


def _plan_role(args: argparse.Namespace) -> str:
    role = getattr(args, "role", "") or grogu_plans.current_role()
    if not role:
        raise grogu_plans.PlanError(
            "pass --role, or export GROGU_ROLE. Plan access is role-scoped: "
            "who is asking decides what may be read."
        )
    return role


def _read_body(args: argparse.Namespace) -> str:
    if getattr(args, "file", None):
        if args.file == "-":
            return sys.stdin.read()
        return Path(args.file).expanduser().read_text(encoding="utf8")
    return args.body or ""


def plan_new(args: argparse.Namespace) -> int:
    store = plan_store(args)
    plan = store.create(
        args.title,
        task_id=args.task or "",
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
        print_json([store.summary(plan["id"]) for plan in plans])
        return 0
    for plan in plans:
        review = " [awaiting review]" if plan.get("review_required") and plan.get("status") != grogu_plans.APPROVED else ""
        task = f" ({plan['task_id']})" if plan.get("task_id") else ""
        print(f"{plan['id']}  {plan.get('status', '?'):<12}{task} {plan.get('title', '')}{review}")
    return 0


def plan_status(args: argparse.Namespace) -> int:
    store = plan_store(args)
    summary = store.summary(store.resolve(args.id))
    if args.json:
        print_json(summary)
        return 0
    print(f"{summary['id']}  {summary['status']}  {summary['title']}")
    print(f"  stages: " + ", ".join(
        f"{stage}={summary['stage_state'].get(stage, '?')}" for stage in summary["stages"]
    ))
    print(f"  amendment rounds: {summary['rounds']}   engineer/tester rounds: {summary['defect_rounds']}")
    if summary.get("escalated"):
        print("  escalated to the architect: the engineer/tester loop stopped converging")
    if summary["review_required"] and summary["status"] != grogu_plans.APPROVED:
        print("  the user asked for this plan; it needs `grogu plan approve` before work starts")
    for stream in summary["workstreams"]:
        depends = f" after {', '.join(stream['depends_on'])}" if stream["depends_on"] else ""
        print(f"  workstream {stream['name']}: {', '.join(stream['paths'])}{depends}")
    for amendment in summary["open_amendments"]:
        print(f"  amendment {amendment['id']} from {amendment['raised_by']}: {amendment['claim']}")
    for defect in summary["open_defects"]:
        print(f"  defect {defect['id']} -> {defect['owner']} ({defect['route']}): {defect['report']}")
    pending = {role: count for role, count in summary["steering_pending"].items() if count}
    for role, count in sorted(pending.items()):
        print(f"  {count} unread steering note(s) for the {role}")
    return 0


def plan_write(args: argparse.Namespace) -> int:
    store = plan_store(args)
    body = _read_body(args)
    plan_id = store.resolve(args.id)
    store.write_stage(plan_id, args.stage, body, role=getattr(args, "role", "") or "")
    print(f"wrote {args.stage} plan for {plan_id} ({len(body)} bytes)")
    return 0


def plan_show(args: argparse.Namespace) -> int:
    store = plan_store(args)
    plan_id = store.resolve(args.id)
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
        store.resolve(args.id), args.stage, args.state, note=args.note or ""
    )
    print(f"{plan['id']} {args.stage}={args.state}")
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
    result = store.finalize(store.resolve(args.id), note=args.note or "")
    print(f"finalized {result['plan']}")
    for path in result["emitted"]:
        print(f"  unsealed {path}")
    print("  stage the plan directory so the pull request carries the plans it implements")
    return 0


def plan_gate(args: argparse.Namespace) -> int:
    store = plan_store(args)
    result = store.gate(store.resolve(args.id), args.stage)
    if args.json:
        print_json(result)
    else:
        verdict = "allowed" if result["allowed"] else "blocked"
        print(f"{result['plan']} {result['gate']}: {verdict} (status {result['status']})")
        for blocker in result["blockers"]:
            print(f"  - {blocker}")
    return 0 if result["allowed"] else 3


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
    stream = store.add_workstream(
        store.resolve(args.id),
        name=args.name,
        paths=args.path,
        depends_on=args.depends_on or [],
    )
    print(f"workstream {stream['name']}: {', '.join(stream['paths'])}")
    return 0


def plan_workstreams(args: argparse.Namespace) -> int:
    store = plan_store(args)
    plan_id = store.resolve(args.id)
    conflicts = store.workstream_conflicts(plan_id)
    batches = store.parallel_batches(plan_id)
    if args.json:
        print_json({"batches": batches, "conflicts": conflicts})
    else:
        for index, batch in enumerate(batches, start=1):
            print(f"wave {index}: {', '.join(batch)}")
        for conflict in conflicts:
            left, right = conflict["workstreams"]
            print(f"conflict: {left} and {right} both claim {' / '.join(conflict['paths'])}")
        if not conflicts and len(batches) and max(len(batch) for batch in batches) > 1:
            print("file sets are disjoint; these waves may run in parallel worktrees")
    return 3 if (conflicts and args.check) else 0


def plan_steer(args: argparse.Namespace) -> int:
    store = plan_store(args)
    plan_id = store.resolve(args.id) if args.id else ""
    note = store.steer(
        " ".join(args.text),
        plan_id=plan_id,
        role=args.role or "all",
        requires_replan=args.requires_replan,
    )
    scope = plan_id or "repository"
    print(f"steering #{note['seq']} recorded for {note['role']} on {scope}")
    if note["requires_replan"]:
        print("plan moved to needs_review: the architect must fold this in before work continues")
    return 0


def plan_steering(args: argparse.Namespace) -> int:
    store = plan_store(args)
    plan_id = store.resolve(args.id) if args.id else ""
    role = getattr(args, "role", "") or "all"
    if args.ack:
        acked = store.ack_steering(role=_plan_role(args), plan_id=plan_id)
        print(f"acked steering for {acked['role']} (repo {acked['repository_seq']}, plan {acked['plan_seq']})")
        return 0
    result = store.steering(role=role, plan_id=plan_id, unread=args.unread)
    if args.json:
        print_json(result)
        return 0
    for scope in ("repository", "plan"):
        for note in result.get(scope, []):
            binding = " [requires replan]" if note.get("requires_replan") else ""
            print(f"{scope} #{note['seq']}  {note['at']}  ->{note['role']}{binding}: {note['text']}")
    return 0


def plan_brief(args: argparse.Namespace) -> int:
    store = plan_store(args)
    plan_id = store.resolve(args.id) if args.id else ""
    brief = store.brief(args.role, plan_id=plan_id, base_dir=ROOT / ".github" / "agents")
    if args.json:
        print_json(brief)
        return 0
    if brief["base"]:
        print(brief["base"].rstrip())
    if brief["overlay"]:
        print(f"\n## Repository specifics ({brief['overlay_path']})\n")
        print(brief["overlay"].rstrip())
    else:
        print(
            f"\n(no repository overlay at {brief['overlay_path']}; "
            "write one to give this role repository-specific context)"
        )
    notes = brief["steering"].get("repository", []) + brief["steering"].get("plan", [])
    if notes:
        print("\n## Standing steering from the user\n")
        for note in notes:
            print(f"- {note['text']}")
    return 0


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
    print("  fix the overlay or the harness, not just this plan")
    return 0


def plan_friction(args: argparse.Namespace) -> int:
    store = plan_store(args)
    if args.note:
        entry = store.note_friction(
            args.note,
            plan_id=store.resolve(args.id) if args.id else "",
            role=getattr(args, "role", "") or "",
        )
        print(f"recorded friction #{entry['seq']} from the {entry['role']}")
        return 0
    if args.resolve:
        entry = store.resolve_friction(args.resolve, note=args.resolution or "addressed")
        print(f"friction #{entry['seq']} resolved")
        return 0
    report = store.friction(include_resolved=args.all)
    if args.json:
        print_json(report)
        return 0
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="grogu")
    parser.add_argument("--version", action="version", version=f"grogu {VERSION}")
    subparsers = parser.add_subparsers(dest="command")

    subparser = subparsers.add_parser("doctor")
    subparser.set_defaults(handler=doctor)

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
    remember.add_argument("--confidence", type=float, default=0.8)
    remember.add_argument("--provenance", default="user")
    remember.set_defaults(handler=memory_remember)
    link = memory_subparsers.add_parser("link", parents=[memory_common])
    link.add_argument("source")
    link.add_argument("target")
    link.add_argument("--kind", required=True)
    link.add_argument("--confidence", type=float, default=0.8)
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
    personal_remember_parser.add_argument("--confidence", type=float, default=0.8)
    personal_remember_parser.add_argument("--provenance", default="user")
    personal_remember_parser.set_defaults(handler=personal_remember)
    personal_link_parser = personal_subparsers.add_parser("link")
    personal_link_parser.add_argument("source")
    personal_link_parser.add_argument("target")
    personal_link_parser.add_argument("--kind", required=True)
    personal_link_parser.add_argument("--confidence", type=float, default=0.8)
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
        "--source", required=True, help="where this candidate was observed, e.g. gmail, imessage"
    )
    personal_suggest_parser.add_argument("--tag", action="append")
    personal_suggest_parser.add_argument("--confidence", type=float, default=0.5)
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

    imessage = subparsers.add_parser(
        "imessage",
        help="opt-in local macOS Messages access with confirmation-gated sending",
    )
    imessage_subparsers = imessage.add_subparsers(
        dest="imessage_command", required=True
    )
    imessage_status_parser = imessage_subparsers.add_parser("status")
    imessage_status_parser.set_defaults(handler=imessage_status)
    imessage_search_parser = imessage_subparsers.add_parser("search")
    imessage_search_parser.add_argument("query")
    imessage_search_parser.add_argument("--limit", type=int, default=20)
    imessage_search_parser.add_argument(
        "--no-seaglass",
        action="store_true",
        help="force the local SQL LIKE scan even if a seaglass MCP server is configured",
    )
    imessage_search_parser.set_defaults(handler=imessage_search)
    imessage_draft_parser = imessage_subparsers.add_parser("draft")
    imessage_draft_parser.add_argument("--recipient", required=True)
    imessage_draft_parser.add_argument("--display-name", default="")
    imessage_draft_parser.add_argument("--message", required=True)
    imessage_draft_parser.set_defaults(handler=imessage_draft)
    imessage_send_parser = imessage_subparsers.add_parser("send")
    imessage_send_parser.add_argument("draft")
    imessage_send_parser.add_argument("--confirm", action="store_true")
    imessage_send_parser.set_defaults(handler=imessage_send)

    gmail = subparsers.add_parser(
        "gmail", help="opt-in Gmail access with draft-first safety"
    )
    gmail_subparsers = gmail.add_subparsers(dest="gmail_command", required=True)
    gmail_status_parser = gmail_subparsers.add_parser("status")
    gmail_status_parser.set_defaults(handler=gmail_status)
    gmail_search_parser = gmail_subparsers.add_parser("search")
    gmail_search_parser.add_argument("query")
    gmail_search_parser.add_argument("--limit", type=int, default=20)
    gmail_search_parser.set_defaults(handler=gmail_search)
    gmail_draft_parser = gmail_subparsers.add_parser("draft")
    gmail_draft_parser.add_argument("--to", required=True)
    gmail_draft_parser.add_argument("--subject", required=True)
    gmail_draft_parser.add_argument("--message", required=True)
    gmail_draft_parser.set_defaults(handler=gmail_draft)
    gmail_send_parser = gmail_subparsers.add_parser("send")
    gmail_send_parser.add_argument("draft")
    gmail_send_parser.add_argument("--confirm", action="store_true")
    gmail_send_parser.set_defaults(handler=gmail_send)

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

    plan_list_parser = plan_subparsers.add_parser("list", parents=[plan_common])
    plan_list_parser.add_argument("--status", choices=grogu_plans.PLAN_STATUSES)
    plan_list_parser.add_argument("--all", action="store_true", help="include superseded")
    plan_list_parser.add_argument("--json", action="store_true")
    plan_list_parser.set_defaults(handler=plan_list)

    plan_status_parser = plan_subparsers.add_parser(
        "status", help="bounded plan summary with no plan prose", parents=[plan_common]
    )
    plan_status_parser.add_argument("id")
    plan_status_parser.add_argument("--json", action="store_true")
    plan_status_parser.set_defaults(handler=plan_status)

    plan_write_parser = plan_subparsers.add_parser(
        "write", help="write a plan stage (architect only)",
        parents=[plan_common, role_common],
    )
    plan_write_parser.add_argument("id")
    plan_write_parser.add_argument("stage", choices=grogu_plans.STAGES)
    plan_write_parser.add_argument("--body", default="")
    plan_write_parser.add_argument("--file", help="read the body from a file, or - for stdin")
    plan_write_parser.set_defaults(handler=plan_write)

    plan_show_parser = plan_subparsers.add_parser(
        "show", help="read a plan stage the role is allowed to read",
        parents=[plan_common, role_common],
    )
    plan_show_parser.add_argument("id")
    plan_show_parser.add_argument("--stage", choices=grogu_plans.STAGES, default=grogu_plans.IMPLEMENTATION)
    plan_show_parser.set_defaults(handler=plan_show)

    plan_approve_parser = plan_subparsers.add_parser("approve", parents=[plan_common])
    plan_approve_parser.add_argument("id")
    plan_approve_parser.add_argument("--note")
    plan_approve_parser.set_defaults(handler=plan_approve)

    plan_stage_parser = plan_subparsers.add_parser(
        "stage", help="record stage progress", parents=[plan_common]
    )
    plan_stage_parser.add_argument("id")
    plan_stage_parser.add_argument("stage", choices=grogu_plans.STAGES)
    plan_stage_parser.add_argument("state", choices=grogu_plans.STAGE_STATES)
    plan_stage_parser.add_argument("--note")
    plan_stage_parser.set_defaults(handler=plan_stage)

    plan_supersede_parser = plan_subparsers.add_parser("supersede", parents=[plan_common])
    plan_supersede_parser.add_argument("id")
    plan_supersede_parser.add_argument("--note")
    plan_supersede_parser.set_defaults(handler=plan_supersede)

    plan_finalize_parser = plan_subparsers.add_parser(
        "finalize",
        help="unseal every stage so the finished plan ships in the pull request",
        parents=[plan_common],
    )
    plan_finalize_parser.add_argument("id")
    plan_finalize_parser.add_argument("--note")
    plan_finalize_parser.set_defaults(handler=plan_finalize)

    plan_gate_parser = plan_subparsers.add_parser(
        "gate", help="may the pipeline enter a stage (exit 3 when blocked)",
        parents=[plan_common],
    )
    plan_gate_parser.add_argument("id")
    plan_gate_parser.add_argument("--stage", choices=grogu_plans.GATES, required=True)
    plan_gate_parser.add_argument("--json", action="store_true")
    plan_gate_parser.set_defaults(handler=plan_gate)

    plan_amend_parser = plan_subparsers.add_parser(
        "amend", help="ask the architect to change the plan",
        parents=[plan_common, role_common],
    )
    plan_amend_parser.add_argument("id")
    plan_amend_parser.add_argument("--claim", required=True)
    plan_amend_parser.add_argument("--evidence", default="")
    plan_amend_parser.add_argument("--stage", choices=grogu_plans.STAGES, default=grogu_plans.IMPLEMENTATION)
    plan_amend_parser.set_defaults(handler=plan_amend)

    plan_amendments_parser = plan_subparsers.add_parser("amendments", parents=[plan_common])
    plan_amendments_parser.add_argument("id")
    plan_amendments_parser.add_argument("--all", action="store_true")
    plan_amendments_parser.add_argument("--json", action="store_true")
    plan_amendments_parser.set_defaults(handler=plan_amendments)

    plan_resolve_parser = plan_subparsers.add_parser(
        "resolve", help="architect decision on an amendment or escalation",
        parents=[plan_common, role_common],
    )
    plan_resolve_parser.add_argument("id")
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
    plan_defect_parser.add_argument("id")
    plan_defect_parser.add_argument("--report", required=True)
    plan_defect_parser.add_argument(
        "--route", choices=grogu_plans.DEFECT_ROUTES, required=True
    )
    plan_defect_parser.add_argument("--evidence", default="")
    plan_defect_parser.set_defaults(handler=plan_defect)

    plan_defects_parser = plan_subparsers.add_parser("defects", parents=[plan_common])
    plan_defects_parser.add_argument("id")
    plan_defects_parser.add_argument("--all", action="store_true")
    plan_defects_parser.add_argument("--json", action="store_true")
    plan_defects_parser.set_defaults(handler=plan_defects)

    plan_defect_resolve_parser = plan_subparsers.add_parser(
        "defect-resolve", parents=[plan_common]
    )
    plan_defect_resolve_parser.add_argument("id")
    plan_defect_resolve_parser.add_argument("defect")
    plan_defect_resolve_parser.add_argument("--note", required=True)
    plan_defect_resolve_parser.set_defaults(handler=plan_defect_resolve)

    plan_workstream_parser = plan_subparsers.add_parser(
        "workstream", help="declare a parallelisable unit and the files it owns",
        parents=[plan_common],
    )
    plan_workstream_parser.add_argument("id")
    plan_workstream_parser.add_argument("--name", required=True)
    plan_workstream_parser.add_argument("--path", action="append", required=True)
    plan_workstream_parser.add_argument("--depends-on", action="append")
    plan_workstream_parser.set_defaults(handler=plan_workstream)

    plan_workstreams_parser = plan_subparsers.add_parser(
        "workstreams", help="parallel waves, and any file-set conflicts between them",
        parents=[plan_common],
    )
    plan_workstreams_parser.add_argument("id")
    plan_workstreams_parser.add_argument(
        "--check", action="store_true", help="exit 3 when workstreams overlap"
    )
    plan_workstreams_parser.add_argument("--json", action="store_true")
    plan_workstreams_parser.set_defaults(handler=plan_workstreams)

    plan_steer_parser = plan_subparsers.add_parser(
        "steer", help="record steering that reaches agents spawned later",
        parents=[plan_common],
    )
    plan_steer_parser.add_argument("text", nargs="+")
    plan_steer_parser.add_argument("--plan", dest="id", help="scope to one plan")
    plan_steer_parser.add_argument(
        "--role", choices=(*grogu_plans.ROLES, "all"), default="all"
    )
    plan_steer_parser.add_argument(
        "--requires-replan",
        action="store_true",
        help="block the gates until the architect folds this into the plan",
    )
    plan_steer_parser.set_defaults(handler=plan_steer)

    plan_steering_parser = plan_subparsers.add_parser(
        "steering", help="steering visible to a role", parents=[plan_common, role_common]
    )
    plan_steering_parser.add_argument("--plan", dest="id")
    plan_steering_parser.add_argument(
        "--unread", action="store_true", help="only notes this role has not acked"
    )
    plan_steering_parser.add_argument(
        "--ack", action="store_true", help="mark everything visible as seen"
    )
    plan_steering_parser.add_argument("--json", action="store_true")
    plan_steering_parser.set_defaults(handler=plan_steering)

    plan_brief_parser = plan_subparsers.add_parser(
        "brief",
        help="assemble a role's prompt: shared contract, repository overlay, steering",
        parents=[plan_common],
    )
    plan_brief_parser.add_argument("--role", choices=grogu_plans.ROLES, required=True)
    plan_brief_parser.add_argument("--plan", dest="id")
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
    plan_retro_parser.add_argument("id")
    plan_retro_parser.add_argument("--json", action="store_true")
    plan_retro_parser.set_defaults(handler=plan_retro)

    plan_friction_parser = plan_subparsers.add_parser(
        "friction", help="recorded friction and signals recurring across plans",
        parents=[plan_common, role_common],
    )
    plan_friction_parser.add_argument("--note", help="record friction you just hit")
    plan_friction_parser.add_argument("--plan", dest="id")
    plan_friction_parser.add_argument("--resolve", type=int, metavar="SEQ")
    plan_friction_parser.add_argument("--resolution")
    plan_friction_parser.add_argument("--all", action="store_true")
    plan_friction_parser.add_argument("--json", action="store_true")
    plan_friction_parser.set_defaults(handler=plan_friction)

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


def _run_copilot(copilot: str, arguments: list[str], environment: dict[str, str]) -> int:
    """Run Copilot as a child that owns the terminal directly.

    stdin/stdout/stderr are inherited untouched, so Copilot's TUI is never
    piped, buffered or rewritten by Grogu. Terminal-generated signals reach
    Copilot through the foreground process group; `restore_signals` resets the
    handlers Grogu ignores here before Copilot is executed.
    """
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
        os.execvpe(copilot, [copilot, *arguments], os.environ.copy())
        return 127

    arguments = copilot_arguments(arguments)
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
        "imessage",
        "gmail",
        "banner",
        "task",
        "plan",
        "session",
        "worktree",
    }
)


def main(arguments: list[str]) -> int:
    if arguments[:2] == ["banner", "status-line"]:
        sys.stdout.write(grogu_banner.status_line_frame() + "\n")
        return 0
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
    try:
        return parsed.handler(parsed)
    except grogu_plans.PlanError as error:
        print(f"grogu: {error}", file=sys.stderr)
        return 3
    except grogu_tasks.TaskError as error:
        print(f"grogu: {error}", file=sys.stderr)
        return 2
    except (grogu_imessage.IMessageError, grogu_gmail.GmailError, ValueError) as error:
        print(f"grogu: {error}", file=sys.stderr)
        return 2
    finally:
        # Steering rides out on whatever the agent already ran, so nobody has to
        # remember to poll for it.
        banner = grogu_plans.pending_banner(
            Path(parsed.repo).expanduser() if getattr(parsed, "repo", None) else None
        )
        if banner:
            print(banner, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
