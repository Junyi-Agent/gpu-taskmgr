"""Stop procedure, restart-recovery assessment and daemon takeover tracking.

Shared by the shim (normal supervision), the daemon (startup recovery, dead-shim
detection, takeover) and `taskmgr-remote resolve`.
"""

import os
import signal
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Literal, TypedDict, cast

from . import procutil
from .common import TERMINAL_STATES, try_flock_fd, utcnow_iso
from .records import Job, JsonDict, JsonValue, Runner
from .store import (
    Store,
    end_without_result,
    finalize_run,
    interrupt_run,
    load_task,
    run_command,
)

STOP_OUTCOME = {"cancel": "cancelled", "timeout": "timed_out"}

Decision = Literal[
    "none",
    "requeued",
    "unknown",
    "restored",
    "shim_alive",
    "interrupted",
    "finalized",
    "still_running",
    "handed_to_daemon",
    "takeover",
]


class _AssessmentBase(TypedDict):
    decision: Decision


class Assessment(_AssessmentBase, total=False):
    """Result of `assess`. Which optional keys are present depends on `decision`:

    state      none, restored, interrupted, finalized, requeued
    reason     unknown
    identity   unknown (except record_incomplete), shim_alive, still_running, handed_to_daemon
    alive_pids still_running, handed_to_daemon, takeover
    takeover   takeover (the caller owns its shim.lock fd)
    """

    state: str
    reason: str
    identity: JsonDict | None
    alive_pids: list[int]
    takeover: "Takeover"


class GroupStopper:
    """Non-blocking stop procedure (Section 13.1): SIGTERM, grace, SIGKILL, confirm.
    A cancel skips SIGTERM and the grace period."""

    def __init__(
        self,
        pgid: int,
        min_start: int,
        jobid: str,
        grace: float,
        confirm: float,
        reap: Callable[[], object] | None = None,
    ) -> None:
        self.pgid: int = pgid
        self.min_start: int = min_start
        self.jobid: str = jobid
        self.grace: float = float(grace)
        self.confirm: float = float(confirm)
        self.reap: Callable[[], object] | None = reap
        self.phase: str | None = None
        self.deadline: float = 0.0
        self.alive: list[int] = []

    def _kill(self, sig: int) -> None:
        # TASKMGR_TEST_IGNORE_KILL: test-only, simulates processes that survive SIGKILL.
        if os.environ.get("TASKMGR_TEST_IGNORE_KILL") == "1":
            return
        try:
            os.killpg(self.pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def start(self, reason: str) -> None:
        if reason == "cancel":
            self.kill()
            return
        self._kill(signal.SIGTERM)
        self.phase = "term"
        self.deadline = time.monotonic() + self.grace

    def kill(self) -> None:
        self._kill(signal.SIGKILL)
        self.phase = "kill"
        self.deadline = time.monotonic() + self.confirm

    def honor_cancel(self, job: Job) -> None:
        """A cancel that arrives during the grace period of another stop ends the wait; the
        stop keeps its reason."""
        if job.get("cancel_requested") and self.phase == "term":
            self.kill()

    def step(self) -> str | None:
        """Advance; returns None (in progress), "stopped" or "unconfirmed"."""
        if self.reap:
            _ = self.reap()
        self.alive = procutil.group_members(self.pgid, self.min_start, self.jobid)
        if not self.alive:
            return "stopped"
        now = time.monotonic()
        if self.phase == "term" and now >= self.deadline:
            self.kill()
        elif self.phase == "kill" and now >= self.deadline:
            return "unconfirmed"
        return None


def set_stopping(store: Store, jobid: str, reason: str) -> bool:
    """Persist running -> stopping with the stop reason. Returns False if not applicable."""
    with store.state_lock():
        jobdir, job = store.load_job(jobid)
        if job["state"] not in ("running", "stopping"):
            return False
        if job["state"] == "running" or job.get("stop_reason") != reason:
            job["state"] = "stopping"
            job["stop_reason"] = reason
            _ = store.save_job(jobdir, job)
        return True


def requested_stop(job: Job, elapsed: float | None, timeout: float | None) -> str | None:
    """The reason to stop a running entry now, if any: a cancel request, a stop reason
    already recorded in job.json, or timeout_seconds exceeded (`elapsed` since the start)."""
    if job.get("cancel_requested"):
        return "cancel"
    if job["state"] == "stopping" and job.get("stop_reason"):
        return job["stop_reason"]
    if timeout and elapsed is not None and elapsed >= timeout:
        return "timeout"
    return None


def end_cancelled(store: Store, jobid: str, runner: Runner | None = None) -> Job:
    """End a cancelled job when nothing of it runs, so the stop is confirmed at once:
    running -> stopping -> cancelled.

    With `runner` (the in-memory record of a run whose entry never started) the job ends
    without a normal exit and the record is written; without it the run is finalized.
    """
    _ = set_stopping(store, jobid, "cancel")
    if runner is None:
        return finalize_run(store, jobid, status="cancelled")
    with store.state_lock():
        jobdir, job = store.load_job(jobid)
        runner["status"] = "cancelled"
        jobdir = end_without_result(store, jobdir, job, "cancelled", None, runner)
        store.write_runner(jobdir, runner)
        return job


def record_unconfirmed(
    store: Store,
    jobid: str,
    jobdir: str,
    runner: Runner | None,
    reason: str | None,
    alive: list[int],
) -> None:
    """The stop procedure could not confirm the group gone after SIGKILL: note the stop
    reason in runner.json (read from disk when `runner` is None) and mark the job unknown."""
    if runner is None:
        runner, _ = store.read_runner(jobdir)
    if runner is not None:
        runner["stop_unconfirmed_reason"] = reason
        store.write_runner(jobdir, runner)
    _ = mark_unknown(
        store,
        jobid,
        "stop_unconfirmed",
        "processes still alive after SIGKILL",
        {"alive_pids": alive},
    )


def mark_unknown(
    store: Store,
    jobid: str,
    kind: str,
    message: str,
    details: object = None,
) -> Job:
    with store.state_lock():
        jobdir, job = store.load_job(jobid)
        if job["state"] not in ("running", "stopping"):
            return job
        runner, _ = store.read_runner(jobdir)
        if runner is not None:
            runner["unknown"] = {
                "kind": kind,
                "message": message,
                "details": cast(JsonValue, details),  # passed through to JSON
                "at": utcnow_iso(),
            }
            store.write_runner(jobdir, runner)
        job["state"] = "unknown"
        job["error"] = {"kind": kind, "message": message}
        _ = store.save_job(jobdir, job)
        return job


def identity_of(runner: object) -> JsonDict | None:
    keys = (
        "boot_id",
        "shim_pid",
        "shim_start_time",
        "pgid",
        "leader_pid",
        "leader_start_time",
        "argv_hash",
    )
    if not isinstance(runner, dict):
        return None
    r = cast(JsonDict, runner)
    return {k: r.get(k) for k in keys}


def _has_identity(runner: Runner) -> bool:
    return (
        runner.get("boot_id") is not None
        and runner.get("pgid") is not None
        and runner.get("leader_start_time") is not None
    )


def may_have_started(runner: Runner) -> bool:
    """False only if the record proves the entry command never ran (Section 12.1)."""
    return runner.get("pgid") is not None or runner.get("boot_id") is not None


def requeue_gpu_busy(store: Store, jobid: str) -> Job | None:
    """The shim died between a "GPU busy" exit and the relaunch (process group gone).

    The entry did no work, so the same job goes back to queued (keeping its jobid and
    enqueue_seq; not an interruption, `auto_restarts` is unchanged): the workdir is kept
    for the next dispatch and runner.json is moved aside as runner.prev-N.json. A requested cancel ends the job
    as cancelled instead. Returns None when the run record does not show such an exit or
    the job is not running/stopping (an unknown job is never queued again).
    """
    with store.state_lock():
        jobdir, job = store.load_job(jobid)
        runner, _ = store.read_runner(jobdir)
        if (
            job["state"] not in ("running", "stopping")
            or runner is None
            or not store.ended_gpu_busy(runner)
        ):
            return None
        if not job.get("cancel_requested"):
            # The workdir stays: the next dispatch of this same job reuses it (GPU contract).
            # job.json first: a crash before the rename leaves a queued job whose runner.json
            # shows the busy exit, which dispatch recognizes and sets aside as well.
            job["state"] = "queued"
            job["dispatched_at"] = None
            job["stop_reason"] = None
            job["error"] = None
            _ = job.pop("pending_reason", None)
            jobdir = store.save_job(jobdir, job)
            _ = store.set_runner_aside(jobdir)
            return job
    # Outside the lock: finalizing reads the task's result.json. end_cancelled re-checks
    # the state under the lock.
    return end_cancelled(store, jobid)


def finish_stopped(
    store: Store,
    jobid: str,
    stop_reason: str | None,
    exited: bool,
    warnings: Iterable[str] = (),
) -> Job:
    """Finalize once the process group is confirmed gone.

    An entry that exited with gpu_busy_exit_code (and was not stopped for cancel or
    timeout) is not finalized: the job is queued again (`requeue_gpu_busy`).
    """
    if stop_reason not in STOP_OUTCOME and exited:
        requeued = requeue_gpu_busy(store, jobid)
        if requeued is not None:
            return requeued
    if stop_reason in STOP_OUTCOME:
        return finalize_run(store, jobid, status=STOP_OUTCOME[stop_reason], warnings=warnings)
    if exited:
        return finalize_run(store, jobid, warnings=warnings)
    return finalize_run(
        store,
        jobid,
        status="failed",
        warnings=warnings,
        error={
            "kind": "exit_code_lost",
            "message": "the task ended while its supervisor was gone; exit code unavailable",
        },
    )


def assess(store: Store, jobid: str, resolve: bool = False) -> Assessment:
    """Section 12.2 check for one job in running/stopping (or unknown when resolve=True).

    Returns an Assessment describing the decision. For decision "takeover" it
    holds a Takeover object (the caller owns its shim.lock fd).
    """
    jobdir, job = store.load_job(jobid)
    state = job["state"]
    # Without resolve an unknown job is left alone (the health check lists jobs under
    # the state lock but checks them without it, so the state may have changed).
    if state == "queued" or state in TERMINAL_STATES or (state == "unknown" and not resolve):
        return {"decision": "none", "state": state}
    runner, err = store.read_runner(jobdir)
    if runner is None:
        if not resolve:
            _ = mark_unknown(store, jobid, "record_incomplete", "runner.json is %s" % err)
        return {"decision": "unknown", "reason": "runner.json is %s" % err}

    if runner.get("final"):
        # An interrupted run is ended by step 5; an unknown job (resolve) is not re-run,
        # nor is a run an operator marked stopped (`resolve --as stopped` failed half-way).
        j = (
            interrupt_run(store, jobid, runner.get("interrupted_reason", ""), rerun=not resolve)
            if runner.get("status") == "interrupted"
            else finalize_run(store, jobid)
        )
        return {"decision": "restored", "state": j["state"]}

    lock_path = os.path.join(jobdir, "shim.lock")
    lock_fd = try_flock_fd(lock_path)
    if lock_fd is None and os.path.exists(lock_path):
        return {"decision": "shim_alive", "identity": identity_of(runner)}
    keep_fd = False
    try:
        if runner.get("status") == "waiting_gpu":
            # The shim died while waiting to relaunch after a "GPU busy" exit. It writes
            # this status only once the process group is gone, so nothing is running.
            j = requeue_gpu_busy(store, jobid)
            if j is not None:
                return {"decision": "requeued", "state": j["state"]}
        stop_reason = job.get("stop_reason") if state in ("stopping", "unknown") else None
        exited = bool(runner.get("exited"))
        if job.get("cancel_requested") and (
            state == "unknown" or (stop_reason is None and not exited)
        ):
            # A cancel request stays effective across restarts; on an unknown job it
            # turns into cancelled once the stop is confirmed (Section 13.2).
            stop_reason = "cancel"
        if not exited and not _has_identity(runner):
            if not may_have_started(runner):
                j = interrupt_run(store, jobid, "entry command never started", rerun=not resolve)
                return {"decision": "interrupted", "state": j["state"]}
            if not resolve:
                _ = mark_unknown(store, jobid, "record_incomplete", "identity record incomplete")
            return {
                "decision": "unknown",
                "reason": "identity record incomplete",
                "identity": identity_of(runner),
            }
        try:
            current_boot = procutil.boot_id()
        except OSError as e:
            if not resolve:
                _ = mark_unknown(store, jobid, "undeterminable", "cannot read boot_id: %s" % e)
            return {
                "decision": "unknown",
                "reason": "cannot read boot_id",
                "identity": identity_of(runner),
            }
        if runner.get("boot_id") != current_boot:
            if exited:
                j = finish_stopped(store, jobid, stop_reason, True)
                return {"decision": _finished(j), "state": j["state"]}
            j = interrupt_run(store, jobid, "machine rebooted", rerun=not resolve)
            return {"decision": "interrupted", "state": j["state"]}
        pgid, leader_start = _group_identity(runner)
        try:
            members = procutil.group_members(pgid, leader_start, jobid)
        except procutil.ProcUnavailable as e:
            if not resolve:
                _ = mark_unknown(store, jobid, "undeterminable", "cannot read /proc: %s" % e)
            return {
                "decision": "unknown",
                "reason": "cannot read /proc",
                "identity": identity_of(runner),
            }
        if not members:
            warnings: list[str] = []
            if "leftover" in (job.get("stop_reason"), runner.get("stop_unconfirmed_reason")):
                warnings = ["leftover_processes"]
            j = finish_stopped(store, jobid, stop_reason, exited, warnings)
            return {"decision": _finished(j), "state": j["state"]}
        if resolve:
            if runner.get("stop_unconfirmed_reason"):
                # SIGKILL already failed once: an operator has to look at it.
                return {
                    "decision": "still_running",
                    "alive_pids": members,
                    "identity": identity_of(runner),
                }
            # The unknown came from an undeterminable check that can now be decided:
            # hand the run back to the daemon, which takes over tracking (case 4.4).
            with store.state_lock():
                jobdir, job = store.load_job(jobid)
                job["state"] = "stopping" if job.get("stop_reason") else "running"
                job["error"] = None
                _ = store.save_job(jobdir, job)
            return {
                "decision": "handed_to_daemon",
                "alive_pids": members,
                "identity": identity_of(runner),
            }
        keep_fd = True
        tk = Takeover(store, jobid, jobdir, runner, lock_fd, exited, job)
        return {"decision": "takeover", "takeover": tk, "alive_pids": members}
    finally:
        if lock_fd is not None and not keep_fd:
            os.close(lock_fd)


def _finished(job: Job) -> Decision:
    return "requeued" if job["state"] == "queued" else "finalized"


def _group_identity(runner: Runner) -> tuple[int, int]:
    """(pgid, leader_start_time), read with plain subscripts as before.

    Missing keys raise KeyError; the values are not re-checked (a None pgid
    simply matches no process in procutil.group_members).
    """
    r = cast(Mapping[str, object], cast(object, runner))
    return cast(int, r["pgid"]), cast(int, r["leader_start_time"])


class Takeover:
    """Daemon-side tracking of a run whose shim died while the task lives on."""

    def __init__(
        self,
        store: Store,
        jobid: str,
        jobdir: str,
        runner: Runner,
        lock_fd: int | None,
        exited: bool,
        job: Job,
    ) -> None:
        self.store: Store = store
        self.jobid: str = jobid
        self.lock_fd: int | None = lock_fd
        self.pgid: int
        self.min_start: int
        self.pgid, self.min_start = _group_identity(runner)
        self.exited: bool = exited
        self.started_epoch: float | None = runner.get("started_at_epoch")
        self.timeout: float | None = run_command(load_task(jobdir), job)[1]
        self.stop_reason: str | None = (
            job.get("stop_reason") if job["state"] == "stopping" else None
        )
        self.stopper: GroupStopper | None = None
        self.last_poll: float = 0.0
        self.warnings: list[str] = []
        self.done: bool = False
        cfg = store.config
        self.poll: float = float(cfg["control_poll_seconds"])
        self.grace: float = cfg["stop_grace_seconds"]
        self.confirm: float = cfg["kill_confirm_seconds"]
        self._note()
        if self.exited:
            if self.stop_reason in (None, "leftover"):
                self.warnings.append("leftover_processes")
            self._begin_stop(self.stop_reason or "leftover")
        elif self.stop_reason:
            self._begin_stop(self.stop_reason)

    def _jobdir(self) -> str:
        # The directory stays in in-progress/ while the job runs, but look it up anyway.
        return cast(str, self.store.find_job_dir(self.jobid))

    def _note(self) -> None:
        jobdir = self._jobdir()
        runner, _ = self.store.read_runner(jobdir)
        if runner is not None:
            runner.setdefault("takeovers", []).append({"at": utcnow_iso(), "by_pid": os.getpid()})
            self.store.write_runner(jobdir, runner)

    def _begin_stop(self, reason: str) -> None:
        self.stop_reason = reason
        if reason in STOP_OUTCOME:
            _ = set_stopping(self.store, self.jobid, reason)
        self.stopper = GroupStopper(self.pgid, self.min_start, self.jobid, self.grace, self.confirm)
        self.stopper.start(reason)

    def close(self) -> None:
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
        self.done = True

    def tick(self) -> None:
        if self.done:
            return None
        try:
            if self.stopper is None:
                if not procutil.group_members(self.pgid, self.min_start, self.jobid):
                    _ = finish_stopped(self.store, self.jobid, None, self.exited, self.warnings)
                    return self.close()
                now = time.monotonic()
                if now - self.last_poll >= self.poll:
                    self.last_poll = now
                    _, job = self.store.load_job(self.jobid)
                    if job["state"] not in ("running", "stopping"):
                        return self.close()
                    elapsed = time.time() - self.started_epoch if self.started_epoch else None
                    reason = requested_stop(job, elapsed, self.timeout)
                    if reason:
                        self._begin_stop(reason)
                return None
            now = time.monotonic()
            if now - self.last_poll >= self.poll:
                self.last_poll = now
                self.stopper.honor_cancel(self.store.load_job(self.jobid)[1])
            r = self.stopper.step()
            if r == "stopped":
                _ = finish_stopped(
                    self.store, self.jobid, self.stop_reason, self.exited, self.warnings
                )
                self.close()
            elif r == "unconfirmed":
                record_unconfirmed(
                    self.store,
                    self.jobid,
                    self._jobdir(),
                    None,
                    self.stop_reason,
                    self.stopper.alive,
                )
                self.close()
        except procutil.ProcUnavailable as e:
            _ = mark_unknown(self.store, self.jobid, "undeterminable", "cannot read /proc: %s" % e)
            self.close()
