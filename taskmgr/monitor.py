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
from .common import TERMINAL_STATES, try_flock_fd, try_read_json, utcnow_iso
from .records import Job, JsonDict, JsonValue, Runner
from .store import Store, finalize_run, interrupt_run, load_task

STOP_OUTCOME = {"cancel": "cancelled", "timeout": "timed_out"}

Decision = Literal[
    "none",
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

    state      none, restored, interrupted, finalized
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
    """Non-blocking stop procedure (Section 13.1): SIGTERM, grace, SIGKILL, confirm."""

    def __init__(
        self,
        pgid: int,
        min_start: int,
        grace: float,
        confirm: float,
        reap: Callable[[], object] | None = None,
    ) -> None:
        self.pgid: int = pgid
        self.min_start: int = min_start
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

    def start(self) -> None:
        self._kill(signal.SIGTERM)
        self.phase = "term"
        self.deadline = time.monotonic() + self.grace

    def step(self) -> str | None:
        """Advance; returns None (in progress), "stopped" or "unconfirmed"."""
        if self.reap:
            _ = self.reap()
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


def finish_stopped(
    store: Store,
    jobid: str,
    stop_reason: str | None,
    exited: bool,
    warnings: Iterable[str] = (),
) -> Job:
    """Finalize once the process group is confirmed gone."""
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
    if state == "queued" or state in TERMINAL_STATES:
        return {"decision": "none", "state": state}
    runner, err = store.read_runner(jobdir)
    if runner is None:
        if not resolve:
            _ = mark_unknown(store, jobid, "record_incomplete", "runner.json is %s" % err)
        return {"decision": "unknown", "reason": "runner.json is %s" % err}

    if runner.get("final"):
        if runner.get("status") == "interrupted":
            j = interrupt_run(store, jobid, runner.get("interrupted_reason", ""))
        else:
            j = finalize_run(store, jobid)
        return {"decision": "restored", "state": j["state"]}

    lock_path = os.path.join(jobdir, "shim.lock")
    lock_fd = try_flock_fd(lock_path)
    if lock_fd is None and os.path.exists(lock_path):
        return {"decision": "shim_alive", "identity": identity_of(runner)}
    keep_fd = False
    try:
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
                j = interrupt_run(store, jobid, "entry command never started")
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
                return {"decision": "finalized", "state": j["state"]}
            j = interrupt_run(store, jobid, "machine rebooted")
            return {"decision": "interrupted", "state": j["state"]}
        pgid, leader_start = _group_identity(runner)
        try:
            members = procutil.group_members(pgid, leader_start)
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
            return {"decision": "finalized", "state": j["state"]}
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
        self.timeout: float | None = load_task(jobdir).get("timeout_seconds")
        self.stop_reason: str | None = (
            job.get("stop_reason") if job["state"] == "stopping" else None
        )
        self.stopper: GroupStopper | None = None
        self.last_poll: float = 0.0
        self.warnings: list[str] = []
        self.done: bool = False
        cfg = store.config
        self.poll: float = float(cfg.get("control_poll_seconds", 2))
        self.grace: float = cfg.get("stop_grace_seconds", 30)
        self.confirm: float = cfg.get("kill_confirm_seconds", 10)
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
        self.stopper = GroupStopper(self.pgid, self.min_start, self.grace, self.confirm)
        self.stopper.start()

    def close(self) -> None:
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
        self.done = True

    def tick(self) -> None:
        if self.done:
            return
        try:
            if self.stopper is None:
                if not procutil.group_members(self.pgid, self.min_start):
                    _ = finish_stopped(self.store, self.jobid, None, self.exited, self.warnings)
                    return self.close()
                now = time.monotonic()
                if now - self.last_poll >= self.poll:
                    self.last_poll = now
                    _, job = self.store.load_job(self.jobid)
                    if job["state"] not in ("running", "stopping"):
                        return self.close()
                    if job.get("cancel_requested"):
                        self._begin_stop("cancel")
                    elif (
                        self.timeout
                        and self.started_epoch
                        and time.time() - self.started_epoch >= self.timeout
                    ):
                        self._begin_stop("timeout")
                return
            r = self.stopper.step()
            if r == "stopped":
                _ = finish_stopped(
                    self.store, self.jobid, self.stop_reason, self.exited, self.warnings
                )
                self.close()
            elif r == "unconfirmed":
                self._record_unconfirmed()
                self.close()
        except procutil.ProcUnavailable as e:
            _ = mark_unknown(self.store, self.jobid, "undeterminable", "cannot read /proc: %s" % e)
            self.close()

    def _record_unconfirmed(self) -> None:
        jobdir = self._jobdir()
        runner, _ = self.store.read_runner(jobdir)
        if runner is not None:
            runner["stop_unconfirmed_reason"] = self.stop_reason
            self.store.write_runner(jobdir, runner)
        _ = mark_unknown(
            self.store,
            self.jobid,
            "stop_unconfirmed",
            "processes still alive after SIGKILL",
            {"alive_pids": self.stopper.alive if self.stopper else []},
        )


def runner_status_snapshot(jobdir: str) -> JsonValue:
    runner, _ = try_read_json(os.path.join(jobdir, "runner.json"))
    return runner
