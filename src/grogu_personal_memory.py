"""User-scoped personal memory: durable facts about the person Grogu assists.

This store is intentionally separate from `grogu_memory.MemoryStore`, which is
repository-local and code-focused. Personal memory lives under the user's
Grogu home (`GROGU_HOME`, never inside a repository or committed to Git) and
holds knowledge about the user's life, relationships, and preferences instead
of the codebase.

Nothing here is ever written implicitly. Explicit capture (`remember`) writes
directly to the confirmed graph. Passive or inferred candidates go through
`suggest`, land in a separate pending queue, and only reach the graph after an
explicit `confirm`.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from pathlib import Path
from typing import Iterable, List, Optional

SCHEMA_VERSION = 1
NODE_TYPES = frozenset(
    {"person", "preference", "project", "goal", "event", "fact", "interest"}
)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf8")
    os.replace(temporary, path)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_jsonl(path: Path) -> List[dict]:
    try:
        lines = path.read_text(encoding="utf8").splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _write_jsonl(path: Path, entries: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _slug(value: str) -> str:
    slug = "".join(char.lower() if char.isalnum() else "-" for char in value)
    return "-".join(part for part in slug.split("-") if part)


def _validate_type(node_type: str) -> str:
    node_type = node_type.strip().lower()
    if node_type not in NODE_TYPES:
        raise ValueError(
            f"unknown personal memory type {node_type!r}; expected one of "
            + ", ".join(sorted(NODE_TYPES))
        )
    return node_type


class PersonalMemoryStore:
    """User-scoped, cross-repository memory about the person Grogu assists.

    Storage lives under `<home>/memory/`, entirely separate from any
    repository's `.grogu/intelligence/` directory and from the project
    catalog. Nothing here is ever committed to a repository.
    """

    def __init__(self, home: Optional[Path] = None) -> None:
        self.home = Path(home or Path.home() / ".grogu").expanduser()
        self.directory = self.home / "memory"
        self.manifest_path = self.directory / "manifest.json"
        self.graph_path = self.directory / "graph.json"
        self.pending_path = self.directory / "pending.jsonl"

    def _manifest(self) -> dict:
        existing = _read_json(self.manifest_path)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "kind": "grogu.personal_memory",
            "created_at": existing.get("created_at", now()),
            "updated_at": now(),
            "derivation": "grogu-personal-memory-v1",
        }
        _write_json(self.manifest_path, manifest)
        return manifest

    def _load_graph(self) -> dict:
        graph = _read_json(self.graph_path)
        if not graph or graph.get("kind") != "grogu.personal_memory_graph":
            return {
                "schema_version": SCHEMA_VERSION,
                "kind": "grogu.personal_memory_graph",
                "nodes": {},
                "edges": [],
                "updated_at": now(),
                "derivation": "grogu-personal-memory-v1",
            }
        graph.setdefault("nodes", {})
        graph.setdefault("edges", [])
        return graph

    def status(self) -> dict:
        manifest = _read_json(self.manifest_path)
        graph = self._load_graph()
        pending = _read_jsonl(self.pending_path)
        return {
            "home": str(self.home),
            "initialized": bool(manifest),
            "manifest": manifest,
            "graph": {"nodes": len(graph["nodes"]), "edges": len(graph["edges"])},
            "pending": len(pending),
        }

    def remember(
        self,
        node_type: str,
        name: str,
        summary: str,
        tags: Optional[Iterable[str]] = None,
        confidence: float = 0.8,
        provenance: Optional[dict] = None,
    ) -> dict:
        node_type = _validate_type(node_type)
        if not name.strip() or not summary.strip():
            raise ValueError("name and summary are required")
        self._manifest()
        graph = self._load_graph()
        node_id = f"{node_type}:{_slug(name)}"
        existing = graph["nodes"].get(node_id, {})
        node = {
            "id": node_id,
            "type": node_type,
            "name": name.strip(),
            "summary": summary.strip(),
            "tags": sorted(set(tags or [])),
            "confidence": max(0.0, min(1.0, confidence)),
            "provenance": existing.get("provenance", []) + [provenance or {"kind": "user"}],
            "created_at": existing.get("created_at", now()),
            "updated_at": now(),
        }
        graph["nodes"][node_id] = node
        graph["updated_at"] = node["updated_at"]
        _write_json(self.graph_path, graph)
        return node

    def link(
        self,
        source: str,
        target: str,
        kind: str,
        confidence: float = 0.8,
        provenance: Optional[dict] = None,
    ) -> dict:
        graph = self._load_graph()
        if source not in graph["nodes"] or target not in graph["nodes"]:
            raise ValueError("source and target nodes must exist before linking")
        edge = {
            "source": source,
            "target": target,
            "kind": kind.strip(),
            "confidence": max(0.0, min(1.0, confidence)),
            "provenance": provenance or {"kind": "user"},
            "updated_at": now(),
        }
        graph["edges"] = [
            item
            for item in graph["edges"]
            if not (
                item["source"] == source
                and item["target"] == target
                and item["kind"] == edge["kind"]
            )
        ]
        graph["edges"].append(edge)
        graph["updated_at"] = edge["updated_at"]
        _write_json(self.graph_path, graph)
        return edge

    def forget(self, node_id: str) -> bool:
        graph = self._load_graph()
        if node_id not in graph["nodes"]:
            return False
        del graph["nodes"][node_id]
        graph["edges"] = [
            edge
            for edge in graph["edges"]
            if edge["source"] != node_id and edge["target"] != node_id
        ]
        graph["updated_at"] = now()
        _write_json(self.graph_path, graph)
        return True

    def list(self, node_type: str = "", limit: int = 100) -> List[dict]:
        graph = self._load_graph()
        nodes = list(graph["nodes"].values())
        if node_type:
            node_type = _validate_type(node_type)
            nodes = [node for node in nodes if node["type"] == node_type]
        nodes.sort(key=lambda node: (-node.get("confidence", 0), node["id"]))
        return nodes[: max(0, limit)]

    def recall(
        self,
        limit: int = 40,
        query: str = "",
        node_id: str = "",
        depth: int = 1,
    ) -> dict:
        graph = self._load_graph()
        nodes = graph["nodes"]
        selected = set()
        if node_id:
            selected.add(node_id)
            frontier = {node_id}
            for _ in range(max(0, depth)):
                next_frontier = set()
                for edge in graph["edges"]:
                    if edge["source"] in frontier:
                        next_frontier.add(edge["target"])
                    if edge["target"] in frontier:
                        next_frontier.add(edge["source"])
                selected.update(next_frontier)
                frontier = next_frontier
        elif query:
            needle = query.lower()
            selected = {
                key
                for key, node in nodes.items()
                if needle in json.dumps(node, sort_keys=True).lower()
            }
        else:
            selected = set(nodes)
        chosen = [nodes[key] for key in selected if key in nodes]
        chosen.sort(key=lambda node: (-node.get("confidence", 0), node["id"]))
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "grogu.personal_memory_context",
            "nodes": chosen[: max(0, limit)],
            "edges": [
                edge
                for edge in graph["edges"]
                if edge["source"] in selected and edge["target"] in selected
            ][: max(0, limit * 2)],
            "derivation": "grogu-personal-memory-v1",
        }

    def suggest(
        self,
        node_type: str,
        name: str,
        summary: str,
        source: str,
        tags: Optional[Iterable[str]] = None,
        confidence: float = 0.5,
    ) -> dict:
        """Queue a passively observed candidate fact for later confirmation.

        Nothing here is persisted to the confirmed graph; `confirm` or
        `reject` resolve the candidate explicitly.
        """
        node_type = _validate_type(node_type)
        if not name.strip() or not summary.strip():
            raise ValueError("name and summary are required")
        if not source.strip():
            raise ValueError("source is required so the candidate's origin is traceable")
        candidate = {
            "id": str(uuid.uuid4()),
            "type": node_type,
            "name": name.strip(),
            "summary": summary.strip(),
            "tags": sorted(set(tags or [])),
            "confidence": max(0.0, min(1.0, confidence)),
            "source": source.strip(),
            "status": "pending",
            "created_at": now(),
        }
        pending = _read_jsonl(self.pending_path)
        pending.append(candidate)
        _write_jsonl(self.pending_path, pending)
        return candidate

    def review(self, limit: int = 50) -> List[dict]:
        pending = [entry for entry in _read_jsonl(self.pending_path) if entry.get("status") == "pending"]
        pending.sort(key=lambda entry: entry.get("created_at", ""))
        return pending[: max(0, limit)]

    def confirm(self, candidate_id: str) -> dict:
        pending = _read_jsonl(self.pending_path)
        remaining = []
        confirmed = None
        for entry in pending:
            if entry.get("id") == candidate_id and entry.get("status") == "pending":
                confirmed = entry
                continue
            remaining.append(entry)
        if confirmed is None:
            raise ValueError(f"no pending candidate with id {candidate_id!r}")
        node = self.remember(
            confirmed["type"],
            confirmed["name"],
            confirmed["summary"],
            tags=confirmed.get("tags"),
            confidence=confirmed.get("confidence", 0.5),
            provenance={"kind": "confirmed-suggestion", "source": confirmed.get("source", "")},
        )
        _write_jsonl(self.pending_path, remaining)
        return node

    def reject(self, candidate_id: str) -> bool:
        pending = _read_jsonl(self.pending_path)
        remaining = [
            entry
            for entry in pending
            if not (entry.get("id") == candidate_id and entry.get("status") == "pending")
        ]
        if len(remaining) == len(pending):
            return False
        _write_jsonl(self.pending_path, remaining)
        return True
