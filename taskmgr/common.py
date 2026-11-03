"""Shared helpers: constants, durable JSON IO, file locks, time, errors."""

import contextlib
import datetime
import errno
import fcntl
import fnmatch
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Generator, Sequence
from typing import TYPE_CHECKING, cast

from .records import JsonDict as JsonDict
from .records import JsonValue as JsonValue
from .records import RemoteConfig

if TYPE_CHECKING:
    from typing_extensions import override as override
else:  # runtime stays stdlib-only; typing.override only exists from Python 3.12

    def override(func):
        return func


PROTOCOL_VERSION = 2

# What this code implements beyond protocol 2, reported in every taskmgr-remote reply as
# "features" (a sorted list). A server without the key runs code older than this list.
#   strict_task_json  the server rejects task.json keys it does not know (no silent ignore)
#   precompile        task.json `precompile` (compile without GPUs before the entry runs)
# `task submit` refuses a package that uses a feature its server does not report
# (package.task_features), before the upload.
FEATURES: tuple[str, ...] = ("precompile", "strict_task_json")

# The package directory. Inside a zipapp it is a path into the archive
# (/path/task.pyz/taskmgr), which os.path functions accept but open() does not.
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
# The code root: the checkout (or installed tree) that holds taskmgr/, or the .pyz file.
CODE_ROOT = os.path.dirname(PACKAGE_DIR)
SCHEMA_VERSION = 1

TASK_ID_RE = re.compile(r"^[A-Z]{2}-[0-9]{2,4}\Z")
SUBMISSION_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}\Z")
JOBID_RE = re.compile(r"^[0-9a-f]{16}\Z")

TERMINAL_STATES = ("succeeded", "failed", "invalid_result", "cancelled", "timed_out")
GPU_HOLDING_STATES = ("running", "stopping", "unknown")
ALL_STATES = ("queued",) + GPU_HOLDING_STATES + TERMINAL_STATES
# job.json `phase` of a job whose task.json has `precompile`: first the precompile command
# without GPUs, then the entry on the job's GPUs. A job without `precompile` has no phase.
PHASE_PRECOMPILE = "precompile"
PHASE_GPU = "gpu"

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
    "gpu_busy_exit_code": 75,
    "gpu_retry_interval_seconds": 1,
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


def atomic_write_bytes(path: str, data: bytes) -> None:
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


class NotRegularFile(OSError):
    """The path exists but is not a regular file (a directory, FIFO, device, socket, or a
    symlink when links are not followed)."""


def read_regular_file(path: str, follow_symlinks: bool = True) -> bytes:
    """Read a regular file without ever blocking on what is there instead.

    A task can replace its result.json or progress.json with a FIFO, a directory or a
    symlink. The file is opened with O_NONBLOCK (and O_NOFOLLOW unless follow_symlinks)
    and checked with fstat before reading. Raises FileNotFoundError when nothing is at
    `path` and NotRegularFile when something other than a regular file is.
    """
    flags = os.O_RDONLY | os.O_NONBLOCK | (0 if follow_symlinks else os.O_NOFOLLOW)
    st = os.stat(path) if follow_symlinks else os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise NotRegularFile("not a regular file: %s" % path)
    try:
        fd = os.open(path, flags)
    except OSError as e:
        if e.errno == errno.ELOOP:  # O_NOFOLLOW on a symlink
            raise NotRegularFile("not a regular file: %s" % path) from e
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):  # replaced since the stat above
            raise NotRegularFile("not a regular file: %s" % path)
        chunks: list[bytes] = []
        while True:
            b = os.read(fd, 1 << 20)
            if not b:
                return b"".join(chunks)
            chunks.append(b)
    finally:
        os.close(fd)


def try_read_json(path: str, follow_symlinks: bool = True) -> tuple[JsonValue, str | None]:
    """Return (obj, error_string). obj is None on missing, corrupt or unreadable files
    (including anything that is not a regular file, see read_regular_file)."""
    try:
        return cast(
            JsonValue, json.loads(read_regular_file(path, follow_symlinks).decode("utf-8"))
        ), None
    except FileNotFoundError:
        return None, "missing"
    except (ValueError, UnicodeDecodeError, RecursionError) as e:
        return None, "corrupt: %s" % e
    except NotRegularFile:
        return None, "unreadable: not a regular file"
    except OSError as e:
        return None, "unreadable: %s" % e


@contextlib.contextmanager
def flocked(path: str) -> Generator[int, None, None]:
    """Blocking exclusive flock on a lock file, created if needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        os.close(fd)


def try_flock_fd(path: str, create: bool = False, directory: bool = False) -> int | None:
    """Try a non-blocking exclusive flock. Returns the fd (lock held) or None."""
    flags = os.O_RDONLY if directory else os.O_RDWR | (os.O_CREAT if create else 0)
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


# ---------------------------------------------------------------------------
# Code root: a directory tree, or a zipapp (.pyz) built by `python3 -m taskmgr.pyz`


def code_is_zipapp() -> bool:
    """True when this code runs from a .pyz archive."""
    return os.path.isfile(CODE_ROOT)


def read_code_file(name: str) -> str | None:
    """Text of a file at the top of the code root (next to taskmgr/), or None."""
    try:
        if code_is_zipapp():
            with zipfile.ZipFile(CODE_ROOT) as z:
                return z.read(name).decode("utf-8")
        with open(os.path.join(CODE_ROOT, name)) as f:
            return f.read()
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None


def _ignored(rel: str, ignore: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(part, pat) for part in rel.split("/") for pat in ignore)


def copy_package_tree(sub: str, dest: str, ignore: Sequence[str] = ()) -> int:
    """Copy taskmgr/<sub> (the whole package when sub is "") to the new directory dest.

    Works from a checkout and from a zipapp; file modes are kept. A path is skipped when
    one of its components matches a glob in `ignore`. Returns the number of files copied.
    """
    if not code_is_zipapp():
        src = os.path.join(PACKAGE_DIR, sub) if sub else PACKAGE_DIR
        _ = shutil.copytree(src, dest, ignore=shutil.ignore_patterns(*ignore))
        return sum(len(names) for _, _, names in os.walk(dest))
    prefix = os.path.basename(PACKAGE_DIR) + "/" + (sub.strip("/") + "/" if sub else "")
    os.makedirs(dest)
    count = 0
    with zipfile.ZipFile(CODE_ROOT) as z:
        for info in z.infolist():
            if info.is_dir() or not info.filename.startswith(prefix):
                continue
            rel = info.filename[len(prefix) :]
            if not rel or _ignored(rel, ignore):
                continue
            target = os.path.join(dest, *rel.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info) as src_f, open(target, "wb") as dst_f:
                shutil.copyfileobj(src_f, dst_f)
            mode = (info.external_attr >> 16) & 0o777
            os.chmod(target, mode or 0o644)
            count += 1
    if count == 0:
        raise FileNotFoundError("%s has no %s" % (CODE_ROOT, prefix))
    return count
