"""Attempt supervisor (shim): one detached process per attempt (Sections 1.1, 3, 10.4, 12.1)."""

import hashlib
import json
import os
import pwd
import shutil
import signal
import stat
import sys
import time

from . import gpu, procutil
from .common import TaskmgrError, utcnow_iso
from .monitor import STOP_OUTCOME, GroupStopper, mark_unknown, set_stopping
from .store import end_without_result, finalize_attempt, load_task


def log(msg):
    sys.stderr.write("%s shim[%d] %s\n" % (utcnow_iso(), os.getpid(), msg))
    sys.stderr.flush()


def _crash_point(name):
    # TASKMGR_TEST_SHIM_CRASH: test-only hook that kills the shim at a named point.
    if os.environ.get("TASKMGR_TEST_SHIM_CRASH") == name:
        log("test crash at %s" % name)
        os._exit(99)


def build_env(cfg, task, job, n, workdir):
    """Entry-process environment, built in the order of Section 3.2."""
    try:
        pw = pwd.getpwuid(os.getuid())
        home, user = pw.pw_dir, pw.pw_name
    except KeyError:
        home, user = os.environ.get("HOME", "/"), os.environ.get("USER", "")
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": home, "USER": user,
           "LANG": "C.UTF-8"}
    env.update({str(k): str(v) for k, v in (cfg.get("base_env") or {}).items()})
    venv = (task.get("environment") or {}).get("venv")
    if venv:
        env["VIRTUAL_ENV"] = venv
        env["PATH"] = os.path.join(venv, "bin") + (":" + env["PATH"] if env.get("PATH") else "")
    env.update(task.get("env") or {})
    env.update({
        "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
        "CUDA_VISIBLE_DEVICES": ",".join(g["uuid"] for g in job.get("gpus", [])),
        "TASKMGR_JOBID": job["jobid"],
        "TASKMGR_TASK_ID": job["task_id"],
        "TASKMGR_ATTEMPT": str(n),
        "TASKMGR_WORKDIR": workdir,
        "TASKMGR_PROGRESS_FILE": os.path.join(workdir, "progress.json"),
        "TASKMGR_RESULT_FILE": os.path.join(workdir, "result.json"),
    })
    return env


def resolve_entry(argv0, workdir, path_env):
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


def prestart_checks(task, job, env, workdir):
    errors = []
    envi = task.get("environment") or {}
    for p in ([envi["venv"]] if envi.get("venv") else []) + list(envi.get("required_paths") or []):
        if not os.path.exists(p):
            errors.append("required path does not exist: %s" % p)
    path, err = resolve_entry(task["entry"]["argv"][0], workdir, env.get("PATH", ""))
    if err:
        errors.append(err)
    uuids = [g["uuid"] for g in job.get("gpus", [])]
    if uuids:
        try:
            visible = {u for _, u in gpu.query_gpus()}
            missing = [u for u in uuids if u not in visible]
            if missing:
                errors.append("GPUs not visible on this machine: %s" % ",".join(missing))
        except TaskmgrError as e:
            errors.append(e.message)
    return path, errors


def prepare_workdir(jobdir, adir):
    workdir = os.path.join(adir, "workdir")
    if os.path.exists(workdir):
        raise RuntimeError("workdir already exists: %s" % workdir)
    shutil.copytree(os.path.join(jobdir, "package"), workdir, symlinks=True)
    for dirpath, dirnames, filenames in os.walk(workdir):
        os.chmod(dirpath, stat.S_IMODE(os.lstat(dirpath).st_mode) | 0o700)
        for f in filenames:
            p = os.path.join(dirpath, f)
            if not os.path.islink(p):
                os.chmod(p, stat.S_IMODE(os.lstat(p).st_mode) | 0o600)
    return workdir


def run(store, jobid, n, lock_fd):
    os.set_inheritable(lock_fd, False)  # the task must never inherit shim.lock
    jobdir, job = store.load_job(jobid)
    adir = store.attempt_dir(jobdir, n)
    runner, _ = store.read_runner(adir)
    if job.get("current_attempt") != n or job["state"] not in ("running", "stopping") \
            or not isinstance(runner, dict) or runner.get("pgid") is not None:
        log("job %s attempt %d is not startable (state=%s); exiting" % (jobid, n, job["state"]))
        return 0
    cfg = store.config
    task = load_task(jobdir)
    runner.update({"shim_pid": os.getpid(), "shim_start_time": procutil.start_time(os.getpid()),
                   "status": "starting"})
    store.write_runner(adir, runner)

    workdir = prepare_workdir(jobdir, adir)
    env = build_env(cfg, task, job, n, workdir)
    path, errors = prestart_checks(task, job, env, workdir)
    if job.get("cancel_requested"):
        errors = []  # cancelled before start: do not run, end as cancelled below
    if errors or job.get("cancel_requested"):
        with store.state_lock():
            jobdir, job = store.load_job(jobid)
            runner.update({"final": True, "ended_at": utcnow_iso(), "return_code": None,
                           "signal": None})
            if job.get("cancel_requested"):
                runner["status"] = "cancelled"
                end_without_result(store, jobdir, job, n, "cancelled", None, runner)
            else:
                err = {"kind": "setup", "message": "; ".join(errors), "details": errors}
                runner["status"] = "failed"
                runner["error"] = err
                end_without_result(store, jobdir, job, n, "failed", err, runner)
            store.write_runner(store.attempt_dir(store.find_job_dir(jobid), n), runner)
        log("pre-start check failed: %s" % errors)
        return 0

    argv = task["entry"]["argv"]
    out_fd = os.open(os.path.join(adir, "stdout.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                     0o644)
    err_fd = os.open(os.path.join(adir, "stderr.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                     0o644)
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
            os.dup2(devnull, 0)
            os.dup2(out_fd, 1)
            os.dup2(err_fd, 2)
            os.chdir(workdir)
            os.execve(path, argv, env)
        except BaseException as e:  # noqa: BLE001 - must never return into the shim
            try:
                os.write(2, ("taskmgr: cannot exec %s: %s\n" % (argv[0], e)).encode())
            finally:
                os._exit(127)
    os.close(rfd)
    os.close(out_fd)
    os.close(err_fd)
    leader_start = procutil.start_time(pid)
    runner.update({
        "boot_id": procutil.boot_id(),
        "pgid": pid,
        "leader_pid": pid,
        "leader_start_time": leader_start,
        "argv_hash": hashlib.sha256(json.dumps(argv).encode()).hexdigest(),
        "argv": argv,
        "started_at": utcnow_iso(),
        "started_at_epoch": time.time(),
        "status": "running",
    })
    store.write_runner(adir, runner)  # fsynced before the child may exec (Section 12.1)
    _crash_point("before_exec")
    os.write(wfd, b"x")
    os.close(wfd)
    log("started pid=%d argv=%s" % (pid, argv))
    return supervise(store, jobid, n, adir, runner, pid, task, cfg)


def supervise(store, jobid, n, adir, runner, pid, task, cfg):
    t0 = time.monotonic()
    timeout = task.get("timeout_seconds")
    poll = float(cfg.get("control_poll_seconds", 2))
    grace, confirm = cfg.get("stop_grace_seconds", 30), cfg.get("kill_confirm_seconds", 10)
    exit_status = None
    stopper = None
    stop_reason = None
    last_poll = 0.0
    warnings = []

    def reap():
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

    def record_exit(st):
        rc = os.waitstatus_to_exitcode(st)
        runner.update({"exited": True, "ended_at": utcnow_iso(),
                       "return_code": rc if rc >= 0 else None,
                       "signal": -rc if rc < 0 else None})
        store.write_runner(adir, runner)
        log("entry exited rc=%s signal=%s" % (runner["return_code"], runner["signal"]))
        _crash_point("after_exit_recorded")

    while True:
        reap()
        if stopper is None:
            if exit_status is not None:
                break
            now = time.monotonic()
            if now - last_poll >= poll:
                last_poll = now
                _, job = store.load_job(jobid)
                if job.get("cancel_requested"):
                    stop_reason = "cancel"
                elif job["state"] == "stopping" and job.get("stop_reason"):
                    stop_reason = job["stop_reason"]
            if stop_reason is None and timeout and now - t0 >= timeout:
                stop_reason = "timeout"
            if stop_reason:
                log("stopping process group %d: %s" % (pid, stop_reason))
                set_stopping(store, jobid, n, stop_reason)
                stopper = GroupStopper(pid, runner["leader_start_time"], grace, confirm, reap)
                stopper.start()
        else:
            r = stopper.step()
            if r == "stopped":
                break
            if r == "unconfirmed":
                return unconfirmed(store, jobid, n, adir, runner, stop_reason, stopper.alive)
        time.sleep(0.1)

    if stop_reason is None:
        # Section 10.4 step 2: leftover processes in the group.
        members = procutil.group_members(pid, runner["leader_start_time"])
        if members:
            log("leftover processes %s; running stop procedure" % members)
            warnings.append("leftover_processes")
            stopper = GroupStopper(pid, runner["leader_start_time"], grace, confirm)
            stopper.start()
            while True:
                r = stopper.step()
                if r == "stopped":
                    break
                if r == "unconfirmed":
                    runner["warnings"] = warnings
                    return unconfirmed(store, jobid, n, adir, runner, "leftover", stopper.alive)
                time.sleep(0.1)
    if exit_status is None:
        _, st = os.waitpid(pid, 0)
        exit_status = st
        record_exit(st)
    _crash_point("before_finalize")
    status = STOP_OUTCOME.get(stop_reason)
    job = finalize_attempt(store, jobid, n, status=status, warnings=warnings)
    log("finalized: %s" % job["state"])
    return 0


def unconfirmed(store, jobid, n, adir, runner, reason, alive):
    runner["stop_unconfirmed_reason"] = reason
    store.write_runner(adir, runner)
    mark_unknown(store, jobid, n, "stop_unconfirmed",
                 "processes still alive after SIGKILL", {"alive_pids": alive})
    log("stop unconfirmed, job is unknown; alive=%s" % alive)
    return 0


def main(store, jobid, n, lock_fd):
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        return run(store, jobid, n, lock_fd)
    except Exception as e:  # noqa: BLE001
        import traceback
        log("shim error: %s\n%s" % (e, traceback.format_exc()))
        return 1
