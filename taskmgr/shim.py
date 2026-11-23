"""Run supervisor (shim): one detached process per job run (Sections 1.1, 3, 10.4, 12.1)."""

import hashlib
import json
import os
import pwd
import shutil
import signal
import stat
import sys
import time
from typing import cast

from . import gpu, procutil
from .common import PHASE_GPU, PHASE_PRECOMPILE, TaskmgrError, utcnow_iso
from .monitor import (
    STOP_OUTCOME,
    GroupStopper,
    end_cancelled,
    may_have_started,
    record_unconfirmed,
    requested_stop,
    requeue_gpu_busy,
    set_stopping,
)
from .records import ErrorInfo, Job, RemoteConfig, Runner, TaskEnvironment, TaskSpec
from .store import (
    PRIVATE_DIR,
    PRIVATE_FILE,
    Store,
    end_without_result,
    finalize_run,
    load_task,
    precompiling,
    run_command,
    run_gpus,
)


def log(msg: str) -> None:
    _ = sys.stderr.write("%s shim[%d] %s\n" % (utcnow_iso(), os.getpid(), msg))
    _ = sys.stderr.flush()


def _crash_point(name: str) -> None:
    # TASKMGR_TEST_SHIM_CRASH: test-only hook that kills the shim at a named point.
    if os.environ.get("TASKMGR_TEST_SHIM_CRASH") == name:
        log("test crash at %s" % name)
        os._exit(99)


def build_env(
    cfg: RemoteConfig, task: TaskSpec, job: Job, workdir: str, busy_code: int | None
) -> dict[str, str]:
    """Entry-process environment, built in the order of Section 3.2.

    TASKMGR_GPU_BUSY_EXIT_CODE is the configured gpu_busy_exit_code; it is removed (even
    when base_env sets it) when the server turned that convention off. A precompile runs without GPUs and without
    that convention."""
    try:
        pw = pwd.getpwuid(os.getuid())
        home, user = pw.pw_dir, pw.pw_name
    except KeyError:
        home, user = os.environ.get("HOME", "/"), os.environ.get("USER", "")
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": home, "USER": user, "LANG": "C.UTF-8"}
    base_env: dict[str, str] = cfg["base_env"] or {}
    env.update({str(k): str(v) for k, v in base_env.items()})
    environment: TaskEnvironment = task.get("environment") or {}
    venv = environment.get("venv")
    if venv:
        env["VIRTUAL_ENV"] = venv
        env["PATH"] = os.path.join(venv, "bin") + (":" + env["PATH"] if env.get("PATH") else "")
    task_env: dict[str, str] = task.get("env") or {}
    env.update(task_env)
    env.update(
        {
            "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
            "CUDA_VISIBLE_DEVICES": ",".join(g["uuid"] for g in run_gpus(job)),
            "TASKMGR_JOBID": job["jobid"],
            "TASKMGR_TASK_ID": job["task_id"],
            "TASKMGR_WORKDIR": workdir,
            "TASKMGR_PROGRESS_FILE": os.path.join(workdir, "progress.json"),
            "TASKMGR_RESULT_FILE": os.path.join(workdir, "result.json"),
        }
    )
    if busy_code is not None and not precompiling(job):
        env["TASKMGR_GPU_BUSY_EXIT_CODE"] = str(busy_code)
    else:  # also when base_env sets it: the helper would exit with a code nobody waits for
        _ = env.pop("TASKMGR_GPU_BUSY_EXIT_CODE", None)
    return env


def resolve_entry(argv0: str, workdir: str, path_env: str) -> tuple[str | None, str | None]:
    """Resolve argv[0] per Section 2.2. Returns (path, error)."""
    if "/" in argv0:
        p = argv0 if argv0.startswith("/") else os.path.join(workdir, argv0)
        if not os.path.isfile(p):
            return None, "entry %s does not exist" % argv0
        if not os.access(p, os.X_OK):
            return None, "entry %s is not executable" % argv0
        return p, None
    found = shutil.which(argv0, path=path_env)
    if not found:
        return None, "entry command %r not found on PATH %s" % (argv0, path_env)
    return found, None


def prestart_checks(
    task: TaskSpec, job: Job, env: dict[str, str], workdir: str
) -> tuple[str | None, list[str]]:
    errors: list[str] = []
    envi: TaskEnvironment = task.get("environment") or {}
    venv = envi.get("venv")
    required_paths: list[str] = envi.get("required_paths") or []
    for p in ([venv] if venv else []) + list(required_paths):
        if not os.path.exists(p):
            errors.append("required path does not exist: %s" % p)
    path, err = resolve_entry(run_command(task, job)[0][0], workdir, env.get("PATH", ""))
    if err:
        errors.append(err)
    uuids = [g["uuid"] for g in run_gpus(job)]
    if uuids:
        try:
            visible = {u for _, u in gpu.query_gpus()}
            missing = [u for u in uuids if u not in visible]
            if missing:
                errors.append("GPUs not visible on this machine: %s" % ",".join(missing))
        except TaskmgrError as e:
            errors.append(e.message)
    return path, errors


def prepare_workdir(jobdir: str) -> str:
    workdir = os.path.join(jobdir, "workdir")
    if os.path.exists(workdir):
        raise RuntimeError("workdir already exists: %s" % workdir)
    _ = shutil.copytree(os.path.join(jobdir, "package"), workdir, symlinks=True)
    # Writable by the owner, no group or other bits (a package of an older version has them).
    for dirpath, _dirnames, filenames in os.walk(workdir):
        os.chmod(dirpath, PRIVATE_DIR)
        for f in filenames:
            p = os.path.join(dirpath, f)
            if not os.path.islink(p):
                os.chmod(p, (stat.S_IMODE(os.lstat(p).st_mode) & 0o700) | PRIVATE_FILE)
    return workdir


def run(store: Store, jobid: str, lock_fd: int) -> int:
    os.set_inheritable(lock_fd, False)  # the task must never inherit shim.lock
    jobdir, job = store.load_job(jobid)
    runner, _ = store.read_runner(jobdir)
    if (
        job["state"] not in ("running", "stopping")
        or not isinstance(runner, dict)
        or runner.get("pgid") is not None
    ):
        log("job %s is not startable (state=%s); exiting" % (jobid, job["state"]))
        return 0
    cfg = store.config
    task = load_task(jobdir)
    runner.update(
        {
            "shim_pid": os.getpid(),
            "shim_start_time": procutil.start_time(os.getpid()),
            "status": "starting",
        }
    )
    store.write_runner(jobdir, runner)

    # A job queued again keeps its workdir (its old run record was set aside as
    # runner.prev-N.json): under the GPU contract a "GPU busy" run did no work, and the
    # run after a precompile needs what the precompile built there.
    requeued = any(n.startswith("runner.prev-") for n in os.listdir(jobdir))
    workdir = os.path.join(jobdir, "workdir")
    if not (requeued and os.path.isdir(workdir)):
        workdir = prepare_workdir(jobdir)
    env = build_env(cfg, task, job, workdir, store.gpu_busy_exit_code)
    path, errors = prestart_checks(task, job, env, workdir)
    if job.get("cancel_requested"):
        errors = []  # cancelled before start: do not run, end as cancelled below
    if errors or job.get("cancel_requested"):
        err: ErrorInfo = {"kind": "setup", "message": "; ".join(errors), "details": [*errors]}
        end_before_launch(store, jobid, runner, err)
        log("pre-start check failed: %s" % errors)
        return 0

    assert path is not None  # resolve_entry succeeded: no pre-start errors
    separator: str | None = None
    if requeued:
        separator = "--- taskmgr: launch after requeue (%s) ---" % (
            "precompile done" if job.get("phase") == PHASE_GPU else "GPU busy"
        )
    argv, timeout = run_command(task, job)
    while True:
        pid = launch(store, jobdir, runner, argv, path, env, workdir, separator)
        if supervise(store, jobid, jobdir, runner, pid, timeout, cfg) != "gpu_busy":
            return 0
        if job.get("gpu_request"):
            # The scheduler chose these GPUs; one is busy after all. Queue the job again
            # so that the scheduler chooses again (maybe other GPUs) on its next pass.
            requeued = requeue_gpu_busy(store, jobid)
            log("entry exited GPU busy; job is now %s" % (requeued or job)["state"])
            return 0
        if not wait_for_gpu(store, jobid, jobdir, runner, cfg):
            return 0
        # The workdir is reused: a 75 exit means the entry did no work (GPU contract), and
        # copying the package again on every retry would cost I/O for nothing.
        separator = "--- taskmgr: relaunch #%d after GPU busy ---" % runner.get(
            "gpu_busy_retries", 0
        )


def end_before_launch(store: Store, jobid: str, runner: Runner, error: ErrorInfo) -> None:
    """End a run whose entry was never started: cancelled if a cancel was requested (the
    stop is confirmed at once), else failed with `error`."""
    with store.state_lock():
        jobdir, job = store.load_job(jobid)
        runner.update(
            {"final": True, "ended_at": utcnow_iso(), "return_code": None, "signal": None}
        )
        if job.get("cancel_requested"):
            _ = end_cancelled(store, jobid, runner)
            return
        runner["status"] = "failed"
        runner["error"] = error
        jobdir = end_without_result(store, jobdir, job, "failed", error, runner)
        store.write_runner(jobdir, runner)


def launch(
    store: Store,
    jobdir: str,
    runner: Runner,
    argv: list[str],
    path: str,
    env: dict[str, str],
    workdir: str,
    separator: str | None,
) -> int:
    """Start the entry (Section 12.1): fork, the child setsid()s and waits on a pipe, the
    identity record is written and fsynced, then the child is released to exec.

    Returns the leader pid. `separator` is appended as a line to both logs first (logs are
    appended across relaunches, never truncated).
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    out_fd = os.open(os.path.join(jobdir, "stdout.log"), flags, PRIVATE_FILE)
    err_fd = os.open(os.path.join(jobdir, "stderr.log"), flags, PRIVATE_FILE)
    if separator:
        for fd in (out_fd, err_fd):
            _ = os.write(fd, (separator + "\n").encode())
    rfd, wfd = os.pipe()
    pid = os.fork()
    if pid == 0:  # child: new session, wait for the go signal, then exec
        try:
            os.setsid()
            os.close(wfd)
            go = os.read(rfd, 1)
            if go != b"x":
                os._exit(126)
            devnull = os.open(os.devnull, os.O_RDONLY)
            _ = os.dup2(devnull, 0)
            _ = os.dup2(out_fd, 1)
            _ = os.dup2(err_fd, 2)
            os.chdir(workdir)
            procutil.default_signals()
            os.execve(path, argv, env)
        except BaseException as e:  # noqa: BLE001 - must never return into the shim
            try:
                _ = os.write(2, ("taskmgr: cannot exec %s: %s\n" % (argv[0], e)).encode())
            finally:
                os._exit(127)
    os.close(rfd)
    os.close(out_fd)
    os.close(err_fd)
    _crash_point("before_identity")
    leader_start = procutil.start_time(pid)
    # A relaunch replaces the identity and clears the previous launch's exit only here, so
    # until the new identity is durable the record still shows the "GPU busy" exit.
    for k in ("exited", "return_code", "signal"):
        _ = runner.pop(k, None)
    _ = runner.pop("ended_at", None)
    runner.update(
        {
            "boot_id": procutil.boot_id(),
            "pgid": pid,
            "leader_pid": pid,
            "leader_start_time": leader_start,
            "argv_hash": hashlib.sha256(json.dumps(argv).encode()).hexdigest(),
            "argv": argv,
            "started_at": utcnow_iso(),
            "started_at_epoch": time.time(),
            "status": "running",
            "exited": False,
        }
    )
    store.write_runner(jobdir, runner)  # fsynced before the child may exec (Section 12.1)
    _crash_point("before_exec")
    _ = os.write(wfd, b"x")
    os.close(wfd)
    log("started pid=%d argv=%s" % (pid, argv))
    return pid


def set_waiting(store: Store, jobid: str, reason: str | None) -> bool:
    """Set (or clear, with None) job.json pending_reason; the job stays running.

    A leftover cleanup after the "GPU busy" exit left the job stopping/leftover; it goes
    back to running. Returns False if the job is no longer running/stopping.
    """
    with store.state_lock():
        jobdir, job = store.load_job(jobid)
        if job["state"] not in ("running", "stopping"):
            return False
        changed = job.get("pending_reason") != reason
        if (
            reason is not None
            and job["state"] == "stopping"
            and job.get("stop_reason") == "leftover"
        ):
            job["state"] = "running"
            job["stop_reason"] = None
            changed = True
        if changed:
            if reason is None:
                _ = job.pop("pending_reason", None)
            else:
                job["pending_reason"] = reason
            _ = store.save_job(jobdir, job)
        return True


def wait_for_gpu(store: Store, jobid: str, jobdir: str, runner: Runner, cfg: RemoteConfig) -> bool:
    """After a "GPU busy" exit (group gone): record it, wait gpu_retry_interval_seconds
    while honoring cancel, and return True to relaunch. False: the job was finalized
    (cancelled) or is no longer ours to run."""
    code = runner.get("return_code")
    interval = float(cfg["gpu_retry_interval_seconds"])
    poll = float(cfg["control_poll_seconds"])
    retries = runner.get("gpu_busy_retries", 0) + 1
    runner.update(
        {"status": "waiting_gpu", "gpu_busy_retries": retries, "last_gpu_busy_at": utcnow_iso()}
    )
    store.write_runner(jobdir, runner)
    log("entry exited %s (GPU busy); relaunch #%d in %g s" % (code, retries, interval))
    reason = "entry exited %s (GPU busy); retry #%d in %g s" % (code, retries, interval)
    if not set_waiting(store, jobid, reason):
        log("job is no longer running; not relaunching")
        return False
    deadline = time.monotonic() + interval
    last_poll: float | None = None
    while True:
        now = time.monotonic()
        if last_poll is None or now - last_poll >= poll:
            last_poll = now
            _, job = store.load_job(jobid)
            if job["state"] not in ("running", "stopping"):
                log("job is %s while waiting for the GPU; not relaunching" % job["state"])
                return False
            if job.get("cancel_requested"):
                # Nothing is running, so the stop is confirmed at once:
                # running -> stopping -> cancelled.
                log("cancel requested while waiting for the GPU")
                job = end_cancelled(store, jobid)
                log("finalized: %s" % job["state"])
                return False
        if now >= deadline:
            return True
        time.sleep(min(0.1, max(0.0, deadline - now)))


def supervise(
    store: Store,
    jobid: str,
    jobdir: str,
    runner: Runner,
    pid: int,
    timeout: float | None,
    cfg: RemoteConfig,
) -> str:
    """Supervise one launch until its process group is gone.

    Returns "gpu_busy" when the entry exited with gpu_busy_exit_code and was not stopped
    for cancel or timeout (nothing is finalized; the caller relaunches), else "done" (the
    job was finalized or became unknown). timeout_seconds counts from this launch's start.
    """
    t0 = time.monotonic()
    poll = float(cfg["control_poll_seconds"])
    # A precompile has no GPU, so its exit with that code is an ordinary failure.
    busy_code = None if runner.get("phase") == PHASE_PRECOMPILE else store.gpu_busy_exit_code
    retry_interval = float(cfg["gpu_retry_interval_seconds"])
    grace, confirm = cfg["stop_grace_seconds"], cfg["kill_confirm_seconds"]
    # cast: reap() (a closure) sets exit_status, so the checker must not narrow it to None.
    exit_status: int | None = cast("int | None", None)
    stopper: GroupStopper | None = None
    stop_reason: str | None = None
    last_poll = 0.0
    warnings: list[str] = []
    # run() records leader_start_time before supervise() is called.
    min_start = cast(int, runner.get("leader_start_time"))

    def reap() -> None:
        nonlocal exit_status
        if exit_status is not None:
            return
        try:
            p, st = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        if p == pid:
            exit_status = st
            record_exit(st)

    def record_exit(st: int) -> None:
        rc = os.waitstatus_to_exitcode(st)
        runner.update(
            {
                "exited": True,
                "ended_at": utcnow_iso(),
                "return_code": rc if rc >= 0 else None,
                "signal": -rc if rc < 0 else None,
            }
        )
        store.write_runner(jobdir, runner)
        log("entry exited rc=%s signal=%s" % (runner.get("return_code"), runner.get("signal")))
        _crash_point("after_exit_recorded")

    # One loop for both stops: a requested one (cancel, timeout, a stop reason in job.json)
    # while the entry runs, and the leftover cleanup after it exited (Section 10.4 step 2).
    while True:
        reap()
        if stopper is None:
            now = time.monotonic()
            if exit_status is not None:
                members = procutil.group_members(pid, min_start, jobid)
                if not members:
                    break
                log("leftover processes %s; running stop procedure" % members)
                warnings.append("leftover_processes")
                runner["warnings"] = warnings
                store.write_runner(jobdir, runner)  # survives a shim crash during cleanup
                stop_reason = "leftover"
            elif now - last_poll >= poll:
                last_poll = now
                _, job = store.load_job(jobid)
                if job.get("pending_reason") is not None and now - t0 >= retry_interval:
                    # A relaunch that has kept running for one retry interval got its GPU;
                    # clearing earlier would make the display flicker on every retry.
                    _ = set_waiting(store, jobid, None)
                stop_reason = requested_stop(job, now - t0, timeout)
            elif timeout and now - t0 >= timeout:
                stop_reason = "timeout"
            if stop_reason:
                if stop_reason != "leftover":
                    log("stopping process group %d: %s" % (pid, stop_reason))
                _ = set_stopping(store, jobid, stop_reason)
                stopper = GroupStopper(pid, min_start, jobid, grace, confirm, reap)
                stopper.start(stop_reason)
        else:
            now = time.monotonic()
            if now - last_poll >= poll:
                last_poll = now
                stopper.honor_cancel(store.load_job(jobid)[1])
            r = stopper.step()
            if r == "stopped":
                break
            if r == "unconfirmed":
                record_unconfirmed(store, jobid, jobdir, runner, stop_reason, stopper.alive)
                log("stop unconfirmed, job is unknown; alive=%s" % stopper.alive)
                return "done"
        time.sleep(0.1)

    if exit_status is None:
        _, st = os.waitpid(pid, 0)
        exit_status = st
        record_exit(st)
    # A leftover cleanup is no stop of the entry: its "GPU busy" exit still counts.
    exited_busy = busy_code is not None and runner.get("return_code") == busy_code
    if exited_busy and stop_reason not in STOP_OUTCOME:
        return "gpu_busy"
    _crash_point("before_finalize")
    status = STOP_OUTCOME.get(stop_reason) if stop_reason else None
    job = finalize_run(store, jobid, status=status)
    log("finalized: %s" % job["state"])
    return "done"


def fail_unstarted(store: Store, jobid: str, e: Exception) -> None:
    """The shim failed before it started the entry: end the run as failed (kind
    shim_error) instead of leaving it to recovery, which would count an interruption
    and run the job again. A run that may have started is left to recovery."""
    with store.state_lock():
        jobdir, job = store.load_job(jobid)
        runner, _ = store.read_runner(jobdir)
        if (
            job["state"] not in ("running", "stopping")
            or runner is None
            or runner.get("final")
            or may_have_started(runner)
            or runner.get("shim_pid") not in (None, os.getpid())
        ):
            return
        msg = "the run supervisor failed before starting the entry: %s" % e
        end_before_launch(store, jobid, runner, {"kind": "shim_error", "message": msg})
    log("run ended as failed (shim_error)")


def main(store: Store, jobid: str, lock_fd: int) -> int:
    _ = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        return run(store, jobid, lock_fd)
    except Exception as e:  # noqa: BLE001
        import traceback

        log("shim error: %s\n%s" % (e, traceback.format_exc()))
        try:
            fail_unstarted(store, jobid, e)
        except Exception as e2:  # noqa: BLE001 - recovery handles the run instead
            log("cannot record the shim error: %s" % e2)
        return 1
