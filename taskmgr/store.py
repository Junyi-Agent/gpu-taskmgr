"""Remote storage root: layout, config, state lock, job records, finalization, retry clones."""

import contextlib
import fcntl
import json
import os
import secrets
import shutil
import socket
import stat
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping
from typing import cast

from .common import (
    DEFAULT_REMOTE_CONFIG,
    JOB_DIRS,
    JOBID_RE,
    SCHEMA_VERSION,
    STATE_DIRS,
    TERMINAL_STATES,
    TaskmgrError,
    atomic_write_json,
    fsync_dir,
    read_json,
    try_read_json,
    utcnow_iso,
)
from .records import (
    ErrorInfo,
    Job,
    JsonDict,
    JsonValue,
    RemoteConfig,
    Runner,
    TaskmgrMeta,
    TaskSpec,
)


class Store:
    def __init__(self, root: str) -> None:
        if not root or not os.path.isabs(root):
            raise TaskmgrError("bad_root", "--root must be an absolute path: %r" % (root,))
        self.root: str = root
        self._lock_fd: int | None = None
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
            elif obj:
                raise TaskmgrError("bad_config", "config.json is not a JSON object")
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
    def find_job_dir(self, jobid: object) -> str | None:
        if not isinstance(jobid, str) or not JOBID_RE.match(jobid):
            return None
        for d in JOB_DIRS:
            p = self.path(d, jobid)
            if os.path.isdir(p):
                return p
        return None

    def load_job(self, jobid: str) -> tuple[str, Job]:
        jd = self.find_job_dir(jobid)
        if jd is None:
            raise TaskmgrError("not_found", "job %s not found" % jobid)
        job, err = try_read_json(os.path.join(jd, "job.json"))
        if job is None:
            raise TaskmgrError("corrupt_job", "job.json of %s is %s" % (jobid, err))
        return jd, cast(Job, cast(object, job))

    def iter_jobs(self) -> Iterator[tuple[str, Job]]:
        """Yield (dir, job) for every job; skips unreadable records."""
        for d in JOB_DIRS:
            base = self.path(d)
            try:
                names = sorted(os.listdir(base))
            except FileNotFoundError:
                continue
            for name in names:
                if not JOBID_RE.match(name):
                    continue
                job, _ = try_read_json(os.path.join(base, name, "job.json"))
                if isinstance(job, dict):
                    yield os.path.join(base, name), cast(Job, cast(object, job))

    def save_job(self, jobdir: str, job: Job) -> str:
        """Write job.json then move the directory to match its state. Needs the state lock."""
        assert self._lock_depth, "state lock required"
        job["updated_at"] = utcnow_iso()
        atomic_write_json(os.path.join(jobdir, "job.json"), job)
        want = self.path(STATE_DIRS[job["state"]], job["jobid"])
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
        raw = read_json(p, {}) or {}
        c: JsonDict = raw if isinstance(raw, dict) else {}
        seq = int(cast("int | float | str", c.get("next_enqueue_seq", 1)))
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


def has_run(job: Job) -> bool:
    """Whether the job was ever dispatched (and so has a run record and run files)."""
    return job.get("dispatched_at") is not None


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
        with open(p, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return {"schema_version": SCHEMA_VERSION}, ["result.json was not written"], warnings
    except OSError as e:
        return {"schema_version": SCHEMA_VERSION}, ["result.json unreadable: %s" % e], warnings
    try:
        parsed = cast(JsonValue, json.loads(raw.decode("utf-8")))
        if not isinstance(parsed, dict):
            raise ValueError("top level is not a JSON object")
    except (ValueError, UnicodeDecodeError) as e:
        keep, i = p + ".corrupt", 0
        while os.path.lexists(keep):
            i += 1
            keep = "%s.corrupt.%d" % (p, i)
        os.replace(p, keep)
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


def final_job_dir(jobdir: str, status: str) -> str:
    """Where the job directory lives once the job is in `status` (done/ for terminal)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(jobdir)))
    return os.path.join(root, STATE_DIRS.get(status, "in-progress"), os.path.basename(jobdir))


def build_meta(
    job: Job,
    runner: Runner,
    jobdir: str,
    status: str,
    validation_errors: Iterable[str],
    error: ErrorInfo | None,
    warnings: Iterable[str],
) -> TaskmgrMeta:
    jobdir = final_job_dir(jobdir, status)
    workdir = os.path.join(jobdir, "workdir")
    return {
        "jobid": job["jobid"],
        "task_id": job.get("task_id"),
        "retry_of": job.get("retry_of"),
        "host": socket.gethostname(),
        "gpus": job.get("gpus", []),
        "status": status,
        "return_code": runner.get("return_code"),
        "signal": runner.get("signal"),
        "started_at": runner.get("started_at"),
        "ended_at": runner.get("ended_at"),
        "validation_errors": list(validation_errors),
        "error": error,
        "warnings": list(warnings),
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
        "final": True,
    }


def write_result(jobdir: str, business: JsonDict, meta: TaskmgrMeta) -> None:
    workdir = os.path.join(jobdir, "workdir")
    if not os.path.isdir(workdir):
        os.makedirs(workdir, exist_ok=True)
    res: dict[str, object] = dict(business)
    _ = res.pop("_taskmgr", None)
    _ = res.setdefault("schema_version", SCHEMA_VERSION)
    res["_taskmgr"] = meta
    atomic_write_json(os.path.join(workdir, "result.json"), res)


def finalize_run(
    store: Store,
    jobid: str,
    *,
    status: str | None = None,
    error: ErrorInfo | None = None,
    warnings: Iterable[str] = (),
    run_status: str | None = None,
    job_extra: Mapping[str, JsonValue] | None = None,
) -> Job:
    """Finish a run whose process group is confirmed gone (Section 10.4 steps 3-5).

    status None derives the state from return code, signal and the result
    (Section 11). Idempotent: a job that is already terminal (or queued) is left
    untouched apart from repairing result.json.
    """
    jobdir, job = store.load_job(jobid)
    raw_runner, _ = store.read_runner(jobdir)
    runner: Runner = raw_runner or {"jobid": jobid}
    task = load_task(jobdir)
    warns = list(runner.get("warnings", [])) + list(warnings)
    tm: object = runner.get("taskmgr")
    if runner.get("final") and isinstance(tm, dict):
        meta = tm
        if not result_complete(jobdir):
            business, _, _ = read_task_result(os.path.join(jobdir, "workdir"), own=jobid)
            write_result(jobdir, business, meta)
        status = meta["status"]
    else:
        business, verrs, w2 = read_task_result(os.path.join(jobdir, "workdir"), own=jobid)
        warns += w2
        verrs = verrs or validate_outputs(business, task)
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
        warns = list(dict.fromkeys(warns))
        # Persist the step 2-3 findings first so a crash here does not lose them.
        runner["warnings"] = warns
        store.write_runner(jobdir, runner)
        meta = build_meta(job, runner, jobdir, status, verrs, error, warns)
        write_result(jobdir, business, meta)
        runner.update(
            {
                "final": True,
                "status": run_status or status,
                "taskmgr": meta,
                "finalized_at": utcnow_iso(),
            }
        )
        store.write_runner(jobdir, runner)
    with store.state_lock():
        jobdir, job = store.load_job(jobid)
        if job["state"] in TERMINAL_STATES or job["state"] == "queued":
            return job
        job["state"] = status
        job["error"] = meta.get("error")
        job["ended_at"] = runner.get("ended_at") or utcnow_iso()
        if job_extra:
            cast(dict[str, object], cast(object, job)).update(job_extra)
        _ = store.save_job(jobdir, job)
    return job


def result_complete(jobdir: str) -> bool:
    obj, _ = try_read_json(os.path.join(jobdir, "workdir", "result.json"))
    res = _as_dict(obj)
    meta = _as_dict(res.get("_taskmgr")) if res is not None else None
    return meta is not None and meta.get("final") is True and "status" in meta


def interrupt_run(store: Store, jobid: str, reason: str) -> Job:
    """Section 12.2 step 5: mark the run interrupted, then cancel / fail / re-run.

    A re-run is a new job (an automatic retry, see `clone_job`); the interrupted job
    itself ends as failed with error.kind "interrupted" and points to it via
    `retried_as`. Returns the interrupted job.
    """
    with store.state_lock():
        jobdir, job = store.load_job(jobid)
        if job["state"] in TERMINAL_STATES or job["state"] == "queued":
            return job
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
        store.write_runner(jobdir, runner)
        maxr = int(store.config.get("max_auto_restarts", 2))
        restarts = job.get("auto_restarts", 0)
        if job.get("cancel_requested"):
            _ = end_without_result(store, jobdir, job, "cancelled", None, runner)
        elif restarts >= maxr:
            _ = end_without_result(
                store,
                jobdir,
                job,
                "failed",
                {
                    "kind": "too_many_interruptions",
                    "message": "interrupted %d times (max_auto_restarts=%d); last: %s"
                    % (restarts + 1, maxr, reason),
                },
                runner,
            )
        else:
            # A crash after the clone was renamed into queue/ but before this job was
            # saved leaves the clone behind; reuse it instead of cloning twice. Only this
            # step creates a job with retry_of == jobid while jobid is not terminal.
            rerun = next(
                (j for _, j in store.iter_jobs() if j.get("retry_of") == jobid),
                None,
            )
            if rerun is None:
                rerun = clone_job(store, jobdir, job, automatic=True)
            elif rerun["jobid"] not in job.get("retried_as", []):
                job["retried_as"] = job.get("retried_as", []) + [rerun["jobid"]]
            jobdir = cast(str, store.find_job_dir(jobid))
            _ = end_without_result(
                store, jobdir, job, "failed", {"kind": "interrupted", "message": reason}, runner
            )
        return job


def clone_job(store: Store, old_jobdir: str, old: Job, *, automatic: bool) -> Job:
    """Create a new queued job that runs the same package again. Needs the state lock.

    `task retry` (automatic=False) puts the new job at the back of the queue; an
    automatic re-run after an interruption keeps the original queue position and counts
    the restart. The new job is built under incoming/ and renamed into queue/, like a
    submission; then the old job records the new jobid in `retried_as` and is saved.
    """
    store.ensure_layout()
    jobid = store.new_jobid()  # asserts the state lock
    updir = store.path("incoming", "retry-%s" % jobid)
    os.mkdir(updir)
    lock_fd = os.open(updir, os.O_RDONLY)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)  # keeps clean_incoming away while copying
    try:
        _ = shutil.copy2(
            os.path.join(old_jobdir, "package.tar.gz"), os.path.join(updir, "package.tar.gz")
        )
        _ = shutil.copytree(
            os.path.join(old_jobdir, "package"), os.path.join(updir, "package"), symlinks=True
        )
        make_read_only(os.path.join(updir, "package"))
        os.sync()  # package/ is the source of the run: durable before the job is visible
        now = utcnow_iso()
        job: Job = {
            "schema_version": SCHEMA_VERSION,
            "jobid": jobid,
            "task_id": old["task_id"],
            "state": "queued",
            "submitted_at": now,
            "updated_at": now,
            "enqueue_seq": old["enqueue_seq"] if automatic else store.next_seq(),
            "content_hash": old["content_hash"],
            "submission_key": old["submission_key"],
            "gpus": old["gpus"],
            "cancel_requested": False,
            "auto_restarts": old.get("auto_restarts", 0) + 1 if automatic else 0,
            "dispatched_at": None,
            "retry_of": old["jobid"],
            "retried_as": [],
            "stop_reason": None,
            "error": None,
        }
        atomic_write_json(os.path.join(updir, "job.json"), job)
        os.rename(updir, store.path("queue", jobid))
        fsync_dir(store.path("incoming"))
        fsync_dir(store.path("queue"))
    finally:
        os.close(lock_fd)
        if os.path.exists(updir):
            remove_tree(updir)
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
) -> str:
    """Move a job to a terminal state without a normal exit of its run. Needs the lock.

    With a runner (the job was dispatched), result.json gets a `_taskmgr` block; a job
    that never ran (runner None) has no run files and gets none.
    """
    if runner is not None:
        business, _, w = read_task_result(os.path.join(jobdir, "workdir"))
        meta = build_meta(
            job, runner, jobdir, state, [], error, list(runner.get("warnings", [])) + w
        )
        write_result(jobdir, business, meta)
        if not runner.get("taskmgr"):
            runner["taskmgr"] = meta
            store.write_runner(jobdir, runner)
    job["state"] = state
    job["error"] = error
    job["ended_at"] = utcnow_iso()
    return store.save_job(jobdir, job)


def make_read_only(root: str) -> None:
    for dirpath, _dirnames, filenames in os.walk(root, topdown=False):
        for f in filenames:
            p = os.path.join(dirpath, f)
            if not os.path.islink(p):
                os.chmod(p, stat.S_IMODE(os.lstat(p).st_mode) & ~0o222)
        os.chmod(dirpath, stat.S_IMODE(os.lstat(dirpath).st_mode) & ~0o222)


def remove_tree(path: str) -> None:
    """rmtree that also handles read-only package copies."""

    def onerror(func: Callable[[str], object], p: str, _exc: object) -> None:
        try:
            os.chmod(os.path.dirname(p), 0o700)
            os.chmod(p, stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)
            _ = func(p)
        except OSError:
            pass

    shutil.rmtree(path, onerror=onerror)
