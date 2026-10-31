"""Scheduler daemon: `taskmgr-remote serve` (Sections 1.1, 5.2, 7.2, 9.3, 12.2)."""

import fcntl
import os
import signal
import sys
import time
import traceback
from collections.abc import Callable
from types import FrameType
from typing import TextIO

from . import procutil
from .common import (
    CODE_ROOT,
    GPU_HOLDING_STATES,
    TERMINAL_STATES,
    try_flock_fd,
    utcnow_iso,
)
from .monitor import Takeover, assess, mark_unknown, may_have_started
from .records import ErrorInfo, Job, Runner
from .store import (
    Store,
    end_without_result,
    finalize_run,
    has_run,
    remove_tree,
    result_complete,
)

INCOMING_MAX_AGE = 3600
# Consecutive errors while recovering or tracking one job before it is marked unknown.
MAX_JOB_ERRORS = 3


_log_file: TextIO | None = None


def log(msg: str) -> None:
    line = "%s daemon[%d] %s\n" % (utcnow_iso(), os.getpid(), msg)
    _ = sys.stderr.write(line)
    _ = sys.stderr.flush()
    if _log_file is not None:
        _ = _log_file.write(line)
        _log_file.flush()


class Daemon:
    def __init__(self, store: Store) -> None:
        self.store: Store = store
        self.takeovers: dict[str, Takeover] = {}  # jobid -> Takeover
        self.stop: bool = False
        self._daemon_lock_fd: int | None = None
        self.job_errors: dict[str, int] = {}  # jobid -> consecutive errors
        self.given_up: set[str] = set()  # could not even be marked unknown; skipped

    def guarded(self, jobid: str, what: str, fn: Callable[[str], object]) -> None:
        """Run fn(jobid); an error there must not stop the other jobs or scheduling.

        After MAX_JOB_ERRORS consecutive errors the job is marked unknown (kind
        recovery_error; its GPUs stay held until an operator resolves it) and no longer
        tracked. A job that cannot even be marked unknown is skipped until a restart.
        """
        try:
            _ = fn(jobid)
        except Exception as e:  # noqa: BLE001
            n = self.job_errors.get(jobid, 0) + 1
            self.job_errors[jobid] = n
            log(
                "%s of %s failed (%d/%d): %s\n%s"
                % (what, jobid, n, MAX_JOB_ERRORS, e, traceback.format_exc())
            )
            if n < MAX_JOB_ERRORS:
                return
            _ = self.job_errors.pop(jobid, None)
            tk = self.takeovers.pop(jobid, None)
            if tk is not None:
                tk.close()
            msg = "%s failed %d times: %s" % (what, n, e)
            try:
                _ = mark_unknown(self.store, jobid, "recovery_error", msg)
                log("marked %s unknown: %s" % (jobid, msg))
            except Exception as e2:  # noqa: BLE001
                self.given_up.add(jobid)
                log("cannot mark %s unknown (%s); skipping it until restart" % (jobid, e2))
        else:
            _ = self.job_errors.pop(jobid, None)

    # -- startup ------------------------------------------------------------
    def acquire_instance_lock(self) -> bool:
        # TASKMGR_TEST_SKIP_DAEMON_LOCK: test-only, to prove state.lock alone prevents
        # double dispatch when several daemons run.
        if os.environ.get("TASKMGR_TEST_SKIP_DAEMON_LOCK") == "1":
            return True
        fd = try_flock_fd(self.store.path("locks", "daemon.lock"), create=True)
        if fd is None:
            return False
        self._daemon_lock_fd = fd
        os.ftruncate(fd, 0)
        _ = os.write(fd, ("%d\n" % os.getpid()).encode())
        return True

    def clean_incoming(self) -> None:
        base = self.store.path("incoming")
        for name in sorted(os.listdir(base)):
            p = os.path.join(base, name)
            fd = try_flock_fd(p, directory=os.path.isdir(p))
            if fd is None:
                continue  # an upload is in progress
            try:
                age = time.time() - os.lstat(p).st_mtime
                if age > INCOMING_MAX_AGE:
                    log("removing stale upload %s" % name)
                    if os.path.isdir(p):
                        remove_tree(p)
                    else:
                        os.unlink(p)
            finally:
                os.close(fd)

    def recover(self) -> None:
        """Startup recovery (Section 12.2) under the state lock."""
        st = self.store
        with st.state_lock():
            for jobdir, job in list(st.iter_jobs()):
                jid = job["jobid"]
                # Directory follows state; job.json wins.
                want = st.dir_for(job["state"], jid)
                if os.path.abspath(jobdir) != want:
                    log("repairing location of %s -> %s" % (jid, want))
                    _ = st.save_job(jobdir, job)
                if job["state"] in ("running", "stopping"):
                    self.guarded(jid, "recovery", self.check_run)
                elif job["state"] in TERMINAL_STATES and has_run(job):
                    self.guarded(jid, "result repair", self.repair_terminal)

    def repair_terminal(self, jobid: str) -> None:
        jobdir, _ = self.store.load_job(jobid)
        runner, _ = self.store.read_runner(jobdir)
        if (
            isinstance(runner, dict)
            and runner.get("final")
            and runner.get("taskmgr")
            and not result_complete(jobdir)
        ):
            _ = finalize_run(self.store, jobid)

    def check_run(self, jobid: str) -> None:
        if jobid in self.takeovers:
            return
        res = assess(self.store, jobid)
        d = res["decision"]
        if d == "takeover":
            tk = res.get("takeover")
            assert tk is not None  # assess() always attaches the Takeover for this decision
            log("taking over tracking of %s (alive pids %s)" % (jobid, res.get("alive_pids")))
            self.takeovers[jobid] = tk
        elif d not in ("shim_alive", "none"):
            log("recovery of %s: %s" % (jobid, {k: v for k, v in res.items() if k != "takeover"}))

    # -- scheduling -----------------------------------------------------------
    def schedule_pass(self) -> None:
        st = self.store
        cfg = st.config
        with st.state_lock():
            jobs = list(st.iter_jobs(active=True))
            held: dict[str, str] = {}  # uuid -> state of the holder
            cpu_active = 0
            for _, job in jobs:
                if job["state"] in GPU_HOLDING_STATES:
                    if job.get("gpus"):
                        for g in job["gpus"]:
                            held[g["uuid"]] = job["state"]
                    else:
                        cpu_active += 1
            queued = sorted(
                (j for j in jobs if j[1]["state"] == "queued"),
                key=lambda e: (e[1].get("enqueue_seq", 0), e[1]["jobid"]),
            )
            reserved: set[str] = set()
            max_cpu = cfg["max_cpu_jobs"]
            for jobdir, job in queued:
                uuids = [g["uuid"] for g in job.get("gpus", [])]
                if not uuids:
                    if (max_cpu is None or cpu_active < int(max_cpu)) and self.dispatch(
                        jobdir, job
                    ) in ("running", "unknown"):
                        cpu_active += 1
                    continue
                if any(held.get(u) == "unknown" for u in uuids):
                    continue  # do not let an unknown job lock other GPUs via reservation
                if all(u not in held and u not in reserved for u in uuids):
                    now_state = self.dispatch(jobdir, job)
                    if now_state in ("running", "unknown"):
                        for u in uuids:
                            held[u] = now_state
                else:
                    reserved.update(uuids)

    def dispatch(self, jobdir: str, job: Job) -> str | None:
        """Record the dispatch, then start a detached shim. Needs the state lock.

        Returns the job's new state ("running", or "unknown" / "failed" when it could not
        be started), or None if the job was no longer queued.
        """
        st = self.store
        jobdir, job = st.load_job(job["jobid"])  # re-read under the lock
        if job["state"] != "queued":
            return None
        old, _ = st.read_runner(jobdir)
        if old is not None and st.ended_gpu_busy(old):
            # A crash while a "GPU busy" job was being queued again (monitor.requeue_gpu_busy)
            # left its last run record behind; that run did no work.
            _ = st.set_runner_aside(jobdir)
            old = None
        # A crash between writing runner.json and job.json leaves a queued job with a
        # run record. Without an identity nothing was started: dispatch again. With
        # one (not expected) a task may have run: an operator has to decide.
        if old is not None and may_have_started(old):
            log("queued job %s already has a started run; marking unknown" % job["jobid"])
            job["state"] = "unknown"
            job["dispatched_at"] = old.get("dispatched_at") or utcnow_iso()
            job["error"] = {
                "kind": "record_incomplete",
                "message": "queued job already has a run record with an execution identity",
            }
            _ = st.save_job(jobdir, job)
            return job["state"]
        runner: Runner = {
            "schema_version": 1,
            "jobid": job["jobid"],
            "status": "dispatched",
            "gpus": job.get("gpus", []),
            "dispatched_at": utcnow_iso(),
            "final": False,
            "exited": False,
        }
        st.write_runner(jobdir, runner)
        job["state"] = "running"
        job["dispatched_at"] = runner["dispatched_at"]
        job["stop_reason"] = None
        job["error"] = None
        jobdir = st.save_job(jobdir, job)
        err = self.spawn_shim(job["jobid"], jobdir)
        if err:
            log("shim start failed for %s: %s" % (job["jobid"], err))
            runner.update(
                {
                    "final": True,
                    "status": "failed",
                    "return_code": None,
                    "signal": None,
                    "ended_at": utcnow_iso(),
                    "error": {"kind": "shim_start_failed", "message": err},
                }
            )
            st.write_runner(jobdir, runner)
            failure: ErrorInfo = {"kind": "shim_start_failed", "message": err}
            _ = end_without_result(st, jobdir, job, "failed", failure, runner)
            return job["state"]
        log("dispatched %s gpus=%s" % (job["jobid"], [g["uuid"] for g in job.get("gpus", [])]))
        return job["state"]

    def spawn_shim(self, jobid: str, jobdir: str) -> str | None:
        """Double-fork a shim into its own session, handing it the locked shim.lock."""
        lock_fd = os.open(os.path.join(jobdir, "shim.lock"), os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        pkg_parent = CODE_ROOT  # a directory, or the .pyz (zip files work on PYTHONPATH)
        # TASKMGR_SHIM_EXECUTABLE: test-only override to simulate a shim that cannot start.
        exe = os.environ.get("TASKMGR_SHIM_EXECUTABLE") or sys.executable
        argv = [
            exe,
            "-m",
            "taskmgr.remote",
            "--root",
            self.store.root,
            "shim",
            jobid,
            "--lock-fd",
            str(lock_fd),
        ]
        env = dict(os.environ)
        env["PYTHONPATH"] = pkg_parent + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        rfd, wfd = os.pipe()
        try:
            pid = os.fork()
        except OSError as e:
            for fd in (rfd, wfd, lock_fd):
                os.close(fd)
            return "fork failed: %s" % e
        if pid == 0:
            try:
                os.close(rfd)
                os.setsid()
                if os.fork() > 0:
                    os._exit(0)
                log_fd = os.open(
                    os.path.join(jobdir, "shim.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644
                )
                devnull = os.open(os.devnull, os.O_RDONLY)
                _ = os.dup2(devnull, 0)
                _ = os.dup2(log_fd, 1)
                _ = os.dup2(log_fd, 2)
                os.set_inheritable(lock_fd, True)
                os.chdir("/")
                procutil.default_signals()
                os.execve(exe, argv, env)
            except BaseException as e:  # noqa: BLE001
                try:
                    _ = os.write(wfd, ("%s: %s" % (type(e).__name__, e)).encode() or b"error")
                finally:
                    os._exit(127)
        os.close(wfd)
        os.close(lock_fd)
        try:
            _ = os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        chunks: list[bytes] = []
        while True:
            b = os.read(rfd, 4096)
            if not b:
                break
            chunks.append(b)
        os.close(rfd)
        msg = b"".join(chunks).decode(errors="replace")
        return msg or None

    # -- main loop ------------------------------------------------------------
    def health_check(self) -> None:
        """Detect dead shims while running; same rules as startup recovery."""
        with self.store.state_lock():
            for _, job in list(self.store.iter_jobs(active=True)):
                jid = job["jobid"]
                if (
                    job["state"] in ("running", "stopping")
                    and jid not in self.takeovers
                    and jid not in self.given_up
                ):
                    self.guarded(jid, "health check", self.check_run)

    def tick_takeovers(self) -> None:
        for jid, tk in list(self.takeovers.items()):
            self.guarded(jid, "takeover", lambda j: self.takeovers[j].tick())
            if tk.done:
                _ = self.takeovers.pop(jid, None)

    def run(self) -> int:
        st = self.store
        st.ensure_layout()
        if not self.acquire_instance_lock():
            _ = sys.stdout.write(
                '{"error": {"code": "daemon_running", "message": "another daemon holds locks/daemon.lock"}}\n'
            )
            return 2
        global _log_file
        _log_file = open(st.path("daemon", "daemon.log"), "a")
        _ = signal.signal(signal.SIGTERM, self._on_signal)
        _ = signal.signal(signal.SIGINT, self._on_signal)
        _ = signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        log("starting; root=%s" % st.root)
        self._step("incoming cleanup", self.clean_incoming)
        self._step("recovery", self.recover)
        control_poll = float(st.config["control_poll_seconds"])
        interval = min(float(st.config["scheduler_poll_seconds"]), control_poll)
        last_health = 0.0
        while not self.stop:
            self._step("takeover tracking", self.tick_takeovers)
            now = time.monotonic()
            if now - last_health >= control_poll:
                last_health = now
                self._step("health check", self.health_check)
            self._step("scheduling", self.schedule_pass)
            self._sleep(interval)
        log("stopping (running shims and tasks are left alone)")
        return 0

    def _step(self, what: str, fn: Callable[[], None]) -> None:
        """Run one phase of the loop; an error is logged and the next phase still runs."""
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - keep the daemon alive
            log("%s error: %s\n%s" % (what, e, traceback.format_exc()))

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self.stop and time.monotonic() < end:
            time.sleep(min(0.1, end - time.monotonic()) if end > time.monotonic() else 0)

    def _on_signal(self, _signum: int, _frame: FrameType | None) -> None:
        self.stop = True
