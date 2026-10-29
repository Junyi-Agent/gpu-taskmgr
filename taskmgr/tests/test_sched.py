"""GPU scheduling: conflicts, disjoint concurrency, no starvation, unknown jobs, multi-daemon."""

import json
import os
import subprocess
import sys
import time

from helpers import GPUS, OK_RESULT, REMOTE, Env


class SchedTest(Env):
    def gate(self, name):
        """Script that blocks until `release(name)` is called."""
        return "while [ ! -f %s ]; do sleep 0.05; done; %s" % (
            os.path.join(self.tmp, "rel-" + name),
            OK_RESULT,
        )

    def release(self, name):
        open(os.path.join(self.tmp, "rel-" + name), "w").close()

    def test_conflict_queues_and_disjoint_runs_concurrently(self):
        a = self.submit(self.make_pkg(script=self.gate("a"), gpus=[0]))
        b = self.submit(self.make_pkg(script=self.gate("b"), gpus=[0]))
        c = self.submit(self.make_pkg(script=self.gate("c"), gpus=[1]))
        self.start_daemon()
        self.wait_state(a, "running")
        self.wait_state(c, "running")  # disjoint GPU: concurrent
        time.sleep(1)
        self.assertEqual(self.job(b)["state"], "queued")
        self.release("a")
        self.wait_state(a, "succeeded")
        self.wait_state(b, "running")
        self.release("b")
        self.release("c")
        self.wait_state(b, "succeeded")
        self.wait_state(c, "succeeded")

    def test_multi_gpu_job_not_starved(self):
        a = self.submit(self.make_pkg(script=self.gate("a"), gpus=[0]))
        self.start_daemon()
        self.wait_state(a, "running")
        big = self.submit(self.make_pkg(script=self.gate("big"), gpus=[0, 1]))
        small = self.submit(self.make_pkg(script=self.gate("small"), gpus=[1]))
        other = self.submit(self.make_pkg(script=self.gate("other"), gpus=[2]))
        self.wait_state(other, "running")  # unrelated GPU is unaffected
        time.sleep(1)
        # GPU 1 is idle but reserved for the earlier multi-GPU job.
        self.assertEqual(self.job(small)["state"], "queued")
        self.assertEqual(self.job(big)["state"], "queued")
        self.release("a")
        self.wait_state(big, "running")
        self.assertEqual(self.job(small)["state"], "queued")
        self.release("big")
        self.wait_state(small, "running")
        for n in ("small", "other"):
            self.release(n)
        self.wait_state(small, "succeeded")

    def test_unknown_job_does_not_lock_other_gpus(self):
        u = self.submit(self.make_pkg(gpus=[0]))
        # Fixture: put u into unknown holding GPU 0 (as after an unconfirmed stop).
        jd = self.job_dir(u)
        job = self.job(u)
        job.update({"state": "unknown", "current_attempt": 1})
        os.makedirs(os.path.join(jd, "attempts", "0001"))
        with open(os.path.join(jd, "attempts", "0001", "runner.json"), "w") as f:
            json.dump({"attempt": 1, "final": False, "exited": False}, f)
        with open(os.path.join(jd, "job.json"), "w") as f:
            json.dump(job, f)
        os.rename(jd, os.path.join(self.root(), "in-progress", u))
        big = self.submit(self.make_pkg(gpus=[0, 1]))
        small = self.submit(self.make_pkg(gpus=[1]))
        self.start_daemon()
        self.wait_state(small, "succeeded")
        time.sleep(1)
        self.assertEqual(self.job(big)["state"], "queued")
        self.assertEqual(self.job(u)["state"], "unknown")

    def test_index_mapping_and_cuda_visible_devices_order(self):
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
        self.start_daemon()
        self.wait_state(j, "succeeded")
        with open(out) as f:
            self.assertEqual(f.read().strip(), "%s,%s|PCI_BUS_ID" % (GPUS[2][1], GPUS[0][1]))

    def test_cpu_jobs_and_max_cpu_jobs(self):
        self.set_config(max_cpu_jobs=1)
        a = self.submit(self.make_pkg(script=self.gate("a")))
        b = self.submit(self.make_pkg(script=self.gate("b")))
        g = self.submit(self.make_pkg(script=self.gate("g"), gpus=[0]))
        self.start_daemon()
        self.wait_state(a, "running")
        self.wait_state(g, "running")  # CPU cap does not affect GPU jobs
        time.sleep(1)
        self.assertEqual(self.job(b)["state"], "queued")
        self.release("a")
        self.wait_state(b, "running")
        self.release("b")
        self.release("g")
        self.wait_state(b, "succeeded")

    def test_second_daemon_refuses_and_lockless_daemons_never_double_dispatch(self):
        self.start_daemon()
        time.sleep(0.5)
        p = subprocess.run(
            [sys.executable, REMOTE, "--root", self.root(), "serve"],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
        self.assertEqual(p.returncode, 2)
        self.stop_daemon()
        # Bypass daemon.lock: state.lock alone must prevent double dispatch.
        ids = [
            self.submit(self.make_pkg(script="sleep 0.2; " + OK_RESULT, gpus=[i % 4]))
            for i in range(12)
        ] + [self.submit(self.make_pkg(script="sleep 0.1; " + OK_RESULT)) for _ in range(6)]
        env = {"TASKMGR_TEST_SKIP_DAEMON_LOCK": "1"}
        for _ in range(3):
            self.start_daemon(env=env)
        for j in ids:
            self.wait_state(j, "succeeded", timeout=60)
            job = self.job(j)
            self.assertEqual(len(job["attempts"]), 1, job)
            self.assertEqual(os.listdir(os.path.join(self.job_dir(j), "attempts")), ["0001"])
