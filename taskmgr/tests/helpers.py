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

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TASK = os.path.join(REPO, "bin", "task")
REMOTE = os.path.join(REPO, "bin", "taskmgr-remote")
if REPO not in sys.path:
    sys.path.insert(0, REPO)

FAST_CONFIG = {"stop_grace_seconds": 1, "kill_confirm_seconds": 1, "control_poll_seconds": 0.3,
               "scheduler_poll_seconds": 0.2, "max_auto_restarts": 2}
OK_RESULT = "echo '{\"outputs\": {}}' > \"$TASKMGR_RESULT_FILE\""
GPUS = [(0, "GPU-00000000-0000-0000-0000-000000000000"),
        (1, "GPU-11111111-1111-1111-1111-111111111111"),
        (2, "GPU-22222222-2222-2222-2222-222222222222"),
        (3, "GPU-33333333-3333-3333-3333-333333333333")]


class Env(unittest.TestCase):
    """Base class: builds a sandbox with N local-transport servers."""

    servers = ("s1",)
    remote_config = {}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="taskmgr-test-")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.gpu_file = os.path.join(self.tmp, "gpus.txt")
        self.set_gpus(GPUS)
        self.smi = os.path.join(self.tmp, "nvidia-smi")
        with open(self.smi, "w") as f:
            f.write("#!/bin/sh\ncat %s\n" % self.gpu_file)
        os.chmod(self.smi, 0o755)
        self.boot_file = os.path.join(self.tmp, "boot_id")
        self.set_boot("boot-1")
        self.env = dict(os.environ)
        self.env.update({"TASKMGR_HOME": self.home, "TASKMGR_NVIDIA_SMI": self.smi,
                         "TASKMGR_BOOT_ID_FILE": self.boot_file,
                         "PYTHONPATH": REPO, "PYTHONDONTWRITEBYTECODE": "1"})
        for k in ("TASKMGR_TEST_SHIM_CRASH", "TASKMGR_SHIM_EXECUTABLE",
                  "TASKMGR_TEST_SKIP_DAEMON_LOCK", "TASKMGR_TEST_PROTOCOL_VERSION"):
            self.env.pop(k, None)
        self.machines = []
        for name in self.servers:
            root = os.path.join(self.tmp, "remote-" + name)
            os.makedirs(root)
            cfg = dict(FAST_CONFIG)
            cfg.update(self.remote_config)
            with open(os.path.join(root, "config.json"), "w") as f:
                json.dump(cfg, f)
            self.machines.append({"name": name, "enabled": True, "transport": "local",
                                  "remote_root": root,
                                  "remote_command": "%s %s" % (sys.executable, REMOTE)})
        self.write_machines()
        self.daemons = {}
        self.pkg_count = 0

    def tearDown(self):
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

    def _pids_under(self, root):
        pids = []
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
    def set_gpus(self, gpus):
        with open(self.gpu_file, "w") as f:
            for i, u in gpus:
                f.write("%d, %s\n" % (i, u))

    def set_boot(self, value):
        with open(self.boot_file, "w") as f:
            f.write(value + "\n")

    def write_machines(self):
        with open(os.path.join(self.home, "machines.json"), "w") as f:
            json.dump({"servers": self.machines}, f)

    def server(self, name):
        return next(m for m in self.machines if m["name"] == name)

    def root(self, name="s1"):
        return self.server(name)["remote_root"]

    def set_config(self, name="s1", **kw):
        p = os.path.join(self.root(name), "config.json")
        with open(p) as f:
            cfg = json.load(f)
        cfg.update(kw)
        with open(p, "w") as f:
            json.dump(cfg, f)

    def make_pkg(self, task_id="AA-01", script=None, gpus=(), required=None, extra=None,
                 files=None, argv=None, timeout=None, env=None):
        """Create a package whose entry runs `sh -c script` (default: write a valid result)."""
        script = OK_RESULT if script is None else script
        self.pkg_count += 1
        d = os.path.join(self.tmp, "pkg-%d" % self.pkg_count)
        os.makedirs(d)
        task = {"schema_version": 1, "task_id": task_id,
                "entry": {"argv": argv or ["sh", "-c", script]}, "gpus": list(gpus)}
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
            f.write("# %s\n\nTest task.\n" % task_id)
        with open(os.path.join(d, "progress.json"), "w") as f:
            json.dump({"schema_version": 1, "stage": "init",
                       "updated_at": "2026-10-29T08:00:00Z"}, f)
        for rel, content in (files or {}).items():
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(content)
        return d

    # -- running commands -----------------------------------------------------
    def task(self, *args, env=None, timeout=120):
        e = dict(self.env)
        e.update(env or {})
        p = subprocess.run([sys.executable, TASK] + [str(a) for a in args], env=e,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        out = p.stdout.decode()
        try:
            obj = json.loads(out) if out.strip() else None
        except ValueError:
            obj = out
        return p.returncode, obj, p.stderr.decode()

    def ok(self, *args, **kw):
        rc, obj, err = self.task(*args, **kw)
        self.assertEqual(rc, 0, "task %s -> %d\nstdout=%s\nstderr=%s" % (args, rc, obj, err))
        return obj

    def submit(self, pkg, host="s1", key=None):
        args = ["submit", pkg] + (["--host", host] if host else []) + \
            (["--submission-key", key] if key else [])
        return self.ok(*args)["jobid"]

    def remote(self, *args, name="s1", env=None, stdin=None):
        e = dict(self.env)
        e.update(env or {})
        p = subprocess.run([sys.executable, REMOTE, "--root", self.root(name)] +
                           [str(a) for a in args], env=e, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, stdin=stdin or subprocess.DEVNULL,
                           timeout=60)
        try:
            obj = json.loads(p.stdout.decode())
        except ValueError:
            obj = p.stdout.decode()
        return p.returncode, obj, p.stderr.decode()

    def start_daemon(self, name="s1", env=None):
        e = dict(self.env)
        e.update(env or {})
        log = open(os.path.join(self.tmp, "daemon-%s.log" % name), "ab")
        p = subprocess.Popen([sys.executable, REMOTE, "--root", self.root(name), "serve"],
                             env=e, stdout=log, stderr=log, start_new_session=True)
        log.close()
        self.daemons.setdefault(name, []).append(p)
        return p

    def stop_daemon(self, name="s1", group=False):
        for p in self.daemons.pop(name, []):
            if p.poll() is None:
                if group:
                    os.killpg(p.pid, signal.SIGTERM)
                else:
                    p.terminate()
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()

    def daemon_log(self, name="s1"):
        try:
            with open(os.path.join(self.tmp, "daemon-%s.log" % name)) as f:
                return f.read()
        except FileNotFoundError:
            return ""

    # -- inspecting remote state ----------------------------------------------
    def job_dir(self, jobid, name="s1"):
        for d in ("queue", "in-progress", "done"):
            p = os.path.join(self.root(name), d, jobid)
            if os.path.isdir(p):
                return p
        return None

    def job(self, jobid, name="s1"):
        with open(os.path.join(self.job_dir(jobid, name), "job.json")) as f:
            return json.load(f)

    def runner(self, jobid, n=1, name="s1"):
        with open(os.path.join(self.job_dir(jobid, name), "attempts", "%04d" % n,
                               "runner.json")) as f:
            return json.load(f)

    def result(self, jobid, n=1, name="s1"):
        with open(os.path.join(self.job_dir(jobid, name), "attempts", "%04d" % n, "workdir",
                               "result.json")) as f:
            return json.load(f)

    def wait_for(self, pred, timeout=30, msg="condition", interval=0.1):
        end = time.time() + timeout
        while time.time() < end:
            v = pred()
            if v:
                return v
            time.sleep(interval)
        self.fail("timed out waiting for %s\n--- daemon log ---\n%s" % (msg, self.daemon_log()))

    def wait_state(self, jobid, states, name="s1", timeout=30):
        if isinstance(states, str):
            states = (states,)

        def check():
            try:
                return self.job(jobid, name)["state"] in states
            except (OSError, ValueError, TypeError):
                return False
        self.wait_for(check, timeout, "job %s to reach %s" % (jobid, states))
        return self.job(jobid, name)

    def wait_runner(self, jobid, key, n=1, name="s1", timeout=30):
        def check():
            try:
                return self.runner(jobid, n, name).get(key) is not None
            except (OSError, ValueError, TypeError):
                return False
        self.wait_for(check, timeout, "runner.%s of %s" % (key, jobid))
        return self.runner(jobid, n, name)


def pid_alive(pid):
    try:
        with open("/proc/%d/stat" % pid) as f:
            data = f.read()
    except OSError:
        return False
    return data[data.rfind(")") + 2] not in "ZX"


def kill_quiet(pid, sig=signal.SIGKILL, group=False):
    try:
        (os.killpg if group else os.kill)(pid, sig)
    except OSError:
        pass
