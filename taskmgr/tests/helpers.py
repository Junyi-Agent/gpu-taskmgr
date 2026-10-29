"""Test fixtures: fake GPUs, fake boot id, local-transport servers, daemons, packages.

Everything runs on one Linux machine without GPUs or network (Section 18.1).
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable, Iterable
from typing import IO, TypeVar, cast

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TASK = os.path.join(REPO, "bin", "task")
REMOTE = os.path.join(REPO, "bin", "taskmgr-remote")
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from taskmgr.common import STATE_DIRS  # noqa: E402
from taskmgr.records import Job, JsonDict, JsonValue, Runner, Server, override  # noqa: E402

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
        self.tmp = tempfile.mkdtemp(prefix="taskmgr-test-")
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
        self.env = dict(os.environ)
        self.env.update(
            {
                "TASKMGR_HOME": self.home,
                "TASKMGR_NVIDIA_SMI": self.smi,
                "TASKMGR_BOOT_ID_FILE": self.boot_file,
                "PYTHONPATH": REPO,
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        for k in (
            "TASKMGR_TEST_SHIM_CRASH",
            "TASKMGR_SHIM_EXECUTABLE",
            "TASKMGR_TEST_SKIP_DAEMON_LOCK",
            "TASKMGR_TEST_PROTOCOL_VERSION",
        ):
            _ = self.env.pop(k, None)
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
        self.daemons = {}
        self.pkg_count = 0

    @override
    def tearDown(self) -> None:
        for name in list(self.daemons):
            self.stop_daemon(name)
        # Kill any task / shim process left behind by this test.
        for name in self.servers:
            for pid in self._pids_under(self.root(name)):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pids_under(self, root: str) -> list[int]:
        pids: list[int] = []
        for n in os.listdir("/proc"):
            if not n.isdigit():
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
        self, *args: object, env: dict[str, str] | None = None, timeout: float = 120
    ) -> tuple[int, JsonValue, str]:
        e = dict(self.env)
        e.update(env or {})
        p = subprocess.run(
            [sys.executable, TASK] + [str(a) for a in args],
            env=e,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        out = p.stdout.decode()
        obj: JsonValue
        try:
            obj = cast(JsonValue, json.loads(out)) if out.strip() else None
        except ValueError:
            obj = out
        return p.returncode, obj, p.stderr.decode()

    def ok(
        self, *args: object, env: dict[str, str] | None = None, timeout: float = 120
    ) -> JsonValue:
        rc, obj, err = self.task(*args, env=env, timeout=timeout)
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
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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
        self.fail("timed out waiting for %s\n--- daemon log ---\n%s" % (msg, self.daemon_log()))

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
