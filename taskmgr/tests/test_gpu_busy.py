"""An entry that cannot get its GPU exits 75 and is started again (desc-2 D17)."""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from types import ModuleType
from typing import cast
from unittest import mock

from taskmgr.common import override
from taskmgr.records import JsonDict, JsonValue
from taskmgr.tests.helpers import GPUS, OK_RESULT, REPO, Env, kill_quiet, pid_alive

TEMPLATE = os.path.join(REPO, "taskmgr", "template")
EXAMPLES = os.path.join(REPO, "taskmgr", "examples")


class GpuBusyTest(Env):
    remote_config: JsonDict = {"gpu_retry_interval_seconds": 1}
    launches: str = ""  # assigned in setUp

    @override
    def setUp(self) -> None:
        super().setUp()
        self.launches = os.path.join(self.tmp, "launches")

    def count(self) -> int:
        try:
            with open(self.launches) as f:
                return len(f.read().splitlines())
        except FileNotFoundError:
            return 0

    def busy_script(self, busy: int | None = None, gate: str | None = None, then: str = "") -> str:
        """Count the launch, record whether the workdir was reused (the `stale` file left by
        a previous launch is there: one line in `dirty` per reuse), then exit 75 for the first
        `busy` launches (or while `gate` does not exist), else run `then` and write a result.
        """
        script = (
            "if [ -e stale ]; then echo dirty >> {dirty}; fi; touch stale; "
            + "echo x >> {c}; n=$(wc -l < {c}); echo launch $n; echo err $n >&2; "
        ).format(c=self.launches, dirty=os.path.join(self.tmp, "dirty"))
        if busy is not None:
            script += "if [ $n -le %d ]; then exit 75; fi; " % busy
        if gate is not None:
            script += "if [ ! -f %s ]; then exit 75; fi; " % gate
        return script + then + OK_RESULT

    def read(self, path: str) -> str:
        with open(path) as f:
            return f.read()

    def pending(self, jobid: str) -> JsonValue:
        try:
            return self.job(jobid).get("pending_reason")
        except (OSError, ValueError):  # the directory moved or does not exist yet
            return None

    def waiting(self, jobid: str) -> bool:
        try:
            return self.runner(jobid).get("status") == "waiting_gpu"
        except (OSError, ValueError):
            return False

    def test_busy_exits_are_relaunched_then_succeed(self) -> None:
        _ = self.start_daemon()
        # At least two busy exits, then busy until `gate` exists: the job stays pending
        # while its state is read below, however slow the machine is.
        gate = os.path.join(self.tmp, "gate")
        j = self.submit(self.make_pkg(script=self.busy_script(busy=2, gate=gate), gpus=[0]))
        _ = self.wait_for(lambda: self.pending(j), 20, "pending_reason")
        self.assertEqual(self.job(j)["state"], "running")
        self.assertIn("GPU busy", self.text(self.job(j), "pending_reason"))
        rc, out, _ = self.task("jobs", text=True)
        assert isinstance(out, str)
        self.assertEqual(rc, 0)
        self.assertTrue(out.splitlines()[1].endswith("  pending-gpu"), out)
        rc, colored, _ = self.task("jobs", text=True, color=True)
        assert isinstance(colored, str)
        self.assertIn("\x1b[33mpending-gpu\x1b[0m", colored)
        detail = self.ok("jobs")
        self.assertIn("GPU busy", self.text(detail, "jobs", 0, "pending_reason"))
        st = self.ok("status", j, "--host", "s1")
        self.assertEqual(self.get(st, "state"), "running")
        self.assertIn("GPU busy", self.text(st, "pending_reason"))
        self.assertGreaterEqual(self.num(st, "run", "gpu_busy_retries"), 1)

        open(gate, "w").close()
        job = self.wait_state(j, "succeeded", timeout=30)
        self.assertNotIn("pending_reason", job)
        n = self.count()  # n - 1 busy exits, then the launch that succeeded
        self.assertGreaterEqual(n, 3)
        # The workdir is reused: every relaunch saw the file launch 1 left behind.
        self.assertEqual(self.read(os.path.join(self.tmp, "dirty")), "dirty\n" * (n - 1))
        r = self.runner(j)
        self.assertEqual(self.get(r, "gpu_busy_retries"), n - 1)
        self.assertIsNotNone(self.get(r, "last_gpu_busy_at"))
        self.assertEqual((self.get(r, "status"), self.get(r, "return_code")), ("succeeded", 0))
        self.assertEqual((job["retried_as"], job["auto_restarts"]), ([], 0))
        self.assertEqual(self.jobids(), {j})
        jd = self.job_dir(j)
        sep = "--- taskmgr: relaunch #%d after GPU busy ---"

        def expected(word: str) -> list[str]:
            lines = ["%s 1" % word]
            for i in range(2, n + 1):
                lines += [sep % (i - 1), "%s %d" % (word, i)]
            return lines

        self.assertEqual(self.read(os.path.join(jd, "stdout.log")).splitlines(), expected("launch"))
        self.assertEqual(self.read(os.path.join(jd, "stderr.log")).splitlines(), expected("err"))
        self.assertIn("(GPU busy); relaunch #%d" % (n - 1), self.read(os.path.join(jd, "shim.log")))
        rc, out, _ = self.task("jobs", text=True)
        assert isinstance(out, str)
        self.assertTrue(out.splitlines()[1].endswith("  succeeded"), out)

    def test_cancel_while_waiting(self) -> None:
        self.set_config(gpu_retry_interval_seconds=60)
        _ = self.start_daemon()
        j = self.submit(self.make_pkg(script=self.busy_script(busy=100), gpus=[0]))
        _ = self.wait_for(lambda: self.waiting(j), 20, "waiting")
        _ = self.ok("cancel", j, "--host", "s1")
        job = self.wait_state(j, "cancelled", timeout=10)
        self.assertEqual(job["stop_reason"], "cancel")
        self.assertNotIn("pending_reason", job)
        self.assertEqual(self.get(self.result(j), "_taskmgr", "status"), "cancelled")
        self.assertEqual(self.get(self.runner(j), "status"), "cancelled")
        time.sleep(1.5)
        self.assertEqual(self.count(), 1)  # never launched again

    def test_waiting_job_keeps_its_gpu_fifo(self) -> None:
        _ = self.start_daemon()
        gate = os.path.join(self.tmp, "gate")
        a = self.submit(self.make_pkg(script=self.busy_script(gate=gate), gpus=[0]))
        _ = self.wait_for(lambda: self.pending(a), 20, "pending_reason")
        b = self.submit(self.make_pkg(gpus=[0]))
        c = self.submit(self.make_pkg(gpus=[1]))  # another GPU is not blocked
        _ = self.wait_state(c, "succeeded")
        time.sleep(1.5)
        self.assertEqual(self.job(b)["state"], "queued")
        self.assertEqual(self.job(a)["state"], "running")
        open(gate, "w").close()
        ja = self.wait_state(a, "succeeded", timeout=20)
        jb = self.wait_state(b, "succeeded", timeout=20)
        self.assertGreaterEqual(self.text(jb, "dispatched_at"), self.text(ja, "ended_at"))
        self.assertGreaterEqual(self.num(self.runner(a), "gpu_busy_retries"), 1)

    def test_timeout_counts_only_the_launch_that_runs(self) -> None:
        _ = self.start_daemon()
        # Three busy exits with 1 s waits (> timeout 2 s in total), then a 1 s run: no timeout.
        ok = self.submit(
            self.make_pkg(script=self.busy_script(busy=3, then="sleep 1; "), timeout=2)
        )
        job = self.wait_state(ok, "succeeded", timeout=30)
        self.assertEqual(self.num(self.runner(ok), "gpu_busy_retries"), 3)
        self.assertIsNone(job["stop_reason"])
        # The launch that runs is still limited.
        os.unlink(self.launches)
        slow = self.submit(
            self.make_pkg(script=self.busy_script(busy=1, then="sleep 60; "), timeout=2)
        )
        job = self.wait_state(slow, "timed_out", timeout=30)
        self.assertEqual(self.num(self.runner(slow), "gpu_busy_retries"), 1)
        self.assertEqual(job["stop_reason"], "timeout")

    def test_shim_killed_while_waiting_requeues_the_same_job(self) -> None:
        self.set_config(gpu_retry_interval_seconds=60)
        _ = self.start_daemon()
        j = self.submit(self.make_pkg(script=self.busy_script(busy=1), gpus=[0]))
        seq = self.job(j)["enqueue_seq"]
        _ = self.wait_for(lambda: self.waiting(j), 20, "waiting")
        shim = self.num(self.runner(j), "shim_pid")
        kill_quiet(shim)
        _ = self.wait_for(lambda: not pid_alive(shim), 10, "shim death")
        job = self.wait_state(j, "succeeded", timeout=30)
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.jobids(), {j})
        self.assertEqual((job["auto_restarts"], job["retried_as"]), (0, []))
        self.assertEqual(job["enqueue_seq"], seq)
        self.assertIn("requeued", self.daemon_log())
        jd = self.job_dir(j)
        with open(os.path.join(jd, "runner.prev-1.json")) as f:
            prev = self.obj(cast(JsonValue, json.load(f)))
        self.assertEqual((prev["status"], prev["gpu_busy_retries"]), ("waiting_gpu", 1))
        self.assertNotIn("gpu_busy_retries", self.runner(j))
        self.assertNotEqual(self.num(self.runner(j), "shim_pid"), shim)
        self.assertEqual(
            self.read(os.path.join(jd, "stdout.log")).splitlines(),
            ["launch 1", "--- taskmgr: launch after requeue (GPU busy) ---", "launch 2"],
        )
        # The requeued job keeps its workdir too: launch 2 saw launch 1's file.
        self.assertEqual(self.read(os.path.join(self.tmp, "dirty")), "dirty\n")

    def test_shim_crash_right_after_a_busy_exit_requeues(self) -> None:
        # The shim dies after recording exit 75, before it writes the waiting state. The
        # re-dispatched run crashes the same way after exit 0, which is finalized as usual.
        _ = self.start_daemon(env={"TASKMGR_TEST_SHIM_CRASH": "after_exit_recorded"})
        j = self.submit(self.make_pkg(script=self.busy_script(busy=1)))
        job = self.wait_state(j, "succeeded", timeout=30)
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.jobids(), {j})
        self.assertEqual((job["auto_restarts"], job["retried_as"]), (0, []))
        with open(os.path.join(self.job_dir(j), "runner.prev-1.json")) as f:
            prev = self.obj(cast(JsonValue, json.load(f)))
        self.assertEqual((prev["exited"], prev["return_code"], prev["final"]), (True, 75, False))

    def test_dispatch_sets_aside_a_busy_run_record(self) -> None:
        # A crash while queueing a busy job again can leave runner.json in a queued job.
        j = self.submit(self.make_pkg())
        jd = self.job_dir(j)
        with open(os.path.join(jd, "runner.json"), "w") as f:
            json.dump({"jobid": j, "status": "waiting_gpu", "exited": True, "return_code": 75}, f)
        _ = self.start_daemon()
        _ = self.wait_state(j, "succeeded")
        self.assertTrue(os.path.exists(os.path.join(self.job_dir(j), "runner.prev-1.json")))

    def test_feature_disabled_makes_75_an_ordinary_failure(self) -> None:
        self.set_config(gpu_busy_exit_code=None)
        _ = self.start_daemon()
        j = self.submit(self.make_pkg(script=self.busy_script(busy=5)))
        job = self.wait_state(j, "failed")
        self.assertEqual(self.get(job, "error", "kind"), "exit_code")
        self.assertEqual(self.get(self.result(j), "_taskmgr", "return_code"), 75)
        self.assertEqual(self.count(), 1)
        self.assertNotIn("gpu_busy_retries", self.runner(j))

    def test_disabled_convention_unsets_the_variable_from_base_env(self) -> None:
        # Review P1-4: with gpu_busy_exit_code null the entry must not see the variable,
        # even when base_env sets it.
        seen = os.path.join(self.tmp, "seen")
        script = 'echo "${TASKMGR_GPU_BUSY_EXIT_CODE-unset}" >> %s; ' % seen + OK_RESULT
        self.set_config(
            gpu_busy_exit_code=None, base_env={"TASKMGR_GPU_BUSY_EXIT_CODE": "88", "A": "1"}
        )
        _ = self.start_daemon()
        j = self.submit(self.make_pkg(script=script))
        _ = self.wait_state(j, "succeeded")
        self.stop_daemon()
        self.set_config(gpu_busy_exit_code=80)
        _ = self.start_daemon()
        j = self.submit(self.make_pkg(script=script))
        _ = self.wait_state(j, "succeeded")
        self.assertEqual(self.read(seen).split(), ["unset", "80"])

    def test_busy_code_outside_1_to_255_is_a_config_error(self) -> None:
        # Review P1-5: taskmgr_task.py only accepts 1-255 and falls back to 75 otherwise.
        from taskmgr.common import TaskmgrError
        from taskmgr.store import Store

        for good in (1, 75, 255, None):
            self.set_config(gpu_busy_exit_code=good)
            self.assertEqual(Store(self.root()).gpu_busy_exit_code, good)
        for bad in (0, 256, 300, -1, True, "75"):
            self.set_config(gpu_busy_exit_code=bad)
            with self.assertRaises(TaskmgrError) as cm:
                _ = Store(self.root()).config
            self.assertEqual(cm.exception.code, "bad_config", bad)
            self.assertIn("gpu_busy_exit_code", cm.exception.message)

    def test_gpu_busy_helper_exits_75(self) -> None:
        p = subprocess.run(
            [sys.executable, "-c", "import taskmgr_task as tt; tt.gpu_busy('held by pid 9')"],
            cwd=TEMPLATE,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(p.returncode, 75)
        self.assertIn("held by pid 9", p.stderr.decode())


class FakeCuda:
    """Injected CUDA driver: `busy` ordinals report CUDA_ERROR_DEVICE_UNAVAILABLE."""

    def __init__(self, busy: tuple[int, ...] = (), init_rc: int = 0) -> None:
        self.busy: tuple[int, ...] = busy
        self.init_rc: int = init_rc
        self.retained: list[int] = []

    def init(self) -> int:
        return self.init_rc

    def device_get(self, ordinal: int) -> tuple[int, int]:
        return 0, ordinal + 100

    def primary_ctx_retain(self, device: int) -> tuple[int, int]:
        if device - 100 in self.busy:
            return 46, 0
        self.retained.append(device)
        return 0, 0x1000 + device

    def error_name(self, code: int) -> str:
        return {100: "CUDA_ERROR_NO_DEVICE"}.get(code, "CUresult %d" % code)


ClaimGpu = Callable[[Callable[[], object]], None]


class TaskHelperTest(Env):
    """taskmgr_task.claim_gpu() / gpu_busy() without a GPU (the driver is injected)."""

    def load(self) -> ModuleType:
        spec = importlib.util.spec_from_file_location(
            "taskmgr_task_under_test", os.path.join(TEMPLATE, "taskmgr_task.py")
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def claim(self, mod: ModuleType, visible: str | None, fake: FakeCuda) -> None:
        claim = cast(ClaimGpu, getattr(mod, "claim_gpu"))
        saved = os.environ.get("CUDA_VISIBLE_DEVICES")
        if visible is None:
            _ = os.environ.pop("CUDA_VISIBLE_DEVICES", None)
        else:
            os.environ["CUDA_VISIBLE_DEVICES"] = visible
        try:
            claim(lambda: fake)
        finally:
            if saved is None:
                _ = os.environ.pop("CUDA_VISIBLE_DEVICES", None)
            else:
                os.environ["CUDA_VISIBLE_DEVICES"] = saved

    def test_claim_gpu(self) -> None:
        mod = self.load()
        uuids = "%s,%s" % (GPUS[0][1], GPUS[1][1])
        ok = FakeCuda()
        self.claim(mod, uuids, ok)
        self.assertEqual(ok.retained, [100, 101])
        self.assertEqual(cast(list[int], getattr(mod, "_retained")), [0x1000 + 100, 0x1000 + 101])
        # The second GPU is held by another process: exit 75 and say which one.
        busy = FakeCuda(busy=(1,))
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            self.claim(mod, uuids, busy)
        self.assertEqual(cm.exception.code, 75)
        self.assertIn("GPU 1 (%s) is held by another process" % GPUS[1][1], err.getvalue())
        # No GPUs: nothing is loaded at all.
        for visible in ("", None):
            unused = FakeCuda(init_rc=999)
            self.claim(mod, visible, unused)
            self.assertEqual(unused.retained, [])
        # Other CUDA errors are real problems.
        with self.assertRaisesRegex(RuntimeError, "cuInit failed: CUDA_ERROR_NO_DEVICE"):
            self.claim(mod, "0", FakeCuda(init_rc=100))
        # This test environment has no libcuda.so.1: the default loader says so.
        load_libcuda = cast(Callable[[], object], getattr(mod, "load_libcuda"))
        with self.assertRaisesRegex(RuntimeError, "libcuda.so.1"):
            _ = load_libcuda()

    def test_gpu_busy(self) -> None:
        gpu_busy = cast(Callable[[str], None], getattr(self.load(), "gpu_busy"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            gpu_busy("no luck")
        self.assertEqual(cm.exception.code, 75)
        self.assertIn("no luck", err.getvalue())
        # The runner exports the server's code; junk or out-of-range values fall back to 75.
        for raw, want in (("42", 42), ("", 75), ("x", 75), ("0", 75), ("256", 75)):
            with (
                mock.patch.dict(os.environ, {"TASKMGR_GPU_BUSY_EXIT_CODE": raw}),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as cm,
            ):
                gpu_busy("")
            self.assertEqual(cm.exception.code, want, raw)

    def test_helper_copies_identical_and_standalone(self) -> None:
        with open(os.path.join(TEMPLATE, "taskmgr_task.py"), "rb") as f:
            template = f.read()
        for name in sorted(os.listdir(EXAMPLES)):
            with open(os.path.join(EXAMPLES, name, "taskmgr_task.py"), "rb") as f:
                self.assertEqual(f.read(), template, name)
        for d in [TEMPLATE] + [os.path.join(EXAMPLES, n) for n in os.listdir(EXAMPLES)]:
            for name in os.listdir(d):
                if name.endswith(".py"):
                    src = self.read_text(os.path.join(d, name))
                    self.assertNotRegex(src, r"(?m)^\s*(import|from)\s+taskmgr\b", name)
            self.assertIn("tt.claim_gpu()", self.read_text(os.path.join(d, "run.py")))

    def read_text(self, path: str) -> str:
        with open(path) as f:
            return f.read()
