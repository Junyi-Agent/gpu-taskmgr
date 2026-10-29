"""Helpers for task code: atomic, flock-protected progress.json / result.json writes.

Standard library only. Paths come from TASKMGR_PROGRESS_FILE / TASKMGR_RESULT_FILE
(set by the runner) and fall back to ./progress.json and ./result.json.

    import taskmgr_task as tt
    tt.write_progress("profiling", percent=40, message="kernel 3/8")
    tt.update_outputs(speedup=1.37)            # read-modify-write under a lock
    tt.write_result({"best_config": {...}})    # replace outputs entirely

Never write the "_taskmgr" key; the runner owns it.

GPU contract: exit code 75 (GPU_BUSY_EXIT_CODE, EX_TEMPFAIL) means "I could not get the
GPU and did no work"; taskmgr then starts the entry again later, so the job waits instead
of failing. Call claim_gpu() before any work; call gpu_busy() if your own CUDA code finds
the device busy or unavailable. Never exit 75 for any other reason.

    tt.claim_gpu()                             # first line of main()
"""

import contextlib
import ctypes
import datetime
import fcntl
import json
import os
import sys
import tempfile
from collections.abc import Callable, Generator, Mapping
from typing import NoReturn, Protocol, cast

SCHEMA_VERSION = 1
GPU_BUSY_EXIT_CODE = 75  # EX_TEMPFAIL
CUDA_SUCCESS = 0
CUDA_ERROR_DEVICE_UNAVAILABLE = 46


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


# ---------------------------------------------------------------------------
# GPU contract


def gpu_busy(message: str = "") -> NoReturn:
    """Exit with GPU_BUSY_EXIT_CODE: the GPU is held by someone else and no work was done.

    taskmgr starts the entry again after a short wait (the job stays running, shown as
    "pending (gpu busy)"). Call it before producing any side effects.
    """
    _ = sys.stderr.write(
        "taskmgr_task: GPU busy, exiting %d to be started again later%s\n"
        % (GPU_BUSY_EXIT_CODE, ": " + message if message else "")
    )
    _ = sys.stderr.flush()
    raise SystemExit(GPU_BUSY_EXIT_CODE)


class CudaDriver(Protocol):
    """The few CUDA driver API calls claim_gpu() needs; each returns a CUresult code."""

    def init(self) -> int: ...

    def device_get(self, ordinal: int) -> tuple[int, int]: ...

    def primary_ctx_retain(self, device: int) -> tuple[int, int]: ...

    def error_name(self, code: int) -> str: ...


_CuCall1 = Callable[[object], int]
_CuCall2 = Callable[[object, object], int]


class LibCuda:
    """CudaDriver backed by libcuda.so.1 through ctypes (no CUDA Python packages needed)."""

    def __init__(self, lib: ctypes.CDLL) -> None:
        # Every function returns CUresult (an int, the ctypes default restype); arguments
        # are passed as explicit ctypes values, so no argtypes are needed.
        self._init: _CuCall1 = cast("_CuCall1", lib["cuInit"])
        self._get: _CuCall2 = cast("_CuCall2", lib["cuDeviceGet"])
        self._retain: _CuCall2 = cast("_CuCall2", lib["cuDevicePrimaryCtxRetain"])
        self._name: _CuCall2 = cast("_CuCall2", lib["cuGetErrorName"])

    def init(self) -> int:
        return self._init(ctypes.c_uint(0))

    def device_get(self, ordinal: int) -> tuple[int, int]:
        dev = ctypes.c_int(0)
        return self._get(ctypes.byref(dev), ctypes.c_int(ordinal)), dev.value

    def primary_ctx_retain(self, device: int) -> tuple[int, int]:
        ctx = ctypes.c_void_p(None)
        return self._retain(ctypes.byref(ctx), ctypes.c_int(device)), ctx.value or 0

    def error_name(self, code: int) -> str:
        name = ctypes.c_char_p(None)
        if self._name(ctypes.c_int(code), ctypes.byref(name)) != CUDA_SUCCESS or not name.value:
            return "CUresult %d" % code
        return name.value.decode(errors="replace")


def load_libcuda() -> CudaDriver:
    try:
        return LibCuda(ctypes.CDLL("libcuda.so.1"))
    except (OSError, AttributeError) as e:  # library or one of its functions missing
        raise RuntimeError("cannot load libcuda.so.1 (is the NVIDIA driver installed?): %s" % e)


# Primary contexts retained by claim_gpu(); never released, so the process keeps its GPUs.
_retained: list[int] = []


def claim_gpu(load: Callable[[], CudaDriver] = load_libcuda) -> None:
    """Take every visible GPU now by retaining its primary CUDA context, or exit 75.

    Under Exclusive_Process compute mode a GPU held by another process cannot be used;
    then this calls gpu_busy() and taskmgr starts the task again later. The contexts stay
    retained for the life of the process, and frameworks such as torch reuse the primary
    context in the same process. A task without GPUs (CUDA_VISIBLE_DEVICES empty or unset)
    returns at once. Other CUDA errors raise RuntimeError. `load` exists for tests.
    """
    visible = [d for d in os.environ.get("CUDA_VISIBLE_DEVICES", "").split(",") if d.strip()]
    if not visible:
        return
    cuda = load()

    def check(rc: int, what: str, ordinal: int | None = None) -> None:
        gpu = "" if ordinal is None else "GPU %d (%s) " % (ordinal, visible[ordinal])
        if rc == CUDA_ERROR_DEVICE_UNAVAILABLE:
            gpu_busy("%sis held by another process (%s)" % (gpu or "a GPU ", what))
        if rc != CUDA_SUCCESS:
            raise RuntimeError("%s%s failed: %s" % (gpu, what, cuda.error_name(rc)))

    check(cuda.init(), "cuInit")
    for i in range(len(visible)):  # CUDA numbers the visible devices 0..n-1
        rc, dev = cuda.device_get(i)
        check(rc, "cuDeviceGet", i)
        rc, ctx = cuda.primary_ctx_retain(dev)
        check(rc, "cuDevicePrimaryCtxRetain", i)
        _retained.append(ctx)
