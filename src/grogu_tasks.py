"""Repository-backed task tracking for concurrent Grogu sessions.

Two stores with different lifetimes live side by side:

* `<repo>/.grogu/tasks/<id>.json` is the durable, shared record. One file per
  task keeps Git merges trivial and makes the store reviewable in a pull
  request, so several people can track the same work.
* `<repo>/.grogu/state/` holds volatile per-machine state: the leases that stop
  two concurrent sessions from claiming the same task, and the inbox used to
  hand a late user update to a running session. It ignores itself in Git.

Every mutation happens under an exclusive lock on the state directory and is
written atomically, so concurrent Grogu sessions cannot interleave a
read-modify-write.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import socket
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Optional

import grogu_platform

SCHEMA_VERSION = 2
STORE_DIRNAME = ".grogu"
DEFAULT_LEASE_SECONDS = 1800

OPEN = "open"
ACTIVE = "active"
BLOCKED = "blocked"
REVIEW = "review"
DONE = "done"
CANCELLED = "cancelled"
STATUSES = (OPEN, ACTIVE, BLOCKED, REVIEW, DONE, CANCELLED)
CLOSED_STATUSES = frozenset({DONE, CANCELLED})


class TaskError(Exception):
    """A task operation that the caller asked for cannot be performed."""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _parse(timestamp: str) -> dt.datetime:
    return dt.datetime.fromisoformat(timestamp)


def actor() -> str:
    """Identity recorded on task events and leases.

    Deliberately not `user@hostname`. Plan manifests and task files are
    committed and published in pull requests, and a machine hostname is a
    piece of personal information that is nowhere else in the repository --
    it is not in the commit metadata, so recording it here is the only reason
    it would ever be public. The login name alone distinguishes actors on a
    shared checkout, which is all this is for.
    """
    explicit = os.environ.get("GROGU_ACTOR")
    if explicit:
        return explicit
    return os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"


def agent() -> str:
    """Stable Grogu agent name for provenance, when one is available."""
    return os.environ.get("GROGU_AGENT", "").strip()


def session_id() -> str:
    """Best-effort identifier for the Grogu session holding a lease."""
    return os.environ.get("GROGU_SESSION_ID", "")


def session_pid() -> int:
    """PID of the Grogu launcher that owns this session, `0` when unknown.

    `grogu task ...` processes are short lived, so their own PID says nothing
    about whether the work is still in progress. `grogu` exports its launcher
    PID into the Copilot environment, which gives leases taken inside a session
    a lifetime that ends when that session ends.
    """
    raw = os.environ.get("GROGU_SESSION_PID", "")
    return int(raw) if raw.isdigit() else 0


def lease_holder() -> dict:
    return {
        "owner": actor(),
        "agent": agent(),
        "host": socket.gethostname(),
        "session_pid": session_pid(),
        "session_id": session_id(),
    }


def _coerce_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def repository_root(start: Optional[Path] = None) -> Path:
    """The Git work tree containing `start`, or `start` itself."""
    start = Path(start or Path.cwd()).expanduser().resolve()
    try:
        completed = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return start
    if completed.returncode == 0 and completed.stdout.strip():
        return Path(completed.stdout.strip())
    return start


def primary_worktree(root: Path) -> Path:
    """The main working tree of `root`'s repository, or `root` itself.

    Repository task and plan state must be shared by every linked worktree.
    `--git-common-dir` points at the primary checkout's `.git` directory for
    both a plain clone and its linked worktrees.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return root
    if result.returncode != 0:
        return root
    common = result.stdout.strip()
    if not common:
        return root
    candidate = Path(common).parent
    try:
        return candidate.resolve() if candidate.is_dir() else root
    except OSError:
        return root


class TaskStore:
    """Task records, leases and inbox messages for one repository."""

    def __init__(self, root: Optional[Path] = None) -> None:
        requested_root = Path(root or repository_root()).expanduser().resolve()
        self.root = primary_worktree(requested_root)
        self.store = self.root / STORE_DIRNAME
        self.tasks_dir = self.store / "tasks"
        self.state_dir = self.store / "state"
        self.leases_dir = self.state_dir / "leases"
        self.inbox_dir = self.state_dir / "inbox"

    # -- storage -----------------------------------------------------------

    def _ensure_state(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        marker = self.state_dir / ".gitignore"
        if not marker.exists():
            # Volatile state must never reach a commit, in this repository or
            # in any other repository Grogu is pointed at.
            marker.write_text("*\n", encoding="utf8")

    @contextmanager
    def locked(self) -> Iterator[None]:
        self._ensure_state()
        handle = os.open(self.state_dir / "store.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with grogu_platform.exclusive_lock(handle):
                yield
        finally:
            os.close(handle)

    def _write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf8")
        os.replace(temporary, path)

    def _read_json(self, path: Path) -> dict:
        try:
            value = json.loads(path.read_text(encoding="utf8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _normalize_task(self, task: dict) -> dict:
        if not task:
            return {}
        normalized = dict(task)
        normalized["schema_version"] = max(
            _coerce_int(normalized.get("schema_version"), 1), SCHEMA_VERSION
        )
        created_owner = (
            normalized.get("created_by_owner")
            or normalized.get("created_by")
            or actor()
        )
        normalized["created_by"] = normalized.get("created_by") or created_owner
        normalized["created_by_owner"] = created_owner
        normalized["created_by_agent"] = normalized.get("created_by_agent") or ""
        normalized["created_by_session_id"] = normalized.get("created_by_session_id") or ""
        normalized["created_by_session_pid"] = _coerce_int(
            normalized.get("created_by_session_pid"), 0
        )
        normalized.pop("created_by_host", None)
        normalized["assignee_owner"] = (
            normalized.get("assignee_owner")
            if normalized.get("assignee_owner") is not None
            else normalized.get("assignee") or ""
        )
        normalized["assignee_agent"] = normalized.get("assignee_agent") or ""
        normalized["assignee_session_id"] = normalized.get("assignee_session_id") or ""
        normalized["assignee_session_pid"] = _coerce_int(
            normalized.get("assignee_session_pid"), 0
        )
        normalized.pop("assignee_host", None)
        if "parent_task_id" not in normalized:
            normalized["parent_task_id"] = normalized.get("parent_id") or normalized.get("parent")
        log = []
        for entry in normalized.get("log", []) or []:
            if not isinstance(entry, dict):
                continue
            migrated = dict(entry)
            migrated["by"] = migrated.get("by") or migrated.get("actor") or normalized["created_by"]
            migrated["agent"] = migrated.get("agent") or ""
            migrated["session_id"] = migrated.get("session_id") or ""
            migrated["session_pid"] = _coerce_int(migrated.get("session_pid"), 0)
            migrated["host"] = migrated.get("host") or ""
            log.append(migrated)
        normalized["log"] = log
        return normalized

    def _identity_fields(self, task: dict, prefix: str) -> dict:
        return {
            "owner": task.get(prefix) or "",
            "agent": task.get(f"{prefix}_agent") or "",
            "session_id": task.get(f"{prefix}_session_id") or "",
            "session_pid": _coerce_int(task.get(f"{prefix}_session_pid"), 0),
        }

    def _lease_identity(self, lease: dict) -> dict:
        if not lease:
            return {}
        return {
            "owner": lease.get("owner") or "",
            "agent": lease.get("agent") or "",
            "session_id": lease.get("session_id") or "",
            "session_pid": _coerce_int(lease.get("session_pid"), 0),
            "host": lease.get("host") or "",
        }

    def _set_identity_fields(self, task: dict, prefix: str, identity: dict) -> None:
        owner = identity.get("owner") or ""
        if prefix == "created_by":
            task[prefix] = owner or actor()
        else:
            task[prefix] = owner or None
        task[f"{prefix}_owner"] = owner
        task[f"{prefix}_agent"] = identity.get("agent") or ""
        task[f"{prefix}_session_id"] = identity.get("session_id") or ""
        task[f"{prefix}_session_pid"] = _coerce_int(identity.get("session_pid"), 0)

    def _clear_assignment_fields(self, task: dict) -> None:
        task["assignee"] = None
        task["assignee_owner"] = ""
        task["assignee_agent"] = ""
        task["assignee_session_id"] = ""
        task["assignee_session_pid"] = 0

    def _write_task(self, task: dict) -> None:
        self._write_json(self.task_path(task["id"]), self._normalize_task(task))

    def _is_grogu_owned(self, task: dict) -> bool:
        return bool(task.get("created_by_agent"))

    def _cleanup_subordinates(self, parent_id: str) -> list:
        cleaned = []
        for child in self.list_tasks():
            if child["id"] == parent_id:
                continue
            if child.get("parent_task_id") != parent_id:
                continue
            if not self._is_grogu_owned(child):
                continue
            lease = self.lease(child["id"])
            if lease and self.lease_is_live(lease):
                continue
            child = self.load(child["id"])
            if child["status"] in CLOSED_STATUSES:
                continue
            self.lease_path(child["id"]).unlink(missing_ok=True)
            child["status"] = CANCELLED
            self._clear_assignment_fields(child)
            self._log(child, "cleanup", f"parent {parent_id}")
            self._write_task(child)
            cleaned.append(child["id"])
        return cleaned

    def grouped_tasks(self) -> dict:
        groups = {}
        tasks = self.list_tasks()
        for task in tasks:
            key = (
                task.get("assignee_owner") or task.get("assignee") or "",
                task.get("assignee_agent") or "",
                task.get("assignee_session_id") or "",
                _coerce_int(task.get("assignee_session_pid"), 0),
            )
            group = groups.setdefault(
                key,
                {
                    "assignee": self._identity_fields(task, "assignee"),
                    "by_status": {status: 0 for status in STATUSES},
                    "tasks": [],
                },
            )
            group["by_status"][task.get("status", OPEN)] = (
                group["by_status"].get(task.get("status", OPEN), 0) + 1
            )
            group["tasks"].append(
                {
                    "id": task["id"],
                    "status": task.get("status", OPEN),
                    "title": task.get("title", ""),
                    "updated_at": task.get("updated_at"),
                }
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "total": len(tasks),
            "groups": [
                groups[key]
                for key in sorted(groups, key=lambda item: (item[1], item[2], item[3], item[0]))
            ],
        }

    def task_path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.json"

    def lease_path(self, task_id: str) -> Path:
        return self.leases_dir / f"{task_id}.json"

    def inbox_path(self, task_id: str) -> Path:
        return self.inbox_dir / f"{task_id}.jsonl"

    # -- identifiers -------------------------------------------------------

    def _new_id(self) -> str:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
        while True:
            candidate = f"t-{stamp}-{secrets.token_hex(3)}"
            if not self.task_path(candidate).exists():
                return candidate

    def resolve(self, reference: str) -> str:
        """Accept a full id, an unambiguous id prefix, or `#<issue>`."""
        if reference.startswith("#") and reference[1:].isdigit():
            issue = int(reference[1:])
            matches = [
                task["id"] for task in self.list_tasks() if task.get("issue") == issue
            ]
        elif self.task_path(reference).exists():
            return reference
        else:
            matches = [
                task["id"] for task in self.list_tasks() if task["id"].startswith(reference)
            ]
        if not matches:
            raise TaskError(f"no task matches {reference!r}")
        if len(matches) > 1:
            raise TaskError(f"{reference!r} matches {len(matches)} tasks: {', '.join(sorted(matches))}")
        return matches[0]

    # -- reads -------------------------------------------------------------

    def load(self, task_id: str) -> dict:
        task = self._normalize_task(self._read_json(self.task_path(task_id)))
        if not task:
            raise TaskError(f"task {task_id} was not found")
        return task

    def list_tasks(self) -> list:
        if not self.tasks_dir.is_dir():
            return []
        tasks = [
            self._normalize_task(self._read_json(path))
            for path in sorted(self.tasks_dir.glob("*.json"))
        ]
        return [task for task in tasks if task.get("id")]

    def lease(self, task_id: str) -> dict:
        return self._read_json(self.lease_path(task_id))

    def lease_is_live(self, lease: dict, moment: Optional[dt.datetime] = None) -> bool:
        if not lease:
            return False
        moment = moment or dt.datetime.now(dt.timezone.utc)
        try:
            expires = _parse(lease["expires_at"])
        except (KeyError, ValueError):
            return False
        if expires <= moment:
            return False
        holder = int(lease.get("session_pid", 0))
        if holder and lease.get("host") == socket.gethostname():
            # On this machine liveness is knowable, so a session that died
            # releases its leases immediately instead of after the TTL.
            return _process_alive(holder)
        return True

    def lease_is_mine(self, lease: dict) -> bool:
        holder = lease_holder()
        if not lease:
            return False
        for key in ("owner", "host", "session_pid"):
            if lease.get(key) != holder[key]:
                return False
        for key in ("agent", "session_id"):
            if lease.get(key) and holder.get(key) and lease.get(key) != holder.get(key):
                return False
        return True

    def view(self, task_id: str) -> dict:
        task = dict(self.load(task_id))
        lease = self.lease(task_id)
        task["lease"] = lease or None
        task["lease_live"] = self.lease_is_live(lease)
        task["ownership"] = {
            "created_by": self._identity_fields(task, "created_by"),
            "assignee": self._identity_fields(task, "assignee"),
            "lease": self._lease_identity(lease) if lease else None,
        }
        return task

    # -- writes ------------------------------------------------------------

    def _log(self, task: dict, event: str, text: str = "") -> None:
        entry = {"at": now(), "by": actor(), "event": event}
        entry["agent"] = agent()
        entry["session_id"] = session_id()
        entry["session_pid"] = session_pid()
        if text:
            entry["text"] = text
        task.setdefault("log", []).append(entry)
        task["updated_at"] = entry["at"]
        task["revision"] = int(task.get("revision", 0)) + 1

    def create(
        self,
        title: str,
        body: str = "",
        labels: Optional[Iterable[str]] = None,
        priority: str = "normal",
        issue: Optional[int] = None,
        parent_task_id: Optional[str] = None,
    ) -> dict:
        if not title.strip():
            raise TaskError("a task needs a title")
        with self.locked():
            task_id = self._new_id()
            if parent_task_id and not self.task_path(parent_task_id).exists():
                raise TaskError(f"parent task {parent_task_id} was not found")
            identity = lease_holder()
            timestamp = now()
            task = {
                "schema_version": SCHEMA_VERSION,
                "id": task_id,
                "title": title.strip(),
                "body": body,
                "status": OPEN,
                "priority": priority,
                "labels": sorted(set(labels or [])),
                "issue": issue,
                "assignee": None,
                "created_at": timestamp,
                "created_by": actor(),
                "created_by_owner": identity["owner"],
                "created_by_agent": identity["agent"],
                "created_by_session_id": identity["session_id"],
                "created_by_session_pid": identity["session_pid"],
                "parent_task_id": parent_task_id,
                "updated_at": timestamp,
                "revision": 0,
                "log": [],
            }
            self._log(task, "created", title.strip())
            self._write_task(task)
            return task

    def update(
        self,
        task_id: str,
        status: Optional[str] = None,
        title: Optional[str] = None,
        body: Optional[str] = None,
        note: Optional[str] = None,
        issue: Optional[int] = None,
        priority: Optional[str] = None,
        labels: Optional[Iterable[str]] = None,
    ) -> dict:
        if status is not None and status not in STATUSES:
            raise TaskError(f"unknown status {status!r}; expected one of {', '.join(STATUSES)}")
        with self.locked():
            task = self.load(task_id)
            if title is not None:
                task["title"] = title.strip()
                self._log(task, "title", task["title"])
            if body is not None:
                task["body"] = body
                self._log(task, "body")
            if issue is not None:
                task["issue"] = issue
                self._log(task, "issue", f"#{issue}")
            if priority is not None:
                task["priority"] = priority
                self._log(task, "priority", priority)
            if labels is not None:
                task["labels"] = sorted(set(labels))
                self._log(task, "labels", ", ".join(task["labels"]))
            if status is not None:
                task["status"] = status
                self._log(task, "status", status)
                if status in CLOSED_STATUSES:
                    self.lease_path(task_id).unlink(missing_ok=True)
                    self._cleanup_subordinates(task_id)
            if note:
                self._log(task, "note", note)
            self._write_task(task)
            return task

    def claim(
        self,
        task_id: str,
        ttl: int = DEFAULT_LEASE_SECONDS,
        force: bool = False,
    ) -> dict:
        with self.locked():
            task = self.load(task_id)
            if task["status"] in CLOSED_STATUSES:
                raise TaskError(f"task {task_id} is {task['status']}")
            existing = self.lease(task_id)
            mine = self.lease_is_mine(existing)
            if existing and self.lease_is_live(existing) and not mine and not force:
                raise TaskError(
                    f"task {task_id} is leased by {existing.get('owner')} until "
                    f"{existing.get('expires_at')}; use --force to take it over"
                )
            moment = dt.datetime.now(dt.timezone.utc)
            stamp = moment.replace(microsecond=0).isoformat()
            lease = dict(lease_holder())
            lease.update(
                {
                    "task_id": task_id,
                    "claimed_at": existing.get("claimed_at", stamp) if mine else stamp,
                    "renewed_at": stamp,
                    "expires_at": (moment + dt.timedelta(seconds=ttl))
                    .replace(microsecond=0)
                    .isoformat(),
                    "ttl_seconds": ttl,
                }
            )
            self._write_json(self.lease_path(task_id), lease)
            if existing and not mine:
                self._log(task, "claim", f"taken over from {existing.get('owner')}")
            else:
                self._log(task, "claim")
            self._set_identity_fields(task, "assignee", lease)
            if task["status"] == OPEN:
                task["status"] = ACTIVE
            self._write_task(task)
            task["lease"] = lease
            return task

    def heartbeat(self, task_id: str, ttl: Optional[int] = None) -> dict:
        with self.locked():
            lease = self.lease(task_id)
            if not lease:
                raise TaskError(f"task {task_id} has no lease to renew")
            if not self.lease_is_mine(lease):
                raise TaskError(
                    f"task {task_id} is leased by {lease.get('owner')} "
                    f"(session {lease.get('session_pid')})"
                )
            seconds = ttl or int(lease.get("ttl_seconds", DEFAULT_LEASE_SECONDS))
            moment = dt.datetime.now(dt.timezone.utc)
            lease["renewed_at"] = moment.replace(microsecond=0).isoformat()
            lease["expires_at"] = (
                (moment + dt.timedelta(seconds=seconds)).replace(microsecond=0).isoformat()
            )
            lease["ttl_seconds"] = seconds
            self._write_json(self.lease_path(task_id), lease)
            return lease

    def release(self, task_id: str, status: Optional[str] = None, note: str = "") -> dict:
        if status is not None and status not in STATUSES:
            raise TaskError(f"unknown status {status!r}; expected one of {', '.join(STATUSES)}")
        with self.locked():
            task = self.load(task_id)
            lease = self.lease(task_id)
            if lease and not self.lease_is_mine(lease) and self.lease_is_live(lease):
                raise TaskError(
                    f"task {task_id} is leased by {lease.get('owner')}; release it there"
                )
            self.lease_path(task_id).unlink(missing_ok=True)
            if status is not None:
                task["status"] = status
            elif task["status"] == ACTIVE:
                task["status"] = OPEN
            if task["status"] == OPEN:
                self._clear_assignment_fields(task)
            self._log(task, "release", note or task["status"])
            if task["status"] in CLOSED_STATUSES:
                self.lease_path(task_id).unlink(missing_ok=True)
                self._cleanup_subordinates(task_id)
            self._write_task(task)
            return task

    def collect_expired(self) -> list:
        """Drop leases whose holder is gone; returns the released task ids."""
        released = []
        if not self.leases_dir.is_dir():
            return released
        with self.locked():
            for path in sorted(self.leases_dir.glob("*.json")):
                lease = self._read_json(path)
                if self.lease_is_live(lease):
                    continue
                path.unlink(missing_ok=True)
                task_id = lease.get("task_id", path.stem)
                released.append(task_id)
                if self.task_path(task_id).exists():
                    task = self.load(task_id)
                    if task["status"] == ACTIVE:
                        task["status"] = OPEN
                        self._clear_assignment_fields(task)
                    self._log(task, "lease-expired", str(lease.get("owner", "")))
                    self._write_task(task)
        return released

    # -- handoff inbox -----------------------------------------------------

    def tell(self, task_id: str, text: str) -> dict:
        """Queue a user update for whichever session is working on the task."""
        if not text.strip():
            raise TaskError("an update needs text")
        with self.locked():
            self.load(task_id)
            message = {
                "id": secrets.token_hex(4),
                "at": now(),
                "from": actor(),
                "text": text.strip(),
                "delivered_at": None,
            }
            path = self.inbox_path(task_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf8") as handle:
                handle.write(json.dumps(message, sort_keys=True) + "\n")
            return message

    def _messages(self, task_id: str) -> list:
        path = self.inbox_path(task_id)
        if not path.is_file():
            return []
        messages = []
        for line in path.read_text(encoding="utf8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                messages.append(message)
        return messages

    def inbox(self, task_id: str, consume: bool = False, include_delivered: bool = False) -> list:
        with self.locked():
            messages = self._messages(task_id)
            pending = [m for m in messages if not m.get("delivered_at")]
            if consume and pending:
                stamp = now()
                for message in messages:
                    if not message.get("delivered_at"):
                        message["delivered_at"] = stamp
                path = self.inbox_path(task_id)
                temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
                temporary.write_text(
                    "".join(json.dumps(m, sort_keys=True) + "\n" for m in messages),
                    encoding="utf8",
                )
                os.replace(temporary, path)
            return messages if include_delivered else pending

    def pending_counts(self) -> dict:
        if not self.inbox_dir.is_dir():
            return {}
        counts = {}
        for path in sorted(self.inbox_dir.glob("*.jsonl")):
            pending = [m for m in self._messages(path.stem) if not m.get("delivered_at")]
            if pending:
                counts[path.stem] = len(pending)
        return counts


def _process_alive(pid: int) -> bool:
    return grogu_platform.process_alive(pid)


def issue_payload(number: int, repository: Optional[str] = None) -> dict:
    """Read a GitHub issue through `gh`, so issues can seed local tasks."""
    command = ["gh", "issue", "view", str(number), "--json", "number,title,body,labels"]
    if repository:
        command += ["--repo", repository]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as error:
        raise TaskError(f"gh is required to adopt issues: {error}") from error
    if completed.returncode != 0:
        raise TaskError(completed.stderr.strip() or f"gh could not read issue #{number}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise TaskError(f"gh returned unreadable JSON: {error}") from error
    return payload
