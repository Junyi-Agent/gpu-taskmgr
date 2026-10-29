"""Daemon restarts, shim crashes, takeover, reboot, unknown state and resolve (Sections 12, 13)."""

import json
import os
import subprocess
import time

from taskmgr import procutil
from taskmgr.records import JsonDict, Runner
from taskmgr.tests.helpers import REPO, Env, kill_quiet, pid_alive


class RecoveryTest(Env):
    def counter(self) -> str:
        return os.path.join(self.tmp, "runs")

    def runs(self) -> int:
        try:
            with open(self.counter()) as f:
                return len(f.read().splitlines())
        except FileNotFoundError:
            return 0

    def kill_shim(self, jobid: str) -> Runner:
        r = self.wait_runner(jobid, "pgid")
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
        self.assertEqual(len(job["attempts"]), 1)

    def test_daemon_restart_while_running(self) -> None:
        _ = self.start_daemon()
        j = self.submit(self.make_pkg(script="sleep 2; exit 0"))
        r = self.wait_runner(j, "pgid")
        self.stop_daemon()
        _ = self.start_daemon()
        _ = self.wait_state(j, "invalid_result", timeout=20)
        self.assertEqual(self.num(self.runner(j), "shim_pid"), self.num(r, "shim_pid"))
        self.assertEqual(self.get(self.result(j), "_taskmgr", "return_code"), 0)
        self.assertEqual(len(self.job(j)["attempts"]), 1)

    def test_crash_after_exit_recorded_is_not_rerun(self) -> None:
        _ = self.start_daemon(env={"TASKMGR_TEST_SHIM_CRASH": "after_exit_recorded"})
        j = self.submit(self.make_pkg(script="echo x >> %s; exit 0" % self.counter()))
        _ = self.wait_state(j, "invalid_result", timeout=20)
        r = self.runner(j)
        self.assertTrue(self.get(r, "final"))
        self.assertEqual(self.get(r, "return_code"), 0)
        self.assertEqual(self.runs(), 1)
        self.assertEqual(len(self.job(j)["attempts"]), 1)
        with open(os.path.join(self.job_dir(j), "attempts", "0001", "shim.log")) as f:
            self.assertIn("test crash", f.read())

    def test_crash_before_identity_reruns_from_package(self) -> None:
        self.set_config(max_auto_restarts=1)
        _ = self.start_daemon(env={"TASKMGR_TEST_SHIM_CRASH": "before_identity"})
        j = self.submit(self.make_pkg(script="echo x >> %s" % self.counter()))
        job = self.wait_state(j, "failed", timeout=30)
        self.assertEqual(self.get(job, "error", "kind"), "too_many_interruptions")
        self.assertEqual(len(job["attempts"]), 2)
        self.assertEqual(self.runs(), 0)  # the entry command never executed
        self.assertEqual(self.get(self.runner(j, 1), "status"), "interrupted")

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
        self.assertEqual(len(job["attempts"]), 1)

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
        _ = self.ok("cancel", c, "--host", "s1")
        _ = self.wait_state(c, "cancelled", timeout=20)
        _ = self.wait_state(t, "timed_out", timeout=20)
        self.assertFalse(pid_alive(self.num(rc_, "leader_pid")))
        self.assertFalse(pid_alive(self.num(rt, "leader_pid")))

    def test_cancel_request_survives_daemon_restart(self) -> None:
        _ = self.start_daemon()
        j = self.submit(self.make_pkg(script="sleep 60"))
        _ = self.wait_runner(j, "pgid")
        self.stop_daemon()
        r = self.kill_shim(j)
        _ = self.ok("cancel", j, "--host", "s1")
        self.assertEqual(self.job(j)["state"], "running")
        _ = self.start_daemon()
        _ = self.wait_state(j, "cancelled", timeout=20)
        self.assertFalse(pid_alive(self.num(r, "leader_pid")))
        self.assertEqual(len(self.job(j)["attempts"]), 1)

    def test_exit_code_lost_when_shim_and_task_gone(self) -> None:
        _ = self.start_daemon()
        j = self.submit(self.make_pkg(script="echo x >> %s; sleep 1; exit 5" % self.counter()))
        r = self.wait_runner(j, "pgid")
        self.stop_daemon()
        kill_quiet(self.num(r, "shim_pid"))
        _ = self.wait_for(lambda: not pid_alive(self.num(r, "leader_pid")), 10, "task end")
        _ = self.start_daemon()
        job = self.wait_state(j, "failed")
        self.assertEqual(self.get(job, "error", "kind"), "exit_code_lost")
        time.sleep(1)
        self.assertEqual(self.runs(), 1)
        self.assertEqual(len(self.job(j)["attempts"]), 1)

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

    def test_reboot_reruns_with_new_attempt(self) -> None:
        _ = self.start_daemon()
        script = "echo x >> {c}; [ $(wc -l < {c}) -ge 2 ] && exit 0; sleep 60".format(
            c=self.counter()
        )
        j = self.submit(self.make_pkg(script=script, gpus=[0]))
        seq = self.job(j)["enqueue_seq"]
        _ = self._simulate_reboot(j)
        _ = self.start_daemon()
        job = self.wait_state(j, "invalid_result", timeout=20)
        self.assertEqual(len(job["attempts"]), 2)
        self.assertEqual(job["enqueue_seq"], seq)
        self.assertEqual(job["auto_restarts"], 1)
        self.assertEqual(self.get(self.runner(j, 1), "status"), "interrupted")
        self.assertEqual(self.get(self.runner(j, 2), "boot_id"), "boot-2")
        self.assertEqual(self.runs(), 2)

    def test_reboot_beyond_auto_restart_limit_fails(self) -> None:
        self.set_config(max_auto_restarts=0)
        _ = self.start_daemon()
        j = self.submit(self.make_pkg(script="sleep 60"))
        _ = self._simulate_reboot(j)
        _ = self.start_daemon()
        job = self.wait_state(j, "failed")
        self.assertEqual(self.get(job, "error", "kind"), "too_many_interruptions")
        self.assertEqual(len(job["attempts"]), 1)

    def test_reboot_with_cancel_requested_is_cancelled(self) -> None:
        _ = self.start_daemon()
        j = self.submit(self.make_pkg(script="sleep 60"))
        _ = self.wait_runner(j, "pgid")
        self.stop_daemon()
        _ = self.ok("cancel", j, "--host", "s1")
        r = self.runner(j)
        kill_quiet(self.num(r, "shim_pid"))
        kill_quiet(self.num(r, "pgid"), group=True)
        _ = self.wait_for(lambda: not pid_alive(self.num(r, "leader_pid")), 10, "processes gone")
        self.set_boot("boot-2")
        _ = self.start_daemon()
        _ = self.wait_state(j, "cancelled")
        self.assertEqual(len(self.job(j)["attempts"]), 1)

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
        adir = os.path.join(jd, "attempts", "0001")
        os.makedirs(os.path.join(adir, "workdir"))
        runner: JsonDict = {
            "attempt": 1,
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
        with open(os.path.join(adir, "runner.json"), "w") as f:
            json.dump(runner, f)
        job: dict[str, object] = dict(self.job(j))
        job.update(
            {
                "state": state,
                "current_attempt": 1,
                "attempts": [{"attempt": 1, "status": "running"}],
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
        self.assertEqual(os.listdir(os.path.join(self.job_dir(u), "attempts")), ["0001"])
        rc, out, _ = self.task("status", u, "--wait", "--poll-interval", "0.2")
        self.assertEqual(rc, 3)
        self.assertEqual(self.get(out, "state"), "unknown")
        rc, out, _ = self.task("retry", u, "--host", "s1")
        self.assertEqual((rc, self.get(out, "error", "code")), (2, "resolve_required"))
        out = self.ok("cancel", u, "--host", "s1")
        self.assertEqual((self.get(out, "state"), self.get(out, "changed")), ("unknown", True))
        out = self.ok("resolve", u, "--host", "s1")
        self.assertEqual((self.get(out, "state"), self.get(out, "changed")), ("unknown", False))
        self.assertEqual(self.get(out, "identity", "pgid"), proc.pid)
        self.assertEqual(self.get(out, "identity", "boot_id"), "boot-1")
        kill_quiet(proc.pid)
        _ = proc.wait()
        out = self.ok("resolve", u, "--host", "s1")
        self.assertEqual((self.get(out, "state"), self.get(out, "changed")), ("cancelled", True))
        _ = self.wait_state(b, "succeeded")  # GPU released

    def test_resolve_unknown_without_cancel_is_exit_code_lost(self) -> None:
        u, proc = self.make_unknown()
        kill_quiet(proc.pid)
        _ = proc.wait()
        out = self.ok("resolve", u, "--host", "s1")
        self.assertEqual(self.get(out, "state"), "failed")
        self.assertEqual(self.get(self.job(u), "error", "kind"), "exit_code_lost")

    def test_resolve_as_stopped(self) -> None:
        u, _ = self.make_unknown()
        rc, out, _ = self.task("resolve", u, "--host", "s1", "--as", "stopped")
        self.assertEqual(rc, 2)
        rc, out, _ = self.task(
            "resolve", u, "--host", "s1", "--as", "stopped", "--confirm", "0000000000000000"
        )
        self.assertEqual(rc, 2)
        out = self.ok("resolve", u, "--host", "s1", "--as", "stopped", "--confirm", u)
        self.assertEqual((self.get(out, "state"), self.get(out, "changed")), ("failed", True))
        job = self.job(u)
        self.assertEqual(self.get(job, "error", "kind"), "operator_marked_stopped")
        self.assertEqual(self.get(self.runner(u), "status"), "interrupted")
        _ = self.start_daemon()
        time.sleep(1)
        self.assertEqual(len(self.job(u)["attempts"]), 1)  # nothing re-runs
        # With a cancel request the outcome is cancelled.
        u2, _ = self.make_unknown(gpus=(1,))
        _ = self.ok("cancel", u2, "--host", "s1")
        out = self.ok("resolve", u2, "--host", "s1", "--as", "stopped", "--confirm", u2)
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
        j = self.submit(self.make_pkg(script="sleep 60", gpus=[0]))
        r = self.wait_runner(j, "pgid")
        b = self.submit(self.make_pkg(gpus=[0]))
        _ = self.ok("cancel", j, "--host", "s1")
        job = self.wait_state(j, "unknown", timeout=20)
        self.assertEqual(self.get(job, "error", "kind"), "stop_unconfirmed")
        self.assertTrue(pid_alive(self.num(r, "leader_pid")))
        time.sleep(1)
        self.assertEqual(self.job(b)["state"], "queued")
        kill_quiet(self.num(r, "pgid"), group=True)
        _ = self.wait_for(lambda: not pid_alive(self.num(r, "leader_pid")), 10, "manual kill")
        out = self.ok("resolve", j, "--host", "s1")
        self.assertEqual(self.get(out, "state"), "cancelled")
        _ = self.wait_state(b, "succeeded")

    def test_resolve_hands_decidable_unknown_back_to_takeover(self) -> None:
        u, proc = self.make_unknown(unconfirmed=False)
        _ = self.start_daemon()
        out = self.ok("resolve", u, "--host", "s1")
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
        _ = self.ok("cancel", u, "--host", "s1")
        kill_quiet(proc.pid)
        _ = proc.wait()
        out = self.ok("resolve", u, "--host", "s1")
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
