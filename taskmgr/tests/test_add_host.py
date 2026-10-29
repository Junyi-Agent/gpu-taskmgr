"""`task add host` (desc-2 D18): server setup and update, step by step and idempotent.

The "server" is a local-transport machine entry whose HOME is a directory of the test
(test-only `"local": {"home": ...}`, created through TASKMGR_TEST_LOCAL_HOME). Fake
`systemctl` and `loginctl` scripts on PATH stand in for systemd: they keep their state under
$HOME/.fake-systemd, log every call, and `start` really launches the unit's ExecStart (the
daemon), so jobs run end to end. `stop` signals only the main PID, like KillMode=process.
"""

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from typing import cast

from taskmgr import hostsetup
from taskmgr.records import JsonDict, JsonValue, Server, override
from taskmgr.tests.helpers import REMOTE, REPO, TASK, Env, kill_quiet, pid_alive
from taskmgr.transport import Remote

FAKE_SYSTEMCTL = r"""#!%(python)s
import os, shlex, signal, subprocess, sys, time

state = os.path.join(os.environ["HOME"], ".fake-systemd")
os.makedirs(state, exist_ok=True)
args = [a for a in sys.argv[1:] if not a.startswith("--")]
with open(os.path.join(state, "calls.log"), "a") as f:
    f.write("systemctl " + " ".join(args) + "\n")
unit = os.path.join(os.environ["HOME"], ".config/systemd/user/taskmgr-remote.service")
loaded = os.path.join(state, "loaded.service")
pidfile = os.path.join(state, "main.pid")

def read(p):
    try:
        with open(p) as f:
            return f.read()
    except OSError:
        return None

def alive(pid):
    try:
        with open("/proc/%%d/stat" %% pid) as f:
            data = f.read()
    except OSError:
        return False
    return data[data.rfind(")") + 2] not in "ZX"

def main_pid():
    t = read(pidfile)
    pid = int(t) if t and t.strip().isdigit() else 0
    return pid if pid and alive(pid) else 0

def stop():
    pid = main_pid()
    if pid:
        os.kill(pid, signal.SIGTERM)  # KillMode=process: the main process only
        end = time.time() + 30
        while alive(pid) and time.time() < end:
            time.sleep(0.05)
    if os.path.exists(pidfile):
        os.unlink(pidfile)

def start():
    if main_pid():
        return 0
    text = read(loaded)
    if text is None or text != read(unit):
        sys.stderr.write("fake systemctl: unit not loaded or changed on disk; daemon-reload\n")
        return 1
    line = [l for l in text.splitlines() if l.startswith("ExecStart=")][0]
    argv = shlex.split(line[len("ExecStart="):].replace("%%%%", "%%").replace("$$", "$"))
    log = open(os.path.join(state, "daemon.log"), "ab")
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True)
    with open(pidfile, "w") as f:
        f.write("%%d\n" %% p.pid)
    return 0

cmd = args[0] if args else ""
if cmd == "show-environment":
    print("HOME=" + os.environ["HOME"])
elif cmd == "daemon-reload":
    text = read(unit)
    if text is None:
        if os.path.exists(loaded):
            os.unlink(loaded)
    else:
        with open(loaded, "w") as f:
            f.write(text)
elif cmd == "is-active":
    print("active" if main_pid() else "inactive")
    sys.exit(0 if main_pid() else 3)
elif cmd == "is-enabled":
    on = os.path.exists(os.path.join(state, "enabled"))
    print("enabled" if on else "disabled")
    sys.exit(0 if on else 1)
elif cmd == "enable":
    if read(loaded) is None:
        sys.stderr.write("fake systemctl: unit taskmgr-remote.service does not exist\n")
        sys.exit(1)
    open(os.path.join(state, "enabled"), "w").close()
elif cmd == "show":
    print("MainPID=%%d" %% main_pid())
elif cmd == "start":
    sys.exit(start())
elif cmd == "stop":
    stop()
elif cmd == "restart":
    stop()
    sys.exit(start())
else:
    sys.stderr.write("fake systemctl: unsupported %%r\n" %% (args,))
    sys.exit(1)
"""

FAKE_LOGINCTL = r"""#!%(python)s
import os, sys

state = os.path.join(os.environ["HOME"], ".fake-systemd")
os.makedirs(state, exist_ok=True)
args = [a for a in sys.argv[1:] if not a.startswith("--")]
with open(os.path.join(state, "calls.log"), "a") as f:
    f.write("loginctl " + " ".join(args) + "\n")
flag = os.path.join(state, "linger")
if args[:1] == ["show-user"]:
    print("Linger=%%s" %% ("yes" if os.path.exists(flag) else "no"))
elif args[:1] == ["enable-linger"]:
    if os.path.exists(os.path.join(state, "linger-refuse")):
        sys.stderr.write("Could not enable linger: Access denied\n")
        sys.exit(1)
    open(flag, "w").close()
else:
    sys.stderr.write("fake loginctl: unsupported %%r\n" %% (args,))
    sys.exit(1)
"""

# systemctl / loginctl calls that change something.
CHANGING = re.compile(r"^(systemctl (daemon-reload|enable|start|stop|restart)|loginctl enable)")
ALL_STEPS = (
    "ssh",
    "code",
    "remote_root",
    "data",
    "unit",
    "service",
    "linger",
    "verify",
    "machines",
)


class AddHostTest(Env):
    rhome: str = ""

    @override
    def setUp(self) -> None:
        super().setUp()
        self.rhome = os.path.join(self.tmp, "rhome")
        os.makedirs(self.rhome)
        fakebin = os.path.join(self.tmp, "fakebin")
        os.makedirs(fakebin)
        for name, text in (("systemctl", FAKE_SYSTEMCTL), ("loginctl", FAKE_LOGINCTL)):
            path = os.path.join(fakebin, name)
            with open(path, "w") as f:
                _ = f.write(text % {"python": sys.executable})
            os.chmod(path, 0o755)
        self.env["PATH"] = fakebin + os.pathsep + self.env.get("PATH", "")
        self.env["TASKMGR_TEST_LOCAL_HOME"] = self.rhome
        _ = self.env.pop("TASKMGR_TEST_INTERACTIVE", None)

    @override
    def tearDown(self) -> None:
        for pid in self._pids_under(self.rhome):
            kill_quiet(pid)
        super().tearDown()

    # -- helpers ----------------------------------------------------------------
    def add(
        self, *extra: str, answers: str | None = None, name: str = "h1", new: bool = True
    ) -> tuple[int, JsonDict, str]:
        """Run `task add host`; answers != None makes stdin an "interactive" pipe."""
        args = ["add", "host", name] + (["--ssh", "tuner@gpu.example"] if new else []) + list(extra)
        env = dict(self.env)
        if answers is not None:
            env["TASKMGR_TEST_INTERACTIVE"] = "1"
        p = subprocess.run(
            [sys.executable, TASK] + args,
            env=env,
            input=(answers or "").encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
        )
        err = p.stderr.decode()
        try:
            out = self.obj(cast(JsonValue, json.loads(p.stdout.decode())))
        except ValueError:
            self.fail("no JSON on stdout: %r\nstderr:\n%s" % (p.stdout, err))
        return p.returncode, out, err

    def statuses(self, out: JsonDict) -> dict[str, str]:
        return {self.text(s, "name"): self.text(s, "status") for s in self.objs(out, "steps")}

    def detail(self, out: JsonDict, step: str) -> str:
        return next(self.text(s, "detail") for s in self.objs(out, "steps") if s["name"] == step)

    def expect(self, out: JsonDict, err: str, **want: str) -> None:
        """Every step not named in `want` must be ok."""
        got = self.statuses(out)
        expected = {s: want.get(s, "ok") for s in ALL_STEPS}
        self.assertEqual(got, expected, "stderr:\n%s" % err)

    def rpath(self, *parts: str) -> str:
        return os.path.join(self.rhome, *parts)

    def rroot(self) -> str:
        return self.rpath("taskmgr-root")

    def machines_raw(self) -> bytes:
        with open(os.path.join(self.home, "machines.json"), "rb") as f:
            return f.read()

    def entry(self, name: str = "h1") -> JsonDict:
        data = self.obj(cast(JsonValue, json.loads(self.machines_raw())))
        return next(s for s in self.objs(data, "servers") if s["name"] == name)

    def calls(self) -> list[str]:
        try:
            with open(self.rpath(".fake-systemd", "calls.log")) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def main_pid(self) -> int:
        try:
            with open(self.rpath(".fake-systemd", "main.pid")) as f:
                pid = int(f.read())
        except (OSError, ValueError):
            return 0
        return pid if pid_alive(pid) else 0

    def snapshot(self) -> dict[str, object]:
        files = [
            os.path.join(self.home, "machines.json"),
            self.rpath(".config/systemd/user/taskmgr-remote.service"),
            self.rpath("taskmgr", hostsetup.VERSION_FILE),
            os.path.join(self.rroot(), "config.json"),
        ]
        snap: dict[str, object] = {}
        for p in files:
            with open(p, "rb") as f:
                snap[p] = (f.read(), os.stat(p).st_mtime_ns)
        snap["rhome"] = sorted(os.listdir(self.rhome))
        snap["pid"] = self.main_pid()
        return snap

    def write_old_job(self, where: str, state: str, key: str | None = None) -> str:
        """A protocol-1 job directory (attempts/ layout) in the server's remote_root."""
        jobid = secrets.token_hex(8)
        jd = os.path.join(self.rroot(), where, jobid)
        os.makedirs(os.path.join(jd, "attempts", "0001", "workdir"))
        with open(os.path.join(jd, "job.json"), "w") as f:
            json.dump(
                {
                    "schema_version": 1,
                    "jobid": jobid,
                    "task_id": "OL-01",
                    "state": state,
                    "attempts": [{"attempt": 1}],
                    "current_attempt": 1,
                },
                f,
            )
        if key is not None:
            os.makedirs(os.path.join(self.rroot(), "submission-keys"), exist_ok=True)
            with open(os.path.join(self.rroot(), "submission-keys", key + ".json"), "w") as f:
                json.dump({"submission_key": key, "jobid": jobid, "content_hash": "x"}, f)
        return jobid

    def run_job(self, script: str) -> str:
        return self.submit(self.make_pkg(script=script), host="h1")

    def wait_job(self, jobid: str) -> JsonValue:
        return self.ok(
            "status", jobid, "--host", "h1", "--wait", "--poll-interval", "0.3", "--timeout", "60"
        )

    # -- tests --------------------------------------------------------------------
    def test_fresh_host_then_second_run_changes_nothing(self) -> None:
        before_s1 = self.server("s1")
        rc, out, err = self.add("--yes")
        self.assertEqual(rc, 0, err)
        self.expect(
            out,
            err,
            code="changed",
            remote_root="changed",
            unit="changed",
            service="changed",
            linger="changed",
            machines="changed",
        )
        self.assertTrue(self.get(out, "complete"))
        self.assertIn("proceed? [y/N] y (--yes)", err)
        # local machine list: absolute paths, default ssh options; s1 untouched
        e = self.entry()
        self.assertEqual(e["remote_root"], self.rroot())
        self.assertEqual(e["remote_command"], self.rpath("taskmgr", "bin", "taskmgr-remote"))
        self.assertEqual((e["enabled"], e["transport"]), (True, "local"))
        self.assertEqual(self.get(e, "local", "home"), self.rhome)
        ssh = self.obj(e, "ssh")
        self.assertEqual((ssh["user"], ssh["host"]), ("tuner", "gpu.example"))
        opts = self.strs(ssh, "options")
        for o in hostsetup.DEFAULT_SSH_OPTIONS:
            self.assertIn(o, opts)
        self.assertEqual(opts[opts.index("ControlMaster=auto") - 1], "-o")
        self.assertEqual(self.entry("s1"), cast(JsonDict, cast(object, before_s1)))
        # server side
        with tempfile.TemporaryDirectory() as tmp:
            version = hostsetup.build_code(tmp)[0]
        with open(self.rpath("taskmgr", hostsetup.VERSION_FILE)) as f:
            self.assertEqual(f.read().strip(), version)
        self.assertTrue(os.access(self.rpath("taskmgr", "bin", "taskmgr-remote"), os.X_OK))
        self.assertFalse(os.path.exists(self.rpath("taskmgr", "taskmgr", "tests")))
        with open(self.rpath(".config/systemd/user/taskmgr-remote.service")) as f:
            unit = f.read()
        self.assertIn("KillMode=process\n", unit)
        self.assertRegex(
            unit,
            r"\nExecStart=\S*python\S* %s --root %s serve\n"
            % (re.escape(self.rpath("taskmgr", "bin", "taskmgr-remote")), re.escape(self.rroot())),
        )
        with open(os.path.join(self.rroot(), "config.json")) as f:
            self.assertEqual(f.read(), hostsetup.config_text())
        self.assertTrue(os.path.exists(self.rpath(".fake-systemd", "enabled")))
        self.assertTrue(os.path.exists(self.rpath(".fake-systemd", "linger")))
        self.assertTrue(self.main_pid())
        # the daemon started by the (fake) service runs jobs
        jobid = self.run_job('echo \'{"outputs": {"x": 1}}\' > "$TASKMGR_RESULT_FILE"')
        self.assertEqual(self.get(self.wait_job(jobid), "_taskmgr", "status"), "succeeded")

        # second run: nothing to do, no prompt, no TTY and no --yes needed
        snap, ncalls = self.snapshot(), len(self.calls())
        rc, out, err = self.add()
        self.assertEqual(rc, 0, err)
        self.expect(out, err)
        self.assertNotIn("[y/N]", err)
        self.assertIn("already up to date", self.detail(out, "code"))
        self.assertEqual(self.snapshot(), snap)
        changing = [c for c in self.calls()[ncalls:] if CHANGING.match(c)]
        self.assertEqual(changing, [])
        # an existing entry needs no flags at all
        rc, out, err = self.add(new=False)
        self.assertEqual(rc, 0, err)
        self.expect(out, err)
        self.assertEqual(self.snapshot(), snap)

    def test_code_update_restarts_service_and_keeps_running_task(self) -> None:
        rc, _, err = self.add("--yes")
        self.assertEqual(rc, 0, err)
        pid1 = self.main_pid()
        marker = os.path.join(self.tmp, "go")
        jobid = self.run_job(
            "while [ ! -e %s ]; do sleep 0.1; done; %s"
            % (marker, 'echo \'{"outputs": {}}\' > "$TASKMGR_RESULT_FILE"')
        )
        _ = self.wait_for(
            lambda: os.path.isdir(os.path.join(self.rroot(), "in-progress", jobid)), 30, "running"
        )
        for round_ in (1, 2):
            with open(self.rpath("taskmgr", hostsetup.VERSION_FILE), "w") as f:
                _ = f.write("0" * 64 + "\n")  # an older version is installed
            rc, out, err = self.add("--yes")
            self.assertEqual(rc, 0, err)
            self.expect(out, err, code="changed", service="changed")
            self.assertIn("restarted", self.detail(out, "service"))
            olds = [n for n in os.listdir(self.rhome) if n.startswith("taskmgr.old-")]
            self.assertEqual(len(olds), 1, olds)  # only the newest previous tree is kept
            self.assertFalse([n for n in os.listdir(self.rhome) if ".new-" in n])
            pid2 = self.main_pid()
            self.assertTrue(pid2 and pid2 != pid1, (round_, pid1, pid2))
            pid1 = pid2
            if round_ == 1:
                time.sleep(1.1)  # the next .old-<time> gets another name
        # KillMode=process: the task survived both restarts and finishes normally
        self.assertTrue(os.path.isdir(os.path.join(self.rroot(), "in-progress", jobid)))
        open(marker, "w").close()
        self.assertEqual(self.get(self.wait_job(jobid), "_taskmgr", "status"), "succeeded")

    def test_declined_restart_is_offered_again(self) -> None:
        rc, _, err = self.add("--yes")
        self.assertEqual(rc, 0, err)
        pid1 = self.main_pid()
        time.sleep(0.6)  # the stale check allows 0.5 s of clock error
        with open(self.rpath("taskmgr", hostsetup.VERSION_FILE), "w") as f:
            _ = f.write("old\n")
        rc, out, err = self.add(answers="y\nn\n")
        self.assertEqual(rc, 2, err)
        self.expect(
            out,
            err,
            code="changed",
            service="declined",
            linger="skipped",
            verify="skipped",
            machines="skipped",
        )
        self.assertEqual(self.main_pid(), pid1)
        # the code is current now, but the daemon still runs the old one: restart it
        rc, out, err = self.add("--yes")
        self.assertEqual(rc, 0, err)
        self.expect(out, err, service="changed")
        self.assertIn("restarted", self.detail(out, "service"))
        self.assertIn("started before the current code", err)
        self.assertNotEqual(self.main_pid(), pid1)

    def test_existing_config_is_never_overwritten(self) -> None:
        os.makedirs(self.rroot())
        cfg = os.path.join(self.rroot(), "config.json")
        with open(cfg, "w") as f:
            _ = f.write('{"max_cpu_jobs": 3}\n')
        rc, out, err = self.add("--yes")
        self.assertEqual(rc, 0, err)
        self.expect(
            out,
            err,
            code="changed",
            unit="changed",
            service="changed",
            linger="changed",
            machines="changed",
        )
        with open(cfg) as f:
            self.assertEqual(f.read(), '{"max_cpu_jobs": 3}\n')

    def test_declined_prompt_stops_and_changes_nothing(self) -> None:
        machines = self.machines_raw()
        rc, out, err = self.add(answers="n\n")
        self.assertEqual(rc, 2, err)
        skipped = {s: "skipped" for s in ALL_STEPS[2:]}
        self.expect(out, err, code="declined", **skipped)
        self.assertFalse(self.get(out, "complete"))
        self.assertIn("not run: step code declined", self.detail(out, "machines"))
        self.assertEqual(sorted(os.listdir(self.rhome)), [".fake-systemd"])
        self.assertEqual(self.machines_raw(), machines)
        # yes to the code, no to remote_root: the uploaded copy is removed again
        rc, out, err = self.add(answers="y\nn\n")
        self.assertEqual(rc, 2, err)
        skipped = {s: "skipped" for s in ALL_STEPS[3:]}
        self.expect(out, err, code="skipped", remote_root="declined", **skipped)
        self.assertIn("uploaded but not installed", self.detail(out, "code"))
        self.assertEqual(sorted(os.listdir(self.rhome)), [".fake-systemd"])
        self.assertEqual(self.machines_raw(), machines)
        changing = [c for c in self.calls() if CHANGING.match(c)]
        self.assertEqual(changing, [])

    def test_non_interactive_without_yes_changes_nothing(self) -> None:
        machines = self.machines_raw()
        rc, out, err = self.add()
        self.assertEqual(rc, 2, err)
        skipped = {s: "skipped" for s in ALL_STEPS[2:]}
        self.expect(out, err, code="failed", **skipped)
        self.assertIn("pass --yes", self.detail(out, "code"))
        self.assertEqual(sorted(os.listdir(self.rhome)), [".fake-systemd"])
        self.assertEqual(self.machines_raw(), machines)

    def test_incompatible_job_dirs_are_moved_to_a_backup(self) -> None:
        rc, _, err = self.add("--yes")
        self.assertEqual(rc, 0, err)
        good = self.run_job('echo \'{"outputs": {}}\' > "$TASKMGR_RESULT_FILE"')
        _ = self.wait_job(good)
        # (only in done/: the running daemon of this test would trip over a queued one,
        # which is why add host stops the service before moving)
        old = self.write_old_job("done", "succeeded", key="old-key")
        pid1 = self.main_pid()
        rc, out, err = self.add("--yes")
        self.assertEqual(rc, 0, err)
        self.expect(out, err, data="changed", service="changed")
        self.assertIn("stop the taskmgr-remote service", err)
        self.assertIn("KillMode=process", err)
        backups = [n for n in os.listdir(self.rhome) if n.startswith("taskmgr-root.backup-")]
        self.assertEqual(len(backups), 1)
        b = self.rpath(backups[0])
        self.assertTrue(os.path.isdir(os.path.join(b, "done", old, "attempts")))
        self.assertTrue(os.path.isfile(os.path.join(b, "submission-keys", "old-key.json")))
        self.assertFalse(os.path.exists(os.path.join(self.rroot(), "done", old)))
        self.assertTrue(os.path.isdir(os.path.join(self.rroot(), "done", good)))
        self.assertNotEqual(self.main_pid(), pid1)  # stopped for the move, started again
        rc, out, err = self.add("--yes")
        self.assertEqual(rc, 0, err)
        self.expect(out, err)

    def test_live_incompatible_job_is_refused(self) -> None:
        os.makedirs(self.rroot())
        with open(os.path.join(self.rroot(), "config.json"), "w") as f:
            _ = f.write("{}\n")
        live = self.write_old_job("in-progress", "running")
        machines = self.machines_raw()
        rc, out, err = self.add("--yes")
        self.assertEqual(rc, 2, err)
        skipped = {s: "skipped" for s in ALL_STEPS[4:]}
        self.expect(out, err, code="skipped", data="failed", **skipped)
        self.assertIn("may still be running", self.detail(out, "data"))
        self.assertIn("in-progress/" + live, self.detail(out, "data"))
        self.assertTrue(os.path.isdir(os.path.join(self.rroot(), "in-progress", live)))
        self.assertEqual(sorted(os.listdir(self.rhome)), [".fake-systemd", "taskmgr-root"])
        self.assertEqual(self.machines_raw(), machines)

    def test_linger_refused_is_reported_and_the_rest_is_done(self) -> None:
        os.makedirs(self.rpath(".fake-systemd"))
        open(self.rpath(".fake-systemd", "linger-refuse"), "w").close()
        rc, out, err = self.add("--yes")
        self.assertEqual(rc, 2, err)
        self.expect(
            out,
            err,
            code="changed",
            remote_root="changed",
            unit="changed",
            service="changed",
            linger="failed",
            machines="changed",
        )
        self.assertIn("an admin must run `sudo loginctl enable-linger", self.detail(out, "linger"))
        self.assertEqual(self.entry()["remote_root"], self.rroot())

    def test_unreachable_host_fails_at_the_first_step(self) -> None:
        self.server("s1")["local"] = {"fail": "ssh: connect to host s1: Connection timed out"}
        self.write_machines()
        rc, out, err = self.add("--yes", name="s1", new=False)
        self.assertEqual(rc, 2, err)
        self.expect(out, err, ssh="failed", **{s: "skipped" for s in ALL_STEPS[1:]})
        self.assertIn("timed out", self.detail(out, "ssh"))

    def test_bad_arguments(self) -> None:
        rc, obj, _ = self.task("add", "host", "new-host")  # a new host needs --ssh
        self.assertEqual(rc, 2)
        self.assertEqual(self.get(obj, "error", "code"), "bad_args")
        rc, obj, _ = self.task("add", "host", "bad/name", "--ssh", "x@y")
        self.assertEqual(rc, 2)
        self.assertEqual(self.get(obj, "error", "code"), "bad_args")


class AddHostUnitTest(unittest.TestCase):
    """Pure functions; no subprocess except the read-only `inspect`."""

    def test_wrapper_equals_repository_file(self) -> None:
        with open(os.path.join(REPO, "bin", "taskmgr-remote")) as f:
            self.assertEqual(f.read(), hostsetup.WRAPPER)

    def test_unit_matches_the_shipped_template(self) -> None:
        def directives(text: str) -> list[str]:
            return [
                line
                for line in text.splitlines()
                if line and not line.startswith(("#", "ExecStart="))
            ]

        with open(os.path.join(REPO, "taskmgr", "deploy", "taskmgr-remote.user.service")) as f:
            template = f.read()
        unit = hostsetup.unit_text("/usr/bin/python3", "/home/t", "/scratch/t root/%x")
        self.assertEqual(directives(unit), directives(template))
        self.assertIn(
            '\nExecStart=/usr/bin/python3 /home/t/taskmgr/bin/taskmgr-remote --root "/scratch/t root/%%x" serve\n',
            unit,
        )

    def test_ssh_options_and_shell_argv(self) -> None:
        opts = hostsetup.merge_ssh_options(
            ["-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15"],
            ["connecttimeout=3", "StrictHostKeyChecking=accept-new"],
        )
        self.assertEqual(
            opts,
            [
                "-o",
                "ServerAliveInterval=15",
                "-o",
                "connecttimeout=3",
                "-o",
                "StrictHostKeyChecking=accept-new",
            ],
        )
        self.assertEqual(hostsetup.parse_destination("tuner@10.0.0.1"), ("tuner", "10.0.0.1"))
        self.assertEqual(hostsetup.parse_destination("gpu1"), (None, "gpu1"))
        server: Server = {
            "name": "g",
            "remote_root": "/r",
            "remote_command": "/c",
            "ssh": {"host": "h", "user": "u", "port": 2222, "options": opts},
        }
        argv = Remote(server).shell_argv("echo 'a b'")
        self.assertEqual(argv[:3], ["ssh", "-o", "BatchMode=yes"])
        self.assertEqual(argv[-3:], ["u@h", "--", "sh -c 'echo '\"'\"'a b'\"'\"''"])
        self.assertIn("StrictHostKeyChecking=accept-new", argv)

    def test_inspect_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "missing-root")
            p = subprocess.run(
                [sys.executable, REMOTE, "--root", root, "--protocol-version", "2", "inspect"],
                stdout=subprocess.PIPE,
                timeout=60,
            )
            self.assertEqual(p.returncode, 0)
            reply = cast(JsonDict, json.loads(p.stdout))
            self.assertEqual((reply["exists"], reply["incompatible"]), (False, []))
            self.assertFalse(os.path.exists(root))
            shutil.rmtree(tmp, ignore_errors=True)
