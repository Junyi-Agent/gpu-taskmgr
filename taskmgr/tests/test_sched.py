"""GPU scheduling: conflicts, disjoint concurrency, no starvation, unknown jobs, multi-daemon."""

import json
import os
import subprocess
import sys
import time

from taskmgr.common import TERMINAL_STATES
from taskmgr.tests.helpers import GPUS, OK_RESULT, REMOTE, Env


class SchedTest(Env):
    def gate(self, name: str) -> str:
        """Script that blocks until `release(name)` is called."""
        return "while [ ! -f %s ]; do sleep 0.05; done; %s" % (
            os.path.join(self.tmp, "rel-" + name),
            OK_RESULT,
        )

    def release(self, name: str) -> None:
        open(os.path.join(self.tmp, "rel-" + name), "w").close()

    def test_conflict_queues_and_disjoint_runs_concurrently(self) -> None:
        a = self.submit(self.make_pkg(script=self.gate("a"), gpus=[0]))
        b = self.submit(self.make_pkg(script=self.gate("b"), gpus=[0]))
        c = self.submit(self.make_pkg(script=self.gate("c"), gpus=[1]))
        _ = self.start_daemon()
        _ = self.wait_state(a, "running")
        _ = self.wait_state(c, "running")  # disjoint GPU: concurrent
        _ = self.wait_scheduler_passes(2)
        self.assertEqual(self.job(b)["state"], "queued")
        self.release("a")
        _ = self.wait_state(a, "succeeded")
        _ = self.wait_state(b, "running")
        self.release("b")
        self.release("c")
        _ = self.wait_state(b, "succeeded")
        _ = self.wait_state(c, "succeeded")

    def test_multi_gpu_job_not_starved(self) -> None:
        a = self.submit(self.make_pkg(script=self.gate("a"), gpus=[0]))
        _ = self.start_daemon()
        _ = self.wait_state(a, "running")
        big = self.submit(self.make_pkg(script=self.gate("big"), gpus=[0, 1]))
        small = self.submit(self.make_pkg(script=self.gate("small"), gpus=[1]))
        other = self.submit(self.make_pkg(script=self.gate("other"), gpus=[2]))
        _ = self.wait_state(other, "running")  # unrelated GPU is unaffected
        _ = self.wait_scheduler_passes(2)
        # GPU 1 is idle but reserved for the earlier multi-GPU job.
        self.assertEqual(self.job(small)["state"], "queued")
        self.assertEqual(self.job(big)["state"], "queued")
        self.release("a")
        _ = self.wait_state(big, "running")
        self.assertEqual(self.job(small)["state"], "queued")
        self.release("big")
        _ = self.wait_state(small, "running")
        for n in ("small", "other"):
            self.release(n)
        _ = self.wait_state(small, "succeeded")

    def test_unknown_job_does_not_lock_other_gpus(self) -> None:
        u = self.submit(self.make_pkg(gpus=[0]))
        # Fixture: put u into unknown holding GPU 0 (as after an unconfirmed stop).
        jd = self.job_dir(u)
        job = self.job(u)
        job.update({"state": "unknown", "dispatched_at": "2026-10-29T08:00:00Z"})
        with open(os.path.join(jd, "runner.json"), "w") as f:
            json.dump({"jobid": u, "final": False, "exited": False}, f)
        with open(os.path.join(jd, "job.json"), "w") as f:
            json.dump(job, f)
        os.rename(jd, os.path.join(self.root(), "in-progress", u))
        big = self.submit(self.make_pkg(gpus=[0, 1]))
        small = self.submit(self.make_pkg(gpus=[1]))
        _ = self.start_daemon()
        _ = self.wait_state(small, "succeeded")
        _ = self.wait_scheduler_passes(2)
        self.assertEqual(self.job(big)["state"], "queued")
        self.assertEqual(self.job(u)["state"], "unknown")

    def test_index_mapping_and_cuda_visible_devices_order(self) -> None:
        out = os.path.join(self.tmp, "env.txt")
        pkg = self.make_pkg(
            script='echo "$CUDA_VISIBLE_DEVICES|$CUDA_DEVICE_ORDER" > %s; %s' % (out, OK_RESULT),
            gpus=[2, GPUS[0][1]],
        )
        j = self.submit(pkg)
        job = self.job(j)
        self.assertEqual(
            job["gpus"],
            [{"declared": 2, "uuid": GPUS[2][1]}, {"declared": GPUS[0][1], "uuid": GPUS[0][1]}],
        )
        _ = self.start_daemon()
        _ = self.wait_state(j, "succeeded")
        with open(out) as f:
            self.assertEqual(f.read().strip(), "%s,%s|PCI_BUS_ID" % (GPUS[2][1], GPUS[0][1]))

    def test_cpu_jobs_and_max_cpu_jobs(self) -> None:
        self.set_config(max_cpu_jobs=1)
        a = self.submit(self.make_pkg(script=self.gate("a")))
        b = self.submit(self.make_pkg(script=self.gate("b")))
        g = self.submit(self.make_pkg(script=self.gate("g"), gpus=[0]))
        _ = self.start_daemon()
        _ = self.wait_state(a, "running")
        _ = self.wait_state(g, "running")  # CPU cap does not affect GPU jobs
        _ = self.wait_scheduler_passes(2, gpus=[3])
        self.assertEqual(self.job(b)["state"], "queued")
        self.release("a")
        _ = self.wait_state(b, "running")
        self.release("b")
        self.release("g")
        _ = self.wait_state(b, "succeeded")

    def test_second_daemon_refuses_and_lockless_daemons_never_double_dispatch(self) -> None:
        _ = self.start_daemon()
        time.sleep(0.5)
        p = subprocess.run(
            [sys.executable, REMOTE, "--root", self.root(), "run", "daemon"],
            env=self.env,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(p.returncode, 2)
        self.stop_daemon()
        # Bypass daemon.lock: state.lock alone must prevent double dispatch. Every launch
        # appends a line to runs/<jobid>, outside the job's directory.
        runs = os.path.join(self.tmp, "runs")
        os.makedirs(runs)
        log_run = 'echo x >> %s/"$TASKMGR_JOBID"; ' % runs
        ids = [
            self.submit(self.make_pkg(script=log_run + "sleep 0.2; " + OK_RESULT, gpus=[i % 4]))
            for i in range(12)
        ] + [
            self.submit(self.make_pkg(script=log_run + "sleep 0.1; " + OK_RESULT)) for _ in range(6)
        ]
        env = {"TASKMGR_TEST_SKIP_DAEMON_LOCK": "1"}
        daemons = [self.start_daemon(env=env) for _ in range(3)]

        def launches() -> dict[str, int]:
            out: dict[str, int] = {}
            for j in os.listdir(runs):
                with open(os.path.join(runs, j)) as f:
                    out[j] = len(f.read().splitlines())
            return out

        def settled() -> bool:
            # Stop waiting as soon as a job was launched twice or a daemon exited (one that
            # lost a race for a job crashes on the rename), so the assertions below report
            # it (and not a timeout); else once every job has ended.
            if any(n > 1 for n in launches().values()):
                return True
            if any(d.poll() is not None for d in daemons):
                return True
            try:
                return all(self.job(j)["state"] in TERMINAL_STATES for j in ids)
            except (OSError, ValueError):  # a directory is being renamed
                return False

        _ = self.wait_for(settled, 60, "every job to end")
        self.assertEqual(launches(), dict.fromkeys(ids, 1))  # each job was launched once
        self.assertEqual([d.poll() for d in daemons], [None] * 3, self.daemon_log())
        self.assertNotIn("Traceback", self.daemon_log())
        for j in ids:
            job = self.wait_state(j, "succeeded")
            self.assertEqual(job["retried_as"], [], job)
            self.assertIsNotNone(job["dispatched_at"], job)
            self.assertEqual(self.get(self.runner(j), "jobid"), j)
        self.assertEqual(self.jobids(), set(ids))  # nothing was cloned
