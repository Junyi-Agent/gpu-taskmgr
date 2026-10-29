"""Shared helpers: constants, durable JSON IO, file locks, time, errors."""

import contextlib
import datetime
import fcntl
import json
import os
import re
import tempfile
from collections.abc import Generator
from typing import cast

from .records import JsonDict as JsonDict
from .records import JsonValue as JsonValue
from .records import RemoteConfig

PROTOCOL_VERSION = 1
SCHEMA_VERSION = 1

TASK_ID_RE = re.compile(r"^[A-Z]{2}-[0-9]{2,4}\Z")
SUBMISSION_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}\Z")
JOBID_RE = re.compile(r"^[0-9a-f]{16}\Z")

TERMINAL_STATES = ("succeeded", "failed", "invalid_result", "cancelled", "timed_out")
GPU_HOLDING_STATES = ("running", "stopping", "unknown")
ALL_STATES = ("queued",) + GPU_HOLDING_STATES + TERMINAL_STATES

STATE_DIRS: dict[str, str] = {
    "queued": "queue",
    "running": "in-progress",
    "stopping": "in-progress",
    "unknown": "in-progress",
}
for _s in TERMINAL_STATES:
    STATE_DIRS[_s] = "done"
JOB_DIRS = ("queue", "in-progress", "done")

DEFAULT_REMOTE_CONFIG: RemoteConfig = {
    "max_package_bytes": 1024**3,
    "max_cpu_jobs": None,
    "max_auto_restarts": 2,
    "stop_grace_seconds": 30,
    "kill_confirm_seconds": 10,
    "control_poll_seconds": 2,
    "scheduler_poll_seconds": 1,
    "base_env": {},
}


class TaskmgrError(Exception):
    """Error reported to the user as {"error": {...}} with an exit code."""

    def __init__(
        self,
        code: str,
        message: str,
        exit_code: int = 2,
        host: str | None = None,
        details: object = None,
    ) -> None:
        super().__init__(message)
        self.code: str = code
        self.message: str = message
        self.exit_code: int = exit_code
        self.host: str | None = host
        self.details: object = details

    def to_json(self) -> dict[str, object]:
        err: dict[str, object] = {"code": self.code, "message": self.message}
        if self.host is not None:
            err["host"] = self.host
        if self.details is not None:
            err["details"] = self.details
        return {"error": err}


def utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc_iso(value: object) -> datetime.datetime | None:
    """Parse a UTC ISO 8601 timestamp (Z or +00:00 suffix). Returns None if invalid."""
    if not isinstance(value, str):
        return None
    v = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None or dt.utcoffset() != datetime.timedelta(0):
        return None
    return dt


def fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path: str, data: bytes, fsync_parent: bool = True) -> None:
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".tmp-" + os.path.basename(path) + ".", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            _ = f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    if fsync_parent:
        fsync_dir(d)


def atomic_write_json(path: str, obj: object) -> None:
    atomic_write_bytes(path, (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode())


def read_json(path: str, default: JsonValue = None) -> JsonValue:
    """Read a JSON file; return default if it does not exist."""
    try:
        with open(path, "rb") as f:
            return cast(JsonValue, json.loads(f.read().decode("utf-8")))
    except FileNotFoundError:
        return default


def try_read_json(path: str) -> tuple[JsonValue, str | None]:
    """Return (obj, error_string). obj is None on missing or corrupt files."""
    try:
        with open(path, "rb") as f:
            return cast(JsonValue, json.loads(f.read().decode("utf-8"))), None
    except FileNotFoundError:
        return None, "missing"
    except (ValueError, UnicodeDecodeError) as e:
        return None, "corrupt: %s" % e
    except OSError as e:
        return None, "unreadable: %s" % e


@contextlib.contextmanager
def flocked(path: str, exclusive: bool = True) -> Generator[int, None, None]:
    """Blocking flock on a lock file, created if needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield fd
    finally:
        os.close(fd)


def try_flock_fd(path: str, create: bool = False, directory: bool = False) -> int | None:
    """Try a non-blocking exclusive flock. Returns the fd (lock held) or None."""
    if directory:
        flags = os.O_RDONLY
    else:
        flags = os.O_RDWR | (os.O_CREAT if create else 0)
    try:
        fd = os.open(path, flags, 0o644)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd
