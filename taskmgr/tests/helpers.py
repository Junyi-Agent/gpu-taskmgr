"""Test fixtures: fake GPUs, fake boot id, local-transport servers, daemons, packages.

Everything runs on one Linux machine without GPUs or network (Section 18.1). On other
platforms the Env-based tests are skipped; run them in Docker (docs/testing.md).
"""

import atexit
import json
import os
import queue
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable, Iterable
from typing import IO, TypeVar, cast

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TASK = os.path.join(REPO, "bin", "task")
REMOTE = os.path.join(REPO, "bin", "taskmgr-remote")
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from taskmgr.common import STATE_DIRS, override  # noqa: E402
from taskmgr.records import Job, JsonDict, JsonValue, Runner, Server  # noqa: E402

T = TypeVar("T")

FAST_CONFIG: JsonDict = {
    "stop_grace_seconds": 1,
    "kill_confirm_seconds": 1,
    "control_poll_seconds": 0.3,
    "scheduler_poll_seconds": 0.2,
    "max_auto_restarts": 2,
}
OK_RESULT = 'echo \'{"outputs": {}}\' > "$TASKMGR_RESULT_FILE"'
GPUS = [
    (0, "GPU-00000000-0000-0000-0000-000000000000"),
    (1, "GPU-11111111-1111-1111-1111-111111111111"),
    (2, "GPU-22222222-2222-2222-2222-222222222222"),
    (3, "GPU-33333333-3333-3333-3333-333333333333"),
]
LINUX_ONLY = "needs Linux (/proc, setsid); on macOS run the tests in Docker, see docs/testing.md"

# Env instances whose sandbox has not been cleaned up yet. If the run ends without tearDown
# (Ctrl-C, SIGTERM, an error in the runner), the atexit hook stops their daemons / shims and
# removes their directories, so nothing outlives the test run.
_live: "list[Env]" = []


def _cleanup_all() -> None:
    for env in list(_live):
        env.cleanup_sandbox()


def _exit_on_signal(signum: int, _frame: object) -> None:
    # SystemExit would only fail the current test (unittest catches it), so clean up here
    # and then die of the signal as before.
    _cleanup_all()
    _ = signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


_ = atexit.register(_cleanup_all)
for _sig in (signal.SIGTERM, signal.SIGHUP):
    if signal.getsignal(_sig) is signal.SIG_DFL:
        _ = signal.signal(_sig, _exit_on_signal)


def force_rmtree(path: str) -> None:
    """rmtree that first gives u+rwx back to directories made read-only (package copies)."""
    for dirpath, dirnames, _files in os.walk(path):
        for d in [dirpath] + [os.path.join(dirpath, n) for n in dirnames]:
            try:
                if not os.path.islink(d):
                    os.chmod(d, stat.S_IMODE(os.lstat(d).st_mode) | stat.S_IRWXU)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


@unittest.skipUnless(sys.platform == "linux", LINUX_ONLY)
class Env(unittest.TestCase):
    """Base class: builds a sandbox with N local-transport servers."""

    servers: tuple[str, ...] = ("s1",)
    remote_config: JsonDict = {}
    # Assigned in setUp; the class-level values are placeholders.
    tmp: str = ""
    home: str = ""
    gpu_file: str = ""
    smi: str = ""
    boot_file: str = ""
    env: dict[str, str] = {}
    machines: list[Server] = []
    daemons: dict[str, list[subprocess.Popen[bytes]]] = {}
    pkg_count: int = 0

    @override
    def setUp(self) -> None:
        self.daemons = {}
        self.tmp = tempfile.mkdtemp(prefix="taskmgr-test-")
        # Registered first, so it also runs when setUp fails half-way; tearDown calls it
        # too (it is idempotent).
        _live.append(self)
        self.addCleanup(self.cleanup_sandbox)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.gpu_file = os.path.join(self.tmp, "gpus.txt")
        self.set_gpus(GPUS)
        self.smi = os.path.join(self.tmp, "nvidia-smi")
        with open(self.smi, "w") as f:
            _ = f.write("#!/bin/sh\ncat %s\n" % self.gpu_file)
        os.chmod(self.smi, 0o755)
        self.boot_file = os.path.join(self.tmp, "boot_id")
        self.set_boot("boot-1")
        # Nothing in the caller's environment may steer the code under test: drop every
        # TASKMGR_* variable (test hooks, TASKMGR_HOME, ...) and NO_COLOR.
        self.env = {
            k: v for k, v in os.environ.items() if not k.startswith("TASKMGR_") and k != "NO_COLOR"
        }
        self.env.update(
            {
                "TASKMGR_HOME": self.home,
                "TASKMGR_NVIDIA_SMI": self.smi,
                "TASKMGR_BOOT_ID_FILE": self.boot_file,
                "PYTHONPATH": REPO,
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        self.machines = []
        for name in self.servers:
            root = os.path.join(self.tmp, "remote-" + name)
            os.makedirs(root)
            cfg = dict(FAST_CONFIG)
            cfg.update(self.remote_config)
            with open(os.path.join(root, "config.json"), "w") as f:
                json.dump(cfg, f)
            self.machines.append(
                {
                    "name": name,
                    "enabled": True,
                    "transport": "local",
                    "remote_root": root,
                    "remote_command": "%s %s" % (sys.executable, REMOTE),
                }
            )
        self.write_machines()
        self.pkg_count = 0

    @override
    def tearDown(self) -> None:
        self.cleanup_sandbox()

    def cleanup_sandbox(self) -> None:
        """Stop this test's daemons, kill its tasks / shims, remove its directory. Idempotent."""
        if self not in _live:
            return
        _live.remove(self)
        for name in list(self.daemons):
            self.stop_daemon(name)
        # Kill any task / shim process left behind by this test (anything running in or
        # naming the sandbox: daemons, shims, tasks, servers' homes).
        for pid in self._pids_under(self.tmp):
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        force_rmtree(self.tmp)

    def _pids_under(self, root: str) -> list[int]:
        pids: list[int] = []
        for n in os.listdir("/proc"):
            if not n.isdigit() or int(n) == os.getpid():
                continue
            try:
                with open("/proc/%s/cmdline" % n, "rb") as f:
                    cmd = f.read().decode(errors="replace")
                cwd = os.readlink("/proc/%s/cwd" % n)
            except OSError:
                continue
            if root in cmd or cwd.startswith(root):
                pids.append(int(n))
        return pids

    # -- fixtures -------------------------------------------------------------
    def set_gpus(self, gpus: Iterable[tuple[int, str]]) -> None:
        with open(self.gpu_file, "w") as f:
            for i, u in gpus:
                _ = f.write("%d, %s\n" % (i, u))

    def set_boot(self, value: str) -> None:
        with open(self.boot_file, "w") as f:
            _ = f.write(value + "\n")

    def write_machines(self) -> None:
        with open(os.path.join(self.home, "machines.json"), "w") as f:
            json.dump({"servers": self.machines}, f)

    def server(self, name: str) -> Server:
        return next(m for m in self.machines if m["name"] == name)

    def root(self, name: str = "s1") -> str:
        return self.server(name)["remote_root"]

    def set_config(self, name: str = "s1", **kw: JsonValue) -> None:
        p = os.path.join(self.root(name), "config.json")
        with open(p) as f:
            cfg = self.obj(cast(JsonValue, json.load(f)))
        cfg.update(kw)
        with open(p, "w") as f:
            json.dump(cfg, f)

    def make_pkg(
        self,
        task_id: str = "AA-01",
        script: str | None = None,
        gpus: Iterable[int | str] = (),
        required: Iterable[str] | None = None,
        extra: JsonDict | None = None,
        files: dict[str, str] | None = None,
        argv: list[str] | None = None,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        """Create a package whose entry runs `sh -c script` (default: write a valid result)."""
        script = OK_RESULT if script is None else script
        self.pkg_count += 1
        d = os.path.join(self.tmp, "pkg-%d" % self.pkg_count)
        os.makedirs(d)
        task: dict[str, object] = {
            "schema_version": 1,
            "task_id": task_id,
            "entry": {"argv": argv or ["sh", "-c", script]},
            "gpus": list(gpus),
        }
        if required is not None:
            task["result"] = {"required_outputs": list(required)}
        if timeout is not None:
            task["timeout_seconds"] = timeout
        if env is not None:
            task["env"] = env
        if extra:
            task.update(extra)
        with open(os.path.join(d, "task.json"), "w") as f:
            json.dump(task, f)
        with open(os.path.join(d, "TASK.md"), "w") as f:
            _ = f.write("# %s\n\nTest task.\n" % task_id)
        with open(os.path.join(d, "progress.json"), "w") as f:
            json.dump(
                {"schema_version": 1, "stage": "init", "updated_at": "2026-10-29T08:00:00Z"}, f
            )
        for rel, content in (files or {}).items():
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                _ = f.write(content)
        return d

    # -- running commands -----------------------------------------------------
    def task(
        self,
        *args: object,
        env: dict[str, str] | None = None,
        timeout: float = 120,
        text: bool = False,
        color: bool = False,
    ) -> tuple[int, JsonValue, str]:
        """Run `task`. Without text=True, --detail is added so stdout is the JSON object
        (except for `logs`, which has no --detail); without color=True, --plain."""
        e = dict(self.env)
        e.update(env or {})
        p = subprocess.run(
            self._task_argv(args, text, color),
            env=e,
            # Not the runner's stdin: from a terminal, prompts would wait for an answer.
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
        )
        return p.returncode, self._parse_out(p.stdout.decode()), p.stderr.decode()

    def _task_argv(self, args: tuple[object, ...], text: bool, color: bool) -> list[str]:
        argv = [str(a) for a in args]
        at = min(2 if argv[:1] == ["host"] else 1, len(argv))  # after the (sub)command name
        if not text and argv and argv[0] != "logs" and "--detail" not in argv:
            argv.insert(at, "--detail")
        if not color:
            argv.insert(at, "--plain")
        return [sys.executable, TASK] + argv

    def _parse_out(self, out: str) -> JsonValue:
        try:
            return cast(JsonValue, json.loads(out)) if out.strip() else None
        except ValueError:
            return out

    def task_when(
        self,
        marker: str,
        action: Callable[[], object],
        *args: object,
        timeout: float = 120,
    ) -> tuple[int, JsonValue, str]:
        """Run `task` like task(), and call `action` once `marker` appears on its stderr.

        Use it instead of a timer when the test must change something (bring a server back,
        open a gate) only after the command has reached a given point. Fails if the command
        exits or `timeout` passes before the marker appears.
        """
        p = subprocess.Popen(
            self._task_argv(args, False, False),
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = cast(IO[bytes], p.stdout), cast(IO[bytes], p.stderr)
        out_chunks: list[bytes] = []
        lines: queue.Queue[str] = queue.Queue()

        def read_err() -> None:
            for raw in stderr:  # the command flushes every stderr note
                lines.put(raw.decode())
            lines.put("")  # EOF

        readers = [
            threading.Thread(target=lambda: out_chunks.append(stdout.read())),
            threading.Thread(target=read_err),
        ]
        for t in readers:
            t.start()
        err: list[str] = []
        found = False
        end = time.monotonic() + timeout
        try:
            while not found:
                try:
                    line = lines.get(timeout=max(0.0, end - time.monotonic()))
                except queue.Empty:
                    break
                if not line:  # EOF: the command exited
                    break
                err.append(line)
                found = marker in line
            if found:
                _ = action()
                _ = p.wait(max(1.0, end - time.monotonic()))
        finally:
            if p.poll() is None:
                p.kill()
                _ = p.wait()
            for t in readers:
                t.join()
            stdout.close()
            stderr.close()
        while not lines.empty():
            err.append(lines.get())
        if not found:
            self.fail(
                "%r never appeared on stderr of task %s\nstderr=%s" % (marker, args, "".join(err))
            )
        out = b"".join(out_chunks).decode()
        return p.returncode, self._parse_out(out), "".join(err)

    def ok(
        self,
        *args: object,
        env: dict[str, str] | None = None,
        timeout: float = 120,
        text: bool = False,
        color: bool = False,
    ) -> JsonValue:
        rc, obj, err = self.task(*args, env=env, timeout=timeout, text=text, color=color)
        self.assertEqual(rc, 0, "task %s -> %d\nstdout=%s\nstderr=%s" % (args, rc, obj, err))
        return obj

    def submit(self, pkg: str, host: str | None = "s1", key: str | None = None) -> str:
        args: list[str] = (
            ["submit", pkg]
            + (["--host", host] if host else [])
            + (["--submission-key", key] if key else [])
        )
        return self.text(self.ok(*args), "jobid")

    def remote(
        self,
        *args: object,
        name: str = "s1",
        env: dict[str, str] | None = None,
        stdin: IO[bytes] | None = None,
    ) -> tuple[int, JsonValue, str]:
        e = dict(self.env)
        e.update(env or {})
        p = subprocess.run(
            [sys.executable, REMOTE, "--root", self.root(name)] + [str(a) for a in args],
            env=e,
            capture_output=True,
            stdin=stdin or subprocess.DEVNULL,
            timeout=60,
        )
        obj: JsonValue
        try:
            obj = cast(JsonValue, json.loads(p.stdout.decode()))
        except ValueError:
            obj = p.stdout.decode()
        return p.returncode, obj, p.stderr.decode()

    def start_daemon(
        self, name: str = "s1", env: dict[str, str] | None = None
    ) -> subprocess.Popen[bytes]:
        e = dict(self.env)
        e.update(env or {})
        log = open(os.path.join(self.tmp, "daemon-%s.log" % name), "ab")
        p = subprocess.Popen(
            [sys.executable, REMOTE, "--root", self.root(name), "serve"],
            env=e,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        log.close()
        self.daemons.setdefault(name, []).append(p)
        return p

    def stop_daemon(self, name: str = "s1", group: bool = False) -> None:
        for p in self.daemons.pop(name, []):
            if p.poll() is None:
                if group:
                    os.killpg(p.pid, signal.SIGTERM)
                else:
                    p.terminate()
                try:
                    _ = p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()
                    _ = p.wait()

    def daemon_log(self, name: str = "s1") -> str:
        try:
            with open(os.path.join(self.tmp, "daemon-%s.log" % name)) as f:
                return f.read()
        except FileNotFoundError:
            return ""

    # -- inspecting remote state ----------------------------------------------
    def job_dir(self, jobid: str, name: str = "s1") -> str:
        """The job's current directory; FileNotFoundError (an OSError) if it has none."""
        for d in ("queue", "in-progress", "done"):
            p = os.path.join(self.root(name), d, jobid)
            if os.path.isdir(p):
                return p
        raise FileNotFoundError("job %s has no directory on %s" % (jobid, name))

    def job(self, jobid: str, name: str = "s1") -> Job:
        return cast(Job, self._read_obj(os.path.join(self.job_dir(jobid, name), "job.json")))

    def runner(self, jobid: str, name: str = "s1") -> Runner:
        return cast(Runner, self._read_obj(os.path.join(self.job_dir(jobid, name), "runner.json")))

    def result(self, jobid: str, name: str = "s1") -> JsonDict:
        return self.obj(
            self._read_obj(os.path.join(self.job_dir(jobid, name), "workdir", "result.json"))
        )

    def jobids(self, name: str = "s1") -> set[str]:
        """Every jobid that has a directory on the server."""
        return {
            j
            for d in ("queue", "in-progress", "done")
            if os.path.isdir(os.path.join(self.root(name), d))
            for j in os.listdir(os.path.join(self.root(name), d))
        }

    def chain(self, jobid: str, name: str = "s1") -> list[str]:
        """jobid followed by its automatic / manual re-runs (the last of `retried_as`)."""
        out = [jobid]
        while self.job(out[-1], name)["retried_as"]:
            out.append(self.job(out[-1], name)["retried_as"][-1])
        return out

    def _read_obj(self, path: str) -> object:
        with open(path) as f:
            return self.obj(cast(JsonValue, json.load(f)))

    def wait_for(
        self,
        pred: Callable[[], T],
        timeout: float = 30,
        msg: str = "condition",
        interval: float = 0.1,
    ) -> T:
        end = time.time() + timeout
        while time.time() < end:
            v = pred()
            if v:
                return v
            time.sleep(interval)
        # self.fail() spelled out: ruff (RET503) does not know that fail() never returns.
        raise self.failureException(
            "timed out waiting for %s\n--- daemon log ---\n%s" % (msg, self.daemon_log())
        )

    def wait_state(
        self, jobid: str, states: str | tuple[str, ...], name: str = "s1", timeout: float = 30
    ) -> Job:
        if isinstance(states, str):
            states = (states,)

        def check() -> bool:
            # job.json is written before the directory is renamed (Section 8), so also wait
            # until the directory sits where the state says; tests then read files from it.
            try:
                state = self.job(jobid, name)["state"]
                where = os.path.basename(os.path.dirname(self.job_dir(jobid, name)))
            except (OSError, ValueError, TypeError):
                return False
            return state in states and where == STATE_DIRS[state]

        _ = self.wait_for(check, timeout, "job %s to reach %s" % (jobid, states))
        return self.job(jobid, name)

    def wait_runner(self, jobid: str, key: str, name: str = "s1", timeout: float = 30) -> Runner:
        def check() -> bool:
            try:
                return self.obj(self.runner(jobid, name)).get(key) is not None
            except (OSError, ValueError, TypeError):
                return False

        _ = self.wait_for(check, timeout, "runner.%s of %s" % (key, jobid))
        return self.runner(jobid, name)

    def wait_scheduler_passes(
        self, n: int = 1, name: str = "s1", gpus: Iterable[int] = (), timeout: float = 30
    ) -> list[str]:
        """Return once the daemon on `name` has run n scheduler passes that began after this
        call; use it instead of a fixed sleep before asserting that a job is "still queued".

        The marker is a no-op probe job, submitted after everything the caller has set up.
        A pass walks the queue in submission order, so the pass that dispatches the probe
        has also considered every job queued before it. Each probe is waited for until it
        has succeeded, so it holds no slot afterwards. The probe is a CPU job: with a full
        max_cpu_jobs, pass a free GPU in `gpus`. Returns the probe jobids, so callers that
        compare job sets (jobids(), `task jobs`) can leave them out.
        """
        probes: list[str] = []
        for _ in range(n):
            j = self.submit(self.make_pkg(task_id="ZZ-99", gpus=gpus), host=name)
            _ = self.wait_state(j, "succeeded", name, timeout)
            probes.append(j)
        return probes

    # -- typed access to parsed JSON ------------------------------------------
    def get(self, x: object, *path: str | int) -> JsonValue:
        """x[path[0]][path[1]]...: str steps index dicts, int steps index lists."""
        cur = cast(JsonValue, x)
        for p in path:
            if isinstance(p, int):
                if not isinstance(cur, list):
                    self.fail("expected a list at %r in %r, got %r" % (p, path, cur))
                cur = cur[p]
            else:
                if not isinstance(cur, dict):
                    self.fail("expected an object at %r in %r, got %r" % (p, path, cur))
                cur = cur[p]
        return cur

    def obj(self, x: object, *path: str | int) -> JsonDict:
        v = self.get(x, *path)
        if not isinstance(v, dict):
            self.fail("expected an object at %r, got %r" % (path, v))
        return v

    def arr(self, x: object, *path: str | int) -> list[JsonValue]:
        v = self.get(x, *path)
        if not isinstance(v, list):
            self.fail("expected a list at %r, got %r" % (path, v))
        return v

    def objs(self, x: object, *path: str | int) -> list[JsonDict]:
        return [self.obj(e) for e in self.arr(x, *path)]

    def text(self, x: object, *path: str | int) -> str:
        v = self.get(x, *path)
        if not isinstance(v, str):
            self.fail("expected a string at %r, got %r" % (path, v))
        return v

    def strs(self, x: object, *path: str | int) -> list[str]:
        return [self.text(e) for e in self.arr(x, *path)]

    def num(self, x: object, *path: str | int) -> int:
        v = self.get(x, *path)
        if not isinstance(v, int):
            self.fail("expected an integer at %r, got %r" % (path, v))
        return v


def pid_alive(pid: int) -> bool:
    try:
        with open("/proc/%d/stat" % pid) as f:
            data = f.read()
    except OSError:
        return False
    return data[data.rfind(")") + 2] not in "ZX"


def kill_quiet(pid: int, sig: int = signal.SIGKILL, group: bool = False) -> None:
    try:
        (os.killpg if group else os.kill)(pid, sig)
    except OSError:
        pass
