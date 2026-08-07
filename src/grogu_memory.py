"""Repository-local intelligence schemas and deterministic incremental indexing."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional

SCHEMA_VERSION = 1
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


def _git_summary(root: Path) -> dict:
    commits = _run(root, ["log", "-8", "--format=%H%x09%aI%x09%s", "--"]).splitlines()
    recent = []
    for line in commits:
        commit, timestamp, subject = (line.split("\t", 2) + ["", "", ""])[:3]
        if commit:
            recent.append({"commit": commit, "at": timestamp, "subject": subject})
    return {
        "head": _run(root, ["rev-parse", "HEAD"]),
        "branch": _run(root, ["branch", "--show-current"]),
        "remote": _remote(root),
        "recent_commits": recent,
    }


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
            "remote": _remote(self.root),
            "created_at": existing.get("created_at", now()),
            "updated_at": now(),
            "derivation": "deterministic-grogu-index-v1",
        }
        _write_json(self.manifest_path, manifest)
        return manifest

    def index(self) -> dict:
        manifest = self.initialize()
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
            "kind": "grogu.repository_index",
            "repository_id": manifest["repository_id"],
            "indexed_at": now(),
            "git": _git_summary(self.root),
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
            "derivation": "deterministic-grogu-index-v1",
        }
        _write_json(self.index_path, summary)
        _write_json(self.cache_path, next_cache)
        manifest["updated_at"] = summary["indexed_at"]
        _write_json(self.manifest_path, manifest)
        return {"manifest": manifest, "index": summary, "changed": changed, "removed": removed}

    def status(self) -> dict:
        manifest = _read_json(self.manifest_path)
        index = _read_json(self.index_path)
        return {
            "root": str(self.root),
            "initialized": bool(manifest),
            "manifest": manifest,
            "index": index.get("summary", {}),
            "indexed_head": index.get("git", {}).get("head"),
            "current_head": _git_summary(self.root).get("head"),
        }

    def context(self, limit: int = 40) -> dict:
        manifest = _read_json(self.manifest_path)
        index = _read_json(self.index_path)
        files = list(index.get("files", {}).values())
        priority = {"instructions": 0, "manifest": 1, "configuration": 2, "test": 3, "source": 4}
        files.sort(key=lambda item: (priority.get(item.get("role"), 9), item["path"]))
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "grogu.repository_context",
            "repository": manifest,
            "git": index.get("git", {}),
            "summary": index.get("summary", {}),
            "important_files": files[: max(0, limit)],
            "derivation": "deterministic-grogu-index-v1",
        }
