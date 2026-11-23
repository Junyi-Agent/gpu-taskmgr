"""task.json `gpus` as an object: GPUs chosen by condition when the job is launched."""

import os
import time

from taskmgr import gpu
from taskmgr.records import GpuRequest, JsonDict, JsonValue
from taskmgr.tests.helpers import GPUS, OK_RESULT, Env

FULL = 81559  # MiB, an 80 GB GPU


def u(i: int) -> str:
    return GPUS[i][1]


class GpuSelectValidationTest(Env):
    def check(self, gpus: JsonValue) -> tuple[int, JsonDict]:
        rc, out, _ = self.task("validate", "package", self.make_pkg(extra={"gpus": gpus}))
        return rc, self.obj(out)

    def test_validate(self) -> None:
        goods: list[JsonValue] = [
            {"count": 1},
            {"count": 2, "min_free_gb": 40},
            {"count": 1, "min_free_gb": 0.5, "from": [0, u(1)]},
        ]
        for good in goods:
            with self.subTest(good=good):
                rc, out = self.check(good)
                self.assertEqual(rc, 0, out)
                self.assertEqual(self.strs(out, "features"), ["gpu_select"])
        bads: list[tuple[JsonValue, str]] = [
            ({}, "schema"),
            ({"count": 0}, "schema"),
            ({"count": True}, "schema"),
            ({"count": 1, "min_free_gb": -1}, "schema"),
            ({"count": 1, "min_free_gb": True}, "schema"),
            ({"count": 1, "from": []}, "schema"),
            ({"count": 1, "from": [0, 0]}, "gpu"),
            ({"count": 1, "from": ["MIG-x"]}, "gpu"),
            ({"count": 3, "from": [0, 1]}, "gpu"),
            ({"count": 1, "free": 40}, "unknown_key"),
            ("any", "schema"),
        ]
        for bad, code in bads:
            with self.subTest(bad=bad):
                rc, out = self.check(bad)
                self.assertEqual(rc, 1, out)
                self.assertIn(code, [e["code"] for e in self.objs(out, "errors")])

    def test_server_refuses_what_the_machine_cannot_give(self) -> None:
        impossible: list[JsonValue] = [
            {"count": 5},
            {"count": 1, "min_free_gb": 100},  # every GPU has 79.6 GiB
            {"count": 1, "from": [9]},
        ]
        for gpus in impossible:
            with self.subTest(gpus=gpus):
                rc, out, _ = self.task(
                    "create", "job", self.make_pkg(extra={"gpus": gpus}), "--host", "s1"
                )
                self.assertEqual(rc, 2)
                self.assertEqual(self.get(out, "error", "code"), "invalid_gpus")
        j = self.submit(self.make_pkg(extra={"gpus": {"count": 2, "from": [u(3), 1]}}))
        job = self.job(j)
        self.assertEqual(job["gpus"], [])
        self.assertEqual(job.get("gpu_request"), {"count": 2, "from_uuids": [u(3), u(1)]})

    def test_select_gpus(self) -> None:
        """The choice itself (no daemon): conditions, exclusive mode, preference."""
        state = [
            gpu.GpuState(0, u(0), FULL, FULL, "Default", 1),  # a process, room left
            gpu.GpuState(1, u(1), 10000, FULL, "Default", 0),  # idle, little memory free
            gpu.GpuState(2, u(2), FULL, FULL, "Exclusive_Process", 1),  # taken
            gpu.GpuState(3, u(3), 70000, FULL, "Default", 0),
        ]
        any1: GpuRequest = {"count": 1}
        roomy: GpuRequest = {"count": 1, "min_free_gb": 40}
        self.assertEqual(gpu.select_gpus(any1, state, [])[0], [{"declared": 3, "uuid": u(3)}])
        self.assertEqual(gpu.select_gpus(roomy, state, [])[0], [{"declared": 0, "uuid": u(0)}])
        self.assertEqual(gpu.select_gpus(roomy, state, [u(0)])[0], [{"declared": 3, "uuid": u(3)}])
        two: GpuRequest = {"count": 2, "min_free_gb": 40}
        chosen, _ = gpu.select_gpus(two, state, [])
        self.assertEqual([g["declared"] for g in chosen or []], [0, 3])
        chosen, why = gpu.select_gpus(two, state, [u(3)])
        self.assertIsNone(chosen)
        self.assertEqual(why, "waiting for 2 GPUs with >= 40 GiB free (1 qualify now)")
        listed: GpuRequest = {"count": 1, "from_uuids": [u(1), u(2)]}
        self.assertEqual(gpu.select_gpus(listed, state, [])[0], [{"declared": 1, "uuid": u(1)}])
        state[1] = state[1]._replace(mode="Prohibited")
        self.assertIsNone(gpu.select_gpus(listed, state, [])[0])


class GpuSelectRunTest(Env):
    remote_config: JsonDict = {"gpu_retry_interval_seconds": 0.5}

    def pkg(self, gpus: JsonDict, script: str = OK_RESULT) -> str:
        return self.make_pkg(script='echo "$CUDA_VISIBLE_DEVICES"; ' + script, extra={"gpus": gpus})

    def chosen(self, jobid: str) -> list[JsonValue]:
        return [self.get(g, "declared") for g in self.arr(self.job(jobid), "gpus")]

    def test_choice_respects_taskmgr_jobs_memory_and_exclusive_mode(self) -> None:
        self.set_gpu_info(
            [
                (0, u(0), FULL, FULL, "Default"),
                (1, u(1), 10000, FULL, "Default"),
                (2, u(2), FULL, FULL, "Exclusive_Process"),
                (3, u(3), FULL, FULL, "Default"),
            ]
        )
        self.set_gpu_apps([(u(2), 999999, 500)])
        _ = self.start_daemon()
        gate = os.path.join(self.tmp, "gate")
        hold = self.submit(
            self.make_pkg(
                script="while [ ! -f %s ]; do sleep 0.05; done; " % gate + OK_RESULT, gpus=[3]
            )
        )
        _ = self.wait_state(hold, "running")
        # GPU 3: taskmgr job; 1: too little memory; 2: exclusive and taken. GPU 0 remains.
        one = self.submit(self.pkg({"count": 1, "min_free_gb": 40}))
        _ = self.wait_state(one, "succeeded")
        self.assertEqual(self.chosen(one), [0])
        self.assertEqual(self.get(self.result(one), "_taskmgr", "gpus", 0, "uuid"), u(0))
        with open(os.path.join(self.job_dir(one), "stdout.log")) as f:
            self.assertEqual(f.read().strip(), u(0))
        # Two such GPUs: only GPU 0 qualifies while job `hold` runs.
        two = self.submit(self.pkg({"count": 2, "min_free_gb": 40}))
        _ = self.wait_for(lambda: self.job(two).get("pending_reason"), 20, "pending_reason")
        job = self.job(two)
        self.assertEqual((job["state"], job["gpus"]), ("queued", []))
        self.assertIn("2 GPUs with >= 40 GiB free (1 qualify now)", str(job.get("pending_reason")))
        st = self.ok("get", "job", two, "--host", "s1")
        self.assertEqual(self.get(st, "gpu_request"), {"count": 2, "min_free_gb": 40})
        holders = self.objs(st, "gpu_holders")
        self.assertIn(999999, [h["pid"] for h in holders])
        rc, text, _ = self.task("get", "jobs", text=True)
        assert isinstance(text, str)
        self.assertEqual(rc, 0)
        self.assertIn("pending-gpu", [line.split()[-1] for line in text.splitlines()[1:]])
        rc, text, _ = self.task("get", "job", two, "--host", "s1", text=True)
        assert isinstance(text, str)
        self.assertIn("2 GPU(s) with >= 40 GiB free, chosen at launch", text)
        open(gate, "w").close()
        _ = self.wait_state(two, "succeeded")
        self.assertEqual(self.chosen(two), [0, 3])
        rc, text, _ = self.task("get", "job", two, "--host", "s1", text=True)
        assert isinstance(text, str)
        self.assertEqual(rc, 0)

    def test_any_idle_gpu(self) -> None:
        # GPU 0 runs a process in Default mode: not idle, but has room.
        self.set_gpu_apps([(u(0), 999999, 500)])
        _ = self.start_daemon()
        idle = self.submit(self.pkg({"count": 1}))
        _ = self.wait_state(idle, "succeeded")
        self.assertEqual(self.chosen(idle), [1])
        roomy = self.submit(self.pkg({"count": 1, "min_free_gb": 1}))
        _ = self.wait_state(roomy, "succeeded")
        self.assertEqual(self.chosen(roomy), [0])
        listed = self.submit(self.pkg({"count": 1, "from": [3, 2]}))
        _ = self.wait_state(listed, "succeeded")
        self.assertEqual(self.chosen(listed), [2])

    def test_busy_exit_chooses_again(self) -> None:
        """Exit 75 on a chosen GPU (someone took it after the choice): the job is queued
        again and the scheduler chooses again, now without that GPU."""
        self.set_gpu_info(
            [(0, u(0), FULL, FULL, "Exclusive_Process")]
            + [(i, u(i), 70000, FULL, "Exclusive_Process") for i in (1, 2, 3)]
        )
        launches = os.path.join(self.tmp, "launches")
        script = (
            "echo x >> %s; touch stale; " % launches
            + 'if [ "$CUDA_VISIBLE_DEVICES" = %s ]; then echo "%s, 999999, 500" >> %s; exit 75; fi; '
            % (u(0), u(0), self.apps_file)
            + OK_RESULT
        )
        _ = self.start_daemon()
        j = self.submit(self.pkg({"count": 1}, script))
        job = self.wait_state(j, "succeeded")
        self.assertEqual(self.chosen(j), [1])  # GPU 0 first (most free), then 1
        self.assertEqual((job["retried_as"], job["auto_restarts"]), ([], 0))
        with open(launches) as f:
            self.assertEqual(len(f.read().splitlines()), 2)
        self.assertTrue(os.path.exists(os.path.join(self.job_dir(j), "runner.prev-1.json")))
        self.assertIn("chose GPUs [0]", self.daemon_log())
        # A retry chooses its GPUs again (GPU 0 is still taken).
        r = self.text(self.ok("retry", "job", j, "--host", "s1"), "jobid")
        self.assertEqual(self.job(r).get("gpu_request"), {"count": 1})
        _ = self.wait_state(r, "succeeded")
        self.assertEqual(self.chosen(r), [1])

    def test_old_server_is_refused_before_upload(self) -> None:
        rc, out, _ = self.task(
            "create",
            "job",
            self.pkg({"count": 1}),
            "--host",
            "s1",
            env={"TASKMGR_TEST_FEATURES": "precompile,strict_task_json"},
        )
        self.assertEqual(rc, 2)
        self.assertIn("without gpu_select support", self.text(out, "error", "message"))


class GpuSelectBusyBackoffTest(Env):
    remote_config: JsonDict = {"gpu_retry_interval_seconds": 1.5}

    def test_busy_exit_waits_the_retry_interval_and_keeps_one_record(self) -> None:
        """A job that chooses its GPUs and keeps exiting 75 is dispatched again only
        gpu_retry_interval_seconds after each exit (not on the next pass), and its run
        records set aside for those exits stay one file however often it is queued again."""
        launches = os.path.join(self.tmp, "launches")
        script = "date +%%s.%%N >> %s; exit 75" % launches
        _ = self.start_daemon()
        j = self.submit(self.make_pkg(script=script, extra={"gpus": {"count": 1}}))

        def times() -> list[float]:
            try:
                with open(launches) as f:
                    return [float(line) for line in f.read().split()]
            except OSError:
                return []

        _ = self.wait_for(lambda: len(times()) >= 4, 30, "4 launches")
        t = times()[:4]
        gaps = [t[i + 1] - t[i] for i in range(len(t) - 1)]
        self.assertGreaterEqual(min(gaps), 1.5, t)

        def busy_reason() -> str:
            try:
                text = str(self.job(j).get("pending_reason"))
            except (OSError, ValueError):  # the directory moved meanwhile
                return ""
            return text if "GPU busy, #" in text else ""

        self.assertRegex(
            self.wait_for(busy_reason, 10, "pending_reason"),
            r"^entry exited 75 \(GPU busy, #\d+\); choosing GPUs again from \S+Z$",
        )
        _ = self.ok("cancel", "job", j, "--host", "s1")
        job = self.wait_state(j, "cancelled")

        self.assertGreaterEqual(job.get("gpu_busy_requeues", 0), 3)
        prev = sorted(n for n in os.listdir(self.job_dir(j)) if n.startswith("runner.prev-"))
        self.assertEqual(prev, ["runner.prev-1.json"])


class GpuStateReadTest(Env):
    def test_reads_are_reused_and_a_hung_nvidia_smi_does_not_stall_other_jobs(self) -> None:
        """A job waiting for GPUs to qualify does not make the daemon run nvidia-smi on
        every pass, and a hung nvidia-smi holds up a CPU job by the selection timeout
        (GPU_QUERY_TIMEOUT), not by the 60 s of other queries."""
        calls = os.path.join(self.tmp, "smi-calls")
        hang = os.path.join(self.tmp, "hang")
        with open(self.smi) as f:
            body = f.read()
        hook = 'echo "$*" >> %s\nif [ -f %s ]; then exec sleep 1000; fi\n' % (calls, hang)
        with open(self.smi, "w") as f:
            _ = f.write(body.replace("#!/bin/sh\n", "#!/bin/sh\n" + hook, 1))
        self.set_gpu_info([(i, uuid, FULL, FULL, "Prohibited") for i, uuid in GPUS])
        _ = self.start_daemon()
        j = self.submit(self.make_pkg(extra={"gpus": {"count": 1}}))
        _ = self.wait_for(lambda: self.job(j).get("pending_reason"), 20, "pending_reason")

        def count() -> int:
            with open(calls) as f:
                return len(f.read().splitlines())

        before = count()
        time.sleep(3)  # 15 passes at scheduler_poll_seconds 0.2
        self.assertLessEqual(count() - before, 4)  # one read (two calls) per 5 s at most

        open(hang, "w").close()
        hung = count()
        _ = self.wait_for(lambda: count() > hung, 20, "a hung nvidia-smi call")
        t0 = time.monotonic()
        cpu = self.submit(self.make_pkg())
        os.unlink(hang)  # the call in progress stays hung
        _ = self.wait_state(cpu, "succeeded", timeout=40)
        self.assertLess(time.monotonic() - t0, 25)
        self.assertEqual(self.daemon_log().count("cannot read the GPU state"), 1)
        self.assertIn("timed out", str(self.job(j).get("pending_reason")))
        _ = self.ok("cancel", "job", j, "--host", "s1")
