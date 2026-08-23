"""Small operating-system primitives shared by Grogu's stores and runners.

Keep platform branching here so repository, task, plan, skill, and design
behavior stays identical on every supported host. These helpers guard against
accidental concurrency and runaway code; they are not security boundaries.
"""

from __future__ import annotations

import errno
import os
import sys
import time
from contextlib import contextmanager
from typing import Iterator

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes
else:
    import fcntl

try:
    import resource
except ModuleNotFoundError:  # Windows has no POSIX resource module.
    resource = None  # type: ignore[assignment]

_LOCK_RETRY_SECONDS = 0.05
_LOCK_CONTENTION_ERRORS = frozenset(
    {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
)
_WINDOWS_ERROR_INVALID_PARAMETER = 87

if os.name == "nt":
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.argtypes = (
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    )
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetExitCodeProcess.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL


@contextmanager
def exclusive_lock(descriptor: int) -> Iterator[None]:
    """Hold an exclusive advisory lock on an open read/write descriptor.

    POSIX ``flock`` locks the whole file. Windows CRT locking is byte-range
    based, so lock byte zero and create that byte when a new lock file is
    empty. Both locks are released by the operating system if the process
    exits, and both wait until a competing process releases the file.
    """
    if os.name != "nt":
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        return

    position = os.lseek(descriptor, 0, os.SEEK_CUR)
    if os.fstat(descriptor).st_size == 0:
        os.write(descriptor, b"\0")
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            break
        except OSError as error:
            if error.errno not in _LOCK_CONTENTION_ERRORS:
                raise
            time.sleep(_LOCK_RETRY_SECONDS)
    try:
        yield
    finally:
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        finally:
            os.lseek(descriptor, position, os.SEEK_SET)


def limit_cpu_time(seconds: int) -> None:
    """Best-effort CPU-time cap for a child process on supported POSIX hosts."""
    if resource is None:
        return
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (seconds, seconds))
    except (ValueError, OSError):
        pass


def process_alive(pid: int) -> bool:
    """Whether a process exists and has not exited on the local machine."""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    handle = _kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not handle:
        return _windows_open_failure_is_alive(ctypes.get_last_error())
    try:
        exit_code = wintypes.DWORD()
        return bool(
            _kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            and exit_code.value == _STILL_ACTIVE
        )
    finally:
        _kernel32.CloseHandle(handle)


def _windows_open_failure_is_alive(error_code: int) -> bool:
    """Treat only Windows' missing-process result as proof a PID is dead.

    Access denial and transient system failures cannot prove liveness, so
    callers that clean up leases or session holders must conservatively keep
    them rather than reclaiming state from a process that may still exist.
    """
    return error_code != _WINDOWS_ERROR_INVALID_PARAMETER


def configure_standard_streams() -> None:
    """Use UTF-8 for Windows pipes so Unicode CLI output remains writable."""
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
