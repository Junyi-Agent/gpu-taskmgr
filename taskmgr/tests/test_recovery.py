"""Daemon restarts, shim crashes, takeover, reboot, unknown state and resolve (Sections 12, 13)."""

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from typing import cast

from taskmgr import procutil
from taskmgr.records import JsonDict, JsonValue, Runner
from taskmgr.store import Store
from taskmgr.tests.helpers import OK_RESULT, REPO, TASK, Env, kill_quiet, pid_alive


class RecoveryTest(Env):
    def counter(self) -> str:
        return os.path.join(self.tmp, "runs")

    def runs(self) -> int:
        try:
            with open(self.counter()) as f:
                return len(f.read().splitlines())
        except FileNotFoundError:
            return 0

    def shim_log(self, jobid: str) -> str:
        try:
            with open(os.path.join(self.job_dir(jobid), "shim.log")) as f:
                return f.read()
        except OSError:
            return ""

    def kill_shim(self, jobid: str) -> Runner:
        r = self.wait_runner(jobid, "pgid")
        # The identity is written before the shim releases the child to exec; a shim killed
        # in between leaves a child that exits without running the entry. Wait until the
        # shim has released it ("started pid=..." is logged right after).
        _ = self.wait_for(lambda: "started pid=" in self.shim_log(jobid), 10, "entry release")
        kill_quiet(self.num(r, "shim_pid"))
        _ = self.wait_for(lambda: not pid_alive(self.num(r, "shim_pid")), 10, "shim death")
        return r

    def test_systemd_user_unit_uses_killmode_process(self) -> None:
        with open(os.path.join(REPO, "taskmgr", "deploy", "taskmgr-remote.user.service")) as f:
            unit = f.read()
        self.assertIn("KillMode=process", unit)
        self.assertIn(" serve", unit)
        self.assertNotIn("User=", unit)  # user unit: runs as the unprivileged owner

    def test_task_survives_daemon_stop_and_keeps_real_exit_code(self) -> None:
        _ = self.start_daemon()
        gate = os.path.join(self.tmp, "go")
        j = self.submit(
            self.make_pkg(script="while [ ! -f %s ]; do sleep 0.05; done; exit 7" % gate)
        )
        r = self.wait_runner(j, "pgid")
        # Stop the daemon the way systemd does with KillMode=process (and even its group).
        self.stop_daemon(group=True)
        self.assertTrue(pid_alive(self.num(r, "shim_pid")))
        self.assertTrue(pid_alive(self.num(r, "leader_pid")))
        open(gate, "w").close()
        job = self.wait_state(j, "failed")  # the shim finishes the job without the daemon
        self.assertEqual(self.get(self.result(j), "_taskmgr", "return_code"), 7)
        _ = self.start_daemon()
        time.sleep(1)
        self.assertEqual(self.job(j)["state"], "failed")
        self.assertEqual(job["retried_as"], [])
        self.assertEqual(self.jobids(), {j})

    def test_daemon_restart_while_running(self) -> None:
        _ = self.start_daemon()
        j = self.submit(self.make_pkg(script="sleep 2; exit 0"))
        r = self.wait_runner(j, "pgid")
        self.stop_daemon()
        _ = self.start_daemon()
        _ = self.wait_state(j, "invalid_result", timeout=20)
        self.assertEqual(self.num(self.runner(j), "shim_pid"), self.num(r, "shim_pid"))
        self.assertEqual(self.get(self.result(j), "_taskmgr", "return_code"), 0)
        self.assertEqual(self.jobids(), {j})

    def test_crash_after_exit_recorded_is_not_rerun(self) -> None:
        _ = self.start_daemon(env={"TASKMGR_TEST_SHIM_CRASH": "after_exit_recorded"})
        j = self.submit(self.make_pkg(script="echo x >> %s; exit 0" % self.counter()))
        _ = self.wait_state(j, "invalid_result", timeout=20)
        r = self.runner(j)
        self.assertTrue(self.get(r, "final"))
        self.assertEqual(self.get(r, "return_code"), 0)
        self.assertEqual(self.runs(), 1)
        self.assertEqual(self.jobids(), {j})
        with open(os.path.join(self.job_dir(j), "shim.log")) as f:
            self.assertIn("test crash", f.read())

    def test_crash_before_exec_is_not_rerun(self) -> None:
        """The shim dies after the identity record is durable, before it releases the child
        to exec. The record says the entry may have run, so recovery must not run it again:
        the child exits without exec when the pipe closes, the group is gone, and the job
        ends failed / exit_code_lost (docs/recovery.md step 4.5)."""
        _ = self.start_daemon(env={"TASKMGR_TEST_SHIM_CRASH": "before_exec"})
        j = self.submit(self.make_pkg(script="echo x >> %s; " % self.counter() + OK_RESULT))
        job = self.wait_state(j, "failed", timeout=20)

        self.assertEqual(self.get(job, "error", "kind"), "exit_code_lost")
        self.assertEqual(job["retried_as"], [])
        self.assertEqual(self.jobids(), {j})
        r = self.runner(j)
        self.assertIsNotNone(r.get("pgid"))  # the identity was recorded
        self.assertEqual((r.get("final"), r.get("return_code")), (True, None))
        self.assertEqual(self.get(self.result(j), "_taskmgr", "status"), "failed")
        self.assertEqual(self.runs(), 0)  # the entry command never executed
        self.assertIn("test crash at before_exec", self.shim_log(j))

    def test_crash_before_finalize_is_finalized_by_recovery(self) -> None:
        """The shim dies after the exit is recorded and the group is gone, before it
        finalizes: recovery finishes the finalization with the recorded exit and stop
        reason, and nothing runs again (docs/recovery.md step 2)."""
        _ = self.start_daemon(env={"TASKMGR_TEST_SHIM_CRASH": "before_finalize"})
        ok = self.submit(self.make_pkg(script="echo x >> %s; " % self.counter() + OK_RESULT))
        slow = self.submit(self.make_pkg(script="sleep 60", timeout=1))
        job = self.wait_state(ok, "succeeded", timeout=20)

        self.assertEqual(job["retried_as"], [])
        r = self.runner(ok)
        self.assertEqual((r.get("final"), r.get("return_code")), (True, 0))
        self.assertEqual(self.get(self.result(ok), "_taskmgr", "status"), "succeeded")
        self.assertEqual(self.runs(), 1)
        self.assertIn("test crash at before_finalize", self.shim_log(ok))

        # A stop for the timeout: the job was stopping, and recovery keeps the reason.
        job = self.wait_state(slow, "timed_out", timeout=20)
        self.assertEqual(job["stop_reason"], "timeout")
        self.assertEqual(self.get(self.result(slow), "_taskmgr", "status"), "timed_out")
        self.assertIn("test crash at before_finalize", self.shim_log(slow))
        self.assertEqual(self.jobids(), {ok, slow})

    def test_crash_before_identity_reruns_as_new_jobs_up_to_the_limit(self) -> None:
        # max_auto_restarts=2: the original job and two automatic re-runs, each
        # interrupted before its entry command started; the last one gives up.
        _ = self.start_daemon(env={"TASKMGR_TEST_SHIM_CRASH": "before_identity"})
        j = self.submit(self.make_pkg(script="echo x >> %s" % self.counter()))
        seq = self.job(j)["enqueue_seq"]

        def chain_done() -> bool:
            try:
                err = self.job(self.chain(j)[-1])["error"]
            except (OSError, ValueError):
                return False
            return err is not None and err["kind"] == "too_many_interruptions"

        _ = self.wait_for(chain_done, 30, "the re-run chain to give up")
        ids = self.chain(j)
        self.assertEqual(len(ids), 3)
        self.assertEqual(self.jobids(), set(ids))
        for i, jid in enumerate(ids):
            job = self.wait_state(jid, "failed")
            self.assertEqual(job["auto_restarts"], i)
            self.assertEqual(job["enqueue_seq"], seq)  # keeps the queue position
            self.assertEqual(job["retry_of"], ids[i - 1] if i else None)
            r = self.runner(jid)
            self.assertEqual(
                (r.get("status"), r.get("interrupted_reason")),
                ("interrupted", "entry command never started"),
            )
            m = self.obj(self.result(jid), "_taskmgr")
            self.assertEqual((m["status"], m["retry_of"]), ("failed", job["retry_of"]))
            kind = self.get(job, "error", "kind")
            self.assertEqual(kind, "too_many_interruptions" if i == 2 else "interrupted")
        self.assertEqual(self.job(ids[2])["retried_as"], [])
        self.assertEqual(self.runs(), 0)  # the entry command never executed

    def test_takeover_after_shim_death_ends_exit_code_lost(self) -> None:
        _ = self.start_daemon()
        # The task waits for a gate file, so it is certainly alive when the daemon notices
        # the dead shim (a fixed sleep raced the health check under load).
        gate = os.path.join(self.tmp, "go")
        j = self.submit(
            self.make_pkg(script="while [ ! -f %s ]; do sleep 0.05; done; exit 0" % gate)
        )
        _ = self.kill_shim(j)
        _ = self.wait_for(lambda: self.runner(j).get("takeovers"), 10, "takeover")
        self.assertEqual(self.job(j)["state"], "running")
        open(gate, "w").close()
        job = self.wait_state(j, "failed", timeout=20)
        self.assertEqual(self.get(job, "error", "kind"), "exit_code_lost")
        self.assertIsNone(self.get(self.result(j), "_taskmgr", "return_code"))
        self.assertEqual(job["retried_as"], [])

    def test_takeover_group_with_exited_leader_is_not_stopped(self) -> None:
        _ = self.start_daemon()
        marker = os.path.join(self.tmp, "member.pid")
        j = self.submit(self.make_pkg(script="sleep 4 & echo $! > %s; sleep 1; exit 0" % marker))
        r = self.kill_shim(j)
        _ = self.wait_for(lambda: not pid_alive(self.num(r, "leader_pid")), 10, "leader exit")
        with open(marker) as f:
            member = int(f.read())
        self.assertTrue(pid_alive(member))
        time.sleep(1)
        self.assertEqual(self.job(j)["state"], "running")
        _ = self.wait_for(lambda: not pid_alive(member), 10, "member exit")
        job = self.wait_state(j, "failed", timeout=10)
        self.assertEqual(self.get(job, "error", "kind"), "exit_code_lost")

    def test_cancel_and_timeout_during_takeover(self) -> None:
        _ = self.start_daemon()
        c = self.submit(self.make_pkg(script="trap '' TERM; while true; do sleep 0.1; done"))
        t = self.submit(self.make_pkg(script="sleep 60", timeout=4))
        rc_ = self.kill_shim(c)
        rt = self.kill_shim(t)
        _ = self.wait_for(lambda: self.runner(c).get("takeovers"), 10, "takeover")
        _ = self.ok("cancel", "job", c, "--host", "s1")
        _ = self.wait_state(c, "cancelled", timeout=20)
        _ = self.wait_state(t, "timed_out", timeout=20)
        self.assertFalse(pid_alive(self.num(rc_, "leader_pid")))
        self.assertFalse(pid_alive(self.num(rt, "leader_pid")))

    def test_cancel_during_takeover_kills_without_the_grace_period(self) -> None:
        self.set_config(stop_grace_seconds=60)
        _ = self.start_daemon()
        j = self.submit(self.make_pkg(script="trap '' TERM; while true; do sleep 0.1; done"))
        r = self.kill_shim(j)
        _ = self.wait_for(lambda: self.runner(j).get("takeovers"), 10, "takeover")
        _ = self.ok("cancel", "job", j, "--host", "s1")
        _ = self.wait_state(j, "cancelled", timeout=10)
        self.assertFalse(pid_alive(self.num(r, "leader_pid")))

    def test_cancel_request_survives_daemon_restart(self) -> None:
        _ = self.start_daemon()
        j = self.submit(self.make_pkg(script="sleep 60"))
        _ = self.wait_runner(j, "pgid")
        self.stop_daemon()
        r = self.kill_shim(j)
        _ = self.ok("cancel", "job", j, "--host", "s1")
        self.assertEqual(self.job(j)["state"], "running")
        _ = self.start_daemon()
        _ = self.wait_state(j, "cancelled", timeout=20)
        self.assertFalse(pid_alive(self.num(r, "leader_pid")))
        self.assertEqual(self.jobids(), {j})

    def test_exit_code_lost_when_shim_and_task_gone(self) -> None:
        _ = self.start_daemon()
        j = self.submit(self.make_pkg(script="echo x >> %s; sleep 1; exit 5" % self.counter()))
        _ = self.wait_runner(j, "pgid")
        self.stop_daemon()
        r = self.kill_shim(j)
        _ = self.wait_for(lambda: not pid_alive(self.num(r, "leader_pid")), 10, "task end")
        _ = self.start_daemon()
        job = self.wait_state(j, "failed")
        self.assertEqual(self.get(job, "error", "kind"), "exit_code_lost")
        time.sleep(1)
        self.assertEqual(self.runs(), 1)
        self.assertEqual(self.jobids(), {j})

    def _simulate_reboot(self, jobid: str) -> Runner:
        r = self.wait_runner(jobid, "pgid")
        self.stop_daemon()
        # The "pre-reboot" processes must really be gone before swapping boot_id.
        kill_quiet(self.num(r, "shim_pid"))
        kill_quiet(self.num(r, "pgid"), group=True)
        _ = self.wait_for(
            lambda: (
                not pid_alive(self.num(r, "leader_pid")) and not pid_alive(self.num(r, "shim_pid"))
            ),
            10,
            "processes gone",
        )
        self.set_boot("boot-2")
        return r

    def test_reboot_reruns_as_a_new_job_and_wait_follows_it(self) -> None:
        _ = self.start_daemon()
        script = "echo x >> {c}; [ $(wc -l < {c}) -ge 2 ] && {ok} && exit 0; sleep 60".format(
            c=self.counter(), ok=OK_RESULT
        )
        j = self.submit(self.make_pkg(script=script, gpus=[0]))
        seq = self.job(j)["enqueue_seq"]
        _ = self.wait_runner(j, "pgid")
        waiter = subprocess.Popen(
            [
                sys.executable,
                TASK,
                "wait",
                "job",
                j,
                "-o",
                "json",
                "--plain",
                "--poll-interval",
                "0.2",
            ],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _ = self._simulate_reboot(j)
        _ = self.start_daemon()
        old = self.wait_state(j, "failed", timeout=20)
        self.assertEqual(
            self.get(old, "error"), {"kind": "interrupted", "message": "machine rebooted"}
        )
        self.assertEqual(len(old["retried_as"]), 1)
        new = old["retried_as"][0]
        job = self.wait_state(new, "succeeded", timeout=20)
        self.assertEqual(job["retry_of"], j)
        self.assertEqual(job["enqueue_seq"], seq)
        self.assertEqual(job["auto_restarts"], 1)
        self.assertEqual(job["gpus"], old["gpus"])
        self.assertEqual(self.get(self.runner(j), "status"), "interrupted")
        self.assertEqual(self.get(self.runner(new), "boot_id"), "boot-2")
        self.assertEqual(self.get(self.result(j), "_taskmgr", "status"), "failed")
        self.assertEqual(self.get(self.result(new), "_taskmgr", "retry_of"), j)
        self.assertEqual(self.runs(), 2)
        self.assertEqual(self.jobids(), {j, new})
        # The waiter followed the automatic re-run and reports the job it ended on.
        out, err = waiter.communicate(timeout=30)
        self.assertEqual(waiter.returncode, 0, err)
        self.assertIn(
            "job %s was interrupted; following automatic re-run %s" % (j, new), err.decode()
        )
        res = self.obj(cast(JsonValue, json.loads(out)))
        self.assertEqual(self.get(res, "_taskmgr", "jobid"), new)
        self.assertEqual((res["retry_of"], res["retried_as"]), (j, []))

    def test_reboot_beyond_auto_restart_limit_fails(self) -> None:
        self.set_config(max_auto_restarts=0)
        _ = self.start_daemon()
        j = self.submit(self.make_pkg(script="sleep 60"))
        _ = self._simulate_reboot(j)
        _ = self.start_daemon()
        job = self.wait_state(j, "failed")
        self.assertEqual(self.get(job, "error", "kind"), "too_many_interruptions")
        self.assertEqual(job["retried_as"], [])
        self.assertEqual(self.jobids(), {j})

    def test_reboot_with_cancel_requested_is_cancelled(self) -> None:
        _ = self.start_daemon()
        j = self.submit(self.make_pkg(script="sleep 60"))
        _ = self.wait_runner(j, "pgid")
        self.stop_daemon()
        _ = self.ok("cancel", "job", j, "--host", "s1")
        r = self.runner(j)
        kill_quiet(self.num(r, "shim_pid"))
        kill_quiet(self.num(r, "pgid"), group=True)
        _ = self.wait_for(lambda: not pid_alive(self.num(r, "leader_pid")), 10, "processes gone")
        self.set_boot("boot-2")
        _ = self.start_daemon()
        _ = self.wait_state(j, "cancelled")
        self.assertEqual(self.job(j)["retried_as"], [])
        self.assertEqual(self.jobids(), {j})

    def test_dispatch_over_a_stale_run_record(self) -> None:
        # A crash between writing runner.json and job.json at dispatch leaves a queued
        # job with a run record. Without an execution identity it is dispatched again;
        # with one (a task may have started) it becomes unknown and keeps its GPUs.
        a = self.submit(self.make_pkg())
        b = self.submit(self.make_pkg(gpus=[0]))
        c = self.submit(self.make_pkg(gpus=[0]))
        stale: JsonDict = {"jobid": a, "status": "dispatched", "final": False, "exited": False}
        started: JsonDict = dict(stale, jobid=b, boot_id="boot-1", pgid=999999, leader_pid=999999)
        for jid, rec in ((a, stale), (b, started)):
            with open(os.path.join(self.job_dir(jid), "runner.json"), "w") as f:
                json.dump(rec, f)
        _ = self.start_daemon()
        _ = self.wait_state(a, "succeeded")
        self.assertIsNotNone(self.runner(a).get("pgid"))
        job = self.wait_state(b, "unknown")
        self.assertEqual(self.get(job, "error", "kind"), "record_incomplete")
        time.sleep(1)
        self.assertEqual(self.job(c)["state"], "queued")  # GPU 0 stays held by b
        self.assertEqual(self.jobids(), {a, b, c})

    # -- unknown --------------------------------------------------------------
    def make_unknown(
        self,
        gpus: tuple[int, ...] = (0,),
        state: str = "unknown",
        unconfirmed: bool = True,
        runner_extra: JsonDict | None = None,
        job_extra: JsonDict | None = None,
    ) -> tuple[str, "subprocess.Popen[bytes]"]:
        """Fixture: a job in `state` whose recorded process group is a live process.

        unconfirmed=True records that the stop procedure already failed (SIGKILL did not
        help); False models an unknown caused by an undeterminable check.
        """
        j = self.submit(self.make_pkg(gpus=list(gpus)))
        proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.addCleanup(lambda: (kill_quiet(proc.pid), proc.wait()))
        jd = self.job_dir(j)
        os.makedirs(os.path.join(jd, "workdir"))
        runner: JsonDict = {
            "jobid": j,
            "final": False,
            "exited": False,
            "boot_id": "boot-1",
            "shim_pid": 999999,
            "shim_start_time": 1,
            "pgid": proc.pid,
            "leader_pid": proc.pid,
            "leader_start_time": procutil.start_time(proc.pid),
            "argv_hash": "x",
            "started_at": "2026-10-29T08:00:00Z",
        }
        if unconfirmed:
            runner["stop_unconfirmed_reason"] = "fixture"
        runner.update(runner_extra or {})
        with open(os.path.join(jd, "runner.json"), "w") as f:
            json.dump(runner, f)
        job: dict[str, object] = dict(self.job(j))
        job.update(
            {
                "state": state,
                "dispatched_at": "2026-10-29T08:00:00Z",
                "error": {"kind": "stop_unconfirmed", "message": "fixture"}
                if state == "unknown"
                else None,
            }
        )
        job.update(job_extra or {})
        with open(os.path.join(jd, "job.json"), "w") as f:
            json.dump(job, f)
        os.rename(jd, os.path.join(self.root(), "in-progress", j))
        return j, proc

    def test_unknown_never_reruns_or_releases_and_resolve_check(self) -> None:
        u, proc = self.make_unknown()
        b = self.submit(self.make_pkg(gpus=[0]))
        _ = self.start_daemon()
        time.sleep(1.5)
        self.assertEqual(self.job(u)["state"], "unknown")
        self.assertEqual(self.job(b)["state"], "queued")  # GPU 0 stays held
        self.assertEqual(self.jobids(), {u, b})  # no re-run job
        rc, out, _ = self.task("wait", "job", u, "--poll-interval", "0.2")
        self.assertEqual(rc, 3)
        self.assertEqual(self.get(out, "state"), "unknown")
        rc, out, _ = self.task("retry", "job", u, "--host", "s1")
        self.assertEqual((rc, self.get(out, "error", "code")), (2, "resolve_required"))
        out = self.ok("cancel", "job", u, "--host", "s1")
        self.assertEqual((self.get(out, "state"), self.get(out, "changed")), ("unknown", True))
        out = self.ok("resolve", "job", u, "--host", "s1")
        self.assertEqual((self.get(out, "state"), self.get(out, "changed")), ("unknown", False))
        self.assertEqual(self.get(out, "identity", "pgid"), proc.pid)
        self.assertEqual(self.get(out, "identity", "boot_id"), "boot-1")
        kill_quiet(proc.pid)
        _ = proc.wait()
        out = self.ok("resolve", "job", u, "--host", "s1")
        self.assertEqual((self.get(out, "state"), self.get(out, "changed")), ("cancelled", True))
        _ = self.wait_state(b, "succeeded")  # GPU released

    def test_resolve_unknown_without_cancel_is_exit_code_lost(self) -> None:
        u, proc = self.make_unknown()
        kill_quiet(proc.pid)
        _ = proc.wait()
        out = self.ok("resolve", "job", u, "--host", "s1")
        self.assertEqual(self.get(out, "state"), "failed")
        self.assertEqual(self.get(self.job(u), "error", "kind"), "exit_code_lost")

    def test_resolve_as_stopped(self) -> None:
        u, _ = self.make_unknown()
        rc, out, _ = self.task("resolve", "job", u, "--host", "s1", "--as", "stopped")
        self.assertEqual(rc, 2)
        rc, out, _ = self.task(
            "resolve", "job", u, "--host", "s1", "--as", "stopped", "--confirm", "0000000000000000"
        )
        self.assertEqual(rc, 2)
        out = self.ok("resolve", "job", u, "--host", "s1", "--as", "stopped", "--confirm", u)
        self.assertEqual((self.get(out, "state"), self.get(out, "changed")), ("failed", True))
        job = self.job(u)
        self.assertEqual(self.get(job, "error", "kind"), "operator_marked_stopped")
        self.assertEqual(self.get(self.runner(u), "status"), "interrupted")
        _ = self.start_daemon()
        time.sleep(1)
        self.assertEqual(self.job(u)["retried_as"], [])  # nothing re-runs
        self.assertEqual(self.jobids(), {u})
        # With a cancel request the outcome is cancelled.
        u2, _ = self.make_unknown(gpus=(1,))
        _ = self.ok("cancel", "job", u2, "--host", "s1")
        out = self.ok("resolve", "job", u2, "--host", "s1", "--as", "stopped", "--confirm", u2)
        self.assertEqual(self.get(out, "state"), "cancelled")

    def test_daemon_never_touches_unknown_processes(self) -> None:
        u, proc = self.make_unknown()
        _ = self.start_daemon()
        time.sleep(1.5)
        self.assertEqual(self.job(u)["state"], "unknown")
        self.assertTrue(pid_alive(proc.pid))

    def test_unconfirmed_stop_becomes_unknown_then_resolves(self) -> None:
        # TASKMGR_TEST_IGNORE_KILL makes the stop procedure's signals no-ops, standing in
        # for a process that survives SIGKILL (e.g. stuck in uninterruptible sleep).
        _ = self.start_daemon(env={"TASKMGR_TEST_IGNORE_KILL": "1"})
        # The task runs until the test kills its group.
        j = self.submit(self.make_pkg(script="sleep 3600", gpus=[0]))
        r = self.wait_runner(j, "pgid")
        b = self.submit(self.make_pkg(gpus=[0]))
        _ = self.ok("cancel", "job", j, "--host", "s1")
        job = self.wait_state(j, "unknown", timeout=60)
        self.assertEqual(self.get(job, "error", "kind"), "stop_unconfirmed")
        self.assertTrue(pid_alive(self.num(r, "leader_pid")))
        _ = self.wait_scheduler_passes()  # GPU 0 stays held by the unknown job
        self.assertEqual(self.job(b)["state"], "queued")
        pgid, start = self.num(r, "pgid"), self.num(r, "leader_start_time")
        kill_quiet(pgid, group=True)
        # Wait for what resolve checks: no live process left in the group (the leader
        # can be gone while another member is still exiting).
        _ = self.wait_for(
            lambda: not procutil.group_members(pgid, start, j), 30, "the group to be gone"
        )
        out = self.ok("resolve", "job", j, "--host", "s1")
        # (The daemon log tells whether a health check took over the job after the shim
        # had marked it unknown: resolve then answers shim_alive while that takeover runs.)
        self.assertEqual(self.get(out, "state"), "cancelled", "%s\n%s" % (out, self.daemon_log()))
        _ = self.wait_state(b, "succeeded")

    def test_resolve_hands_decidable_unknown_back_to_takeover(self) -> None:
        u, proc = self.make_unknown(unconfirmed=False)
        _ = self.start_daemon()
        out = self.ok("resolve", "job", u, "--host", "s1")
        self.assertEqual(
            (self.get(out, "decision"), self.get(out, "state")), ("handed_to_daemon", "running")
        )
        _ = self.wait_for(lambda: self.runner(u).get("takeovers"), 10, "takeover")
        kill_quiet(proc.pid)
        _ = proc.wait()
        job = self.wait_state(u, "failed")
        self.assertEqual(self.get(job, "error", "kind"), "exit_code_lost")

    def test_cancel_on_unknown_wins_even_after_exit(self) -> None:
        u, proc = self.make_unknown(runner_extra={"exited": True, "return_code": 0, "signal": None})
        _ = self.ok("cancel", "job", u, "--host", "s1")
        kill_quiet(proc.pid)
        _ = proc.wait()
        out = self.ok("resolve", "job", u, "--host", "s1")
        self.assertEqual(self.get(out, "state"), "cancelled")

    def test_takeover_of_stopping_job_keeps_stop_reason(self) -> None:
        # The shim died after the entry exited but while cancel cleanup was running.
        u, proc = self.make_unknown(
            state="stopping",
            runner_extra={"exited": True, "return_code": 0, "signal": None},
            job_extra={"stop_reason": "cancel", "cancel_requested": True},
        )
        _ = self.start_daemon()
        job = self.wait_state(u, "cancelled", timeout=20)
        self.assertEqual(job["state"], "cancelled")
        self.assertFalse(pid_alive(proc.pid))

    def test_group_members_skip_processes_of_other_jobs(self) -> None:
        # After the recorded group is gone its pgid can be reused by a new session, e.g.
        # another job's task: only processes tagged with this jobid (or untagged ones of
        # the recorded leader's group) count.
        mine, other = "a" * 16, "b" * 16
        env = {k: v for k, v in os.environ.items() if k != "TASKMGR_JOBID"}
        tagged = subprocess.Popen(
            ["sleep", "30"], env=dict(env, TASKMGR_JOBID=other), start_new_session=True
        )
        untagged = subprocess.Popen(["sleep", "30"], env=env, start_new_session=True)
        for p in (tagged, untagged):
            self.addCleanup(p.wait)
            self.addCleanup(kill_quiet, p.pid)
        st = cast(int, procutil.start_time(tagged.pid))
        self.assertEqual(procutil.group_members(tagged.pid, st, other), [tagged.pid])
        self.assertEqual(procutil.group_members(tagged.pid, st, mine), [])
        self.assertEqual(procutil.group_members(tagged.pid, st), [tagged.pid])
        ust = cast(int, procutil.start_time(untagged.pid))
        # Untagged (a task may clear its environment): counts while the recorded leader
        # holds the pgid ...
        self.assertEqual(procutil.group_members(untagged.pid, ust, mine), [untagged.pid])
        # ... but not once the pid belongs to a newer process than the recorded leader.
        self.assertEqual(procutil.group_members(untagged.pid, ust - 1, mine), [])

    def test_job_that_cannot_be_finalized_does_not_block_others(self) -> None:
        # The shim and the task of `a` die while the daemon is down, and finalizing `a`
        # fails every time (result.json cannot be written). Recovery must not crash the
        # daemon: `a` ends unknown after a few attempts and `b` is still scheduled.
        _ = self.start_daemon()
        gate = os.path.join(self.tmp, "go")
        a = self.submit(
            self.make_pkg(script="while [ ! -f %s ]; do sleep 0.05; done; exit 0" % gate)
        )
        _ = self.wait_for(lambda: "started pid=" in self.shim_log(a), 10, "entry release")
        self.stop_daemon()
        r = self.kill_shim(a)
        kill_quiet(self.num(r, "leader_pid"), group=True)
        _ = self.wait_for(lambda: not pid_alive(self.num(r, "leader_pid")), 10, "task death")
        _ = self.block_workdir(a)  # result.json cannot be written
        b = self.submit(self.make_pkg())
        _ = self.start_daemon()
        _ = self.wait_state(b, "succeeded", timeout=20)
        job = self.wait_state(a, "unknown", timeout=20)
        self.assertEqual(self.get(job, "error", "kind"), "recovery_error")
        self.assertIn("failed 3 times", self.text(job, "error", "message"))
        self.assertIsNone(self.daemons["s1"][-1].poll())  # the daemon keeps running

    def block_workdir(self, jobid: str) -> Callable[[], None]:
        """Make writing result.json fail: the workdir is moved aside and a regular file
        takes its name, so write_result's makedirs fails with EEXIST (and any path below
        it with ENOTDIR). Unlike chmod 0500 this also holds for root: these errors come
        from the file type, not from permissions. Returns the function that puts it back."""
        workdir = os.path.join(self.job_dir(jobid), "workdir")
        saved = workdir + ".saved"
        os.rename(workdir, saved)
        open(workdir, "w").close()

        def restore() -> None:
            os.unlink(workdir)
            os.rename(saved, workdir)

        return restore

    # -- unknown jobs are never re-run (review P0-2, P0-3) ----------------------
    def assert_no_rerun(self, jobid: str) -> None:
        self.assertEqual(self.job(jobid).get("retried_as", []), [])
        self.assertEqual([j for j in self.jobids() if self.job(j).get("retry_of") == jobid], [])

    def test_resolve_as_stopped_with_directory_result_json(self) -> None:
        u, proc = self.make_unknown()
        os.makedirs(os.path.join(self.job_dir(u), "workdir", "result.json", "x"))
        kill_quiet(proc.pid)
        _ = proc.wait()
        out = self.ok("resolve", "job", u, "--host", "s1", "--as", "stopped", "--confirm", u)
        self.assertEqual(self.get(out, "state"), "failed")
        self.assertEqual(self.get(self.job(u), "error", "kind"), "operator_marked_stopped")
        self.assertEqual(self.get(self.result(u), "_taskmgr", "status"), "failed")
        self.assert_no_rerun(u)

    def test_failed_resolve_as_stopped_then_resolve_does_not_rerun(self) -> None:
        u, proc = self.make_unknown()
        kill_quiet(proc.pid)
        _ = proc.wait()
        restore = self.block_workdir(u)  # result.json cannot be written
        rc, out, _ = self.task(
            "resolve", "job", u, "--host", "s1", "--as", "stopped", "--confirm", u
        )
        self.assertEqual((rc, self.get(out, "error", "code")), (2, "internal_error"))
        self.assertEqual(self.job(u)["state"], "unknown")
        self.assertFalse(self.runner(u).get("final"))  # the run record is unchanged
        restore()
        out = self.ok("resolve", "job", u, "--host", "s1")
        self.assertEqual(self.get(out, "state"), "failed")
        self.assert_no_rerun(u)

    def test_resolve_of_unknown_marked_stopped_in_runner_only_does_not_rerun(self) -> None:
        # What an earlier `--as stopped` that failed after writing runner.json left behind.
        u, proc = self.make_unknown(
            runner_extra={
                "final": True,
                "status": "interrupted",
                "interrupted_reason": "operator_marked_stopped",
            }
        )
        kill_quiet(proc.pid)
        _ = proc.wait()
        out = self.ok("resolve", "job", u, "--host", "s1")
        self.assertEqual(self.get(out, "state"), "failed")
        self.assertEqual(self.get(self.job(u), "error", "kind"), "operator_marked_stopped")
        self.assert_no_rerun(u)
        u2, proc2 = self.make_unknown(
            gpus=(1,),
            runner_extra={
                "final": True,
                "status": "interrupted",
                "interrupted_reason": "operator_marked_stopped",
            },
            job_extra={"cancel_requested": True},
        )
        kill_quiet(proc2.pid)
        _ = proc2.wait()
        out = self.ok("resolve", "job", u2, "--host", "s1")
        self.assertEqual(self.get(out, "state"), "cancelled")
        self.assert_no_rerun(u2)

    def test_remote_resolve_as_stopped_checks_its_arguments(self) -> None:
        u, _ = self.make_unknown()
        rc, out, _ = self.remote("resolve", "job", u, "--as", "running", "--confirm", u)
        self.assertEqual((rc, self.get(out, "error", "code")), (2, "bad_args"))
        rc, out, _ = self.remote("resolve", "job", u, "--as", "stopped", "--confirm", "0" * 16)
        self.assertEqual((rc, self.get(out, "error", "code")), (2, "bad_args"))
        rc, out, _ = self.remote("resolve", "job", u, "--as", "stopped")
        self.assertEqual((rc, self.get(out, "error", "code")), (2, "bad_args"))
        self.assertEqual(self.job(u)["state"], "unknown")
        q = self.submit(self.make_pkg(gpus=[1]))
        rc, out, _ = self.remote("resolve", "job", q, "--as", "stopped", "--confirm", q)
        self.assertEqual((rc, self.get(out, "error", "code")), (2, "not_unknown"))
        self.assertEqual(self.job(q)["state"], "queued")

    def test_resolve_after_reboot_does_not_rerun_a_timed_out_unknown(self) -> None:
        u, proc = self.make_unknown(job_extra={"stop_reason": "timeout"})
        kill_quiet(proc.pid)
        _ = proc.wait()
        self.set_boot("boot-2")
        out = self.ok("resolve", "job", u, "--host", "s1")
        self.assertEqual(self.get(out, "state"), "timed_out")
        self.assert_no_rerun(u)

    def test_resolve_after_reboot_ends_unknown_as_interrupted_without_rerun(self) -> None:
        u, proc = self.make_unknown()
        kill_quiet(proc.pid)
        _ = proc.wait()
        self.set_boot("boot-2")
        out = self.ok("resolve", "job", u, "--host", "s1")
        self.assertEqual(self.get(out, "state"), "failed")
        self.assertEqual(self.get(self.job(u), "error", "kind"), "interrupted")
        self.assertEqual(self.get(self.runner(u), "status"), "interrupted")
        self.assert_no_rerun(u)
        # An explicit retry is still possible.
        out = self.ok("retry", "job", u, "--host", "s1")
        self.assertEqual(self.get(out, "retry_of"), u)

    def test_resolve_of_unknown_without_identity_does_not_rerun(self) -> None:
        u, _ = self.make_unknown()
        with open(os.path.join(self.job_dir(u), "runner.json"), "w") as f:
            json.dump({"jobid": u, "status": "dispatched", "final": False, "exited": False}, f)
        out = self.ok("resolve", "job", u, "--host", "s1")
        self.assertEqual(self.get(out, "state"), "failed")
        self.assertEqual(self.get(self.job(u), "error", "kind"), "interrupted")
        self.assert_no_rerun(u)

    def test_reboot_during_timeout_stop_ends_timed_out(self) -> None:
        # The daemon finds a job that was stopping for its timeout before a reboot.
        u, proc = self.make_unknown(
            state="stopping", unconfirmed=False, job_extra={"stop_reason": "timeout"}
        )
        kill_quiet(proc.pid)
        _ = proc.wait()
        self.set_boot("boot-2")
        _ = self.start_daemon()
        job = self.wait_state(u, "timed_out", timeout=20)
        self.assertEqual(job["state"], "timed_out")
        self.assert_no_rerun(u)

    def test_health_check_reads_task_files_outside_the_state_lock(self) -> None:
        # The daemon's periodic check of a dead shim's job reads the task's result.json;
        # a slow read there must not hold up every other command (review P0-1).
        from unittest import mock

        from taskmgr import store as store_mod
        from taskmgr.common import try_flock_fd
        from taskmgr.daemon import Daemon

        jobs: list[str] = []
        # Same boot, nothing alive: exit_code_lost (finalize_run). Other boot: interrupted
        # with a re-run (interrupt_run). Cancel requested, GPU-busy exit: end_cancelled.
        cases: list[tuple[JsonDict, JsonDict]] = [
            ({}, {}),
            ({"boot_id": "boot-0"}, {}),
            ({"exited": True, "return_code": 75, "signal": None}, {"cancel_requested": True}),
        ]
        for i, (runner_extra, job_extra) in enumerate(cases):
            j, proc = self.make_unknown(
                gpus=(i,),
                state="running",
                unconfirmed=False,
                runner_extra=runner_extra,
                job_extra=job_extra,
            )
            kill_quiet(proc.pid)
            _ = proc.wait()
            jobs.append(j)
        st = Store(self.root())
        held: list[bool] = []
        real = store_mod.read_task_result

        def spy(workdir: str, own: str | None = None) -> tuple[JsonDict, list[str], list[str]]:
            fd = try_flock_fd(st.path("locks", "state.lock"))
            held.append(fd is None)
            if fd is not None:
                os.close(fd)
            return real(workdir, own)

        with (
            mock.patch.object(store_mod, "read_task_result", spy),
            mock.patch.dict(os.environ, {"TASKMGR_BOOT_ID_FILE": self.boot_file}),
        ):
            Daemon(st).health_check()
        self.assertEqual([self.job(j)["state"] for j in jobs], ["failed", "failed", "cancelled"])
        self.assertEqual(self.get(self.job(jobs[1]), "error", "kind"), "interrupted")
        self.assertGreaterEqual(len(held), 3)
        self.assertNotIn(True, held)
