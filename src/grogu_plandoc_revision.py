"""Append-only revision envelopes and crash-safe package generations."""

from __future__ import annotations

import datetime as dt
import errno
import os
import re
import stat
import unicodedata
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

import grogu_platform
import grogu_plandoc_canon as canon
import grogu_plandoc_patch as patch
import grogu_plandoc_schema as schema

SNAPSHOT_INTERVAL = 50
_LOG_NAME = re.compile(r"^(?P<seq>[0-9]{6})\.json$")


class RevisionError(ValueError):
    """Revision history or package state is inconsistent."""


class ImmutableFileError(RevisionError):
    """An immutable log entry or snapshot would be overwritten."""


def utc_now() -> str:
    """Return a canonical second-resolution UTC timestamp."""
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def revision_id(seq: int) -> str:
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise RevisionError("revision sequence must be a positive integer")
    return f"r{seq:04d}"


def make_revision(
    before: Any,
    after: Any,
    *,
    seq: int,
    actor: str,
    role: str,
    agent: str,
    intent: str,
    origin: str,
    ops: list[dict],
    at: str | None = None,
) -> dict:
    """Create and validate an immutable revision envelope."""
    envelope = {
        "schema_version": 1,
        "revision": revision_id(seq),
        "seq": seq,
        "parent": "" if seq == 1 else revision_id(seq - 1),
        "at": at or utc_now(),
        "actor": actor,
        "role": role,
        "agent": agent,
        "intent": intent,
        "origin": origin,
        "ops": ops,
        "before_digest": canon.digest(before),
        "after_digest": canon.digest(after),
    }
    return schema.validate_revision(envelope)


def _safe_relative(path: str) -> PurePosixPath:
    if not isinstance(path, str) or not path:
        raise RevisionError("package path must be a nonempty string")
    if "\\" in path or ":" in path or path.startswith(("/", "//")):
        raise RevisionError(f"package path {path!r} uses an unsafe path form")
    raw_parts = path.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise RevisionError(f"package path {path!r} must be relative and contained")
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise RevisionError(f"package path {path!r} must be relative and contained")
    if any(not part.rstrip(" .") for part in candidate.parts):
        raise RevisionError(f"package path {path!r} has an unsafe component")
    windows_reserved = {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
    }
    for part in candidate.parts:
        stem = part.rstrip(" .").split(".", 1)[0].casefold()
        if stem in windows_reserved:
            raise RevisionError(f"package path {path!r} uses a reserved name")
    return candidate


def _filesystem_key(path: PurePosixPath) -> tuple[str, ...]:
    """Conservatively detect aliases on case/normalization-insensitive stores."""
    return tuple(
        unicodedata.normalize("NFD", part).casefold().rstrip(" .")
        for part in path.parts
    )


_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


@contextmanager
def _windows_path_guard(path: Path, *, create: bool):
    """Hold non-reparse directory handles so Windows paths cannot be swapped."""
    if os.name != "nt":
        yield
        return
    import ctypes
    from ctypes import wintypes

    class FileInformation(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("creation_time_low", wintypes.DWORD),
            ("creation_time_high", wintypes.DWORD),
            ("access_time_low", wintypes.DWORD),
            ("access_time_high", wintypes.DWORD),
            ("write_time_low", wintypes.DWORD),
            ("write_time_high", wintypes.DWORD),
            ("volume_serial", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(FileInformation),
    )
    get_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    file_read_attributes = 0x0080
    share_read_write = 0x00000001 | 0x00000002
    open_existing = 3
    backup_semantics = 0x02000000
    open_reparse_point = 0x00200000
    directory_attribute = 0x00000010
    reparse_attribute = 0x00000400
    invalid_handle = ctypes.c_void_p(-1).value

    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    components = [current]
    for part in absolute.parts[1:]:
        current /= part
        components.append(current)
    handles = []
    try:
        for index, component in enumerate(components):
            if index and create:
                try:
                    os.mkdir(component)
                except FileExistsError:
                    pass
            handle = create_file(
                str(component),
                file_read_attributes,
                share_read_write,
                None,
                open_existing,
                backup_semantics | open_reparse_point,
                None,
            )
            if handle == invalid_handle:
                raise RevisionError(
                    f"cannot safely open package directory {component}"
                )
            information = FileInformation()
            if not get_information(handle, ctypes.byref(information)):
                close_handle(handle)
                raise RevisionError(
                    f"cannot inspect package directory {component}"
                )
            if not information.attributes & directory_attribute:
                close_handle(handle)
                raise RevisionError(
                    f"package path component is not a directory: {component}"
                )
            if information.attributes & reparse_attribute:
                close_handle(handle)
                raise RevisionError(
                    f"package directory traverses a reparse point: {component}"
                )
            handles.append(handle)
        yield
    finally:
        for handle in reversed(handles):
            close_handle(handle)


def _windows_directory(path: Path, *, create: bool) -> int:
    absolute = Path(os.path.abspath(path))
    with _windows_path_guard(absolute, create=create):
        return os.open(absolute, os.O_RDONLY)


def _windows_read_file(path: Path) -> bytes:
    """Read a regular file from the same non-reparse handle that was checked."""
    if os.name != "nt":
        return path.read_bytes()
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FileInformation(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("creation_time_low", wintypes.DWORD),
            ("creation_time_high", wintypes.DWORD),
            ("access_time_low", wintypes.DWORD),
            ("access_time_high", wintypes.DWORD),
            ("write_time_low", wintypes.DWORD),
            ("write_time_high", wintypes.DWORD),
            ("volume_serial", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(FileInformation),
    )
    get_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    generic_read = 0x80000000
    share_read_write = 0x00000001 | 0x00000002
    open_existing = 3
    open_reparse_point = 0x00200000
    directory_attribute = 0x00000010
    reparse_attribute = 0x00000400
    invalid_handle = ctypes.c_void_p(-1).value

    with _windows_path_guard(path.parent, create=False):
        handle = create_file(
            str(path),
            generic_read,
            share_read_write,
            None,
            open_existing,
            open_reparse_point,
            None,
        )
        if handle == invalid_handle:
            error = ctypes.get_last_error()
            if error in {2, 3}:
                raise FileNotFoundError(str(path))
            raise RevisionError(f"cannot safely open package file {path.name!r}")
        information = FileInformation()
        if not get_information(handle, ctypes.byref(information)):
            close_handle(handle)
            raise RevisionError(f"cannot inspect package file {path.name!r}")
        if information.attributes & (directory_attribute | reparse_attribute):
            close_handle(handle)
            raise RevisionError(
                f"package file {path.name!r} is not a regular non-reparse file"
            )
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)
        if descriptor < 0:
            close_handle(handle)
            raise RevisionError(f"cannot read package file {path.name!r}")
        try:
            chunks = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        finally:
            os.close(descriptor)


def _windows_list_names(path: Path) -> list[str]:
    with _windows_path_guard(path, create=False):
        return sorted(item.name for item in path.iterdir())


def _open_directory_chain(path: Path, *, create: bool) -> int:
    """Open every directory component without following repository symlinks."""
    if os.name == "nt":
        return _windows_directory(path, create=create)
    absolute = Path(os.path.abspath(path))
    descriptor = os.open(absolute.anchor or "/", _DIRECTORY_FLAGS)
    try:
        for part in absolute.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            try:
                child = os.open(
                    part,
                    _DIRECTORY_FLAGS | _NOFOLLOW,
                    dir_fd=descriptor,
                )
            except OSError as error:
                raise RevisionError(
                    f"package directory component {part!r} is missing, "
                    "not a directory, or a symlink"
                ) from error
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_child_directory(parent: int, name: str, *, create: bool) -> int:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise RevisionError(f"invalid package directory component {name!r}")
    if os.name == "nt":
        raise RevisionError("directory-descriptor package writes are unavailable")
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
    try:
        return os.open(name, _DIRECTORY_FLAGS | _NOFOLLOW, dir_fd=parent)
    except OSError as error:
        raise RevisionError(
            f"package directory component {name!r} is missing, "
            "not a directory, or a symlink"
        ) from error


@contextmanager
def _relative_parent(
    root_descriptor: int,
    relative: PurePosixPath,
    *,
    create: bool,
):
    descriptor = os.dup(root_descriptor)
    try:
        for part in relative.parts[:-1]:
            child = _open_child_directory(descriptor, part, create=create)
            os.close(descriptor)
            descriptor = child
        yield descriptor, relative.name
    finally:
        os.close(descriptor)


def _read_file_at(parent: int, name: str) -> bytes:
    flags = os.O_RDONLY | _NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=parent)
    except OSError as error:
        raise RevisionError(f"cannot safely read package file {name!r}") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RevisionError(f"package file {name!r} is not a regular file")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def _read_relative_at(
    root_descriptor: int,
    relative: PurePosixPath,
) -> bytes:
    with _relative_parent(root_descriptor, relative, create=False) as (
        parent,
        name,
    ):
        return _read_file_at(parent, name)


def _atomic_write_at(
    root_descriptor: int,
    relative: PurePosixPath,
    payload: bytes,
    *,
    immutable: bool,
) -> None:
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    with _relative_parent(root_descriptor, relative, create=True) as (
        parent,
        target_name,
    ):
        temporary_name = (
            f".{target_name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            if immutable:
                try:
                    os.link(
                        temporary_name,
                        target_name,
                        src_dir_fd=parent,
                        dst_dir_fd=parent,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    if _read_file_at(parent, target_name) != payload:
                        raise ImmutableFileError(
                            f"immutable file already exists: {relative}"
                        )
                os.unlink(temporary_name, dir_fd=parent)
            else:
                os.replace(
                    temporary_name,
                    target_name,
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                )
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary_name, dir_fd=parent)
            except FileNotFoundError:
                pass


def _windows_atomic_write(path: Path, payload: bytes, *, immutable: bool) -> None:
    with _windows_path_guard(path.parent, create=True):
        if immutable and path.exists():
            if _windows_read_file(path) != payload:
                raise ImmutableFileError(f"immutable file already exists: {path}")
            return
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if immutable:
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    if _windows_read_file(path) != payload:
                        raise ImmutableFileError(
                            f"immutable file already exists: {path}"
                        )
                    return
            else:
                os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()


def atomic_write(
    path: Path,
    payload: bytes,
    *,
    immutable: bool = False,
) -> None:
    """Write without following symlinked package path components."""
    path = Path(path)
    if os.name == "nt":
        _windows_atomic_write(path, payload, immutable=immutable)
        return
    parent = _open_directory_chain(path.parent, create=True)
    try:
        _atomic_write_at(
            parent,
            PurePosixPath(path.name),
            payload,
            immutable=immutable,
        )
    finally:
        os.close(parent)


def safe_read(path: Path) -> bytes:
    """Read a regular file without following any untrusted path component."""
    path = Path(path)
    if os.name == "nt":
        return _windows_read_file(path)
    try:
        parent = _open_directory_chain(path.parent, create=False)
    except RevisionError as error:
        if not path.parent.exists():
            raise FileNotFoundError(str(path)) from error
        raise
    try:
        try:
            return _read_file_at(parent, path.name)
        except RevisionError as error:
            if not path.exists():
                raise FileNotFoundError(str(path)) from error
            raise
    finally:
        os.close(parent)


def _json_bytes(value: Any) -> bytes:
    return canon.pretty_dumpb(value)


def _parse_head_bytes(payload: bytes) -> str:
    try:
        value = payload.decode("ascii")
    except UnicodeDecodeError as error:
        raise RevisionError("HEAD must be ASCII") from error
    if not value.endswith("\n") or value.count("\n") != 1:
        raise RevisionError("HEAD must contain one revision and one newline")
    result = value[:-1]
    if re.fullmatch(r"r[0-9]{4,}", result) is None:
        raise RevisionError(f"HEAD contains invalid revision {result!r}")
    return result


def _head_at(package_descriptor: int, *, missing: str | None = None) -> str:
    try:
        return _parse_head_bytes(_read_file_at(package_descriptor, "HEAD"))
    except RevisionError as error:
        try:
            os.stat("HEAD", dir_fd=package_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            if missing is not None:
                return missing
        raise error


@contextmanager
def _generation_lock(package: Path):
    """Serialize a complete package generation using local ignored state."""
    package = Path(package)
    if os.name == "nt":
        with _windows_path_guard(package, create=True):
            log = package / "log"
            with _windows_path_guard(log, create=True):
                lock_descriptor = os.open(
                    log / "write.lock", os.O_RDWR | os.O_CREAT, 0o600
                )
                try:
                    with grogu_platform.exclusive_lock(lock_descriptor):
                        yield None
                finally:
                    os.close(lock_descriptor)
        return

    package_descriptor = _open_directory_chain(package, create=True)
    log_descriptor = _open_child_directory(
        package_descriptor, "log", create=True
    )
    try:
        lock_descriptor = -1
        for _attempt in range(4):
            try:
                lock_descriptor = os.open(
                    "write.lock",
                    os.O_RDWR | os.O_CREAT | _NOFOLLOW,
                    0o600,
                    dir_fd=log_descriptor,
                )
                break
            except FileNotFoundError:
                # macOS can report ENOENT when two O_CREAT lookups race on the
                # same directory entry. The next lookup opens the winner.
                continue
        if lock_descriptor < 0:
            raise RevisionError("could not create the generation lock")
        try:
            if not stat.S_ISREG(os.fstat(lock_descriptor).st_mode):
                raise RevisionError("generation lock is not a regular file")
            with grogu_platform.exclusive_lock(lock_descriptor):
                yield package_descriptor
        finally:
            os.close(lock_descriptor)
    finally:
        os.close(log_descriptor)
        os.close(package_descriptor)


def write_generation(
    package: Path,
    envelope: Mapping[str, Any],
    *,
    partitions: Mapping[str, bytes],
    artifacts: Mapping[str, bytes],
    snapshot: Any | None = None,
    before_head_replace: Callable[[], None] | None = None,
    base: str | None = None,
    identity_checks: Iterable[Callable[[], Any]] = (),
) -> None:
    """Persist one complete generation with ``HEAD`` replaced last.

    Paths in ``partitions`` and ``artifacts`` are package-relative.  Callers
    supply already-sealed bytes for sealed graph partitions; this layer never
    decodes them. Compiler identity callbacks run before the immutable log or
    any materialized file is written.
    """
    revision = schema.validate_revision(envelope)
    package = Path(package)
    for check in identity_checks:
        check()
    if snapshot is not None and revision["seq"] % SNAPSHOT_INTERVAL != 0:
        raise RevisionError(
            f"snapshot {revision['revision']} is not on the {SNAPSHOT_INTERVAL}-revision boundary"
        )
    partition_paths = {
        relative: _safe_relative(relative) for relative in partitions
    }
    artifact_paths = {relative: _safe_relative(relative) for relative in artifacts}
    folded_partitions = {
        _filesystem_key(candidate): relative
        for relative, candidate in partition_paths.items()
    }
    folded_artifacts = {
        _filesystem_key(candidate): relative
        for relative, candidate in artifact_paths.items()
    }
    if len(folded_partitions) != len(partition_paths):
        raise RevisionError("partition paths contain filesystem aliases")
    if len(folded_artifacts) != len(artifact_paths):
        raise RevisionError("artifact paths contain filesystem aliases")
    overlap = set(folded_partitions) & set(folded_artifacts)
    if overlap:
        raise RevisionError(
            "partition and artifact paths overlap under filesystem normalization"
        )
    for relative, candidate in partition_paths.items():
        if not candidate.parts or candidate.parts[0] != "graph":
            raise RevisionError(f"partition path {relative!r} must be under graph/")
    for relative, candidate in artifact_paths.items():
        first = _filesystem_key(candidate)[0]
        if first in {"head", "graph", "log", "snapshots"}:
            raise RevisionError(f"artifact path {relative!r} targets package control state")
    for relative, payload in (*partitions.items(), *artifacts.items()):
        if not isinstance(payload, bytes):
            raise TypeError(f"package payload {relative!r} must be bytes")
    with _generation_lock(package) as package_descriptor:
        current = (
            _head_at(package_descriptor, missing="")
            if package_descriptor is not None
            else (read_head(package) if (package / "HEAD").exists() else "")
        )
        expected_base = revision["parent"]
        asserted_base = expected_base if base is None else base
        if asserted_base != current:
            raise patch.StaleRevision(asserted_base, current)
        if expected_base != current:
            raise RevisionError(
                f"{revision['revision']} parent {expected_base!r} "
                f"does not match HEAD {current!r}"
            )

        def write(relative: PurePosixPath, payload: bytes, *, immutable=False):
            if package_descriptor is None:
                atomic_write(
                    package.joinpath(*relative.parts),
                    payload,
                    immutable=immutable,
                )
            else:
                _atomic_write_at(
                    package_descriptor,
                    relative,
                    payload,
                    immutable=immutable,
                )

        write(
            PurePosixPath("log", f"{revision['seq']:06d}.json"),
            _json_bytes(revision),
            immutable=True,
        )
        for relative in sorted(partitions, key=canon.utf16_key):
            write(partition_paths[relative], partitions[relative])
        if snapshot is not None:
            write(
                PurePosixPath("snapshots", f"{revision['revision']}.json"),
                _json_bytes(snapshot),
                immutable=True,
            )
        for relative in sorted(artifacts, key=canon.utf16_key):
            write(artifact_paths[relative], artifacts[relative])
        if before_head_replace is not None:
            before_head_replace()
        write(
            PurePosixPath("HEAD"),
            f"{revision['revision']}\n".encode("ascii"),
        )


def read_head(package: Path) -> str:
    """Read and validate the one-line package HEAD."""
    package = Path(package)
    if os.name == "nt":
        path = package / "HEAD"
        try:
            return _parse_head_bytes(_windows_read_file(path))
        except FileNotFoundError as error:
            raise RevisionError("plan document has no HEAD") from error
    descriptor = _open_directory_chain(package, create=False)
    try:
        return _head_at(descriptor)
    finally:
        os.close(descriptor)


def read_revision(package: Path, revision: str) -> dict:
    """Read one revision by id without accepting duplicate JSON keys."""
    if re.fullmatch(r"r[0-9]{4,}", revision) is None:
        raise RevisionError(f"invalid revision id {revision!r}")
    seq = int(revision[1:])
    package = Path(package)
    if os.name == "nt":
        path = package / "log" / f"{seq:06d}.json"
        try:
            payload = _windows_read_file(path)
        except FileNotFoundError as error:
            raise RevisionError(f"missing revision log {revision}") from error
    else:
        descriptor = _open_directory_chain(package, create=False)
        try:
            payload = _read_relative_at(
                descriptor,
                PurePosixPath("log", f"{seq:06d}.json"),
            )
        except RevisionError as error:
            raise RevisionError(f"missing or unsafe revision log {revision}") from error
        finally:
            os.close(descriptor)
    value = canon.loads(payload)
    return schema.validate_revision(value)


def list_revisions(package: Path) -> list[dict]:
    """Return the valid immutable log prefix in sequence order."""
    package = Path(package)
    if os.name == "nt":
        directory = package / "log"
        if not directory.exists():
            return []
        revisions = []
        for name in _windows_list_names(directory):
            if _LOG_NAME.fullmatch(name) is None:
                continue
            revisions.append(
                schema.validate_revision(
                    canon.loads(_windows_read_file(directory / name))
                )
            )
        return revisions
    try:
        package_descriptor = _open_directory_chain(package, create=False)
    except RevisionError:
        return []
    try:
        try:
            log_descriptor = _open_child_directory(
                package_descriptor, "log", create=False
            )
        except RevisionError:
            return []
        try:
            revisions = []
            for name in sorted(os.listdir(log_descriptor)):
                if _LOG_NAME.fullmatch(name) is None:
                    continue
                revisions.append(
                    schema.validate_revision(
                        canon.loads(_read_file_at(log_descriptor, name))
                    )
                )
            return revisions
        finally:
            os.close(log_descriptor)
    finally:
        os.close(package_descriptor)


def verify_log_chain(package: Path) -> list[dict]:
    """Validate sequence, parent links and before/after digest continuity."""
    revisions = list_revisions(package)
    for index, revision in enumerate(revisions, 1):
        if revision["seq"] != index:
            raise RevisionError(
                f"log sequence jumps from {index - 1:06d} to {revision['seq']:06d}"
            )
        if index > 1:
            previous = revisions[index - 2]
            if revision["parent"] != previous["revision"]:
                raise RevisionError(
                    f"{revision['revision']} parent is not {previous['revision']}"
                )
            if revision["before_digest"] != previous["after_digest"]:
                raise RevisionError(
                    f"{revision['revision']} before digest does not match its parent"
                )
    return revisions


def recover_head(
    package: Path,
    *,
    repair: bool = False,
    is_complete: Callable[[dict], bool] | None = None,
) -> str:
    """Fall back to the newest complete log entry when HEAD is invalid."""
    revisions = verify_log_chain(package)
    if not revisions:
        raise RevisionError("plan document has no complete revisions")
    complete = [
        item for item in revisions if is_complete is None or is_complete(copy_document(item))
    ]
    if not complete:
        raise RevisionError("plan document has no complete revisions")
    newest = complete[-1]["revision"]
    try:
        head = read_head(package)
        current = read_revision(package, head)
        if (
            current["revision"] == newest
            and (is_complete is None or is_complete(copy_document(current)))
        ):
            return head
    except RevisionError:
        pass
    if repair:
        atomic_write(package / "HEAD", f"{newest}\n".encode("ascii"))
    return newest


def _snapshot_candidates(package: Path, target_seq: int) -> list[tuple[int, Path]]:
    directory = package / "snapshots"
    if not directory.exists():
        return []
    values = []
    paths = (
        (directory / name for name in _windows_list_names(directory))
        if os.name == "nt"
        else directory.glob("r*.json")
    )
    for path in paths:
        if re.fullmatch(r"r[0-9]{4,}\.json", path.name) is None:
            continue
        seq = int(path.stem[1:])
        if seq <= target_seq and seq % SNAPSHOT_INTERVAL == 0:
            values.append((seq, path))
    return sorted(values, reverse=True)


def replay(
    package: Path,
    *,
    initial: Any,
    target: str | None = None,
    validator: Callable[[Any], Any] | None = None,
) -> Any:
    """Replay at most 50 operations after the newest usable snapshot."""
    revisions = verify_log_chain(package)
    if not revisions:
        return copy_document(initial)
    target_revision = target or recover_head(package)
    target_seq = int(target_revision[1:])
    if target_seq > revisions[-1]["seq"]:
        raise RevisionError(f"target {target_revision} is newer than the log")

    state = copy_document(initial)
    start_seq = 1
    for snapshot_seq, path in _snapshot_candidates(package, target_seq):
        try:
            payload = (
                _windows_read_file(path) if os.name == "nt" else path.read_bytes()
            )
            candidate = canon.loads(payload)
        except (OSError, canon.CanonicalError):
            continue
        if canon.digest(candidate) != revisions[snapshot_seq - 1]["after_digest"]:
            continue
        state = candidate
        start_seq = snapshot_seq + 1
        break

    for revision in revisions[start_seq - 1 : target_seq]:
        if canon.digest(state) != revision["before_digest"]:
            raise RevisionError(
                f"{revision['revision']} before digest does not match replay state"
            )
        state = patch.apply_patch(
            state,
            revision["ops"],
            validator=validator,
        )
        if canon.digest(state) != revision["after_digest"]:
            raise RevisionError(
                f"{revision['revision']} after digest does not match replay state"
            )
    return state


def copy_document(value: Any) -> Any:
    """Return an isolated canonical JSON value."""
    return canon.loads(canon.dumpb(value))


def verify_package(
    package: Path,
    *,
    materialized: Any | None = None,
) -> dict:
    """Verify HEAD, the immutable log chain and an optional materialization."""
    revisions = verify_log_chain(package)
    head = read_head(package)
    current = read_revision(package, head)
    if not revisions or current["revision"] != revisions[-1]["revision"]:
        raise RevisionError("HEAD is not the newest complete revision")
    if materialized is not None:
        actual = canon.digest(materialized)
        if actual != current["after_digest"]:
            raise RevisionError(
                f"materialized digest {actual} does not match {current['after_digest']}"
            )
    return {
        "head": head,
        "revisions": len(revisions),
        "digest": current["after_digest"],
        "ok": True,
    }


class RevisionStore:
    """Small importable facade used by PlanStore integration."""

    def __init__(self, package: Path):
        self.package = Path(package)

    def head(self) -> str:
        return read_head(self.package)

    def revisions(self) -> list[dict]:
        return list_revisions(self.package)

    def recover(
        self,
        *,
        repair: bool = False,
        is_complete: Callable[[dict], bool] | None = None,
    ) -> str:
        return recover_head(
            self.package, repair=repair, is_complete=is_complete
        )

    def verify(self, *, materialized: Any | None = None) -> dict:
        return verify_package(self.package, materialized=materialized)

    def write(
        self,
        envelope: Mapping[str, Any],
        *,
        partitions: Mapping[str, bytes],
        artifacts: Mapping[str, bytes],
        snapshot: Any | None = None,
        base: str | None = None,
        identity_checks: Iterable[Callable[[], Any]] = (),
    ) -> None:
        write_generation(
            self.package,
            envelope,
            partitions=partitions,
            artifacts=artifacts,
            snapshot=snapshot,
            base=base,
            identity_checks=identity_checks,
        )
