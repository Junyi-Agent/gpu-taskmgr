"""`task add host`: set up or update a GPU server over ssh, one step at a time (desc-2 D18).

Every step checks the server, prints what it found, and changes something only when the
server differs from the wanted state; each change is confirmed with [y/N] (`--yes` answers
yes). A run on an up-to-date server changes nothing and asks nothing. Nothing needs root.

Steps: ssh (read-only checks), code, remote_root, data, unit, service, linger, verify
(read-only), machines (the local machine list). Progress and prompts go to stderr; stdout
gets one JSON summary.
"""

import datetime
import difflib
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from typing import Protocol, TypedDict, cast

from . import package
from .common import (
    DEFAULT_REMOTE_CONFIG,
    TaskmgrError,
    atomic_write_bytes,
    flocked,
    read_json,
)
from .records import JsonDict, JsonValue, Server, SshConfig
from .transport import Remote, load_machines

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
CODE_DIR = "taskmgr"  # under the remote $HOME
VERSION_FILE = ".taskmgr-code-version"
UNIT = "taskmgr-remote"
UNIT_DIR = ".config/systemd/user"  # under the remote $HOME
DEFAULT_REMOTE_ROOT = "~/taskmgr-root"
MIN_PYTHON = (3, 10)
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}\Z")
# Options a new machine entry gets; --ssh-option values replace the same key.
DEFAULT_SSH_OPTIONS = (
    "ServerAliveInterval=15",
    "ConnectTimeout=10",
    "ControlMaster=auto",
    "ControlPath=~/.ssh/cm-%r@%h:%p",
    "ControlPersist=10m",
)
# `stat -f -c %T` names of filesystems where flock is unreliable across hosts.
NETWORK_FS = ("nfs", "cifs", "smb", "fuse", "9p", "afs", "ceph", "gluster", "lustre", "gpfs")
# Package parts that are not shipped to the server.
EXCLUDE_NAMES = ("__pycache__", "tests", ".DS_Store")
# bin/taskmgr-remote of the repository (a test checks that both are equal).
WRAPPER = """#!/usr/bin/env python3
\"\"\"Remote entry point wrapper: runs taskmgr.remote from this checkout.\"\"\"

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from taskmgr.remote import main  # noqa: E402

sys.exit(main())
"""
STOP_TIMEOUT = 45.0  # the unit's TimeoutStopSec is 30
START_TIMEOUT = 20.0

# Shell lines every remote script starts with: work in $HOME like an ssh login, and find the
# user manager when the ssh session did not set XDG_RUNTIME_DIR.
PRELUDE = """cd "$HOME" || exit 1
if [ -z "${XDG_RUNTIME_DIR:-}" ] && [ -d "/run/user/$(id -u)" ]; then
  XDG_RUNTIME_DIR="/run/user/$(id -u)"; export XDG_RUNTIME_DIR
fi
"""

# Read-only probe, run with the server's python3 (any Python 3, so it can report an old
# one). argv[1] is the remote_root as given (may start with ~). Prints one JSON object.
PROBE = r"""
import fcntl, json, os, pwd, subprocess, sys, time

def run(argv):
    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, universal_newlines=True)
    except OSError as e:
        return 127, "", str(e)
    try:
        out, err = p.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
        return 124, out.strip(), "timed out"
    return p.returncode, out.strip(), err.strip()

def read(path):
    try:
        with open(path) as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None

def mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None

def started(pid):
    # wall-clock start of pid: now - (uptime - start ticks), precise to a clock tick
    try:
        with open("/proc/%d/stat" % pid) as f:
            data = f.read()
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
        ticks = float(data[data.rfind(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None
    return time.time() - (uptime - ticks / os.sysconf("SC_CLK_TCK"))

home = os.path.expanduser("~")
user = pwd.getpwuid(os.getuid()).pw_name
spec = sys.argv[1]
if spec == "~" or spec.startswith("~/"):
    root = home + spec[1:]
elif os.path.isabs(spec):
    root = spec
else:
    root = os.path.join(home, spec)
root = os.path.normpath(root)
code = os.path.join(home, "taskmgr")
unit = os.path.join(home, ".config", "systemd", "user", "taskmgr-remote.service")
out = {
    "python": sys.executable,
    "python_version": list(sys.version_info[:2]),
    "uname": os.uname()[0],
    "home": home,
    "user": user,
    "root": root,
    "root_exists": os.path.isdir(root),
    "config_exists": os.path.lexists(os.path.join(root, "config.json")),
    "code_exists": os.path.lexists(code),
    "unit": read(unit),
    "unit_mtime": mtime(unit),
    "old_code_dirs": sorted(n for n in os.listdir(home) if n.startswith("taskmgr.old-")),
}
version = read(os.path.join(code, ".taskmgr-code-version"))
out["code_version"] = version.strip() if version is not None else None
out["code_version_mtime"] = mtime(os.path.join(code, ".taskmgr-code-version"))
existing = root
while not os.path.exists(existing):
    existing = os.path.dirname(existing)
rc, fs, _ = run(["stat", "-f", "-c", "%T", existing])
out["fs_path"] = existing
out["fs_type"] = fs if rc == 0 else None
rc, _, err = run(["systemctl", "--user", "show-environment"])
out["systemd_ok"] = rc == 0
out["systemd_error"] = err
out["active"] = run(["systemctl", "--user", "is-active", "taskmgr-remote"])[1]
out["enabled"] = run(["systemctl", "--user", "is-enabled", "taskmgr-remote"])[1]
pid = run(["systemctl", "--user", "show", "taskmgr-remote", "-p", "MainPID"])[1]
pid = pid.split("=", 1)[-1]
out["main_pid"] = int(pid) if pid.isdigit() else 0
out["main_started"] = started(out["main_pid"]) if out["main_pid"] else None
rc, linger, err = run(["loginctl", "show-user", user, "-p", "Linger"])
out["linger"] = linger.split("=", 1)[1] if rc == 0 and linger.startswith("Linger=") else None
out["linger_error"] = err
held, dpid = False, None
try:
    fd = os.open(os.path.join(root, "locks", "daemon.lock"), os.O_RDONLY)
except OSError:
    fd = None
if fd is not None:
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            held = True
        text = os.read(fd, 64).decode(errors="replace").strip()
        dpid = int(text) if held and text.isdigit() else None
    finally:
        os.close(fd)
out["daemon_running"] = held
out["daemon_pid"] = dpid
out["now"] = time.time()
print(json.dumps(out))
"""


class AddHostArgs(Protocol):
    """The `task add host` options (taskmgr.Args has them)."""

    name: str
    ssh: str | None
    port: int | None
    identity_file: str | None
    remote_root: str | None
    ssh_option: list[str] | None
    enabled: bool | None
    yes: bool


class Probe(TypedDict, total=False):
    """What PROBE reports (keys are missing when python3 is missing)."""

    python: str | None
    python_version: list[int]
    uname: str
    home: str
    user: str
    root: str
    root_exists: bool
    config_exists: bool
    code_exists: bool
    code_version: str | None
    code_version_mtime: float | None
    unit: str | None
    unit_mtime: float | None
    old_code_dirs: list[str]
    fs_path: str
    fs_type: str | None
    systemd_ok: bool
    systemd_error: str
    active: str
    enabled: str
    main_pid: int
    main_started: float | None
    linger: str | None
    linger_error: str
    daemon_running: bool
    daemon_pid: int | None
    now: float


class Step(TypedDict):
    """One entry of the summary printed on stdout."""

    name: str
    status: str  # ok | changed | skipped | declined | failed
    detail: str


class Declined(Exception):
    """The user answered no."""


class StepFailed(Exception):
    """The step cannot reach the wanted state; the message says why."""


class NonInteractive(StepFailed):
    pass


def utc_stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def q(s: str) -> str:
    return shlex.quote(s)


# ---------------------------------------------------------------------------
# What gets installed


def build_code(dest_dir: str) -> tuple[str, str, int]:
    """Stage the shipped tree (taskmgr/ and bin/taskmgr-remote) under dest_dir and pack it.

    Returns (version, tar path, file count). The version is package.content_hash of the
    staged tree, so it depends only on file contents and executable bits.
    """
    stage = os.path.join(dest_dir, "tree")
    _ = shutil.copytree(
        PACKAGE_DIR,
        os.path.join(stage, "taskmgr"),
        ignore=shutil.ignore_patterns(*EXCLUDE_NAMES, "*.pyc"),
    )
    os.makedirs(os.path.join(stage, "bin"))
    wrapper = os.path.join(stage, "bin", "taskmgr-remote")
    with open(wrapper, "w") as f:
        _ = f.write(WRAPPER)
    os.chmod(wrapper, 0o755)
    files = sum(len(names) for _, _, names in os.walk(stage))
    tar = os.path.join(dest_dir, "code.tar.gz")
    package.pack_dir(stage, tar)
    return package.content_hash(stage), tar, files


def systemd_quote(arg: str) -> str:
    """Quote one ExecStart argument (systemd expands %, $ and splits on spaces)."""
    arg = arg.replace("%", "%%").replace("$", "$$")
    if re.search(r"[\s\"'\\;]", arg) or not arg:
        return '"%s"' % arg.replace("\\", "\\\\").replace('"', '\\"')
    return arg


def unit_text(python: str, home: str, root: str) -> str:
    """The user unit for this server (absolute paths; same settings as
    deploy/taskmgr-remote.user.service, which a test keeps in sync)."""
    exec_start = " ".join(
        systemd_quote(a)
        for a in (python, "%s/%s/bin/taskmgr-remote" % (home, CODE_DIR), "--root", root, "serve")
    )
    return (
        "# Generated by `task add host`; run it again instead of editing this file.\n"
        + "# KillMode=process: stop/restart signal only the daemon, so running shims and\n"
        + "# tasks keep running (see docs/install.md).\n"
        + "[Unit]\n"
        + "Description=taskmgr remote scheduler daemon (user service)\n"
        + "\n"
        + "[Service]\n"
        + "Type=simple\n"
        + "Environment=PYTHONUNBUFFERED=1\n"
        + "ExecStart=%s\n" % exec_start
        + "KillMode=process\n"
        + "KillSignal=SIGTERM\n"
        + "TimeoutStopSec=30\n"
        + "Restart=on-failure\n"
        + "RestartSec=5\n"
        + "\n"
        + "[Install]\n"
        + "WantedBy=default.target\n"
    )


def config_text() -> str:
    return json.dumps(DEFAULT_REMOTE_CONFIG, indent=2) + "\n"


def merge_ssh_options(options: list[str], extra: list[str]) -> list[str]:
    """Add `-o KEY=VALUE` pairs; an extra option replaces an existing one with the same key."""
    pairs: list[list[str]] = []
    i = 0
    while i < len(options):
        if options[i] == "-o" and i + 1 < len(options):
            pairs.append(["-o", options[i + 1]])
            i += 2
        else:
            pairs.append([options[i]])
            i += 1
    for opt in extra:
        key = opt.split("=", 1)[0].strip().lower()
        pairs = [p for p in pairs if not (len(p) == 2 and p[1].split("=", 1)[0].lower() == key)]
        pairs.append(["-o", opt])
    return [a for p in pairs for a in p]


def parse_destination(dest: str) -> tuple[str | None, str]:
    user, sep, host = dest.rpartition("@")
    if not host or (sep and not user):
        raise TaskmgrError("bad_args", "--ssh must be USER@HOST or HOST, got %r" % dest)
    return (user if sep else None), host


# ---------------------------------------------------------------------------
# Prompts


class Ui:
    """Progress and prompts on stderr; answers from stdin."""

    def __init__(self, yes: bool) -> None:
        self.yes: bool = yes
        # TASKMGR_TEST_INTERACTIVE=1: test-only, answers come from a pipe instead of a TTY.
        self.interactive: bool = sys.stdin.isatty() or (
            os.environ.get("TASKMGR_TEST_INTERACTIVE") == "1"
        )

    @staticmethod
    def say(text: str = "") -> None:
        _ = sys.stderr.write(text + "\n")
        _ = sys.stderr.flush()

    def info(self, text: str) -> None:
        for line in text.splitlines() or [""]:
            self.say("      " + line)

    def confirm(self, change: str) -> None:
        """Describe a change and ask; raises Declined or NonInteractive."""
        self.info("change: " + change)
        if self.yes:
            self.say("      proceed? [y/N] y (--yes)")
            return
        if not self.interactive:
            raise NonInteractive(
                "a change is needed but stdin is not a terminal; run it in a terminal or "
                + "pass --yes (nothing was changed by this step)"
            )
        _ = sys.stderr.write("      proceed? [y/N] ")
        _ = sys.stderr.flush()
        answer = sys.stdin.readline()
        if not answer:
            self.say("")
        if answer.strip().lower() not in ("y", "yes"):
            raise Declined("declined: " + change)


# ---------------------------------------------------------------------------
# The setup run


class HostSetup:
    def __init__(self, args: AddHostArgs, home: str) -> None:
        self.args: AddHostArgs = args
        self.ui: Ui = Ui(args.yes)
        self.home: str = home
        self.machines_path: str = os.path.join(self.home, "machines.json")
        self.existing: Server | None = None
        if os.path.exists(self.machines_path):
            for s in load_machines(self.home):  # also rejects a broken file up front
                if s["name"] == args.name:
                    self.existing = s
        self.entry: Server = self._wanted_entry()
        self.remote: Remote = Remote(self.entry)
        self.probe: Probe = {}
        self.tmp: str = ""
        self.version: str = ""
        self.tar: str = ""
        self.files: int = 0
        self.staged: str | None = None  # uploaded code waiting for the data check
        self.code_changed: bool = False
        self.unit_changed: bool = False
        self.steps: list[Step] = []

    # -- the wanted machine entry ---------------------------------------------
    def _wanted_entry(self) -> Server:
        a = self.args
        if not NAME_RE.match(a.name):
            raise TaskmgrError("bad_args", "host name must match ^[A-Za-z0-9._-]{1,64}$")
        old = self.existing
        if old is None and not a.ssh:
            raise TaskmgrError("bad_args", "--ssh USER@HOST is required for a new host")
        entry: Server = (
            cast(Server, json.loads(json.dumps(old)))
            if old is not None
            else {
                "name": a.name,
                "enabled": True,
                "transport": "ssh",
                "remote_root": "",
                "remote_command": "",
            }
        )
        if old is None:
            # TASKMGR_TEST_LOCAL_HOME: test-only, the new entry uses transport "local" with
            # this directory as the server's HOME.
            test_home = os.environ.get("TASKMGR_TEST_LOCAL_HOME")
            if test_home:
                entry["transport"] = "local"
                entry["local"] = {"home": test_home}
        if a.enabled is not None:
            entry["enabled"] = a.enabled
        ssh: SshConfig = entry.get("ssh") or {}
        if a.ssh:
            user, host = parse_destination(a.ssh)
            ssh["host"] = host
            if user:
                ssh["user"] = user
            else:
                _ = ssh.pop("user", None)
        if a.port is not None:
            ssh["port"] = a.port
        if a.identity_file is not None:
            ssh["identity_file"] = a.identity_file
        options = list(ssh.get("options") or [])
        if old is None:
            options = merge_ssh_options([], list(DEFAULT_SSH_OPTIONS))
        if old is None or a.ssh_option:
            ssh["options"] = merge_ssh_options(options, a.ssh_option or [])
        entry["ssh"] = ssh
        return entry

    def destination(self) -> str:
        if self.entry.get("transport") == "local":
            return "local (test transport)"
        return self.remote.ssh_argv()[-1]

    # -- running steps ----------------------------------------------------------
    def run(self) -> tuple[Mapping[str, object], int]:
        plan: list[tuple[str, str, Callable[[], tuple[str, str]], bool]] = [
            ("ssh", "check the connection and the server", self.step_ssh, True),
            ("code", "code in ~/taskmgr", self.step_code, True),
            ("remote_root", "remote_root and config.json", self.step_root, True),
            ("data", "job directories match the current layout", self.step_data, True),
            ("unit", "systemd user unit", self.step_unit, True),
            ("service", "taskmgr-remote service", self.step_service, True),
            ("linger", "lingering (start at boot, survive logout)", self.step_linger, False),
            ("verify", "the server answers", self.step_verify, True),
            ("machines", "local machine list", self.step_machines, True),
        ]
        self.ui.say("task add host %s: %s" % (self.args.name, self.destination()))
        stopped_by: str | None = None
        self.tmp = tempfile.mkdtemp(prefix="taskmgr-addhost-")
        try:
            for i, (name, title, fn, required) in enumerate(plan):
                if stopped_by is not None:
                    self.steps.append(
                        {"name": name, "status": "skipped", "detail": "not run: " + stopped_by}
                    )
                    continue
                self.ui.say("[%d/%d] %s: %s" % (i + 1, len(plan), name, title))
                try:
                    status, detail = fn()
                except Declined as e:
                    status, detail = "declined", str(e)
                except StepFailed as e:
                    status, detail = "failed", str(e)
                except TaskmgrError as e:
                    status, detail = "failed", e.message
                self.ui.info("-> %s: %s" % (status, detail))
                self.steps.append({"name": name, "status": status, "detail": detail})
                if status in ("declined", "failed") and required:
                    stopped_by = "step %s %s" % (name, status)
        finally:
            if self.staged is not None:
                self._discard_staged(stopped_by or "the run ended early")
            shutil.rmtree(self.tmp, ignore_errors=True)
        complete = all(s["status"] in ("ok", "changed") for s in self.steps)
        self.ui.say(
            "done: %s" % ("the server is set up" if complete else "NOT complete, see the steps")
        )
        out: dict[str, object] = {"host": self.args.name, "steps": self.steps, "complete": complete}
        return out, (0 if complete else 2)

    # -- remote helpers ---------------------------------------------------------
    def sh(self, script: str, stdin: bytes | None = None, timeout: float = 300) -> str:
        """Run a script that must succeed; returns stdout. Raises StepFailed otherwise."""
        rc, out, err = self.remote.shell(PRELUDE + script, stdin, timeout)
        if rc != 0:
            raise StepFailed("remote command failed (exit %d): %s" % (rc, err.strip()[-1500:]))
        return out

    def run_probe(self, root_spec: str | None = None) -> Probe:
        spec = root_spec or self.probe.get("root") or DEFAULT_REMOTE_ROOT
        script = (
            "if ! command -v python3 >/dev/null 2>&1; then\n"
            + '  printf \'{"python": null, "uname": "%s"}\\n\' "$(uname -s)"; exit 0\n'
            + "fi\n"
            + "exec python3 - %s\n" % q(spec)
        )
        out = self.sh(script, PROBE.encode(), timeout=120)
        try:
            obj = cast(JsonValue, json.loads(out.strip().splitlines()[-1]))
        except (ValueError, IndexError):
            raise StepFailed("the server probe printed no JSON: %r" % out[-500:])
        if not isinstance(obj, dict):
            raise StepFailed("the server probe printed no JSON object")
        self.probe = cast(Probe, cast(object, obj))
        return self.probe

    def p_home(self) -> str:
        return self.probe.get("home") or ""

    def code_path(self) -> str:
        return "%s/%s" % (self.p_home(), CODE_DIR)

    def unit_path(self) -> str:
        return "%s/%s/%s.service" % (self.p_home(), UNIT_DIR, UNIT)

    def root(self) -> str:
        return self.probe.get("root") or ""

    def wanted_unit(self) -> str:
        return unit_text(self.probe.get("python") or "python3", self.p_home(), self.root())

    def server_entry(self, code_dir: str | None = None) -> Server:
        e: Server = cast(Server, json.loads(json.dumps(self.entry)))
        e["remote_root"] = self.root()
        e["remote_command"] = "%s/bin/taskmgr-remote" % (code_dir or self.code_path())
        return e

    def systemctl(self, *argv: str) -> None:
        _ = self.sh(shlex.join(["systemctl", "--user"] + list(argv)), timeout=120)

    # -- 1. ssh + environment (read-only) -------------------------------------
    def step_ssh(self) -> tuple[str, str]:
        spec = self.args.remote_root or (self.existing or {}).get("remote_root") or ""
        p = self.run_probe(spec or DEFAULT_REMOTE_ROOT)
        uname = p.get("uname") or "?"
        if uname != "Linux":
            raise StepFailed("the server runs %s; taskmgr-remote needs Linux" % uname)
        python = p.get("python")
        version = tuple(p.get("python_version") or [])
        if not python:
            raise StepFailed("python3 is not on the PATH of a non-interactive ssh session")
        vtext = ".".join(str(v) for v in version)
        if version < MIN_PYTHON:
            raise StepFailed("%s is Python %s; taskmgr needs 3.10 or newer" % (python, vtext))
        self.ui.info(
            "%s: Linux, user %s, home %s" % (self.destination(), p.get("user"), p.get("home"))
        )
        self.ui.info("python3 %s at %s" % (vtext, python))
        if not p.get("systemd_ok"):
            raise StepFailed(
                "`systemctl --user` does not work (%s); taskmgr needs a systemd user instance"
                % (p.get("systemd_error") or "no error text")
            )
        self.ui.info("systemd user instance: ok; linger: %s" % (p.get("linger") or "unknown"))
        self.ui.info(
            "code: %s; unit: %s; service: %s, %s"
            % (
                (p.get("code_version") or "")[:12]
                or ("no version file" if p.get("code_exists") else "none"),
                "installed" if p.get("unit") is not None else "none",
                p.get("active") or "?",
                p.get("enabled") or "not enabled",
            )
        )
        root = self.root()
        fs = p.get("fs_type") or "unknown"
        where = "exists" if p.get("root_exists") else "does not exist yet"
        self.ui.info(
            "remote_root %s (%s; filesystem of %s: %s)" % (root, where, p.get("fs_path"), fs)
        )
        if any(fs.lower().startswith(n) for n in NETWORK_FS):
            self.ui.info(
                "WARNING: %s looks like a network filesystem; taskmgr needs local flock. " % fs
                + "Use --remote-root on a local disk (e.g. /scratch/<user>/taskmgr-root)."
            )
        return "ok", "%s, Python %s, systemd user instance ok" % (self.destination(), vtext)

    # -- 2. code ----------------------------------------------------------------
    def step_code(self) -> tuple[str, str]:
        self.version, self.tar, self.files = build_code(self.tmp)
        installed = self.probe.get("code_version")
        short = self.version[:12]
        self.ui.info("local version %s; installed: %s" % (short, (installed or "none")[:12]))
        if installed == self.version:
            return "ok", "already up to date (%s)" % short
        path = self.code_path()
        size = os.path.getsize(self.tar)
        if self.probe.get("code_exists"):
            what = "replace %s (version %s) with version %s; the current tree is kept as " % (
                path,
                (installed or "unknown")[:12],
                short,
            ) + "%s.old-<time> (older %s.old-* are removed)" % (path, path)
        else:
            what = "install version %s into %s" % (short, path)
        self.ui.confirm(
            "%s: upload %d files (%d KB) to %s.new-*, and move it into place after step data"
            % (what, self.files, size // 1024, path)
        )
        with open(self.tar, "rb") as f:
            data = f.read()
        script = (
            "set -e\n"
            + 'new=$(mktemp -d "$HOME/%s.new-XXXXXX")\n' % CODE_DIR
            + 'if ! tar -xzf - -C "$new"; then rm -rf "$new"; exit 1; fi\n'
            + "printf '%%s\\n' %s > \"$new/%s\"\n" % (q(self.version), VERSION_FILE)
            + 'chmod 755 "$new"\n'
            + 'echo "$new"\n'
        )
        out = self.sh(script, data).strip().splitlines()
        staged = out[-1] if out else ""
        if not staged.startswith(path + ".new-"):
            raise StepFailed("unexpected staging directory %r" % staged)
        self.staged = staged
        self.ui.info("uploaded to %s" % staged)
        # Status is finalized when the staged tree is moved into place (after step data).
        return "changed", "uploaded version %s to %s (installed after step data)" % (short, staged)

    def _install_staged(self) -> None:
        staged = self.staged
        assert staged is not None
        path = self.code_path()
        old = "%s.old-%s" % (path, utc_stamp())
        script = (
            "set -e\n"
            + 'for d in "$HOME"/%s.old-*; do if [ -e "$d" ]; then rm -rf "$d"; fi; done\n'
            % CODE_DIR
            + "if [ -e %s ] || [ -L %s ]; then mv %s %s; fi\n" % (q(path), q(path), q(path), q(old))
            + "mv %s %s\n" % (q(staged), q(path))
        )
        _ = self.sh(script)
        self.staged = None
        self.code_changed = True
        p = self.run_probe()
        if p.get("code_version") != self.version:
            raise StepFailed("after the move %s reports version %r" % (path, p.get("code_version")))
        detail = "installed version %s in %s" % (self.version[:12], path)
        if p.get("old_code_dirs"):
            detail += "; previous tree kept as %s/%s" % (
                self.p_home(),
                (p.get("old_code_dirs") or [""])[-1],
            )
        self.ui.info("code: " + detail)
        for s in self.steps:
            if s["name"] == "code":
                s["detail"] = detail

    def _discard_staged(self, why: str) -> None:
        staged = self.staged
        self.staged = None
        if staged is None:
            return
        try:
            _ = self.sh("rm -rf %s\n" % q(staged))
            removed = "removed"
        except (StepFailed, TaskmgrError):
            removed = "could not be removed"
        for s in self.steps:
            if s["name"] == "code":
                s["status"] = "skipped"
                s["detail"] = "uploaded but not installed (%s); %s %s" % (why, staged, removed)
        self.ui.say("code: the uploaded copy %s was %s (%s)" % (staged, removed, why))

    # -- 3. remote_root + config.json -----------------------------------------
    def step_root(self) -> tuple[str, str]:
        root = self.root()
        cfg = root + "/config.json"
        has_root, has_cfg = (
            bool(self.probe.get("root_exists")),
            bool(self.probe.get("config_exists")),
        )
        if has_root and has_cfg:
            return "ok", "%s and config.json exist (config.json is never overwritten)" % root
        parts: list[str] = []
        if not has_root:
            parts.append("create %s" % root)
        parts.append("create %s with the default settings" % cfg)
        self.ui.confirm(" and ".join(parts))
        script = (
            "set -e\n"
            + "mkdir -p %s\n" % q(root)
            + "if [ ! -e %s ]; then (set -C; cat > %s); fi\n" % (q(cfg), q(cfg))
        )
        _ = self.sh(script, config_text().encode())
        p = self.run_probe()
        if not (p.get("root_exists") and p.get("config_exists")):
            raise StepFailed("%s or its config.json is still missing" % root)
        return "changed", "; ".join(parts)

    # -- 4. data compatibility --------------------------------------------------
    def inspect(self) -> JsonDict:
        return Remote(self.server_entry(self.staged)).call(["inspect"], timeout=300)

    def step_data(self) -> tuple[str, str]:
        result = self._check_data()
        if self.staged is not None:
            self._install_staged()
        return result

    def _check_data(self) -> tuple[str, str]:
        info = self.inspect()
        bad = self._incompatible(info)
        counts = info.get("counts")
        self.ui.info("jobs per state: %s" % json.dumps(counts, sort_keys=True))
        if not bad:
            return "ok", "no incompatible job directories"
        self._list_incompatible(bad)
        live = [e for e in bad if e.get("live")]
        if live:
            raise StepFailed(self._live_message(live, stopped=False))
        p = self.run_probe()  # is a daemon running right now?
        if p.get("daemon_running") or p.get("active") == "active":
            if p.get("active") != "active":
                raise StepFailed(
                    "a daemon that systemd does not manage holds %s/locks/daemon.lock (pid %s); "
                    % (self.root(), p.get("daemon_pid"))
                    + "stop it, then run task add host again"
                )
            self.ui.confirm(
                "stop the taskmgr-remote service before moving these directories (a running "
                + "daemon could pick them up). %s" % self._killmode_note()
            )
            self.systemctl("stop", UNIT)
            _ = self._wait(
                lambda p: not p.get("daemon_running"), STOP_TIMEOUT, "the daemon to exit"
            )
            self.ui.info("service stopped; it is started again in step service")
            info = self.inspect()
            bad = self._incompatible(info)
            live = [e for e in bad if e.get("live")]
            if live:
                raise StepFailed(self._live_message(live, stopped=True))
            if not bad:
                return "changed", "stopped the service; no incompatible job directories left"
        backup = "%s.backup-%s" % (self.root(), utc_stamp())
        moves: list[str] = []
        for e in bad:
            moves.append(_str(e.get("path")))
            moves.extend(_strs(e.get("submission_keys")))
        for rel in moves:
            if not re.match(
                r"^((queue|in-progress|done)/[0-9a-f]{16}|submission-keys/[^/]+)\Z", rel
            ):
                raise StepFailed("refusing to move unexpected path %r" % rel)
        self.ui.confirm(
            "move %d job directories and %d submission keys into %s (same relative paths; "
            % (len(bad), len(moves) - len(bad), backup)
            + "nothing is deleted)"
        )
        lines = ["set -e"]
        for rel in moves:
            src, dst = "%s/%s" % (self.root(), rel), "%s/%s" % (backup, rel)
            lines.append("mkdir -p %s" % q(os.path.dirname(dst)))
            lines.append("if [ -e %s ]; then echo exists: %s >&2; exit 1; fi" % (q(dst), q(dst)))
            lines.append("mv %s %s" % (q(src), q(dst)))
        _ = self.sh("\n".join(lines) + "\n")
        left = self._incompatible(self.inspect())
        if left:
            raise StepFailed("still incompatible after the move: %s" % _paths(left))
        _ = self.run_probe()
        return "changed", "moved %s into %s" % (", ".join(moves), backup)

    @staticmethod
    def _incompatible(info: JsonDict) -> list[JsonDict]:
        raw = info.get("incompatible")
        return [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []

    def _list_incompatible(self, bad: list[JsonDict]) -> None:
        self.ui.info("%d job directories do not match the current layout:" % len(bad))
        for e in bad:
            self.ui.info(
                "  %s  state %s%s: %s"
                % (
                    _str(e.get("path")),
                    _str(e.get("state")) or "?",
                    " (live)" if e.get("live") else "",
                    "; ".join(_strs(e.get("reasons"))),
                )
            )

    def _live_message(self, live: list[JsonDict], stopped: bool) -> str:
        msg = (
            "%d incompatible jobs may still be running (%s); they are not moved. "
            % (len(live), _paths(live))
            + "Let them end, or if their processes are gone move them by hand into "
            + "%s.backup-<time>/, then run task add host again" % self.root()
        )
        if stopped:
            msg += ". The service was stopped and stays stopped until then"
        return msg

    def _killmode_note(self) -> str:
        unit = self.probe.get("unit") or ""
        if "KillMode=process" in unit:
            return "KillMode=process: running tasks keep running."
        return (
            "WARNING: the installed unit has no KillMode=process, so systemd also stops its tasks."
        )

    def _wait(self, cond: Callable[[Probe], bool], timeout: float, what: str) -> Probe:
        end = time.monotonic() + timeout
        while True:
            p = self.run_probe()
            if cond(p):
                return p
            if time.monotonic() >= end:
                raise StepFailed(
                    "timed out after %.0fs waiting for %s; see `journalctl --user -u %s` and "
                    % (timeout, what, UNIT)
                    + "%s/daemon/daemon.log on the server" % self.root()
                )
            time.sleep(0.5)

    # -- 5. systemd unit --------------------------------------------------------
    def step_unit(self) -> tuple[str, str]:
        want = self.wanted_unit()
        have = self.probe.get("unit")
        path = self.unit_path()
        if have == want:
            return "ok", "%s is up to date" % path
        if have is None:
            self.ui.info("%s does not exist" % path)
        else:
            diff = difflib.unified_diff(
                have.splitlines(), want.splitlines(), "installed", "wanted", lineterm=""
            )
            self.ui.info("\n".join(diff))
        self.ui.confirm(
            "%s %s and run `systemctl --user daemon-reload`"
            % ("write" if have is None else "replace", path)
        )
        d = "%s/%s" % (self.p_home(), UNIT_DIR)
        script = (
            "set -e\n"
            + "mkdir -p %s\n" % q(d)
            + "tmp=$(mktemp %s)\n" % q(d + "/.%s.service.XXXXXX" % UNIT)
            + 'cat > "$tmp"\n'
            + 'chmod 644 "$tmp"\n'
            + 'mv "$tmp" %s\n' % q(path)
            + "systemctl --user daemon-reload\n"
        )
        _ = self.sh(script, want.encode())
        self.unit_changed = True
        if self.run_probe().get("unit") != want:
            raise StepFailed("%s does not have the wanted content after writing it" % path)
        return "changed", "%s %s, daemon-reload done" % (
            "wrote" if have is None else "replaced",
            path,
        )

    # -- 6. service -------------------------------------------------------------
    def _stale_reason(self) -> str | None:
        """Why the running daemon must be restarted, or None."""
        if self.code_changed:
            return "the code changed in this run"
        if self.unit_changed:
            return "the unit changed in this run"
        p = self.probe
        started = p.get("main_started")
        if started is None:
            return None
        newest = max(p.get("code_version_mtime") or 0.0, p.get("unit_mtime") or 0.0)
        # The probe measures the start to about 20 ms; the margin avoids false restarts.
        if started + 0.5 < newest:
            return "the daemon started before the current code or unit was installed"
        return None

    def step_service(self) -> tuple[str, str]:
        p = self.run_probe()
        done: list[str] = []
        if p.get("enabled") != "enabled":
            self.ui.info("%s is %s" % (UNIT, p.get("enabled") or "not enabled"))
            self.ui.confirm("`systemctl --user enable %s` (start with the user manager)" % UNIT)
            self.systemctl("enable", UNIT)
            if self.run_probe().get("enabled") != "enabled":
                raise StepFailed("%s is still not enabled" % UNIT)
            done.append("enabled")
        p = self.probe
        old_pid = p.get("main_pid") or 0
        if p.get("active") != "active":
            if p.get("daemon_running"):
                raise StepFailed(
                    "a daemon that systemd does not manage holds %s/locks/daemon.lock (pid %s); "
                    % (self.root(), p.get("daemon_pid"))
                    + "stop it, then run task add host again"
                )
            self.ui.info("%s is %s" % (UNIT, p.get("active") or "inactive"))
            self.ui.confirm("`systemctl --user start %s`" % UNIT)
            self.systemctl("start", UNIT)
            done.append("started")
        else:
            reason = self._stale_reason()
            if reason is not None:
                self.ui.info("%s is active (pid %d), but %s" % (UNIT, old_pid, reason))
                self.ui.confirm(
                    "`systemctl --user restart %s`. %s The new daemon re-attaches to them."
                    % (UNIT, self._killmode_note())
                )
                self.systemctl("restart", UNIT)
                done.append("restarted")
        if done and done[-1] in ("started", "restarted"):
            p = self._wait(
                lambda p: (
                    p.get("active") == "active"
                    and bool(p.get("daemon_running"))
                    and p.get("daemon_pid") == p.get("main_pid")
                    and p.get("main_pid") != old_pid
                ),
                START_TIMEOUT,
                "the daemon to start and take locks/daemon.lock",
            )
            self.ui.info("daemon running, pid %s" % p.get("main_pid"))
        if not done:
            return "ok", "%s is enabled and active (pid %s)" % (UNIT, p.get("main_pid"))
        return "changed", "%s (pid %s)" % (", ".join(done), self.probe.get("main_pid"))

    # -- 7. linger ----------------------------------------------------------------
    def step_linger(self) -> tuple[str, str]:
        user = self.probe.get("user") or ""
        linger = self.probe.get("linger")
        if linger == "yes":
            return "ok", "Linger=yes for %s" % user
        admin = (
            "an admin must run `sudo loginctl enable-linger %s` once; " % user
            + "until then the service stops when your last login session ends"
        )
        self.ui.info("Linger=%s for %s" % (linger or "unknown", user))
        self.ui.confirm("`loginctl enable-linger %s` (needs no root on most hosts)" % user)
        rc, _, err = self.remote.shell(
            PRELUDE + shlex.join(["loginctl", "--no-ask-password", "enable-linger", user]),
            timeout=60,
        )
        if rc != 0:
            raise StepFailed("loginctl enable-linger was refused (%s); %s" % (err.strip(), admin))
        if self.run_probe().get("linger") != "yes":
            raise StepFailed("Linger is still not yes; %s" % admin)
        return "changed", "enabled linger for %s" % user

    # -- 8. verify (read-only) ------------------------------------------------------
    def step_verify(self) -> tuple[str, str]:
        reply = Remote(self.server_entry()).call(["version"], timeout=120)
        p = self.run_probe()
        problems: list[str] = []
        if p.get("code_version") != self.version:
            problems.append("code version is %r" % p.get("code_version"))
        if p.get("unit") != self.wanted_unit():
            problems.append("the unit differs from the wanted one")
        if p.get("active") != "active":
            problems.append("%s is %s" % (UNIT, p.get("active")))
        elif not p.get("daemon_running") or p.get("daemon_pid") != p.get("main_pid"):
            problems.append(
                "the service's main pid %s does not hold locks/daemon.lock (holder: %s)"
                % (p.get("main_pid"), p.get("daemon_pid"))
            )
        if problems:
            raise StepFailed("; ".join(problems))
        return "ok", "taskmgr-remote answers with protocol_version %s; daemon pid %s" % (
            reply.get("protocol_version"),
            p.get("main_pid"),
        )

    # -- 9. local machines.json ----------------------------------------------------
    def step_machines(self) -> tuple[str, str]:
        want = self.server_entry()
        old = self.existing
        if old is not None and _canon(old) == _canon(want):
            return "ok", "%s in %s is up to date" % (self.args.name, self.machines_path)
        if old is None:
            self.ui.info("new entry:")
            for k, v in want.items():
                self.ui.info("  + %s: %s" % (k, json.dumps(v)))
        else:
            self.ui.info("changes to the entry:")
            before = cast(dict[str, object], cast(object, old))
            after = cast(dict[str, object], cast(object, want))
            for k in dict.fromkeys(list(before) + list(after)):
                a, b = before.get(k), after.get(k)
                if a != b:
                    if k in old:
                        self.ui.info("  - %s: %s" % (k, json.dumps(a)))
                    if k in want:
                        self.ui.info("  + %s: %s" % (k, json.dumps(b)))
        self.ui.confirm(
            "%s %s in %s" % ("add" if old is None else "update", self.args.name, self.machines_path)
        )
        os.makedirs(self.home, exist_ok=True)
        with flocked(os.path.join(self.home, "machines.lock")):
            empty: JsonDict = {"servers": []}
            data = read_json(self.machines_path) or empty
            servers = data.get("servers") if isinstance(data, dict) else data
            if not isinstance(servers, list):
                raise StepFailed('%s must contain a "servers" array' % self.machines_path)
            new = cast(JsonValue, json.loads(json.dumps(want)))
            for i, s in enumerate(servers):
                if isinstance(s, dict) and s.get("name") == self.args.name:
                    servers[i] = new
                    break
            else:
                servers.append(new)
            atomic_write_bytes(self.machines_path, (json.dumps(data, indent=2) + "\n").encode())
        return "changed", "%s %s in %s" % (
            "added" if old is None else "updated",
            self.args.name,
            self.machines_path,
        )


def _canon(s: Server) -> str:
    return json.dumps(s, sort_keys=True)


def _str(v: JsonValue) -> str:
    return v if isinstance(v, str) else ""


def _strs(v: JsonValue) -> list[str]:
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _paths(entries: list[JsonDict]) -> str:
    return ", ".join(_str(e.get("path")) for e in entries)


def add_host(args: AddHostArgs, home: str) -> tuple[Mapping[str, object], int]:
    """`task add host`: (summary for stdout, exit code). `home` is the local state dir."""
    return HostSetup(args, home).run()
