"""User-scoped Copilot plugin repositories loaded by the Grogu harness."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit

import grogu_platform

SCHEMA_VERSION = 1
AGENT_PLUGIN_SCHEMA = (
    "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
)
AGENT_MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
_AGENT_FIELDS = frozenset(
    {
        "$schema",
        "name",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
        "extensions",
    }
)
_LEGACY_FIELDS = frozenset(
    {
        "name",
        "description",
        "version",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
        "category",
        "tags",
        "agents",
        "skills",
        "commands",
        "hooks",
        "extensions",
        "mcpServers",
        "lspServers",
    }
)
_LEGACY_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_AGENT_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?$")


class CapabilityError(RuntimeError):
    """A configured capability repository cannot be used safely."""


def _resource_path(root: Path, value: str, field: str, *, directory: bool) -> Path:
    raw = Path(value)
    if raw.is_absolute():
        raise CapabilityError(f"plugin field {field!r} must use a relative path")
    try:
        resolved = (root / raw).resolve(strict=True)
        resolved.relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError) as error:
        raise CapabilityError(
            f"plugin field {field!r} references unavailable path {value!r}: {error}"
        )
    if directory and not resolved.is_dir():
        raise CapabilityError(f"plugin field {field!r} must reference a directory")
    if not directory and not resolved.is_file():
        raise CapabilityError(f"plugin field {field!r} must reference a file")
    return resolved


def _path_list(root: Path, manifest: dict, field: str) -> None:
    value = manifest.get(field)
    if value is None:
        return
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or not values or any(
        not isinstance(item, str) or not item for item in values
    ):
        raise CapabilityError(
            f"plugin field {field!r} must be a path or a non-empty list of paths"
        )
    for item in values:
        _resource_path(root, item, field, directory=True)


def _remote_url(value: object, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise CapabilityError(f"MCP server {name!r} requires a URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise CapabilityError(f"MCP server {name!r} has an invalid URL")
    loopback = parsed.hostname == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme != "https" and not loopback:
        raise CapabilityError(
            f"MCP server {name!r} must use HTTPS unless it is loopback"
        )


def _string_map(value: object, label: str, *, reserved: set[str] | None = None) -> None:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise CapabilityError(f"{label} must map strings to strings")
    if reserved and reserved & set(value):
        names = ", ".join(sorted(reserved & set(value)))
        raise CapabilityError(f"{label} cannot replace reserved values: {names}")


def _portable_cwd(root: Path, value: object, name: str) -> None:
    if not isinstance(value, str):
        raise CapabilityError(f"MCP server {name!r} cwd must be a string")
    if value.startswith("./"):
        _resource_path(root, value, f"MCP server {name!r} cwd", directory=True)
        return
    for placeholder in ("${PLUGIN_ROOT}", "${PLUGIN_DATA}"):
        if value == placeholder or value.startswith(f"{placeholder}/"):
            suffix = value[len(placeholder) :].lstrip("/")
            if ".." in Path(suffix).parts:
                raise CapabilityError(
                    f"MCP server {name!r} cwd escapes {placeholder}"
                )
            if placeholder == "${PLUGIN_ROOT}" and suffix:
                _resource_path(
                    root,
                    suffix,
                    f"MCP server {name!r} cwd",
                    directory=True,
                )
            return
    raise CapabilityError(
        f"MCP server {name!r} cwd must start with ./, "
        "${PLUGIN_ROOT}, or ${PLUGIN_DATA}"
    )


def _validate_server_map(
    value: object,
    location: str,
    *,
    portable: bool,
    root: Path | None = None,
) -> None:
    if not isinstance(value, dict):
        raise CapabilityError(f"{location} must define an MCP server object")
    for name, server in value.items():
        if not isinstance(name, str) or not name or not isinstance(server, dict):
            raise CapabilityError(f"{location} contains an invalid MCP server entry")
        transport = server.get("type")
        if portable and transport not in {"stdio", "streamable-http", "sse"}:
            raise CapabilityError(
                f"MCP server {name!r} has unsupported transport {transport!r}"
            )
        remote = transport in {"http", "streamable-http", "sse"}
        if remote:
            if portable and set(server) - {"type", "url", "headers"}:
                raise CapabilityError(
                    f"MCP server {name!r} has unsupported remote fields"
                )
            _remote_url(server.get("url"), name)
            headers = server.get("headers", {})
            _string_map(headers, f"MCP server {name!r} headers")
            lowered = [header.lower() for header in headers]
            if len(lowered) != len(set(lowered)):
                raise CapabilityError(
                    f"MCP server {name!r} repeats a header name with different casing"
                )
            if any(
                not re.fullmatch(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+", header)
                or "\r" in item
                or "\n" in item
                for header, item in headers.items()
            ):
                raise CapabilityError(
                    f"MCP server {name!r} contains an invalid HTTP header"
                )
            continue
        if portable and set(server) - {"type", "command", "args", "env", "cwd"}:
            raise CapabilityError(
                f"MCP server {name!r} has unsupported stdio fields"
            )
        if not isinstance(server.get("command"), str) or not server["command"]:
            raise CapabilityError(f"MCP server {name!r} requires a command")
        command = server["command"]
        if portable:
            if command.startswith("./"):
                if root is None:
                    raise CapabilityError("portable MCP validation has no plugin root")
                _resource_path(
                    root,
                    command,
                    f"MCP server {name!r} command",
                    directory=False,
                )
            elif "/" in command or "\\" in command or Path(command).is_absolute():
                raise CapabilityError(
                    f"MCP server {name!r} command must be a bare executable "
                    "or start with ./"
                )
        args = server.get("args", [])
        if not isinstance(args, list) or any(
            not isinstance(argument, str) for argument in args
        ):
            raise CapabilityError(
                f"MCP server {name!r} args must be a list of strings"
            )
        if portable and "cwd" in server:
            if root is None:
                raise CapabilityError("portable MCP validation has no plugin root")
            _portable_cwd(root, server["cwd"], name)
        elif "cwd" in server and not isinstance(server["cwd"], str):
            raise CapabilityError(f"MCP server {name!r} cwd must be a string")
        environment = server.get("env", {})
        _string_map(
            environment,
            f"MCP server {name!r} env",
            reserved={"PLUGIN_ROOT", "PLUGIN_DATA"} if portable else None,
        )


def _validate_mcp_file(path: Path, *, portable: bool) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf8"))
    except (OSError, ValueError) as error:
        raise CapabilityError(f"cannot read MCP configuration {path}: {error}")
    if not isinstance(payload, dict):
        raise CapabilityError(f"MCP configuration {path} is not an object")
    if portable and payload.get("$schema") != AGENT_MCP_SCHEMA:
        raise CapabilityError(
            f"MCP configuration {path} must declare {AGENT_MCP_SCHEMA}"
        )
    if portable and set(payload) != {"$schema", "mcpServers"}:
        raise CapabilityError(
            f"MCP configuration {path} contains unsupported top-level fields"
        )
    servers = payload.get("mcpServers") if "mcpServers" in payload else payload
    _validate_server_map(
        servers,
        str(path),
        portable=portable,
        root=path.parent if portable else None,
    )


def _validate_metadata(manifest: dict, fields: set[str] | frozenset[str]) -> None:
    for field in fields:
        if field in manifest and not isinstance(manifest[field], str):
            raise CapabilityError(f"plugin field {field!r} must be text")
    for field in ("keywords", "tags"):
        if field in manifest and (
            not isinstance(manifest[field], list)
            or any(not isinstance(item, str) for item in manifest[field])
        ):
            raise CapabilityError(f"plugin field {field!r} must be a list of strings")


def _validate_manifest(root: Path, manifest: dict, manifest_path: Path) -> str:
    portable = manifest.get("$schema") == AGENT_PLUGIN_SCHEMA
    allowed = _AGENT_FIELDS if portable else _LEGACY_FIELDS
    unknown = sorted(set(manifest) - allowed)
    if unknown:
        raise CapabilityError(
            f"plugin manifest {manifest_path} has unsupported fields: "
            f"{', '.join(unknown)}"
        )
    name = manifest.get("name")
    pattern = _AGENT_NAME if portable else _LEGACY_NAME
    if (
        not isinstance(name, str)
        or not pattern.fullmatch(name)
        or len(name) > 64
        or (portable and (".." in name or "--" in name))
    ):
        raise CapabilityError(f"plugin manifest {manifest_path} has an invalid name")
    _validate_metadata(
        manifest,
        {"version", "description", "homepage", "repository", "license", "category"},
    )
    author = manifest.get("author")
    if author is not None:
        if not isinstance(author, dict) or set(author) - {"name", "email", "url"}:
            raise CapabilityError(f"plugin field 'author' must be an author object")
        _validate_metadata(author, {"name", "email", "url"})
        if not portable and "name" not in author:
            raise CapabilityError("legacy plugin field 'author' requires a name")
    if portable:
        if "extensions" in manifest and (
            not isinstance(manifest["extensions"], dict)
            or any(
                not isinstance(value, dict)
                for value in manifest["extensions"].values()
            )
        ):
            raise CapabilityError(
                "plugin field 'extensions' must map namespaces to objects"
            )
        skills = root / "skills"
        if skills.exists() or skills.is_symlink():
            try:
                resolved_skills = skills.resolve(strict=True)
                resolved_skills.relative_to(root.resolve())
            except (OSError, RuntimeError, ValueError) as error:
                raise CapabilityError(
                    f"portable plugin skills escape the plugin root: {error}"
                )
            if not resolved_skills.is_dir():
                raise CapabilityError("portable plugin skills must be a directory")
            for child in resolved_skills.iterdir():
                skill = child / "SKILL.md"
                if not skill.exists() and not skill.is_symlink():
                    continue
                try:
                    skill.resolve(strict=True).relative_to(root.resolve())
                except (OSError, RuntimeError, ValueError) as error:
                    raise CapabilityError(
                        f"portable plugin skill {skill} escapes the plugin root: {error}"
                    )
        mcp = root / "mcp.json"
        if mcp.exists() or mcp.is_symlink():
            try:
                resolved_mcp = mcp.resolve(strict=True)
                resolved_mcp.relative_to(root.resolve())
            except (OSError, RuntimeError, ValueError) as error:
                raise CapabilityError(
                    f"portable plugin mcp.json escapes the plugin root: {error}"
                )
            if not resolved_mcp.is_file():
                raise CapabilityError("portable plugin mcp.json must be a file")
            _validate_mcp_file(resolved_mcp, portable=True)
        return name

    for field in ("agents", "skills", "commands"):
        _path_list(root, manifest, field)
    extensions = manifest.get("extensions")
    if isinstance(extensions, (str, list)):
        _path_list(root, manifest, "extensions")
    elif extensions is not None and not isinstance(extensions, dict):
        raise CapabilityError(
            "plugin field 'extensions' must be paths or an object"
        )
    hooks = manifest.get("hooks")
    if isinstance(hooks, str):
        _resource_path(root, hooks, "hooks", directory=False)
    elif hooks is not None and not isinstance(hooks, dict):
        raise CapabilityError("plugin field 'hooks' must be a path or an object")
    for field in ("mcpServers", "lspServers"):
        value = manifest.get(field)
        if isinstance(value, str):
            path = _resource_path(root, value, field, directory=False)
            if field == "mcpServers":
                _validate_mcp_file(path, portable=False)
        elif value is not None and not isinstance(value, dict):
            raise CapabilityError(f"plugin field {field!r} must be a path or an object")
        elif field == "mcpServers" and isinstance(value, dict):
            servers = value.get("mcpServers", value)
            _validate_server_map(
                servers,
                "plugin field 'mcpServers'",
                portable=False,
            )
    return name


class CapabilityStore:
    def __init__(self, home: Path):
        self.home = Path(home).expanduser()
        self.path = self.home / "capabilities.json"
        self.lock_path = self.home / "capabilities.lock"

    @contextmanager
    def _lock(self) -> Iterator[None]:
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as error:
            raise CapabilityError(
                f"cannot open capability repository lock {self.lock_path}: {error}"
            )
        try:
            try:
                with grogu_platform.exclusive_lock(descriptor):
                    yield
            except OSError as error:
                raise CapabilityError(
                    f"capability repository store operation failed: {error}"
                )
        finally:
            os.close(descriptor)

    def _read(self) -> dict:
        if not self.path.exists():
            return {"schema_version": SCHEMA_VERSION, "repositories": []}
        try:
            payload = json.loads(self.path.read_text(encoding="utf8"))
        except OSError as error:
            raise CapabilityError(
                f"cannot read the capability repository store at {self.path}: {error}"
            )
        except ValueError:
            raise CapabilityError(
                f"the capability repository store at {self.path} is not valid JSON"
            )
        if not isinstance(payload, dict):
            raise CapabilityError(
                f"the capability repository store at {self.path} is not an object"
            )
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise CapabilityError(
                f"the capability repository store at {self.path} has unsupported "
                f"schema version {payload.get('schema_version')!r}"
            )
        repositories = payload.get("repositories")
        if not isinstance(repositories, list) or any(
            not isinstance(path, str) or not path.strip() for path in repositories
        ):
            raise CapabilityError(
                f"the capability repository store at {self.path} must contain a "
                "list of repository paths"
            )
        if any(not Path(path).is_absolute() for path in repositories):
            raise CapabilityError(
                f"the capability repository store at {self.path} contains a "
                "relative path; repository paths must be canonical and absolute"
            )
        if len(repositories) != len(set(repositories)):
            raise CapabilityError(
                f"the capability repository store at {self.path} contains "
                "duplicate repository paths"
            )
        return payload

    def _write(self, payload: dict) -> None:
        temporary = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        descriptor = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf8") as handle:
                descriptor = None
                json.dump(payload, handle, indent=2)
                handle.write("\n")
            os.replace(temporary, self.path)
        except (OSError, TypeError, ValueError) as error:
            if descriptor is not None:
                os.close(descriptor)
            cleanup = ""
            try:
                temporary.unlink(missing_ok=True)
            except OSError as cleanup_error:
                cleanup = f"; temporary file cleanup also failed: {cleanup_error}"
            raise CapabilityError(
                f"cannot write capability repository store {self.path}: "
                f"{error}{cleanup}"
            )

    @staticmethod
    def _canonical(path: str, *, must_exist: bool) -> Path:
        candidate = Path(path).expanduser()
        try:
            return candidate.resolve(strict=must_exist)
        except (OSError, RuntimeError, ValueError) as error:
            raise CapabilityError(
                f"capability repository {str(candidate)!r} is unavailable: {error}"
            )

    @staticmethod
    def inspect(path: Path) -> dict:
        if not path.is_dir():
            raise CapabilityError(
                f"capability repository {path} is not a readable directory"
            )
        manifest_path = path / "plugin.json"
        try:
            resolved_manifest = manifest_path.resolve(strict=True)
            resolved_manifest.relative_to(path.resolve())
            manifest = json.loads(resolved_manifest.read_text(encoding="utf8"))
        except FileNotFoundError:
            raise CapabilityError(
                f"capability repository {path} has no plugin.json; each configured "
                "repository must be a Copilot plugin"
            )
        except ValueError as error:
            raise CapabilityError(
                f"capability repository manifest {manifest_path} escapes the "
                f"repository or is not valid JSON: {error}"
            )
        except RuntimeError as error:
            raise CapabilityError(
                f"cannot resolve capability repository manifest {manifest_path}: "
                f"{error}"
            )
        except OSError as error:
            raise CapabilityError(
                f"cannot read capability repository manifest {manifest_path}: {error}"
            )
        if not isinstance(manifest, dict):
            raise CapabilityError(
                f"capability repository manifest {manifest_path} is not an object"
            )
        name = _validate_manifest(path, manifest, manifest_path)
        return {"name": name, "path": str(path), "manifest": str(manifest_path)}

    def list(self) -> list[dict]:
        repositories = []
        for stored in self._read()["repositories"]:
            path = self._canonical(stored, must_exist=True)
            repositories.append(self.inspect(path))
        return repositories

    def add(self, value: str) -> dict:
        path = self._canonical(value, must_exist=True)
        repository = self.inspect(path)
        with self._lock():
            payload = self._read()
            stored = str(path)
            if stored not in payload["repositories"]:
                payload["repositories"].append(stored)
                self._write(payload)
        return repository

    def remove(self, value: str) -> dict:
        path = str(self._canonical(value, must_exist=False))
        with self._lock():
            payload = self._read()
            if path not in payload["repositories"]:
                raise CapabilityError(
                    f"capability repository {path} is not configured"
                )
            payload["repositories"].remove(path)
            self._write(payload)
        return {"path": path, "removed": True}

    def plugin_paths(self) -> list[Path]:
        return [Path(repository["path"]) for repository in self.list()]
