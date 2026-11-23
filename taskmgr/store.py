"""Remote storage root: layout, config, state lock, job records, finalization, retry clones."""

import contextlib
import fcntl
import json
import os
import secrets
import shutil
import socket
import stat
import types
from collections.abc import Callable, Generator, Iterable, Iterator
from typing import cast, get_args, get_origin

from .common import (
    DEFAULT_REMOTE_CONFIG,
    JOB_DIRS,
    JOBID_RE,
    PHASE_GPU,
    PHASE_PRECOMPILE,
    SCHEMA_VERSION,
    STATE_DIRS,
    TERMINAL_STATES,
    NotRegularFile,
    TaskmgrError,
    atomic_write_json,
    fsync_dir,
    read_json,
    read_regular_file,
    try_read_json,
    utcnow_iso,
)
from .records import (
    ErrorInfo,
    GpuDecl,
    GpuRequest,
    Job,
    JsonDict,
    JsonValue,
    RemoteConfig,
    Runner,
    TaskmgrMeta,
    TaskSpec,
)

# The job directories of jobs that are not terminal (done/ grows without bound).
ACTIVE_JOB_DIRS = ("queue", "in-progress")


def _type_ok(value: object, tp: object) -> bool:
    """Whether a JSON value matches a RemoteConfig annotation (an int is a valid float)."""
    origin: object = get_origin(tp)
    args: tuple[object, ...] = get_args(tp)
    if origin is types.UnionType:  # records.py writes unions as X | Y
        return any(_type_ok(value, a) for a in args)
    if tp is type(None):
        return value is None
    if tp is int or tp is float:
        ok = (int,) if tp is int else (int, float)
        return isinstance(value, ok) and not isinstance(value, bool)
    if origin is dict:
        return isinstance(value, dict) and all(
            _type_ok(k, args[0]) and _type_ok(v, args[1])
            for k, v in cast(dict[object, object], value).items()
        )
    return isinstance(tp, type) and isinstance(value, tp)


class Store:
    def __init__(self, root: str) -> None:
        if not root or not os.path.isabs(root):
            raise TaskmgrError("bad_root", "--root must be an absolute path: %r" % (root,))
        self.root: str = root
        self._lock_depth: int = 0
        self._config: RemoteConfig | None = None

    # -- layout -------------------------------------------------------------
    def path(self, *parts: str) -> str:
        return os.path.join(self.root, *parts)

    def ensure_layout(self) -> None:
        for d in ("locks", "incoming", "submission-keys", "daemon") + JOB_DIRS:
            os.makedirs(self.path(d), exist_ok=True)

    @property
    def config(self) -> RemoteConfig:
        if self._config is None:
            cfg: dict[str, object] = dict(DEFAULT_REMOTE_CONFIG)
            obj, err = try_read_json(self.path("config.json"))
            if err and err != "missing":
                raise TaskmgrError("bad_config", "config.json is %s" % err)
            if isinstance(obj, dict):
                cfg.update(obj)
            elif err is None:
                raise TaskmgrError("bad_config", "config.json is not a JSON object")
            for key, tp in cast(dict[str, object], RemoteConfig.__annotations__).items():
                if not _type_ok(cfg.get(key), tp):
                    raise TaskmgrError(
                        "bad_config",
                        "config.json: %s has the wrong type: %r" % (key, cfg.get(key)),
                    )
            busy = cfg.get("gpu_busy_exit_code")
            if busy is not None and not (isinstance(busy, int) and 1 <= busy <= 255):
                # taskmgr_task.py exits 75 for any value outside 1-255.
                raise TaskmgrError(
                    "bad_config",
                    "config.json: gpu_busy_exit_code must be null or an integer from 1 to 255: %r"
                    % busy,
                )
            self._config = cast(RemoteConfig, cast(object, cfg))
        return self._config

    # -- state lock (re-entrant within one process) --------------------------
    @contextlib.contextmanager
    def state_lock(self) -> Generator[None, None, None]:
        if self._lock_depth:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return
        os.makedirs(self.path("locks"), exist_ok=True)
        fd = os.open(self.path("locks", "state.lock"), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._lock_depth = 1
            yield
        finally:
            self._lock_depth = 0
            os.close(fd)

    # -- jobs ---------------------------------------------------------------
    def dir_for(self, state: str, jobid: str) -> str:
        """Where the directory of a job in `state` lives (in-progress/ for an unknown state)."""
        return self.path(STATE_DIRS.get(state, "in-progress"), jobid)

    def _scan_job_dir(self, jobid: str) -> str | None:
        for d in JOB_DIRS:
            p = self.path(d, jobid)
            if os.path.isdir(p):
                return p
        return None

    def find_job_dir(self, jobid: object) -> str | None:
        """The job's directory, or None. Without the state lock a scan can miss a directory
        that moves backwards (in-progress/ -> queue/) meanwhile; every move happens under
        the lock, so a miss is confirmed by a second scan under it."""
        if not isinstance(jobid, str) or not JOBID_RE.match(jobid):
            return None
        found = self._scan_job_dir(jobid)
        if found is None and not self._lock_depth:
            with self.state_lock():
                found = self._scan_job_dir(jobid)
        return found

    def load_job(self, jobid: str) -> tuple[str, Job]:
        for again in (False, True):
            jd = self.find_job_dir(jobid)
            if jd is None:
                raise TaskmgrError("not_found", "job %s not found" % jobid)
            job, err = try_read_json(os.path.join(jd, "job.json"))
            if job is not None:
                return jd, cast(Job, cast(object, job))
            if again or err != "missing":  # "missing": the directory moved; look again
                raise TaskmgrError("corrupt_job", "job.json of %s is %s" % (jobid, err))
        raise AssertionError("unreachable")

    def iter_jobs(self, active: bool = False) -> Iterator[tuple[str, Job]]:
        """Yield (dir, job) for every job (active: only queue/ and in-progress/); skips
        unreadable records. Without the state lock a job that moves meanwhile can show up
        twice; queue/ is listed again at the end so one moved back from in-progress/
        (a "GPU busy" re-queue) is not missed."""
        seen: set[str] = set()
        dirs = ACTIVE_JOB_DIRS if active else JOB_DIRS
        if not self._lock_depth:
            dirs += ("queue",)
        for i, d in enumerate(dirs):
            base = self.path(d)
            try:
                names = sorted(os.listdir(base))
            except FileNotFoundError:
                continue
            for name in names:
                if not JOBID_RE.match(name) or (i and d == "queue" and name in seen):
                    continue
                job, _ = try_read_json(os.path.join(base, name, "job.json"))
                if isinstance(job, dict):
                    seen.add(name)
                    yield os.path.join(base, name), cast(Job, cast(object, job))

    def save_job(self, jobdir: str, job: Job) -> str:
        """Write job.json then move the directory to match its state. Needs the state lock."""
        assert self._lock_depth, "state lock required"
        job["updated_at"] = utcnow_iso()
        atomic_write_json(os.path.join(jobdir, "job.json"), job)
        want = self.dir_for(job["state"], job["jobid"])
        if os.path.abspath(jobdir) != want:
            os.rename(jobdir, want)
            fsync_dir(os.path.dirname(jobdir))
            fsync_dir(os.path.dirname(want))
        return want

    def new_jobid(self) -> str:
        assert self._lock_depth
        while True:
            jid = secrets.token_hex(8)
            if self.find_job_dir(jid) is None:
                return jid

    def next_seq(self) -> int:
        assert self._lock_depth
        p = self.path("locks", "counters.json")
        raw, _ = try_read_json(p)
        c: JsonDict = raw if isinstance(raw, dict) else {}
        seq = c.get("next_enqueue_seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            # Missing or corrupt counter: continue after the highest sequence number in use.
            used: list[object] = [j.get("enqueue_seq") for _, j in self.iter_jobs()]
            seq = max((n for n in used if isinstance(n, int)), default=0) + 1
        c["next_enqueue_seq"] = seq + 1
        atomic_write_json(p, c)
        return seq

    # -- the run record (one run per job) -------------------------------------
    @staticmethod
    def read_runner(jobdir: str) -> tuple[Runner | None, str | None]:
        """(runner, None) or (None, error string from try_read_json / "not a JSON object")."""
        obj, err = try_read_json(os.path.join(jobdir, "runner.json"))
        if obj is None:
            return None, err
        if not isinstance(obj, dict):
            return None, "not a JSON object"
        return cast(Runner, cast(object, obj)), None

    @staticmethod
    def write_runner(jobdir: str, runner: Runner) -> None:
        atomic_write_json(os.path.join(jobdir, "runner.json"), runner)

    def set_runner_aside(self, jobdir: str) -> str | None:
        """Rename runner.json to the first free runner.prev-N.json (kept for inspection).

        The record of a GPU-busy exit replaces the last runner.prev-N.json when that one is
        a GPU-busy record too: a job that keeps exiting 75 keeps one such file, however
        often it is queued again (job.json gpu_busy_requeues counts the requeues)."""
        src = os.path.join(jobdir, "runner.json")
        if not os.path.exists(src):
            return None

        i = 1
        while os.path.lexists(os.path.join(jobdir, "runner.prev-%d.json" % i)):
            i += 1
        dst = os.path.join(jobdir, "runner.prev-%d.json" % i)
        if i > 1:
            last = os.path.join(jobdir, "runner.prev-%d.json" % (i - 1))
            if self._busy_record(src) and self._busy_record(last):
                dst = last

        os.rename(src, dst)
        fsync_dir(jobdir)
        return dst

    def _busy_record(self, path: str) -> bool:
        """Whether the run record at path shows a GPU-busy exit (see ended_gpu_busy)."""
        obj, _ = try_read_json(path)
        return isinstance(obj, dict) and self.ended_gpu_busy(cast(Runner, cast(object, obj)))

    # -- GPU busy exits (the entry could not get its GPU) ----------------------
    @property
    def gpu_busy_exit_code(self) -> int | None:
        """The exit code meaning "GPU busy, start me again"; None when disabled."""
        return self.config["gpu_busy_exit_code"]

    def ended_gpu_busy(self, runner: Runner) -> bool:
        """Whether a non-final run record shows an entry that exited for "GPU busy" and was
        not relaunched yet: the shim is between such an exit and the next launch, so no
        work was done and the job can simply be queued again."""
        if runner.get("final") or runner.get("phase") == PHASE_PRECOMPILE:
            return False  # a precompile has no GPU: its exit 75 is an ordinary failure
        if runner.get("status") == "waiting_gpu":
            return True
        code = self.gpu_busy_exit_code
        return bool(runner.get("exited")) and code is not None and runner.get("return_code") == code


def has_run(job: Job) -> bool:
    """Whether the job was ever dispatched (and so has a run record and run files)."""
    return job.get("dispatched_at") is not None


def precompiling(job: Job) -> bool:
    """Whether the job's current (or next) run is its precompile command."""
    return job.get("phase") == PHASE_PRECOMPILE


def initial_phase(task: TaskSpec) -> str | None:
    """The `phase` of a new job of this task."""
    return PHASE_PRECOMPILE if task.get("precompile") else None


def choosing_gpus(job: Job) -> bool:
    """Whether the scheduler must choose the job's GPUs before its next run: it has a
    gpu_request, no GPUs yet, and its next run is not a precompile (which needs none)."""
    return bool(job.get("gpu_request")) and not job.get("gpus") and not precompiling(job)


def held_back(job: Job, now: float, interval: float) -> bool:
    """Whether a job queued again after a GPU-busy exit must still wait before its next
    dispatch (`now` in epoch seconds). A wait longer than `interval` (the wall clock was set
    back, or the interval lowered) has passed."""
    not_before = job.get("not_before_epoch")
    return not_before is not None and 0 < not_before - now <= interval


def run_gpus(job: Job) -> list[GpuDecl]:
    """The GPUs the job's current (or next) run uses: none while it precompiles."""
    return [] if precompiling(job) else job.get("gpus", [])


def run_command(task: TaskSpec, job: Job) -> tuple[list[str], float | None]:
    """(argv, timeout_seconds) of the job's current (or next) run."""
    spec = task.get("precompile")
    if precompiling(job) and spec:
        return spec["argv"], spec.get("timeout_seconds")
    return task["entry"]["argv"], task.get("timeout_seconds")


def job_summary(job: Job) -> dict[str, object]:
    return {
        "jobid": job["jobid"],
        "task_id": job.get("task_id"),
        "state": job.get("state"),
        "submitted_at": job.get("submitted_at"),
        "dispatched_at": job.get("dispatched_at"),
        "enqueue_seq": job.get("enqueue_seq"),
        "cancel_requested": job.get("cancel_requested", False),
        "gpus": job.get("gpus", []),
        "error": job.get("error"),
        "retry_of": job.get("retry_of"),
        "retried_as": job.get("retried_as", []),
        "pending_reason": job.get("pending_reason"),
        "phase": job.get("phase"),
        "cancel_request": job.get("cancel_request"),
        "gpu_request": job.get("gpu_request"),
    }


def load_task(jobdir: str) -> TaskSpec:
    """task.json of a job (validated at submit time); {} when missing."""
    obj = read_json(os.path.join(jobdir, "package", "task.json"), {}) or {}
    return cast(TaskSpec, cast(object, obj))


# ---------------------------------------------------------------------------
# Results and finalization (Sections 10 and 11)


def _as_dict(value: object) -> JsonDict | None:
    return cast(JsonDict, value) if isinstance(value, dict) else None


def validate_outputs(result: object, task: TaskSpec) -> list[str]:
    errs: list[str] = []
    res = _as_dict(result)
    outputs = res.get("outputs") if res is not None else None
    if not isinstance(outputs, dict):
        errs.append("result.json has no 'outputs' object")
        return errs
    result_spec = task.get("result") or {}
    req: list[str] = result_spec.get("required_outputs") or []
    for k in req:
        if k not in outputs:
            errs.append("required output %r is missing" % k)
    return errs


def keep_aside(p: str) -> str:
    """Rename p (a corrupt result.json, or something that is not a regular file) to the
    first free p.corrupt[.N]; returns the new path."""
    keep, i = p + ".corrupt", 0
    while os.path.lexists(keep):
        i += 1
        keep = "%s.corrupt.%d" % (p, i)
    os.replace(p, keep)
    return keep


def read_task_result(workdir: str, own: str | None = None) -> tuple[JsonDict, list[str], list[str]]:
    """Read the task-written result.json per Section 10.3.

    Returns (business_fields, validation_errors, warnings). own=jobid recognizes a
    `_taskmgr` block written by an earlier, interrupted finalization so that
    re-finalizing does not report it as written by the task.
    """
    p = os.path.join(workdir, "result.json")
    warnings: list[str] = []
    verrs: list[str] = []
    try:
        raw = read_regular_file(p, follow_symlinks=False)
    except FileNotFoundError:
        return {"schema_version": SCHEMA_VERSION}, ["result.json was not written"], warnings
    except NotRegularFile:
        keep = keep_aside(p)
        return (
            {"schema_version": SCHEMA_VERSION},
            ["result.json is not a regular file (kept as %s)" % os.path.basename(keep)],
            warnings,
        )
    except OSError as e:
        return {"schema_version": SCHEMA_VERSION}, ["result.json unreadable: %s" % e], warnings
    try:
        parsed = cast(JsonValue, json.loads(raw.decode("utf-8")))
        if not isinstance(parsed, dict):
            raise ValueError("top level is not a JSON object")
    except (ValueError, UnicodeDecodeError, RecursionError) as e:
        if isinstance(e, RecursionError):
            e = ValueError("nested too deeply")
        keep = keep_aside(p)
        return (
            {"schema_version": SCHEMA_VERSION},
            ["result.json is corrupt (kept as %s): %s" % (os.path.basename(keep), e)],
            warnings,
        )
    obj = parsed
    if "_taskmgr" in obj:
        meta = _as_dict(obj.pop("_taskmgr"))
        ours = (
            own is not None
            and meta is not None
            and meta.get("final") is True
            and meta.get("jobid") == own
        )
        if not ours:
            warnings.append("task_wrote_taskmgr")
    _ = obj.setdefault("schema_version", SCHEMA_VERSION)
    return obj, verrs, warnings


def meta_block(
    job: Job,
    status: str,
    error: ErrorInfo | None,
    validation_errors: Iterable[str] = (),
    warnings: Iterable[str] = (),
    runner: Runner | None = None,
) -> TaskmgrMeta:
    """The `_taskmgr` keys every result.json has; `build_meta` adds those of a run."""
    run: Runner = runner or {"jobid": job["jobid"]}
    return {
        "jobid": job["jobid"],
        "task_id": job.get("task_id"),
        "retry_of": job.get("retry_of"),
        "gpus": job.get("gpus", []),
        "status": status,
        "return_code": run.get("return_code"),
        "signal": run.get("signal"),
        "validation_errors": list(validation_errors),
        "error": error,
        "warnings": list(warnings),
        "final": True,
    }


def build_meta(
    store: Store,
    job: Job,
    runner: Runner,
    status: str,
    validation_errors: Iterable[str],
    error: ErrorInfo | None,
    warnings: Iterable[str],
) -> TaskmgrMeta:
    jobdir = store.dir_for(status, job["jobid"])  # where the job lives once it is `status`
    workdir = os.path.join(jobdir, "workdir")
    meta = meta_block(job, status, error, validation_errors, warnings, runner)
    meta.update(
        {
            "host": socket.gethostname(),
            "started_at": runner.get("started_at"),
            "ended_at": runner.get("ended_at"),
            "paths": {
                "job_dir": jobdir,
                "workdir": workdir,
                "stdout": os.path.join(jobdir, "stdout.log"),
                "stderr": os.path.join(jobdir, "stderr.log"),
                "shim_log": os.path.join(jobdir, "shim.log"),
                "progress": os.path.join(workdir, "progress.json"),
                "result": os.path.join(workdir, "result.json"),
                "artifacts": os.path.join(workdir, "artifacts"),
            },
        }
    )
    return meta


def write_result(jobdir: str, business: JsonDict, meta: TaskmgrMeta) -> None:
    workdir = os.path.join(jobdir, "workdir")
    if not os.path.isdir(workdir):
        os.makedirs(workdir, exist_ok=True)
    res: dict[str, object] = dict(business)
    _ = res.pop("_taskmgr", None)
    _ = res.setdefault("schema_version", SCHEMA_VERSION)
    res["_taskmgr"] = meta
    p = os.path.join(workdir, "result.json")
    with contextlib.suppress(FileNotFoundError):
        if not stat.S_ISREG(os.lstat(p).st_mode):
            _ = keep_aside(p)  # os.replace cannot replace a directory
    atomic_write_json(p, res)


def _settled(job: Job) -> bool:
    """Terminal, or queued again: nothing may finalize or interrupt the job's run."""
    return job["state"] in TERMINAL_STATES or job["state"] == "queued"


def finalize_run(
    store: Store,
    jobid: str,
    *,
    status: str | None = None,
    error: ErrorInfo | None = None,
    warnings: Iterable[str] = (),
) -> Job:
    """Finish a run whose process group is confirmed gone (Section 10.4 steps 3-5).

    status None derives the state from return code, signal and the result
    (Section 11). Idempotent: a job that is already terminal is left untouched apart
    from repairing result.json; a queued job (sent back by a "GPU busy" exit) is left
    untouched. The state is checked before anything is written.
    """
    with store.state_lock():
        jobdir, job = store.load_job(jobid)
        if job["state"] == "queued":
            return job
        if status is None and precompile_succeeded(store, jobdir, job):
            if not job.get("cancel_requested"):
                return queue_for_gpus(store, jobdir, job)
            status = "cancelled"
    raw_runner, _ = store.read_runner(jobdir)
    runner: Runner = raw_runner or {"jobid": jobid}
    tm: object = runner.get("taskmgr")
    if runner.get("final") and isinstance(tm, dict):
        meta = tm
        if not result_complete(jobdir):
            business, _, _ = read_task_result(os.path.join(jobdir, "workdir"), own=jobid)
            write_result(jobdir, business, meta)
        status = meta["status"]
    elif _settled(job):
        return job
    else:
        task = load_task(jobdir)
        warns = list(runner.get("warnings", [])) + list(warnings)
        business, verrs, w2 = read_task_result(os.path.join(jobdir, "workdir"), own=jobid)
        warns += w2
        # A precompile writes no result: the entry does.
        verrs = [] if precompiling(job) else verrs or validate_outputs(business, task)
        if status is None:
            rc, sig = runner.get("return_code"), runner.get("signal")
            if sig is not None:
                status = "failed"
                error = error or {"kind": "signal", "message": "killed by signal %d" % sig}
            elif rc is None:
                status = "failed"
                error = error or {"kind": "exit_code_lost", "message": "exit code unavailable"}
            elif rc != 0:
                status = "failed"
                error = error or {"kind": "exit_code", "message": "exit code %d" % rc}
            elif verrs:
                status = "invalid_result"
            else:
                status = "succeeded"
        if status == "failed" and error and precompiling(job):
            error = {"kind": "precompile", "message": "precompile: %s" % error["message"]}
        warns = list(dict.fromkeys(warns))
        # Persist the step 2-3 findings first so a crash here does not lose them.
        runner["warnings"] = warns
        store.write_runner(jobdir, runner)
        meta = build_meta(store, job, runner, status, verrs, error, warns)
        write_result(jobdir, business, meta)
        runner.update(
            {
                "final": True,
                "status": status,
                "taskmgr": meta,
                "finalized_at": utcnow_iso(),
            }
        )
        store.write_runner(jobdir, runner)
    with store.state_lock():
        jobdir, job = store.load_job(jobid)
        if _settled(job):
            return job
        job["state"] = status
        job["error"] = meta.get("error")
        job["ended_at"] = runner.get("ended_at") or utcnow_iso()
        _ = job.pop("pending_reason", None)
        _ = store.save_job(jobdir, job)
    return job


def precompile_succeeded(store: Store, jobdir: str, job: Job) -> bool:
    """Whether the job's run was its precompile command and that exited 0."""
    runner, _ = store.read_runner(jobdir)
    return (
        precompiling(job)
        and job["state"] not in TERMINAL_STATES
        and runner is not None
        and not runner.get("final")
        and runner.get("return_code") == 0
        and runner.get("signal") is None
    )


def queue_for_gpus(store: Store, jobdir: str, job: Job) -> Job:
    """The precompile succeeded: queue the same job for its GPUs. Needs the lock.

    It keeps its jobid, enqueue_seq and workdir, and `dispatched_at`, so `task logs` shows
    the precompile's output. job.json first: a crash before the rename leaves a job in
    phase "gpu" with a precompile's runner.json, which dispatch sets aside as well.
    """
    job["state"] = "queued"
    job["phase"] = PHASE_GPU
    job["stop_reason"] = None
    job["error"] = None
    job["pending_reason"] = "precompiled; waiting for its GPUs"
    jobdir = store.save_job(jobdir, job)
    _ = store.set_runner_aside(jobdir)
    return job


def result_complete(jobdir: str) -> bool:
    obj, _ = try_read_json(os.path.join(jobdir, "workdir", "result.json"), follow_symlinks=False)
    res = _as_dict(obj)
    meta = _as_dict(res.get("_taskmgr")) if res is not None else None
    return meta is not None and meta.get("final") is True and "status" in meta


OPERATOR_MARKED_STOPPED = "operator_marked_stopped"  # `task resolve --as stopped`
OPERATOR_STOPPED_ERROR: ErrorInfo = {
    "kind": OPERATOR_MARKED_STOPPED,
    "message": "operator confirmed all processes are gone",
}


def interrupted_runner(store: Store, jobdir: str, jobid: str, reason: str) -> Runner:
    """The run record made final with status "interrupted" (not written)."""
    raw_runner, _ = store.read_runner(jobdir)
    runner: Runner = raw_runner or {"jobid": jobid}
    runner.update(
        {
            "final": True,
            "status": "interrupted",
            "interrupted_reason": reason,
            "ended_at": runner.get("ended_at") or utcnow_iso(),
        }
    )
    return runner


def mark_run_interrupted(store: Store, jobdir: str, jobid: str, reason: str) -> Runner:
    """Make the run record final with status "interrupted" (its processes are gone)."""
    runner = interrupted_runner(store, jobdir, jobid, reason)
    store.write_runner(jobdir, runner)
    return runner


def interrupt_run(store: Store, jobid: str, reason: str, *, rerun: bool = True) -> Job:
    """Section 12.2 step 5: mark the run interrupted, then cancel / fail / re-run.

    A re-run is a new job (an automatic retry, see `clone_job`); the interrupted job
    itself ends as failed with error.kind "interrupted" and points to it via
    `retried_as`. No re-run is created when a cancel was requested (cancelled), when the
    job was being stopped for its timeout (timed_out), when an operator marked the run
    stopped (failed / operator_marked_stopped), or with rerun=False: `task resolve` of an
    unknown job, which never re-runs (failed / interrupted; `task retry` re-runs it).
    Returns the interrupted job.
    """
    # The task's result.json is read before the lock is taken (the directory of a job
    # that is still active stays in in-progress/, so the path holds under the lock).
    found = store.find_job_dir(jobid)
    result = read_task_result(os.path.join(found, "workdir"), own=jobid) if found else None
    with store.state_lock():
        jobdir, job = store.load_job(jobid)
        if _settled(job):
            return job
        if found is None or os.path.abspath(found) != os.path.abspath(jobdir):
            result = None  # read again under the lock
        stop_reason = job.get("stop_reason") if job["state"] in ("stopping", "unknown") else None
        runner = mark_run_interrupted(store, jobdir, jobid, reason)
        if job.get("cancel_requested"):
            _ = end_without_result(store, jobdir, job, "cancelled", None, runner, result)
            return job
        if stop_reason == "timeout":
            _ = end_without_result(store, jobdir, job, "timed_out", None, runner, result)
            return job
        if reason == OPERATOR_MARKED_STOPPED:
            _ = end_without_result(
                store, jobdir, job, "failed", OPERATOR_STOPPED_ERROR, runner, result
            )
            return job
        # A crash after the clone was renamed into queue/ but before this job was saved
        # leaves the clone behind; reuse it instead of cloning twice. Only this step
        # creates a job with retry_of == jobid while jobid is not terminal.
        rerun_job = next((j for _, j in store.iter_jobs() if j.get("retry_of") == jobid), None)
        maxr = store.config["max_auto_restarts"]
        restarts = job.get("auto_restarts", 0)
        error: ErrorInfo = {"kind": "interrupted", "message": reason}
        if rerun_job is not None:
            if rerun_job["jobid"] not in job.get("retried_as", []):
                job["retried_as"] = job.get("retried_as", []) + [rerun_job["jobid"]]
        elif not rerun:
            error["message"] = "%s; not re-run (the job was unknown)" % reason
        elif restarts >= maxr:
            error = {
                "kind": "too_many_interruptions",
                "message": "interrupted %d times (max_auto_restarts=%d); last: %s"
                % (restarts + 1, maxr, reason),
            }
        else:
            _ = clone_job(store, jobdir, job, automatic=True)
            jobdir = cast(str, store.find_job_dir(jobid))
        _ = end_without_result(store, jobdir, job, "failed", error, runner, result)
        return job


def new_job_record(
    jobid: str,
    task_id: str,
    enqueue_seq: int,
    content_hash: str,
    submission_key: str,
    gpus: list[GpuDecl],
    auto_restarts: int = 0,
    retry_of: str | None = None,
    phase: str | None = None,
    gpu_request: GpuRequest | None = None,
) -> Job:
    """job.json of a job that has just been queued (a submission or a retry). A job with
    a gpu_request has no GPUs until the scheduler chooses them."""
    now = utcnow_iso()
    job: Job = {
        "schema_version": SCHEMA_VERSION,
        "jobid": jobid,
        "task_id": task_id,
        "state": "queued",
        "submitted_at": now,
        "updated_at": now,
        "enqueue_seq": enqueue_seq,
        "content_hash": content_hash,
        "submission_key": submission_key,
        "gpus": gpus,
        "cancel_requested": False,
        "auto_restarts": auto_restarts,
        "dispatched_at": None,
        "retry_of": retry_of,
        "retried_as": [],
        "stop_reason": None,
        "error": None,
    }
    if phase is not None:
        job["phase"] = phase
    if gpu_request is not None:
        job["gpu_request"] = gpu_request
        job["gpus"] = []
    return job


# ---------------------------------------------------------------------------
# Staging: a new job directory is built under incoming/, then renamed into queue/


@contextlib.contextmanager
def staging_dir(store: Store, prefix: str) -> Generator[str, None, None]:
    """A new directory incoming/<prefix>-<random>, flocked while in use (which keeps the
    daemon's clean_incoming away) and removed on exit unless it was enqueued."""
    updir = store.path("incoming", "%s-%s" % (prefix, secrets.token_hex(6)))
    os.mkdir(updir)
    lock_fd = os.open(updir, os.O_RDONLY)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield updir
    finally:
        os.close(lock_fd)
        if os.path.exists(updir):
            remove_tree(updir)


def sync_tree(root: str) -> None:
    """fsync every regular file and directory under root (root included)."""
    for dirpath, _dirnames, filenames in os.walk(root):
        for f in filenames:
            p = os.path.join(dirpath, f)
            if os.path.islink(p):
                continue
            fd = os.open(p, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        fsync_dir(dirpath)


def _link_or_copy(src: str, dst: str) -> None:
    try:
        os.link(src, dst)
    except OSError:
        _ = shutil.copy2(src, dst)


def link_tree(src: str, dst: str) -> None:
    """Recreate src at the new path dst: directories (same modes) and symlinks are
    created, regular files hard-linked (copied where linking fails). Only for content
    that is never modified, such as a job's package: a link shares the file."""
    made: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        target = dst if rel == "." else os.path.join(dst, rel)
        os.mkdir(target, 0o700)
        made.append((dirpath, target))
        for name in dirnames + filenames:
            s, t = os.path.join(dirpath, name), os.path.join(target, name)
            if os.path.islink(s):
                os.symlink(os.readlink(s), t)
            elif name in filenames:
                _link_or_copy(s, t)
    for s, t in reversed(made):
        os.chmod(t, stat.S_IMODE(os.lstat(s).st_mode))


def stage_package(src_jobdir: str, updir: str) -> None:
    """Put the package of an existing job into a staging directory, durably."""
    _link_or_copy(os.path.join(src_jobdir, "package.tar.gz"), os.path.join(updir, "package.tar.gz"))
    pkg = os.path.join(updir, "package")
    link_tree(os.path.join(src_jobdir, "package"), pkg)
    make_read_only(pkg)
    sync_tree(updir)  # package/ is the source of the run: durable before the job is visible


def enqueue_staged(store: Store, updir: str, job: Job) -> None:
    """Write job.json into a staging directory and rename it into queue/. Needs the lock."""
    atomic_write_json(os.path.join(updir, "job.json"), job)
    os.rename(updir, store.path("queue", job["jobid"]))
    fsync_dir(store.path("incoming"))
    fsync_dir(store.path("queue"))


def clone_job(
    store: Store, old_jobdir: str, old: Job, *, automatic: bool, staged: str | None = None
) -> Job:
    """Create a new queued job that runs the same package again. Needs the state lock.

    `task retry` (automatic=False) puts the new job at the back of the queue; an
    automatic re-run after an interruption keeps the original queue position and counts
    the restart. The new job is built in a staging directory and renamed into queue/,
    like a submission; then the old job records the new jobid in `retried_as` and is
    saved. `staged` is a directory `stage_package` filled before the lock was taken;
    without one the package is staged here (hard links, so cheap under the lock).
    """
    if staged is None:
        store.ensure_layout()
        with staging_dir(store, "retry") as updir:
            stage_package(old_jobdir, updir)
            return clone_job(store, old_jobdir, old, automatic=automatic, staged=updir)
    jobid = store.new_jobid()  # asserts the state lock
    job = new_job_record(
        jobid,
        old["task_id"],
        old["enqueue_seq"] if automatic else store.next_seq(),
        old["content_hash"],
        old["submission_key"],
        old["gpus"],
        auto_restarts=old.get("auto_restarts", 0) + 1 if automatic else 0,
        retry_of=old["jobid"],
        phase=initial_phase(load_task(old_jobdir)),  # a new job has an empty workdir
        gpu_request=old.get("gpu_request"),  # chooses its GPUs again
    )
    enqueue_staged(store, staged, job)
    old["retried_as"] = old.get("retried_as", []) + [jobid]
    _ = store.save_job(old_jobdir, old)
    return job


def end_without_result(
    store: Store,
    jobdir: str,
    job: Job,
    state: str,
    error: ErrorInfo | None,
    runner: Runner | None,
    result: tuple[JsonDict, list[str], list[str]] | None = None,
) -> str:
    """Move a job to a terminal state without a normal exit of its run. Needs the lock.

    With a runner (the job was dispatched), result.json gets a `_taskmgr` block; a job
    that never ran (runner None) has no run files and gets none. `result` is what
    read_task_result returned for the workdir before the lock was taken; without it
    result.json is read here.
    """
    if runner is not None:
        business, _, w = result or read_task_result(
            os.path.join(jobdir, "workdir"), own=job["jobid"]
        )
        meta = build_meta(
            store, job, runner, state, [], error, list(runner.get("warnings", [])) + w
        )
        write_result(jobdir, business, meta)
        if not runner.get("taskmgr"):
            runner["taskmgr"] = meta
            store.write_runner(jobdir, runner)
    job["state"] = state
    job["error"] = error
    job["ended_at"] = utcnow_iso()
    _ = job.pop("pending_reason", None)
    return store.save_job(jobdir, job)


def make_read_only(root: str) -> None:
    for dirpath, _dirnames, filenames in os.walk(root, topdown=False):
        for f in filenames:
            p = os.path.join(dirpath, f)
            if not os.path.islink(p):
                os.chmod(p, stat.S_IMODE(os.lstat(p).st_mode) & ~0o222)
        os.chmod(dirpath, stat.S_IMODE(os.lstat(dirpath).st_mode) & ~0o222)


def remove_tree(path: str) -> None:
    """rmtree that also handles read-only package copies. Only directories are made
    writable: a package file may be a hard link shared with another job's package."""
    for dirpath, _dirnames, _filenames in os.walk(path):
        with contextlib.suppress(OSError):
            os.chmod(dirpath, 0o700)

    def onerror(func: Callable[[str], object], p: str, _exc: object) -> None:
        try:
            os.chmod(os.path.dirname(p), 0o700)
            _ = func(p)
        except OSError:
            pass

    shutil.rmtree(path, onerror=onerror)
