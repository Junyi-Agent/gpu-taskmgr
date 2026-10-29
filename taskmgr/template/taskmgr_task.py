"""Helpers for task code: atomic, flock-protected progress.json / result.json writes.

Standard library only. Paths come from TASKMGR_PROGRESS_FILE / TASKMGR_RESULT_FILE
(set by the runner) and fall back to ./progress.json and ./result.json.

    import taskmgr_task as tt
    tt.write_progress("profiling", percent=40, message="kernel 3/8")
    tt.update_outputs(speedup=1.37)            # read-modify-write under a lock
    tt.write_result({"best_config": {...}})    # replace outputs entirely

Never write the "_taskmgr" key; the runner owns it.
"""

import contextlib
import datetime
import fcntl
import json
import os
import tempfile
from collections.abc import Generator, Mapping
from typing import cast

SCHEMA_VERSION = 1


def progress_path() -> str:
    return os.environ.get("TASKMGR_PROGRESS_FILE") or os.path.abspath("progress.json")


def result_path() -> str:
    return os.environ.get("TASKMGR_RESULT_FILE") or os.path.abspath("result.json")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write_json(path: str, obj: object) -> None:
    """Write JSON via temp file + fsync + os.replace so readers never see a partial file."""
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2, sort_keys=True)
            _ = f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def locked(path: str) -> Generator[None, None, None]:
    """flock on <path>.lock to serialize read-modify-write between task processes."""
    fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read(path: str, default: dict[str, object]) -> dict[str, object]:
    try:
        with open(path) as f:
            obj = cast(object, json.load(f))
        return cast("dict[str, object]", obj) if isinstance(obj, dict) else default
    except (FileNotFoundError, ValueError):
        return default


def write_progress(
    stage: object,
    percent: float | None = None,
    message: object = None,
    **extra: object,
) -> dict[str, object]:
    """Replace progress.json (stage and updated_at are always set)."""
    path = progress_path()
    obj: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "stage": str(stage),
        "updated_at": _now(),
    }
    if percent is not None:
        obj["percent"] = max(0, min(100, percent))
    if message is not None:
        obj["message"] = str(message)
    obj.update(extra)
    with locked(path):
        atomic_write_json(path, obj)
    return obj


def update_progress(**fields: object) -> dict[str, object]:
    """Merge fields into progress.json under the lock."""
    path = progress_path()
    with locked(path):
        obj = _read(path, {"schema_version": SCHEMA_VERSION, "stage": "running"})
        obj.update(fields)
        obj["updated_at"] = _now()
        atomic_write_json(path, obj)
    return obj


def write_result(outputs: Mapping[str, object], **extra: object) -> dict[str, object]:
    """Replace result.json with the given outputs object."""
    path = result_path()
    obj: dict[str, object] = {"schema_version": SCHEMA_VERSION, "outputs": dict(outputs)}
    obj.update({k: v for k, v in extra.items() if k != "_taskmgr"})
    with locked(path):
        atomic_write_json(path, obj)
    return obj


def update_outputs(**outputs: object) -> dict[str, object]:
    """Merge keys into result.json "outputs" under the lock (safe across processes)."""
    path = result_path()
    with locked(path):
        obj = _read(path, {"schema_version": SCHEMA_VERSION})
        _ = obj.pop("_taskmgr", None)
        prev = obj.get("outputs")
        out: dict[str, object] = (
            dict(cast("dict[str, object]", prev)) if isinstance(prev, dict) else {}
        )
        out.update(outputs)
        obj["outputs"] = out
        _ = obj.setdefault("schema_version", SCHEMA_VERSION)
        atomic_write_json(path, obj)
    return obj
