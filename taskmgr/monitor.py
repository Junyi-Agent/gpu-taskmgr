"""Stop procedure, restart-recovery assessment and daemon takeover tracking.

Shared by the shim (normal supervision), the daemon (startup recovery, dead-shim
detection, takeover) and `taskmgr-remote resolve`.
"""

import os
import signal
import time

from . import procutil
from .common import TERMINAL_STATES, try_flock_fd, try_read_json, utcnow_iso
from .store import finalize_attempt, load_task, requeue_or_end_interrupted

STOP_OUTCOME = {"cancel": "cancelled", "timeout": "timed_out"}


class GroupStopper:
    """Non-blocking stop procedure (Section 13.1): SIGTERM, grace, SIGKILL, confirm."""

    def __init__(self, pgid, min_start, grace, confirm, reap=None):
        self.pgid, self.min_start = pgid, min_start
        self.grace, self.confirm = float(grace), float(confirm)
        self.reap = reap
        self.phase = None
        self.deadline = None
        self.alive = []

    def _kill(self, sig):
        # TASKMGR_TEST_IGNORE_KILL: test-only, simulates processes that survive SIGKILL.
        if os.environ.get("TASKMGR_TEST_IGNORE_KILL") == "1":
            return
        try:
            os.killpg(self.pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def start(self):
        self._kill(signal.SIGTERM)
        self.phase = "term"
        self.deadline = time.monotonic() + self.grace

    def step(self):
        """Advance; returns None (in progress), "stopped" or "unconfirmed"."""
        if self.reap:
            self.reap()
        self.alive = procutil.group_members(self.pgid, self.min_start)
        if not self.alive:
            return "stopped"
        now = time.monotonic()
        if self.phase == "term" and now >= self.deadline:
            self._kill(signal.SIGKILL)
            self.phase = "kill"
            self.deadline = now + self.confirm
        elif self.phase == "kill" and now >= self.deadline:
            return "unconfirmed"
        return None


def set_stopping(store, jobid, n, reason):
    """Persist running -> stopping with the stop reason. Returns False if not applicable."""
    with store.state_lock():
        jobdir, job = store.load_job(jobid)
        if job.get("current_attempt") != n or job["state"] not in ("running", "stopping"):
            return False
        if job["state"] == "running" or job.get("stop_reason") != reason:
            job["state"] = "stopping"
            job["stop_reason"] = reason
            store.save_job(jobdir, job)
        return True


def mark_unknown(store, jobid, n, kind, message, details=None):
    with store.state_lock():
        jobdir, job = store.load_job(jobid)
        if job.get("current_attempt") != n or job["state"] not in ("running", "stopping"):
            return job
        adir = store.attempt_dir(jobdir, n)
        runner, _ = store.read_runner(adir)
        if isinstance(runner, dict):
            runner["unknown"] = {"kind": kind, "message": message, "details": details,
                                 "at": utcnow_iso()}
            store.write_runner(adir, runner)
        job["state"] = "unknown"
        job["error"] = {"kind": kind, "message": message}
        store.save_job(jobdir, job)
        return job


def identity_of(runner):
    keys = ("boot_id", "shim_pid", "shim_start_time", "pgid", "leader_pid", "leader_start_time",
            "argv_hash")
    return {k: runner.get(k) for k in keys} if isinstance(runner, dict) else None


def _has_identity(runner):
    return all(runner.get(k) is not None for k in ("boot_id", "pgid", "leader_start_time"))


def finish_stopped(store, jobid, n, stop_reason, exited, warnings=()):
    """Finalize once the process group is confirmed gone."""
    if stop_reason in STOP_OUTCOME:
        return finalize_attempt(store, jobid, n, status=STOP_OUTCOME[stop_reason],
                                warnings=warnings)
    if exited:
        return finalize_attempt(store, jobid, n, warnings=warnings)
    return finalize_attempt(store, jobid, n, status="failed", warnings=warnings, error={
        "kind": "exit_code_lost",
        "message": "the task ended while its supervisor was gone; exit code unavailable"})


def assess(store, jobid, resolve=False):
    """Section 12.2 check for one job in running/stopping (or unknown when resolve=True).

    Returns a dict describing the decision. For decision "takeover" the dict
    holds a Takeover object (the caller owns its shim.lock fd).
    """
    jobdir, job = store.load_job(jobid)
    state = job["state"]
    if state == "queued" or state in TERMINAL_STATES:
        return {"decision": "none", "state": state}
    n = job.get("current_attempt")
    if n is None:
        mark_unknown(store, jobid, n, "record_incomplete", "job has no current attempt")
        return {"decision": "unknown", "reason": "record_incomplete"}
    adir = store.attempt_dir(jobdir, n)
    runner, err = store.read_runner(adir)
    if not isinstance(runner, dict):
        if not resolve:
            mark_unknown(store, jobid, n, "record_incomplete", "runner.json is %s" % err)
        return {"decision": "unknown", "reason": "runner.json is %s" % err}

    if runner.get("final"):
        if runner.get("status") == "interrupted":
            j = requeue_or_end_interrupted(store, jobid, n, runner.get("interrupted_reason", ""))
        else:
            j = finalize_attempt(store, jobid, n)
        return {"decision": "restored", "state": j["state"]}

    lock_fd = try_flock_fd(os.path.join(adir, "shim.lock"))
    if lock_fd is None and os.path.exists(os.path.join(adir, "shim.lock")):
        return {"decision": "shim_alive", "identity": identity_of(runner)}
    keep_fd = False
    try:
        stop_reason = job.get("stop_reason") if state in ("stopping", "unknown") else None
        exited = bool(runner.get("exited"))
        if job.get("cancel_requested") and (state == "unknown" or (stop_reason is None
                                                                    and not exited)):
            # A cancel request stays effective across restarts; on an unknown job it
            # turns into cancelled once the stop is confirmed (Section 13.2).
            stop_reason = "cancel"
        if not exited and not _has_identity(runner):
            if runner.get("pgid") is None and runner.get("boot_id") is None:
                j = requeue_or_end_interrupted(store, jobid, n, "entry command never started")
                return {"decision": "interrupted", "state": j["state"]}
            if not resolve:
                mark_unknown(store, jobid, n, "record_incomplete", "identity record incomplete")
            return {"decision": "unknown", "reason": "identity record incomplete",
                    "identity": identity_of(runner)}
        try:
            current_boot = procutil.boot_id()
        except OSError as e:
            if not resolve:
                mark_unknown(store, jobid, n, "undeterminable", "cannot read boot_id: %s" % e)
            return {"decision": "unknown", "reason": "cannot read boot_id",
                    "identity": identity_of(runner)}
        if runner.get("boot_id") != current_boot:
            if exited:
                j = finish_stopped(store, jobid, n, stop_reason, True)
                return {"decision": "finalized", "state": j["state"]}
            j = requeue_or_end_interrupted(store, jobid, n, "machine rebooted")
            return {"decision": "interrupted", "state": j["state"]}
        try:
            members = procutil.group_members(runner["pgid"], runner["leader_start_time"])
        except procutil.ProcUnavailable as e:
            if not resolve:
                mark_unknown(store, jobid, n, "undeterminable", "cannot read /proc: %s" % e)
            return {"decision": "unknown", "reason": "cannot read /proc",
                    "identity": identity_of(runner)}
        if not members:
            warnings = []
            if "leftover" in (job.get("stop_reason"), runner.get("stop_unconfirmed_reason")):
                warnings = ["leftover_processes"]
            j = finish_stopped(store, jobid, n, stop_reason, exited, warnings)
            return {"decision": "finalized", "state": j["state"]}
        if resolve:
            if runner.get("stop_unconfirmed_reason"):
                # SIGKILL already failed once: an operator has to look at it.
                return {"decision": "still_running", "alive_pids": members,
                        "identity": identity_of(runner)}
            # The unknown came from an undeterminable check that can now be decided:
            # hand the attempt back to the daemon, which takes over tracking (case 4.4).
            with store.state_lock():
                jobdir, job = store.load_job(jobid)
                job["state"] = "stopping" if job.get("stop_reason") else "running"
                job["error"] = None
                store.save_job(jobdir, job)
            return {"decision": "handed_to_daemon", "alive_pids": members,
                    "identity": identity_of(runner)}
        keep_fd = True
        tk = Takeover(store, jobid, n, runner, lock_fd, exited, job)
        return {"decision": "takeover", "takeover": tk, "alive_pids": members}
    finally:
        if lock_fd is not None and not keep_fd:
            os.close(lock_fd)


class Takeover:
    """Daemon-side tracking of an attempt whose shim died while the task lives on."""

    def __init__(self, store, jobid, n, runner, lock_fd, exited, job):
        self.store, self.jobid, self.n = store, jobid, n
        self.lock_fd = lock_fd
        self.pgid = runner["pgid"]
        self.min_start = runner["leader_start_time"]
        self.exited = exited
        self.started_epoch = runner.get("started_at_epoch")
        self.timeout = load_task(os.path.dirname(os.path.dirname(store.attempt_dir(
            store.find_job_dir(jobid), n)))).get("timeout_seconds")
        self.stop_reason = job.get("stop_reason") if job["state"] == "stopping" else None
        self.stopper = None
        self.last_poll = 0.0
        self.warnings = []
        self.done = False
        cfg = store.config
        self.poll = float(cfg.get("control_poll_seconds", 2))
        self.grace, self.confirm = cfg.get("stop_grace_seconds", 30), cfg.get(
            "kill_confirm_seconds", 10)
        self._note()
        if self.exited:
            if self.stop_reason in (None, "leftover"):
                self.warnings.append("leftover_processes")
            self._begin_stop(self.stop_reason or "leftover")
        elif self.stop_reason:
            self._begin_stop(self.stop_reason)

    def _note(self):
        adir = self.store.attempt_dir(self.store.find_job_dir(self.jobid), self.n)
        runner, _ = self.store.read_runner(adir)
        if isinstance(runner, dict):
            runner.setdefault("takeovers", []).append({"at": utcnow_iso(), "by_pid": os.getpid()})
            self.store.write_runner(adir, runner)

    def _begin_stop(self, reason):
        self.stop_reason = reason
        if reason in STOP_OUTCOME:
            set_stopping(self.store, self.jobid, self.n, reason)
        self.stopper = GroupStopper(self.pgid, self.min_start, self.grace, self.confirm)
        self.stopper.start()

    def close(self):
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
        self.done = True

    def tick(self):
        if self.done:
            return
        try:
            if self.stopper is None:
                if not procutil.group_members(self.pgid, self.min_start):
                    finish_stopped(self.store, self.jobid, self.n, None, self.exited,
                                   self.warnings)
                    return self.close()
                now = time.monotonic()
                if now - self.last_poll >= self.poll:
                    self.last_poll = now
                    _, job = self.store.load_job(self.jobid)
                    if job["state"] not in ("running", "stopping") \
                            or job.get("current_attempt") != self.n:
                        return self.close()
                    if job.get("cancel_requested"):
                        self._begin_stop("cancel")
                    elif self.timeout and self.started_epoch and \
                            time.time() - self.started_epoch >= self.timeout:
                        self._begin_stop("timeout")
                return
            r = self.stopper.step()
            if r == "stopped":
                finish_stopped(self.store, self.jobid, self.n, self.stop_reason, self.exited,
                               self.warnings)
                self.close()
            elif r == "unconfirmed":
                self._record_unconfirmed()
                self.close()
        except procutil.ProcUnavailable as e:
            mark_unknown(self.store, self.jobid, self.n, "undeterminable",
                         "cannot read /proc: %s" % e)
            self.close()

    def _record_unconfirmed(self):
        adir = self.store.attempt_dir(self.store.find_job_dir(self.jobid), self.n)
        runner, _ = self.store.read_runner(adir)
        if isinstance(runner, dict):
            runner["stop_unconfirmed_reason"] = self.stop_reason
            self.store.write_runner(adir, runner)
        mark_unknown(self.store, self.jobid, self.n, "stop_unconfirmed",
                     "processes still alive after SIGKILL", {"alive_pids": self.stopper.alive})


def runner_status_snapshot(adir):
    runner, _ = try_read_json(os.path.join(adir, "runner.json"))
    return runner
