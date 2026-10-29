"""Remote storage root: layout, config, state lock, job records, and attempt finalization."""

import contextlib
import fcntl
import json
import os
import secrets
import shutil
import socket
import stat

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


class Store:
    def __init__(self, root):
        if not root or not os.path.isabs(root):
            raise TaskmgrError("bad_root", "--root must be an absolute path: %r" % (root,))
        self.root = root
        self._lock_fd = None
        self._lock_depth = 0
        self._config = None

    # -- layout -------------------------------------------------------------
    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def ensure_layout(self):
        for d in ("locks", "incoming", "submission-keys", "daemon") + JOB_DIRS:
            os.makedirs(self.path(d), exist_ok=True)

    @property
    def config(self):
        if self._config is None:
            cfg = dict(DEFAULT_REMOTE_CONFIG)
            obj, err = try_read_json(self.path("config.json"))
            if err and err != "missing":
                raise TaskmgrError("bad_config", "config.json is %s" % err)
            if obj:
                cfg.update(obj)
            self._config = cfg
        return self._config

    # -- state lock (re-entrant within one process) --------------------------
    @contextlib.contextmanager
    def state_lock(self):
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
    def find_job_dir(self, jobid):
        if not isinstance(jobid, str) or not JOBID_RE.match(jobid):
            return None
        for d in JOB_DIRS:
            p = self.path(d, jobid)
            if os.path.isdir(p):
                return p
        return None

    def load_job(self, jobid):
        jd = self.find_job_dir(jobid)
        if jd is None:
            raise TaskmgrError("not_found", "job %s not found" % jobid)
        job, err = try_read_json(os.path.join(jd, "job.json"))
        if job is None:
            raise TaskmgrError("corrupt_job", "job.json of %s is %s" % (jobid, err))
        return jd, job

    def iter_jobs(self):
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
                    yield os.path.join(base, name), job

    def save_job(self, jobdir, job):
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

    def new_jobid(self):
        assert self._lock_depth
        while True:
            jid = secrets.token_hex(8)
            if self.find_job_dir(jid) is None:
                return jid

    def next_seq(self):
        assert self._lock_depth
        p = self.path("locks", "counters.json")
        c = read_json(p, {}) or {}
        seq = int(c.get("next_enqueue_seq", 1))
        c["next_enqueue_seq"] = seq + 1
        atomic_write_json(p, c)
        return seq

    # -- attempts -----------------------------------------------------------
    @staticmethod
    def attempt_dir(jobdir, n):
        return os.path.join(jobdir, "attempts", "%04d" % n)

    @staticmethod
    def existing_attempts(jobdir):
        try:
            return sorted(
                int(n) for n in os.listdir(os.path.join(jobdir, "attempts")) if n.isdigit()
            )
        except FileNotFoundError:
            return []

    @staticmethod
    def read_runner(adir):
        return try_read_json(os.path.join(adir, "runner.json"))

    @staticmethod
    def write_runner(adir, runner):
        atomic_write_json(os.path.join(adir, "runner.json"), runner)


def job_summary(job):
    return {
        "jobid": job["jobid"],
        "task_id": job.get("task_id"),
        "state": job.get("state"),
        "attempt": job.get("current_attempt"),
        "submitted_at": job.get("submitted_at"),
        "enqueue_seq": job.get("enqueue_seq"),
        "cancel_requested": job.get("cancel_requested", False),
        "gpus": job.get("gpus", []),
        "error": job.get("error"),
    }


def load_task(jobdir):
    return read_json(os.path.join(jobdir, "package", "task.json"), {}) or {}


# ---------------------------------------------------------------------------
# Results and finalization (Sections 10 and 11)


def validate_outputs(result, task):
    errs = []
    outputs = result.get("outputs") if isinstance(result, dict) else None
    if not isinstance(outputs, dict):
        errs.append("result.json has no 'outputs' object")
        return errs
    req = (task.get("result") or {}).get("required_outputs") or []
    for k in req:
        if k not in outputs:
            errs.append("required output %r is missing" % k)
    return errs


def read_task_result(workdir, own=None):
    """Read the task-written result.json per Section 10.3.

    Returns (business_fields, validation_errors, warnings). own=(jobid, attempt)
    recognizes a `_taskmgr` block written by an earlier, interrupted finalization
    so that re-finalizing does not report it as written by the task.
    """
    p = os.path.join(workdir, "result.json")
    warnings, verrs = [], []
    try:
        with open(p, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return {"schema_version": SCHEMA_VERSION}, ["result.json was not written"], warnings
    except OSError as e:
        return {"schema_version": SCHEMA_VERSION}, ["result.json unreadable: %s" % e], warnings
    try:
        obj = json.loads(raw.decode("utf-8"))
        if not isinstance(obj, dict):
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
    if "_taskmgr" in obj:
        meta = obj.pop("_taskmgr")
        ours = (
            own is not None
            and isinstance(meta, dict)
            and meta.get("final") is True
            and (meta.get("jobid"), meta.get("attempt")) == tuple(own)
        )
        if not ours:
            warnings.append("task_wrote_taskmgr")
    obj.setdefault("schema_version", SCHEMA_VERSION)
    return obj, verrs, warnings


def final_attempt_dir(adir, status):
    """Where the attempt directory lives once the job is in `status` (done/ for terminal)."""
    jobdir = os.path.dirname(os.path.dirname(adir))
    root = os.path.dirname(os.path.dirname(jobdir))
    return os.path.join(
        root,
        STATE_DIRS.get(status, "in-progress"),
        os.path.basename(jobdir),
        "attempts",
        os.path.basename(adir),
    )


def build_meta(job, runner, adir, status, validation_errors, error, warnings):
    adir = final_attempt_dir(adir, status)
    workdir = os.path.join(adir, "workdir")
    return {
        "jobid": job["jobid"],
        "task_id": job.get("task_id"),
        "attempt": runner.get("attempt"),
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
            "attempt_dir": adir,
            "workdir": workdir,
            "stdout": os.path.join(adir, "stdout.log"),
            "stderr": os.path.join(adir, "stderr.log"),
            "shim_log": os.path.join(adir, "shim.log"),
            "progress": os.path.join(workdir, "progress.json"),
            "result": os.path.join(workdir, "result.json"),
            "artifacts": os.path.join(workdir, "artifacts"),
        },
        "final": True,
    }


def write_result(adir, business, meta):
    workdir = os.path.join(adir, "workdir")
    if not os.path.isdir(workdir):
        os.makedirs(workdir, exist_ok=True)
    res = dict(business)
    res.pop("_taskmgr", None)
    res.setdefault("schema_version", SCHEMA_VERSION)
    res["_taskmgr"] = meta
    atomic_write_json(os.path.join(workdir, "result.json"), res)


def finalize_attempt(
    store, jobid, n, *, status=None, error=None, warnings=(), attempt_status=None, job_extra=None
):
    """Finish an attempt whose process group is confirmed gone (Section 10.4 steps 3-5).

    status None derives the state from return code, signal and the result
    (Section 11). Idempotent: a job no longer on attempt n or already terminal
    is left untouched apart from repairing result.json.
    """
    jobdir, job = store.load_job(jobid)
    adir = store.attempt_dir(jobdir, n)
    runner, _ = store.read_runner(adir)
    runner = runner or {"attempt": n}
    task = load_task(jobdir)
    warnings = list(runner.get("warnings", [])) + list(warnings)
    if runner.get("final") and isinstance(runner.get("taskmgr"), dict):
        meta = runner["taskmgr"]
        if not _result_complete(adir):
            business, _, _ = read_task_result(os.path.join(adir, "workdir"), own=(jobid, n))
            write_result(adir, business, meta)
        status = meta["status"]
    else:
        business, verrs, w2 = read_task_result(os.path.join(adir, "workdir"), own=(jobid, n))
        warnings += w2
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
        warnings = list(dict.fromkeys(warnings))
        # Persist the step 2-3 findings first so a crash here does not lose them.
        runner["warnings"] = warnings
        store.write_runner(adir, runner)
        meta = build_meta(job, runner, adir, status, verrs, error, warnings)
        write_result(adir, business, meta)
        runner.update(
            {
                "final": True,
                "status": attempt_status or status,
                "taskmgr": meta,
                "finalized_at": utcnow_iso(),
            }
        )
        store.write_runner(adir, runner)
    with store.state_lock():
        jobdir, job = store.load_job(jobid)
        if (
            job.get("current_attempt") != n
            or job["state"] in TERMINAL_STATES
            or job["state"] == "queued"
        ):
            return job
        job["state"] = status
        job["error"] = meta.get("error")
        job["ended_at"] = runner.get("ended_at") or utcnow_iso()
        if job_extra:
            job.update(job_extra)
        for a in job.get("attempts", []):
            if a.get("attempt") == n:
                a["status"] = runner.get("status", status)
                a["ended_at"] = job["ended_at"]
        store.save_job(jobdir, job)
    return job


def _result_complete(adir):
    obj, _ = try_read_json(os.path.join(adir, "workdir", "result.json"))
    meta = obj.get("_taskmgr") if isinstance(obj, dict) else None
    return isinstance(meta, dict) and meta.get("final") is True and "status" in meta


def requeue_or_end_interrupted(store, jobid, n, reason):
    """Section 12.2 step 5: mark attempt interrupted, then cancel / fail / requeue."""
    with store.state_lock():
        jobdir, job = store.load_job(jobid)
        if (
            job["state"] in TERMINAL_STATES
            or job["state"] == "queued"
            or job.get("current_attempt") != n
        ):
            return job
        adir = store.attempt_dir(jobdir, n)
        runner, _ = store.read_runner(adir)
        runner = runner or {"attempt": n}
        runner.update(
            {
                "final": True,
                "status": "interrupted",
                "interrupted_reason": reason,
                "ended_at": runner.get("ended_at") or utcnow_iso(),
            }
        )
        if os.path.isdir(adir):
            store.write_runner(adir, runner)
        for a in job.get("attempts", []):
            if a.get("attempt") == n:
                a["status"] = "interrupted"
                a["ended_at"] = runner["ended_at"]
        maxr = int(store.config.get("max_auto_restarts", 2))
        if job.get("cancel_requested"):
            end_without_result(store, jobdir, job, n, "cancelled", None, runner)
        elif job.get("auto_restarts", 0) >= maxr:
            end_without_result(
                store,
                jobdir,
                job,
                n,
                "failed",
                {
                    "kind": "too_many_interruptions",
                    "message": "interrupted %d times (max_auto_restarts=%d); last: %s"
                    % (job.get("auto_restarts", 0) + 1, maxr, reason),
                },
                runner,
            )
        else:
            job["auto_restarts"] = job.get("auto_restarts", 0) + 1
            job["state"] = "queued"
            job["current_attempt"] = None
            job["stop_reason"] = None
            store.save_job(jobdir, job)
        return job


def end_without_result(store, jobdir, job, n, state, error, runner):
    """Move a job to a terminal state for an attempt without a normal exit. Needs the lock."""
    if n is not None:
        adir = store.attempt_dir(jobdir, n)
        if os.path.isdir(adir):
            business, _, w = read_task_result(os.path.join(adir, "workdir"))
            meta = build_meta(
                job,
                runner or {"attempt": n},
                adir,
                state,
                [],
                error,
                list((runner or {}).get("warnings", [])) + w,
            )
            write_result(adir, business, meta)
            if runner is not None and not runner.get("taskmgr"):
                runner["taskmgr"] = meta
                store.write_runner(adir, runner)
    job["state"] = state
    job["error"] = error
    job["ended_at"] = utcnow_iso()
    return store.save_job(jobdir, job)


def remove_tree(path):
    """rmtree that also handles read-only package copies."""

    def onerror(func, p, _exc):
        try:
            os.chmod(os.path.dirname(p), 0o700)
            os.chmod(p, stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)
            func(p)
        except OSError:
            pass

    shutil.rmtree(path, onerror=onerror)
