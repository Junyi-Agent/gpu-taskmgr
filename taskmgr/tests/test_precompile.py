"""task.json `precompile`: compile without GPUs, then wait for them (user decision 2026-11-01)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from taskmgr.common import override
from taskmgr.records import JsonValue
from taskmgr.tests.helpers import GPUS, OK_RESULT, REPO, Env

TEMPLATE = os.path.join(REPO, "taskmgr", "template")


class PrecompileTest(Env):
    def path(self, name: str) -> str:
        return os.path.join(self.tmp, name)

    def precompile(self, script: str, **fields: JsonValue) -> dict[str, JsonValue]:
        return {"precompile": {"argv": ["sh", "-c", script], **fields}}

    def state_cell(self, jobid: str) -> str:
        rc, out, _ = self.task("get", "jobs", text=True)
        self.assertEqual(rc, 0)
        row = next(line for line in self.text(out).splitlines() if line.startswith(jobid))
        return row.split("  ")[-1].strip()

    def shows(self, jobid: str, cell: str) -> None:
        _ = self.wait_for(lambda: self.state_cell(jobid) == cell, 20, cell)

    def gate(self, name: str) -> str:
        return "while [ ! -f %s ]; do sleep 0.05; done; " % self.path(name)

    def open_gate(self, name: str) -> None:
        with open(self.path(name), "w"):
            pass

    def test_phases_on_a_busy_gpu(self) -> None:
        """While A precompiles its GPU is free for B; then A waits for B and runs after it,
        in the same workdir and with the GPU it did not have before."""
        _ = self.start_daemon()
        compile_script = (
            'echo "compile [$CUDA_VISIBLE_DEVICES] [${TASKMGR_GPU_BUSY_EXIT_CODE-unset}]"; '
            + self.gate("compile-go")
            + "echo built > built.o"
        )
        entry = 'echo "entry [$CUDA_VISIBLE_DEVICES]"; cat built.o; ' + OK_RESULT
        a = self.submit(
            self.make_pkg(script=entry, gpus=[0], extra=self.precompile(compile_script))
        )
        self.shows(a, "precompiling")
        self.assertEqual(self.job(a).get("phase"), "precompile")
        self.assertEqual(self.get(self.ok("get", "job", a), "phase"), "precompile")
        self.assertEqual(self.runner(a).get("gpus"), [])

        hold = self.submit(self.make_pkg(script=self.gate("hold-go") + OK_RESULT, gpus=[0]))
        _ = self.wait_state(hold, "running")  # A does not hold GPU 0 while it compiles
        self.open_gate("compile-go")
        self.shows(a, "pending-gpu")
        job = self.job(a)
        self.assertEqual((job["state"], job.get("phase")), ("queued", "gpu"))
        self.assertIn("waiting for its GPUs", self.text(job, "pending_reason"))
        rc, out, _ = self.task(
            "logs", "job", a, text=True
        )  # the precompile's output, while it waits
        self.assertEqual(rc, 0)
        self.assertIn("compile [] [unset]", self.text(out))
        self.open_gate("hold-go")

        job = self.wait_state(a, "succeeded")
        self.assertNotIn("pending_reason", job)
        self.assertEqual(self.runner(a).get("gpus"), job["gpus"])
        _, out, _ = self.task("logs", "job", a, text=True)
        self.assertIn("compile [] [unset]", self.text(out))
        self.assertIn("launch after requeue (precompile done)", self.text(out))
        self.assertIn("entry [%s]\nbuilt\n" % GPUS[0][1], self.text(out))
        self.assertEqual(job["enqueue_seq"], 1)

    def test_failures(self) -> None:
        _ = self.start_daemon()
        marker = self.path("entry-ran")
        for script, message in (("exit 3", "exit code 3"), ("exit 75", "exit code 75")):
            j = self.submit(
                self.make_pkg(
                    script="touch %s; %s" % (marker, OK_RESULT),
                    gpus=[0],
                    extra=self.precompile(script),
                )
            )
            job = self.wait_state(j, "failed")
            self.assertEqual(self.get(job, "error", "kind"), "precompile")
            self.assertIn(message, self.text(job, "error", "message"))
            self.assertFalse(os.path.exists(marker))
        # A missing command is found before the start, as for an entry.
        j = self.submit(self.make_pkg(extra={"precompile": {"argv": ["no-such-compiler"]}}))
        self.assertEqual(self.get(self.wait_state(j, "failed"), "error", "kind"), "setup")

    def test_timeout_is_its_own(self) -> None:
        _ = self.start_daemon()
        j = self.submit(
            self.make_pkg(extra=self.precompile("sleep 60", timeout_seconds=1), timeout=100)
        )
        _ = self.wait_state(j, "timed_out")
        # The entry's timeout does not count the precompile.
        j = self.submit(self.make_pkg(extra=self.precompile("sleep 2"), timeout=1))
        _ = self.wait_state(j, "succeeded")

    def test_cancel(self) -> None:
        _ = self.start_daemon()
        j = self.submit(self.make_pkg(extra=self.precompile("sleep 60")))
        self.shows(j, "precompiling")
        _ = self.ok("cancel", "job", j)
        _ = self.wait_state(j, "cancelled")
        # While it waits for its GPU.
        hold = self.submit(self.make_pkg(script=self.gate("go") + OK_RESULT, gpus=[0]))
        _ = self.wait_state(hold, "running")
        j = self.submit(self.make_pkg(gpus=[0], extra=self.precompile("true")))
        self.shows(j, "pending-gpu")
        _ = self.ok("cancel", "job", j)
        _ = self.wait_state(j, "cancelled")
        self.open_gate("go")
        _ = self.wait_state(hold, "succeeded")

    def test_retry_precompiles_again(self) -> None:
        _ = self.start_daemon()
        count = self.path("compiles")
        j = self.submit(
            self.make_pkg(script="exit 1", extra=self.precompile("echo x >> %s" % count))
        )
        _ = self.wait_state(j, "failed")
        new = self.text(self.ok("retry", "job", j), "jobid")
        self.assertEqual(self.job(new).get("phase"), "precompile")
        _ = self.wait_state(new, "failed")
        with open(count) as f:
            self.assertEqual(len(f.read().splitlines()), 2)

    def test_shim_crash_right_after_the_precompile(self) -> None:
        # The shim dies after recording exit 0, so recovery moves the job on.
        _ = self.start_daemon(env={"TASKMGR_TEST_SHIM_CRASH": "after_exit_recorded"})
        j = self.submit(self.make_pkg(extra=self.precompile("true")))
        job = self.wait_state(j, "succeeded", timeout=30)
        self.assertEqual((job["auto_restarts"], job["retried_as"]), (0, []))

    def test_dispatch_sets_aside_a_precompile_record(self) -> None:
        # A crash in queue_for_gpus can leave the precompile's runner.json in a queued job.
        j = self.submit(self.make_pkg(extra=self.precompile("true")))
        jd = self.job_dir(j)
        job = self.job(j)
        job["phase"] = "gpu"
        job["dispatched_at"] = job["submitted_at"]
        os.chmod(jd, 0o755)
        with open(os.path.join(jd, "job.json"), "w") as f:
            json.dump(job, f)
        runner = {"jobid": j, "phase": "precompile", "pgid": 1, "exited": True, "return_code": 0}
        with open(os.path.join(jd, "runner.json"), "w") as f:
            json.dump(runner, f)
        from taskmgr.shim import prepare_workdir

        _ = prepare_workdir(jd)
        _ = self.start_daemon()
        _ = self.wait_state(j, "succeeded")
        self.assertTrue(os.path.exists(os.path.join(self.job_dir(j), "runner.prev-1.json")))

    def test_validation(self) -> None:
        cases: list[tuple[JsonValue, str]] = [
            ("sh", "precompile must be an object"),
            ({"argv": []}, "precompile.argv"),
            ({"argv": ["sh"], "timeout_seconds": 0}, "precompile.timeout_seconds"),
            ({"argv": ["./missing.sh"]}, "precompile argv[0]"),
        ]
        for bad, text in cases:
            rc, out, _ = self.task("validate", "package", self.make_pkg(extra={"precompile": bad}))
            self.assertEqual(rc, 1, bad)
            self.assertIn(text, json.dumps(out))


class TemplateFilesTest(unittest.TestCase):
    """precompile.py and cutecache.py against stand-ins for cutlass and torch: the real ones
    need a GPU stack, so this covers taskmgr's changes to them, not CuTe itself."""

    tmp: str = ""

    @override
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="taskmgr-template-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        files = {
            "cutlass/__init__.py": '__version__ = "1"\n',
            "cutlass/cute.py": (
                "import os, types\n"
                "class Compiled:\n"
                "    kernel_info = {}\n"
                "    def __init__(self, fn): self.fn = fn\n"
                "    def export_to_c(self, object_file_path, function_name):\n"
                "        open(object_file_path, 'w').write(self.fn.__name__)\n"
                "def compile(fn, *args, **kwargs):\n"
                "    open(os.environ['COMPILE_LOG'], 'a').write('x\\n')\n"
                "    return Compiled(fn)\n"
                "runtime = types.SimpleNamespace(\n"
                "    load_module=lambda p, enable_tvm_ffi: {'func': open(p).read()})\n"
            ),
            "torch.py": "import types\ncuda = types.SimpleNamespace()\n",
            "kernels.py": (
                "import cutlass.cute as cute\n"
                "def kern(x): return x\n"
                "def _compile_all(): return cute.compile(kern, 3)\n"
            ),
            "check.py": (
                "import cutecache\n"
                "import cutlass.cute as cute\n"
                "m = cutecache.load_precompiled('kernels.py')\n"
                "print(m._compile_all())\n"
                "try:\n"
                "    cute.compile(m.kern, 4)\n"
                "except RuntimeError as e:\n"
                "    print('refused', 'not precompiled' in str(e))\n"
            ),
        }
        for rel, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(self.tmp, rel)), exist_ok=True)
            with open(os.path.join(self.tmp, rel), "w") as f:
                _ = f.write(text)
        for name in ("cutecache.py", "precompile.py"):
            _ = shutil.copy(os.path.join(TEMPLATE, name), self.tmp)

    def run_py(self, *argv: str, gpus: str) -> str:
        env = dict(os.environ)
        env.update(
            {
                "COMPILE_LOG": os.path.join(self.tmp, "compiles"),
                "CUDA_VISIBLE_DEVICES": gpus,
                "TASKMGR_WORKDIR": self.tmp,
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        _ = env.pop("CUTECACHE_DIR", None)
        p = subprocess.run(
            [sys.executable, *argv], cwd=self.tmp, env=env, capture_output=True, text=True
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def test_compile_on_cpu_then_load_only(self) -> None:
        out = self.run_py("precompile.py", "kernels.py", gpus="")
        self.assertIn("1 kernels", out)
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "kernel-cache")))
        self.assertEqual(self.run_py("check.py", gpus="0"), "kern\nrefused True\n")
        with open(os.path.join(self.tmp, "compiles")) as f:
            self.assertEqual(f.read(), "x\n")  # compiled once, on the CPU
