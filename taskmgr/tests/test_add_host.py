"""`task host add|list|remove|ping` (desc-2 D18).

`host add` sets up and updates a server, step by step and idempotent. The "server" is a
local-transport machine entry whose HOME is a directory of the test (test-only
`"local": {"home": ...}`, created through TASKMGR_TEST_LOCAL_HOME). Fake
`systemctl` and `loginctl` scripts on PATH stand in for systemd: they keep their state under
$HOME/.fake-systemd, log every call, and `start` really launches the unit's ExecStart (the
daemon), so jobs run end to end. `stop` signals only the main PID, like KillMode=process.
"""

import fcntl
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
import zipfile
from typing import cast

from taskmgr import hostlegacy, hostsetup, pyz
from taskmgr.common import DEFAULT_REMOTE_CONFIG, override
from taskmgr.records import JsonDict, JsonValue, Server
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
    "old_code",
)


class AddHostTest(Env):
    rhome: str = ""
    dist: str = ""  # taskmgr-remote.pyz built once for the class, uploaded by `host add`
    remote_pyz: str = ""

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.dist = tempfile.mkdtemp(prefix="taskmgr-test-dist-")
        _ = pyz.build(cls.dist)
        cls.remote_pyz = os.path.join(cls.dist, pyz.REMOTE_PYZ)

    @classmethod
    @override
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.dist, ignore_errors=True)
        super().tearDownClass()

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
        self,
        *extra: str,
        answers: str | None = None,
        name: str = "h1",
        new: bool = True,
        archive: str | None = None,
        cmd: str = "add",
    ) -> tuple[int, JsonDict, str]:
        """Run `task host <cmd> --detail` (add, or update); answers != None makes stdin an
        "interactive" pipe.

        Uploads the class's prebuilt taskmgr-remote.pyz unless `archive` names another file.
        """
        args = (
            ["host", cmd, name, "--detail", "--plain"]
            + (["--ssh", "tuner@gpu.example"] if new and cmd == "add" else [])
            + ["--pyz", archive or self.remote_pyz]
            + list(extra)
        )
        env = dict(self.env)
        if answers is not None:
            env["TASKMGR_TEST_INTERACTIVE"] = "1"
        p = subprocess.run(
            [sys.executable, TASK] + args,
            env=env,
            input=(answers or "").encode(),
            capture_output=True,
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

    def rcode(self) -> str:
        """The installed archive, <remote_root>/taskmgr-remote.pyz on the server."""
        return os.path.join(self.rroot(), hostsetup.CODE_FILE)

    def installed_version(self) -> str:
        with zipfile.ZipFile(self.rcode()) as z:
            return z.read(pyz.VERSION_FILE).decode().strip()

    def store(self) -> list[str]:
        return sorted(os.listdir(os.path.join(self.rroot(), hostsetup.CODE_STORE)))

    def set_installed_version(self, version: str) -> None:
        """Install, like `host add` does, a copy of the archive that reports another version."""
        with zipfile.ZipFile(self.rcode()) as z:
            members = [(i, z.read(i)) for i in z.infolist()]
        name = version[:16] + ".pyz"
        fake = os.path.join(self.rroot(), hostsetup.CODE_STORE, name)
        with open(fake, "wb") as f:
            _ = f.write(b"#!/usr/bin/env python3\n")
            with zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED) as z:
                for info, data in members:
                    if info.filename == pyz.VERSION_FILE:
                        data = (version + "\n").encode()
                    z.writestr(info, data)
        os.chmod(fake, 0o755)
        os.symlink(os.path.join(hostsetup.CODE_STORE, name), self.rcode() + ".tmp")
        os.replace(self.rcode() + ".tmp", self.rcode())

    def rroot(self) -> str:
        return self.rpath(".taskmgr-remote")

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
            self.rcode(),
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
        self.assertEqual(e["remote_command"], self.rcode())
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
        version = pyz.code_version()
        self.assertEqual(self.installed_version(), version)
        self.assertTrue(os.access(self.rcode(), os.X_OK))
        with zipfile.ZipFile(self.rcode()) as z:
            self.assertFalse([n for n in z.namelist() if "/tests/" in n])
        # everything of taskmgr is in remote_root (plus the unit under ~/.config)
        self.assertEqual(
            sorted(os.listdir(self.rhome)), [".config", ".fake-systemd", ".taskmgr-remote"]
        )
        top = os.listdir(self.rroot())
        for name in ("config.json", hostsetup.CODE_FILE, hostsetup.CODE_STORE, "locks", "queue"):
            self.assertIn(name, top)
        self.assertEqual(
            os.readlink(self.rcode()), "%s/%s.pyz" % (hostsetup.CODE_STORE, version[:16])
        )
        self.assertEqual(self.store(), [version[:16] + ".pyz"])
        with open(self.rpath(".config/systemd/user/taskmgr-remote.service")) as f:
            unit = f.read()
        self.assertIn("KillMode=process\n", unit)
        self.assertRegex(
            unit,
            r"\nExecStart=\S*python\S* %s --root %s serve\n"
            % (re.escape(self.rcode()), re.escape(self.rroot())),
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
        current_version = self.installed_version()
        current = current_version[:16] + ".pyz"
        for round_ in (1, 2):
            older = str(round_) * 64
            self.set_installed_version(older)  # an older version is installed
            rc, out, err = self.add("--yes")
            self.assertEqual(rc, 0, err)
            self.expect(out, err, code="changed", service="changed")
            self.assertIn("restarted", self.detail(out, "service"))
            self.assertIn("previous archive kept", self.detail(out, "code"))
            # the current archive and the one the link pointed to before; nothing else
            self.assertEqual(self.store(), sorted([current, older[:16] + ".pyz"]))
            self.assertEqual(self.installed_version(), current_version)
            self.assertFalse([n for n in os.listdir(self.rhome) if ".new-" in n])
            pid2 = self.main_pid()
            self.assertTrue(pid2 and pid2 != pid1, (round_, pid1, pid2))
            pid1 = pid2
        # KillMode=process: the task survived both restarts and finishes normally
        self.assertTrue(os.path.isdir(os.path.join(self.rroot(), "in-progress", jobid)))
        open(marker, "w").close()
        self.assertEqual(self.get(self.wait_job(jobid), "_taskmgr", "status"), "succeeded")

    def test_declined_restart_is_offered_again(self) -> None:
        rc, _, err = self.add("--yes")
        self.assertEqual(rc, 0, err)
        pid1 = self.main_pid()
        time.sleep(0.6)  # the stale check allows 0.5 s of clock error
        self.set_installed_version("old")
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
            old_code="skipped",
        )
        self.assertEqual(self.main_pid(), pid1)
        # the code is current now, but the daemon still runs the old one: restart it
        rc, out, err = self.add("--yes")
        self.assertEqual(rc, 0, err)
        self.expect(out, err, service="changed")
        self.assertIn("restarted", self.detail(out, "service"))
        self.assertIn("started before the current code", err)
        self.assertNotEqual(self.main_pid(), pid1)

    def test_old_code_tree_is_replaced_by_the_archive(self) -> None:
        """A server set up by a version before the zipapp: ~/taskmgr (a tree with
        bin/taskmgr-remote) and ~/taskmgr.old-*. After the archive runs, they are deleted."""
        legacy = self.rpath("taskmgr")
        os.makedirs(os.path.join(legacy, "taskmgr"))
        for rel in ("taskmgr/remote.py", hostlegacy.LEGACY_MARKER):
            with open(os.path.join(legacy, rel), "w") as f:
                _ = f.write("old\n")
        os.makedirs(self.rpath("taskmgr.old-20260101T000000Z", "taskmgr"))
        rc, out, err = self.add(answers="y\n" * 7 + "n\n")  # 7 changes before old_code, then no
        self.assertEqual(rc, 2, err)
        self.assertEqual(self.statuses(out)["old_code"], "declined")
        self.assertIn("delete %s" % legacy, err)
        self.assertTrue(os.path.isdir(legacy))
        # --yes does not delete: without a terminal the old code is kept (the run is complete).
        rc, out, err = self.add("--yes", new=False)
        self.assertEqual(rc, 0, err)
        self.expect(out, err, old_code="kept")
        self.assertIn("--prune-old-code", self.detail(out, "old_code"))
        self.assertTrue(os.path.isdir(legacy))
        rc, text, _ = self.task("host", "add", "h1", "--yes", "--pyz", self.remote_pyz, text=True)
        self.assertEqual(rc, 0)
        assert isinstance(text, str)
        self.assertIn("--prune-old-code", text)
        # In a terminal --yes still asks for the deletion; "n" keeps it.
        rc, out, err = self.add("--yes", new=False, answers="n\n")
        self.assertEqual(rc, 2, err)
        self.expect(out, err, old_code="declined")
        self.assertIn("--yes does not answer it", err)
        self.assertTrue(os.path.isdir(legacy))
        # --plan lists it and deletes nothing.
        rc, out, err = self.add("--plan", new=False)
        self.assertEqual(rc, 1, err)
        self.expect(out, err, old_code="planned")
        self.assertTrue(os.path.isdir(legacy))
        rc, out, err = self.add("--yes", "--prune-old-code", new=False)
        self.assertEqual(rc, 0, err)
        self.expect(out, err, old_code="changed")
        self.assertFalse(os.path.exists(legacy))
        self.assertFalse([n for n in os.listdir(self.rhome) if n.startswith("taskmgr.old-")])
        jobid = self.run_job('echo \'{"outputs": {}}\' > "$TASKMGR_RESULT_FILE"')
        self.assertEqual(self.get(self.wait_job(jobid), "_taskmgr", "status"), "succeeded")

    def test_first_zipapp_layout_is_cleaned_up(self) -> None:
        """A server set up by the first zipapp layout: ~/taskmgr-remote.pyz ->
        .taskmgr-remote/<version>.pyz. After the new code runs, both are deleted; the
        versions/ store in remote_root stays."""
        with open(self.remote_pyz, "rb") as f:
            data = f.read()
        old = "0123456789abcdef.pyz"
        os.makedirs(self.rpath(".taskmgr-remote"))
        with open(self.rpath(".taskmgr-remote", old), "wb") as f:
            _ = f.write(data)
        os.symlink(".taskmgr-remote/" + old, self.rpath(hostlegacy.OLD_CODE_LINK))
        rc, out, err = self.add("--yes", "--prune-old-code")
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
            old_code="changed",
        )
        self.assertFalse(os.path.lexists(self.rpath(hostlegacy.OLD_CODE_LINK)))
        self.assertFalse(os.path.exists(self.rpath(".taskmgr-remote", old)))
        self.assertEqual(self.installed_version(), pyz.code_version())
        rc, out, err = self.add("--yes", new=False)
        self.assertEqual(rc, 0, err)
        self.expect(out, err)

    def test_plan_changes_nothing(self) -> None:
        """--plan runs every check and reports each change as planned; nothing changes on
        the server or in the machine list (also no setup lock and no systemctl change)."""
        machines = self.machines_raw()
        rc, out, err = self.add("--plan")
        self.assertEqual(rc, 1, err)
        self.expect(
            out,
            err,
            code="planned",
            remote_root="planned",
            data="skipped",
            unit="planned",
            service="planned",
            linger="planned",
            verify="skipped",
            machines="planned",
        )
        self.assertIs(self.get(out, "plan"), True)
        self.assertEqual(
            self.strs(out, "planned"),
            ["code", "remote_root", "unit", "service", "linger", "machines"],
        )
        self.assertIn("upload", self.detail(out, "code"))
        self.assertIn("enable", self.detail(out, "service"))
        self.assertNotIn("[y/N]", err)
        self.assertEqual(sorted(os.listdir(self.rhome)), [".fake-systemd"])
        self.assertEqual(self.machines_raw(), machines)
        self.assertEqual([c for c in self.calls() if CHANGING.match(c)], [])
        self.assertNotIn("holding the setup lock", err)
        # Set up, then a plan has nothing to do (exit 0).
        rc, _, err = self.add("--yes")
        self.assertEqual(rc, 0, err)
        snap, ncalls = self.snapshot(), len(self.calls())
        rc, out, err = self.add("--plan", new=False)
        self.assertEqual(rc, 0, err)
        self.expect(out, err)
        self.assertEqual(self.strs(out, "planned"), [])
        rc, text, _ = self.task("host", "add", "h1", "--plan", "--pyz", self.remote_pyz, text=True)
        self.assertEqual(rc, 0)
        assert isinstance(text, str)
        self.assertIn("plan: nothing to change on host h1", text)
        self.assertEqual(self.snapshot(), snap)
        # An older version installed: the plan names the upload and the restart.
        self.set_installed_version("1" * 64)
        snap = self.snapshot()
        rc, out, err = self.add("--plan", new=False)
        self.assertEqual(rc, 1, err)
        self.expect(out, err, code="planned", data="skipped", service="planned", verify="skipped")
        self.assertIn("restart", self.detail(out, "service"))
        self.assertEqual(self.snapshot(), snap)
        self.assertEqual([c for c in self.calls()[ncalls:] if CHANGING.match(c)], [])

    def test_old_remote_root_is_reported(self) -> None:
        """Data under ~/taskmgr-root (the old default) is not moved. Setting up another
        remote_root next to it needs a confirmation by hand (--yes refuses);
        TASKMGR_REMOTE_ROOT keeps using it."""
        old = self.rpath("taskmgr-root")
        os.makedirs(old)
        with open(os.path.join(old, "config.json"), "w") as f:
            _ = f.write("{}\n")
        machines = self.machines_raw()
        rc, out, err = self.add("--yes")
        self.assertEqual(rc, 2, err)
        self.expect(out, err, ssh="failed", **{s: "skipped" for s in ALL_STEPS[1:]})
        detail = self.detail(out, "ssh")
        self.assertIn("%s already holds taskmgr data" % old, detail)
        self.assertIn("TASKMGR_REMOTE_ROOT=%s" % old, detail)
        self.assertIn("run without --yes and confirm", detail)
        self.assertEqual(sorted(os.listdir(self.rhome)), [".fake-systemd", "taskmgr-root"])
        self.assertEqual(self.machines_raw(), machines)
        rc, out, err = self.add(answers="n\n")
        self.assertEqual(rc, 2, err)
        self.assertEqual(self.statuses(out)["ssh"], "declined")
        self.assertIn("TASKMGR_REMOTE_ROOT=%s" % old, err)
        self.env[hostsetup.REMOTE_ROOT_ENV] = "~/taskmgr-root"
        rc, _, err = self.add("--yes")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("already holds taskmgr data", err)
        self.assertEqual(self.entry()["remote_root"], self.rpath("taskmgr-root"))
        self.assertTrue(os.path.islink(self.rpath("taskmgr-root", hostsetup.CODE_FILE)))
        self.assertFalse(os.path.exists(self.rroot()))

    def test_unrelated_taskmgr_directory_is_left_alone(self) -> None:
        """Only a tree with the old installer's marker is old code; a checkout of taskmgr
        (taskmgr/remote.py, even with a marker, but with .git) is the user's."""
        mine = self.rpath("taskmgr", "taskmgr", "remote.py")
        os.makedirs(os.path.dirname(mine))
        open(mine, "w").close()
        rc, out, err = self.add("--yes")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.statuses(out)["old_code"], "ok")
        self.assertTrue(os.path.exists(mine))
        for tree in (self.rpath("taskmgr"), self.rpath("taskmgr.old-20260101T000000Z")):
            os.makedirs(os.path.join(tree, ".git"), exist_ok=True)
            open(os.path.join(tree, hostlegacy.LEGACY_MARKER), "w").close()
        rc, out, err = self.add("--yes", new=False)
        self.assertEqual(rc, 0, err)
        self.expect(out, err)
        self.assertTrue(os.path.exists(mine))
        self.assertTrue(os.path.isdir(self.rpath("taskmgr.old-20260101T000000Z", ".git")))

    def test_switching_remote_root_needs_a_confirmation_by_hand(self) -> None:
        """TASKMGR_REMOTE_ROOT other than the entry's (which holds data): --yes refuses and
        changes nothing; confirmed by hand, the entry and the unit use the new one."""
        rc, _, err = self.add("--yes")
        self.assertEqual(rc, 0, err)
        unit = self.rpath(".config/systemd/user/taskmgr-remote.service")
        with open(unit) as f:
            before = f.read()
        pid = self.main_pid()
        self.env[hostsetup.REMOTE_ROOT_ENV] = "~/elsewhere"
        rc, out, err = self.add("--yes", new=False)
        self.assertEqual(rc, 2, err)
        self.expect(out, err, ssh="failed", **{s: "skipped" for s in ALL_STEPS[1:]})
        self.assertIn("%s already holds taskmgr data" % self.rroot(), self.detail(out, "ssh"))
        with open(unit) as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(self.entry()["remote_root"], self.rroot())
        self.assertFalse(os.path.exists(self.rpath("elsewhere")))
        self.assertEqual(self.main_pid(), pid)
        rc, out, err = self.add(new=False, answers="y\n" * 20)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.entry()["remote_root"], self.rpath("elsewhere"))
        self.assertTrue(os.path.isfile(self.rpath("elsewhere", "config.json")))
        self.assertTrue(os.path.isfile(os.path.join(self.rroot(), "config.json")))  # kept

    def test_archive_of_the_running_daemon_is_kept(self) -> None:
        """Old archives are removed only once the daemon runs the new code: one that the
        running daemon was started from stays while a restart is declined."""
        rc, _, err = self.add("--yes")
        self.assertEqual(rc, 0, err)
        current = self.installed_version()[:16] + ".pyz"
        a, b, c = ("a" * 64, "b" * 64, "c" * 64)
        self.set_installed_version(a)
        p = subprocess.run(
            ["systemctl", "--user", "restart", "taskmgr-remote"],
            env=dict(self.env, HOME=self.rhome),
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(p.returncode, 0, p.stderr)  # the daemon runs archive a now
        self.set_installed_version(b)
        rc, out, err = self.add(answers="y\nn\n")  # install the current code; no restart
        self.assertEqual(rc, 2, err)
        self.assertEqual(self.statuses(out)["service"], "declined")
        self.assertEqual(self.store(), sorted([current, a[:16] + ".pyz", b[:16] + ".pyz"]))
        self.set_installed_version(c)
        rc, out, err = self.add("--yes", new=False)
        self.assertEqual(rc, 0, err)
        self.assertIn("removed older archives", self.detail(out, "code"))
        self.assertEqual(self.store(), sorted([current, c[:16] + ".pyz"]))

    def test_interrupted_uploads_are_removed_only_with_a_confirmed_update(self) -> None:
        """Review P1-7: removing old uploads is a change: an up-to-date server is left as it
        is (and nothing is asked); otherwise the removal is part of the confirmed update."""
        rc, _, err = self.add("--yes")
        self.assertEqual(rc, 0, err)
        current_version = self.installed_version()
        store = os.path.join(self.rroot(), hostsetup.CODE_STORE)
        old = os.path.join(store, "0123456789abcdef.pyz.new-abc123")
        fresh = os.path.join(store, "0123456789abcdef.pyz.new-def456")
        for path in (old, fresh):
            open(path, "w").close()
        hour_ago = time.time() - 3600
        os.utime(old, (hour_ago, hour_ago))
        for flags in ((), ("--yes",)):  # without --yes stdin is not a terminal: no question
            rc, out, err = self.add(*flags, new=False)
            self.assertEqual(rc, 0, err)
            self.expect(out, err)
            self.assertIn("interrupted upload", self.detail(out, "code"))
            self.assertNotIn("proceed?", err)
            self.assertTrue(os.path.exists(old))
        self.set_installed_version("1" * 64)
        rc, out, err = self.add(new=False, answers="n\n")
        self.assertEqual(rc, 2, err)
        self.assertEqual(self.statuses(out)["code"], "declined")
        self.assertIn("0123456789abcdef.pyz.new-abc123", err)  # named in the question
        self.assertTrue(os.path.exists(old))
        rc, out, err = self.add("--yes", new=False)
        self.assertEqual(rc, 0, err)
        self.assertIn("removed interrupted uploads", self.detail(out, "code"))
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))  # may be an upload in progress
        self.assertEqual(self.installed_version(), current_version)

    def test_leftover_link_new_does_not_block_the_switch(self) -> None:
        """Review P2-5: a run cut off between `ln -s` and `mv -f` leaves .link-new behind."""
        rc, _, err = self.add("--yes")
        self.assertEqual(rc, 0, err)
        current_version = self.installed_version()
        self.set_installed_version("2" * 64)
        os.symlink("nowhere", self.rcode() + ".link-new")
        rc, out, err = self.add("--yes", new=False)
        self.expect(out, err, code="changed", service="changed")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.installed_version(), current_version)
        self.assertFalse(os.path.lexists(self.rcode() + ".link-new"))

    def test_a_second_run_does_not_interleave(self) -> None:
        """While another run holds the server's setup lock, a run stops after step ssh."""
        run_dir = os.path.join(self.tmp, "run")
        os.makedirs(run_dir)
        self.env["XDG_RUNTIME_DIR"] = run_dir
        path = os.path.join(run_dir, "taskmgr-hostsetup-%d.lock" % os.getuid())
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _ = os.write(fd, b"4242")
            rc, out, err = self.add("--yes")
            self.assertEqual(rc, 2, err)
            self.expect(out, err, ssh="failed", **{s: "skipped" for s in ALL_STEPS[1:]})
            self.assertIn("another `task host add`", self.detail(out, "ssh"))
            self.assertIn("pid 4242", self.detail(out, "ssh"))
            self.assertEqual(sorted(os.listdir(self.rhome)), [".fake-systemd"])
        finally:
            os.close(fd)
        rc, _, err = self.add("--yes")
        self.assertEqual(rc, 0, err)

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
        # which is why host add stops the service before moving)
        old = self.write_old_job("done", "succeeded", key="old-key")
        pid1 = self.main_pid()
        rc, out, err = self.add("--yes")
        self.assertEqual(rc, 0, err)
        self.expect(out, err, data="changed", service="changed")
        self.assertIn("stop the taskmgr-remote service", err)
        self.assertIn("KillMode=process", err)
        backups = os.listdir(os.path.join(self.rroot(), hostsetup.BACKUP_DIR))
        self.assertEqual(len(backups), 1)
        b = os.path.join(self.rroot(), hostsetup.BACKUP_DIR, backups[0])
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
        self.assertEqual(sorted(os.listdir(self.rhome)), [".fake-systemd", ".taskmgr-remote"])
        self.assertNotIn(hostsetup.CODE_STORE, os.listdir(self.rroot()))  # upload removed
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

    def test_remote_root_from_the_environment(self) -> None:
        """TASKMGR_REMOTE_ROOT sets remote_root (`~` is the server's HOME); a later run
        without it keeps the stored one."""
        self.env[hostsetup.REMOTE_ROOT_ENV] = "~/scratch/taskmgr-root"
        rc, out, err = self.add("--yes")
        self.assertEqual(rc, 0, err)
        root = self.rpath("scratch", "taskmgr-root")
        self.assertEqual(self.entry()["remote_root"], root)
        self.assertTrue(os.path.isfile(os.path.join(root, "config.json")))
        self.assertFalse(os.path.exists(self.rroot()))
        del self.env[hostsetup.REMOTE_ROOT_ENV]
        rc, out, err = self.add(new=False)
        self.assertEqual(rc, 0, err)
        self.expect(out, err)
        self.assertEqual(self.entry()["remote_root"], root)
        # --remote-root is gone
        rc, _, _ = self.task("host", "add", "h1", "--remote-root", "/x")
        self.assertEqual(rc, 2)

    def test_missing_archive_fails_and_changes_nothing(self) -> None:
        missing = os.path.join(self.tmp, "nope", pyz.REMOTE_PYZ)
        before = self.machines_raw()
        rc, out, err = self.add("--yes", archive=missing)
        self.assertEqual(rc, 2, err)
        st = self.statuses(out)
        self.assertEqual((st["ssh"], st["code"]), ("ok", "failed"))
        self.assertEqual({st[s] for s in ALL_STEPS[2:]}, {"skipped"})
        self.assertIn("%s not found" % missing, self.detail(out, "code"))
        self.assertIn("python3 -m taskmgr.pyz -o dist", self.detail(out, "code"))
        self.assertFalse(os.path.lexists(self.rcode()))
        self.assertEqual(self.machines_raw(), before)

    def test_archive_of_another_version_is_refused(self) -> None:
        """`host add` never builds: an archive whose version differs from this task's
        must be rebuilt by the user."""
        stale = os.path.join(self.tmp, "stale.pyz")
        with zipfile.ZipFile(self.remote_pyz) as z:
            members = [(i, z.read(i)) for i in z.infolist()]
        with open(stale, "wb") as f:
            _ = f.write(b"#!/usr/bin/env python3\n")
            with zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED) as z:
                for info, data in members:
                    if info.filename == pyz.VERSION_FILE:
                        data = b"0" * 64 + b"\n"
                    z.writestr(info, data)
        rc, out, err = self.add("--yes", archive=stale)
        self.assertEqual(rc, 2, err)
        self.assertEqual(self.statuses(out)["code"], "failed")
        detail = self.detail(out, "code")
        self.assertIn("has code version 000000000000", detail)
        self.assertIn("rebuild it", detail)
        self.assertFalse(os.path.lexists(self.rcode()))
        # task.pyz is not a taskmgr-remote.pyz
        rc, out, err = self.add("--yes", archive=os.path.join(self.dist, pyz.LOCAL_PYZ))
        self.assertEqual(rc, 2, err)
        self.assertIn("wrong entry point", self.detail(out, "code"))

    def test_unreachable_host_fails_at_the_first_step(self) -> None:
        self.server("s1")["local"] = {"fail": "ssh: connect to host s1: Connection timed out"}
        self.write_machines()
        rc, out, err = self.add("--yes", name="s1", new=False)
        self.assertEqual(rc, 2, err)
        self.expect(out, err, ssh="failed", **{s: "skipped" for s in ALL_STEPS[1:]})
        self.assertIn("timed out", self.detail(out, "ssh"))

    def test_bad_arguments(self) -> None:
        rc, obj, _ = self.task("host", "add", "new-host")  # a new host needs --ssh
        self.assertEqual(rc, 2)
        self.assertEqual(self.get(obj, "error", "code"), "bad_args")
        rc, obj, _ = self.task("host", "add", "bad/name", "--ssh", "x@y")
        self.assertEqual(rc, 2)
        self.assertEqual(self.get(obj, "error", "code"), "bad_args")

    # -- host update ------------------------------------------------------------------
    def test_update_installs_this_version_and_keeps_the_entry(self) -> None:
        rc, _, err = self.add("--yes")
        self.assertEqual(rc, 0, err)
        entry, pid1 = self.entry(), self.main_pid()
        current = self.installed_version()
        self.set_installed_version("1" * 64)  # the server runs older code
        rc, out, err = self.add("--yes", cmd="update")
        self.assertEqual(rc, 0, err)
        self.expect(out, err, code="changed", service="changed")
        self.assertIn("task host update h1:", err)
        self.assertIn("restarted", self.detail(out, "service"))
        self.assertEqual(self.installed_version(), current)
        pid2 = self.main_pid()
        self.assertTrue(pid2 and pid2 != pid1, (pid1, pid2))
        self.assertEqual(self.entry(), entry)  # the machine entry is not touched
        # up to date: nothing changes and nothing is asked
        snap, ncalls = self.snapshot(), len(self.calls())
        rc, out, err = self.add(cmd="update")
        self.assertEqual(rc, 0, err)
        self.expect(out, err)
        self.assertNotIn("[y/N]", err)
        self.assertIn("already up to date", self.detail(out, "code"))
        self.assertEqual(self.snapshot(), snap)
        self.assertEqual([c for c in self.calls()[ncalls:] if CHANGING.match(c)], [])
        rc, text, _ = self.task("host", "update", "h1", "--pyz", self.remote_pyz, text=True)
        self.assertEqual(rc, 0)
        assert isinstance(text, str)
        self.assertIn("host h1 is set up", text)

    def test_update_plan_and_declined_change_nothing(self) -> None:
        rc, _, err = self.add("--yes")
        self.assertEqual(rc, 0, err)
        self.set_installed_version("1" * 64)
        snap, ncalls = self.snapshot(), len(self.calls())
        rc, out, err = self.add("--plan", cmd="update")
        self.assertEqual(rc, 1, err)
        self.expect(out, err, code="planned", data="skipped", service="planned", verify="skipped")
        self.assertEqual(self.snapshot(), snap)
        rc, out, err = self.add(cmd="update", answers="n\n")
        self.assertEqual(rc, 2, err)
        self.assertEqual(self.statuses(out)["code"], "declined")
        self.assertEqual(self.snapshot(), snap)
        rc, out, err = self.add(cmd="update")  # no TTY and no --yes
        self.assertEqual(rc, 2, err)
        self.assertEqual(self.snapshot(), snap)
        self.assertEqual([c for c in self.calls()[ncalls:] if CHANGING.match(c)], [])

    def test_update_of_a_disabled_host(self) -> None:
        rc, _, err = self.add("--yes")
        self.assertEqual(rc, 0, err)
        _ = self.ok("host", "disable", "h1")
        self.set_installed_version("1" * 64)
        rc, out, err = self.add("--yes", cmd="update")
        self.assertEqual(rc, 0, err)
        self.expect(out, err, code="changed", service="changed")
        self.assertIs(self.entry()["enabled"], False)  # still disabled

    def test_update_needs_a_known_host_and_takes_no_ssh_options(self) -> None:
        before = self.machines_raw()
        rc, obj, _ = self.task("host", "update", "nope", "--pyz", self.remote_pyz)
        self.assertEqual(rc, 2)
        self.assertEqual(self.get(obj, "error", "code"), "unknown_host")
        self.assertIn("task host add nope --ssh", self.text(self.get(obj, "error"), "message"))
        rc, obj, _ = self.task("host", "update", "h1", "--ssh", "x@y")
        self.assertEqual(rc, 2)
        self.assertEqual(self.get(obj, "error", "code"), "bad_args")
        self.assertEqual(self.machines_raw(), before)
        self.assertFalse(os.path.lexists(self.rcode()))


class HostCmdsTest(Env):
    """`task host list|remove|ping` against plain local-transport servers."""

    servers: tuple[str, ...] = ("s1", "s2")

    def machines_json(self) -> JsonDict:
        with open(os.path.join(self.home, "machines.json")) as f:
            return self.obj(cast(JsonValue, json.load(f)))

    def test_list(self) -> None:
        self.server("s2")["enabled"] = False
        self.write_machines()
        obj = self.ok("host", "list")
        rows = self.objs(obj, "servers")
        self.assertEqual([self.text(r, "name") for r in rows], ["s1", "s2"])
        self.assertEqual([r["enabled"] for r in rows], [True, False])
        self.assertEqual(rows[0]["destination"], "local")
        self.assertEqual(rows[0]["remote_root"], self.root("s1"))
        rc, out, err = self.task("host", "list", text=True)
        self.assertEqual(rc, 0, err)
        lines = self.text(out).splitlines()
        self.assertEqual(lines[0].split(), ["NAME", "ENABLED", "DESTINATION", "REMOTE_ROOT"])
        self.assertEqual(lines[1].split(), ["s1", "yes", "local", self.root("s1")])
        self.assertEqual(lines[2].split(), ["s2", "no", "local", self.root("s2")])
        self.assertNotIn("\033", self.text(out))  # --plain: no color
        self.assertFalse([ln for ln in lines if ln != ln.rstrip()])
        rc, out, _ = self.task("host", "list", text=True, color=True)
        self.assertIn("\033[1mNAME", self.text(out))

    def test_enable_disable(self) -> None:
        before = self.machines_json()
        out = self.obj(self.ok("host", "disable", "s2"))
        self.assertEqual((out["host"], out["enabled"], out["changed"]), ("s2", False, True))
        after = self.machines_json()
        self.assertEqual(self.objs(after, "servers")[1]["enabled"], False)
        # Only the flag changed.
        self.assertEqual(
            [dict(s, enabled=True) for s in self.objs(after, "servers")],
            [dict(s, enabled=True) for s in self.objs(before, "servers")],
        )
        self.assertEqual(self.get(self.ok("host", "disable", "s2"), "changed"), False)
        rc, out, _ = self.task("host", "disable", "s2", text=True)
        self.assertEqual((rc, out), (0, "unchanged: s2 is already disabled\n"))
        rc, out, _ = self.task("host", "list", text=True)
        self.assertEqual(self.text(out).splitlines()[2].split()[:2], ["s2", "no"])
        rc, out, _ = self.task("host", "enable", "s2", text=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.text(out), "enabled: s2 is used for submits and queries again\n")
        self.assertEqual(self.objs(self.machines_json(), "servers")[1]["enabled"], True)
        rc, obj, _ = self.task("host", "enable", "nope")
        self.assertEqual((rc, self.get(obj, "error", "code")), (2, "unknown_host"))

    def test_remove(self) -> None:
        before = self.machines_json()
        s2 = self.objs(before, "servers")[1]
        # without --yes and without a terminal: refused, nothing changed
        rc, obj, _ = self.task("host", "remove", "s1")
        self.assertEqual(rc, 2)
        self.assertEqual(self.get(obj, "error", "code"), "not_interactive")
        self.assertEqual(self.machines_json(), before)
        # declined at the prompt
        env = {"TASKMGR_TEST_INTERACTIVE": "1"}
        rc, obj, err = self.task("host", "remove", "s1", env=env)
        self.assertEqual(rc, 2, err)
        self.assertFalse(self.get(obj, "removed"))
        self.assertIn("proceed? [y/N]", err)
        self.assertEqual(self.machines_json(), before)
        # unknown name
        rc, obj, _ = self.task("host", "remove", "nope", "--yes")
        self.assertEqual(rc, 2)
        self.assertEqual(self.get(obj, "error", "code"), "unknown_host")
        self.assertEqual(self.machines_json(), before)
        # --yes: only s1 goes; the server's files stay
        rc, out, err = self.task("host", "remove", "s1", "--yes", text=True)
        self.assertEqual(rc, 0, err)
        self.assertIn("removed: s1", self.text(out))
        self.assertIn("untouched", self.text(out))
        self.assertEqual(self.machines_json(), {"servers": [s2]})
        self.assertTrue(os.path.isfile(os.path.join(self.root("s1"), "config.json")))
        rc, obj, _ = self.task("host", "remove", "s1", "--yes")
        self.assertEqual(self.get(obj, "error", "code"), "unknown_host")

    def test_ping(self) -> None:
        obj = self.ok("host", "ping")
        rows = self.objs(obj, "hosts")
        self.assertEqual([self.text(r, "host") for r in rows], ["s1", "s2"])
        for r in rows:
            self.assertTrue(r["reachable"])
            self.assertEqual(r["protocol_version"], 2)
            self.assertIsInstance(r["ms"], int)
            self.assertTrue(r["user"] and r["hostname"])
        self.assertTrue(self.get(obj, "complete"))
        rc, out, err = self.task("host", "ping", "s1", text=True)
        self.assertEqual(rc, 0, err)
        lines = self.text(out).splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0].split()[:3], ["HOST", "STATUS", "RTT"])
        self.assertEqual(lines[1].split()[:2], ["s1", "ok"])
        # s2 does not answer: exit 2, s1 is still reported
        self.server("s2")["local"] = {"fail": "ssh: connect to host s2: Connection refused"}
        self.write_machines()
        rc, obj, _ = self.task("host", "ping")
        self.assertEqual(rc, 2)
        rows = self.objs(obj, "hosts")
        self.assertEqual([r["reachable"] for r in rows], [True, False])
        self.assertEqual(rows[1]["code"], "connection_error")
        self.assertIn("Connection refused", self.text(rows[1], "error"))
        self.assertFalse(self.get(obj, "complete"))
        rc, out, _ = self.task("host", "ping", text=True)
        self.assertEqual(rc, 2)
        self.assertRegex(self.text(out), r"\ns2 +unreachable")
        self.assertIn("s2: ssh: connect to host s2: Connection refused", self.text(out))
        rc, _, _ = self.task("host", "ping", "s1")
        self.assertEqual(rc, 0)
        rc, obj, _ = self.task("host", "ping", "nope")
        self.assertEqual(rc, 2)
        self.assertEqual(self.get(obj, "error", "code"), "unknown_host")
        # A disabled server is skipped (so its timeout is not waited for), unless named.
        _ = self.ok("host", "disable", "s2")
        obj = self.ok("host", "ping")
        self.assertEqual([self.text(r, "host") for r in self.objs(obj, "hosts")], ["s1"])
        self.assertEqual(self.get(obj, "skipped"), ["s2"])
        rc, out, _ = self.task("host", "ping", text=True)
        self.assertEqual(rc, 0)
        self.assertIn("skipped: disabled, not pinged: s2", self.text(out))
        rc, obj, _ = self.task("host", "ping", "s2")
        self.assertEqual(rc, 2)
        self.assertEqual([r["reachable"] for r in self.objs(obj, "hosts")], [False])
        _ = self.ok("host", "disable", "s1")
        rc, out, _ = self.task("host", "ping", text=True)
        self.assertEqual(rc, 0)
        self.assertTrue(self.text(out).startswith("skipped: disabled, not pinged: s1, s2"))


class AddHostUnitTest(unittest.TestCase):
    """Pure functions; no subprocess except the read-only `inspect`."""

    def test_unit_matches_the_shipped_template(self) -> None:
        def directives(text: str) -> list[str]:
            return [line for line in text.splitlines() if line and not line.startswith("#")]

        with open(os.path.join(REPO, "taskmgr", "deploy", "taskmgr-remote.user.service")) as f:
            template = f.read().replace("%h", "/home/u")
        unit = hostsetup.unit_text("/usr/bin/python3", "/home/u/.taskmgr-remote")
        self.assertEqual(directives(unit), directives(template))
        unit = hostsetup.unit_text("/usr/bin/python3", "/scratch/t root/%x")
        self.assertIn(
            '\nExecStart=/usr/bin/python3 "/scratch/t root/%%x/taskmgr-remote.pyz" '
            + '--root "/scratch/t root/%%x" serve\n',
            unit,
        )

    def test_config_example_matches_the_defaults(self) -> None:
        with open(os.path.join(REPO, "taskmgr", "deploy", "config.example.json")) as f:
            example = cast(JsonValue, json.load(f))
        self.assertEqual(example, DEFAULT_REMOTE_CONFIG)

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
