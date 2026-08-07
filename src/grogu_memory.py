"""Repository-local knowledge graph and deterministic inventory indexing."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional

SCHEMA_VERSION = 2
INTELLIGENCE_DIRNAME = ".grogu/intelligence"
MAX_INDEXABLE_BYTES = 2 * 1024 * 1024
SKIP_DIRS = {
    ".git",
    ".grogu/state",
    ".grogu/intelligence",
    ".venv",
    "node_modules",
    "dist",
    "build",
    "coverage",
    "__pycache__",
}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _run(root: Path, arguments: List[str]) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def repository_root(start: Optional[Path] = None) -> Path:
    path = Path(start or Path.cwd()).expanduser().resolve()
    detected = _run(path, ["rev-parse", "--show-toplevel"])
    return Path(detected).resolve() if detected else path


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


def _remote(root: Path) -> str:
    value = _run(root, ["config", "--get", "remote.origin.url"])
    if value.startswith("git@") and ":" in value:
        host, path = value.split(":", 1)
        value = f"https://{host[4:]}/{path}"
    if value.endswith(".git"):
        value = value[:-4]
    return value


def _repository_id(root: Path) -> str:
    remote = _remote(root)
    stable = remote or f"local:{root.name}"
    return hashlib.sha256(stable.encode("utf8")).hexdigest()[:20]


def _tracked_files(root: Path) -> Iterable[str]:
    output = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
        capture_output=True,
        check=False,
    )
    if output.returncode == 0:
        for path in output.stdout.decode("utf8", "surrogateescape").split("\0"):
            if path:
                yield path
        return
    for path in root.rglob("*"):
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            if not any(relative == skip or relative.startswith(skip + "/") for skip in SKIP_DIRS):
                yield relative


def _role(path: str) -> str:
    name = Path(path).name.lower()
    parts = set(Path(path).parts)
    if name in {"readme", "readme.md", "agents.md", "contributing.md"} or name.endswith(
        ".instructions.md"
    ):
        return "instructions"
    if name in {
        "package.json",
        "pyproject.toml",
        "go.mod",
        "cargo.toml",
        "pom.xml",
        "build.gradle",
        "dockerfile",
    }:
        return "manifest"
    if any(part.lower() in {"test", "tests", "__tests__", "spec", "specs"} for part in parts):
        return "test"
    if name.endswith((".yml", ".yaml", ".json", ".toml")):
        return "configuration"
    return "source"


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class MemoryStore:
    """Persistent intelligence for the repository being worked on.

    This store is intentionally created in the target repository, never in the
    Grogu source checkout. Only deterministic metadata is indexed automatically;
    model-derived insights can be appended later with explicit provenance.
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = repository_root(root)
        self.directory = self.root / INTELLIGENCE_DIRNAME
        self.manifest_path = self.directory / "manifest.json"
        self.index_path = self.directory / "index.json"
        self.inventory_path = self.directory / "inventory.json"
        self.insights_path = self.directory / "insights.jsonl"
        self.cache_path = self.root / ".grogu/state/memory-cache.json"

    def initialize(self, name: Optional[str] = None) -> dict:
        self.directory.mkdir(parents=True, exist_ok=True)
        existing = _read_json(self.manifest_path)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "kind": "grogu.repository_intelligence",
            "repository_id": existing.get("repository_id", _repository_id(self.root)),
            "name": name or existing.get("name") or self.root.name,
            "created_at": existing.get("created_at", now()),
            "updated_at": now(),
            "derivation": "grogu-knowledge-graph-v2",
        }
        _write_json(self.manifest_path, manifest)
        return manifest

    def index(self) -> dict:
        manifest = self.initialize()
        previous = _read_json(self.inventory_path)
        if not previous:
            # Migrate the first-generation file inventory without treating the
            # old Git metadata as repository knowledge.
            previous = _read_json(self.index_path)
        previous_files = previous.get("files", {})
        cache = _read_json(self.cache_path)
        files: Dict[str, dict] = {}
        next_cache: Dict[str, dict] = {}
        changed = []
        for relative in sorted(_tracked_files(self.root)):
            if any(relative == skip or relative.startswith(skip + "/") for skip in SKIP_DIRS):
                continue
            path = self.root / relative
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size > MAX_INDEXABLE_BYTES:
                continue
            old = previous_files.get(relative, {})
            cached = cache.get(relative, {})
            if (
                old
                and cached.get("size") == stat.st_size
                and cached.get("mtime_ns") == stat.st_mtime_ns
            ):
                files[relative] = old
                next_cache[relative] = {
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
                continue
            try:
                digest = _digest(path)
            except OSError:
                continue
            files[relative] = {
                "path": relative,
                "sha256": digest,
                "size": stat.st_size,
                "role": _role(relative),
            }
            next_cache[relative] = {
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
            if old.get("sha256") != digest:
                changed.append(relative)
        removed = sorted(set(previous_files) - set(files))
        summary = {
            "schema_version": SCHEMA_VERSION,
            "kind": "grogu.repository_inventory",
            "repository_id": manifest["repository_id"],
            "indexed_at": now(),
            "files": files,
            "summary": {
                "file_count": len(files),
                "changed": len(changed),
                "removed": len(removed),
                "by_role": {
                    role: sum(1 for item in files.values() if item["role"] == role)
                    for role in ("instructions", "manifest", "configuration", "test", "source")
                },
            },
            "derivation": "grogu-inventory-v2",
        }
        _write_json(self.inventory_path, summary)
        _write_json(self.cache_path, next_cache)
        graph = self._load_graph()
        indexed_file_nodes = set()
        for relative, item in files.items():
            if item["role"] in {"instructions", "manifest", "configuration", "test"}:
                node_id = f"file:{relative}"
                indexed_file_nodes.add(node_id)
                node = graph["nodes"].get(node_id, {})
                graph["nodes"][node_id] = {
                    "id": node_id,
                    "type": "file",
                    "name": relative,
                    "summary": node.get("summary", f"{item['role']} file: {relative}"),
                    "paths": [relative],
                    "tags": sorted(set(node.get("tags", []) + [item["role"]])),
                    "confidence": node.get("confidence", 0.6),
                    "provenance": node.get("provenance", [{"kind": "deterministic-index"}]),
                    "updated_at": node.get("updated_at", now()),
                }
        task_dir = self.root / ".grogu/tasks"
        indexed_work_nodes = set()
        for task_path in sorted(task_dir.glob("*.json")) if task_dir.is_dir() else []:
            task = _read_json(task_path)
            task_id = task.get("id")
            if not task_id:
                continue
            node_id = f"work:{task_id}"
            indexed_work_nodes.add(node_id)
            labels = task.get("labels", [])
            graph["nodes"][node_id] = {
                "id": node_id,
                "type": "work",
                "name": task_id,
                "summary": " — ".join(
                    item for item in (task.get("title", ""), task.get("body", "")) if item
                ),
                "paths": [],
                "tags": sorted(set(labels + [task.get("status", "open")])),
                "confidence": 0.8 if task.get("status") in {"done", "review"} else 0.6,
                "provenance": [{"kind": "task-record", "task_id": task_id}],
                "updated_at": task.get("updated_at", now()),
            }
        for node_id in list(graph["nodes"]):
            if node_id.startswith("work:") and node_id not in indexed_work_nodes:
                del graph["nodes"][node_id]
        for node_id in list(graph["nodes"]):
            if node_id.startswith("file:") and node_id not in indexed_file_nodes:
                del graph["nodes"][node_id]
        graph["edges"] = [
            edge
            for edge in graph["edges"]
            if edge["source"] in graph["nodes"] and edge["target"] in graph["nodes"]
        ]
        graph["updated_at"] = now()
        _write_json(self.index_path, graph)
        manifest["updated_at"] = summary["indexed_at"]
        _write_json(self.manifest_path, manifest)
        return {
            "manifest": manifest,
            "index": summary,
            "graph": graph,
            "changed": changed,
            "removed": removed,
        }

    def status(self) -> dict:
        manifest = _read_json(self.manifest_path)
        inventory = _read_json(self.inventory_path)
        graph = self._load_graph()
        return {
            "root": str(self.root),
            "initialized": bool(manifest),
            "manifest": manifest,
            "inventory": inventory.get("summary", {}),
            "graph": {
                "nodes": len(graph["nodes"]),
                "edges": len(graph["edges"]),
            },
        }

    def _load_graph(self) -> dict:
        graph = _read_json(self.index_path)
        if not graph or graph.get("kind") != "grogu.knowledge_graph":
            return {
                "schema_version": SCHEMA_VERSION,
                "kind": "grogu.knowledge_graph",
                "repository_id": _repository_id(self.root),
                "nodes": {},
                "edges": [],
                "updated_at": now(),
                "derivation": "grogu-knowledge-graph-v2",
            }
        graph.setdefault("nodes", {})
        graph.setdefault("edges", [])
        return graph

    def remember(
        self,
        node_type: str,
        name: str,
        summary: str,
        paths: Optional[Iterable[str]] = None,
        tags: Optional[Iterable[str]] = None,
        confidence: float = 0.8,
        provenance: Optional[dict] = None,
    ) -> dict:
        if not node_type.strip() or not name.strip() or not summary.strip():
            raise ValueError("node type, name, and summary are required")
        graph = self._load_graph()
        node_id = f"{node_type.strip()}:{name.strip()}"
        node = {
            "id": node_id,
            "type": node_type.strip(),
            "name": name.strip(),
            "summary": summary.strip(),
            "paths": sorted(set(paths or [])),
            "tags": sorted(set(tags or [])),
            "confidence": max(0.0, min(1.0, confidence)),
            "provenance": [provenance or {"kind": "user"}],
            "updated_at": now(),
        }
        graph["nodes"][node_id] = node
        graph["updated_at"] = node["updated_at"]
        _write_json(self.index_path, graph)
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
        _write_json(self.index_path, graph)
        return edge

    def context(
        self,
        limit: int = 40,
        query: str = "",
        node_id: str = "",
        depth: int = 1,
    ) -> dict:
        manifest = _read_json(self.manifest_path)
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
            "kind": "grogu.repository_context",
            "repository_id": manifest.get("repository_id", ""),
            "repository": manifest,
            "nodes": chosen[: max(0, limit)],
            "edges": [
                edge
                for edge in graph["edges"]
                if edge["source"] in selected and edge["target"] in selected
            ][: max(0, limit * 2)],
            "derivation": "grogu-context-traversal-v2",
        }
