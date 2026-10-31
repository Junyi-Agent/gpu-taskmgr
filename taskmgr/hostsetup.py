"""`task host add`: set up or update a GPU server over ssh, one step at a time (desc-2 D18).

Every step checks the server, prints what it found, and changes something only when the
server differs from the wanted state; each change is confirmed with [y/N] (`--yes` answers
yes). A run on an up-to-date server changes nothing and asks nothing. Nothing needs root.

Steps: ssh (read-only checks), code, remote_root, data, unit, service, linger, verify
(read-only), machines (the local machine list), old_code (removes code that older versions
installed outside remote_root). Everything on the server lives in remote_root (default
~/.taskmgr-remote, or $TASKMGR_REMOTE_ROOT): config.json, the job data, the code
versions/<version>.pyz (built by taskmgr.pyz) and the symlink taskmgr-remote.pyz to it. An
installed archive is never modified: an update adds a file and switches the link (see
pyz.MAIN_PY for why). Progress and prompts go to stderr; the
result is a summary of the steps (text on stdout, or JSON with --detail; see hostcmds).
"""

import datetime
import difflib
import json
import os
import re
import shlex
import sys
import time
from collections.abc import Callable, Mapping
from typing import Protocol, TypedDict, cast

from . import pyz
from .common import (
    DEFAULT_REMOTE_CONFIG,
    TaskmgrError,
    atomic_write_bytes,
    flocked,
    read_json,
)
from .pyz import VERSION_FILE as VERSION_FILE
from .records import JsonDict, JsonValue, Server, SshConfig
from .textout import BOLD, CYAN, GREEN, RED, YELLOW, paint
from .transport import Remote, load_machines

# Under remote_root:
CODE_FILE = pyz.REMOTE_PYZ  # the installed code, a symlink into CODE_STORE
CODE_STORE = "versions"  # <version>.pyz files: the current one and the previous one
BACKUP_DIR = "backups"  # backups/<UTC time>/: job directories the new code cannot handle
# Under $HOME, written by older versions:
LEGACY_DIR = "taskmgr"  # the code tree of versions before the zipapp
OLD_CODE_FILE = pyz.REMOTE_PYZ  # ~/taskmgr-remote.pyz, a symlink into OLD_CODE_STORE
OLD_CODE_STORE = ".taskmgr-remote"  # ~/.taskmgr-remote/<version>.pyz
OLD_REMOTE_ROOT = "taskmgr-root"  # the default remote_root of older versions
UNIT = "taskmgr-remote"
UNIT_DIR = ".config/systemd/user"  # under the remote $HOME
DEFAULT_REMOTE_ROOT = "~/.taskmgr-remote"
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
import fcntl, json, os, pwd, re, subprocess, sys, time

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

def zip_text(path, name):
    try:
        import zipfile
        with zipfile.ZipFile(path) as z:
            return z.read(name).decode()
    except Exception:
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
code = os.path.join(root, "taskmgr-remote.pyz")
store = os.path.join(root, "versions")
legacy = os.path.join(home, "taskmgr")
old_code = os.path.join(home, "taskmgr-remote.pyz")
old_store = os.path.join(home, ".taskmgr-remote")
old_root = os.path.join(home, "taskmgr-root")
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
    "code_target": os.readlink(code) if os.path.islink(code) else None,
    "code_store": sorted(os.listdir(store)) if os.path.isdir(store) else [],
    "legacy_code": os.path.isdir(legacy) and not os.path.islink(legacy) and (
        os.path.exists(os.path.join(legacy, ".taskmgr-code-version"))
        or os.path.exists(os.path.join(legacy, "taskmgr", "remote.py"))),
    "unit": read(unit),
    "unit_mtime": mtime(unit),
    "old_code_dirs": sorted(n for n in os.listdir(home) if n.startswith("taskmgr.old-")),
    # code of the first zipapp layout: ~/taskmgr-remote.pyz -> .taskmgr-remote/<v>.pyz
    "old_code_link": os.path.islink(old_code) and old_code != code,
    "old_code_files": sorted(
        n for n in (os.listdir(old_store) if os.path.isdir(old_store) else [])
        if re.match(r"^[0-9a-f]{16}\.pyz(\.new-.*)?$", n)
        and os.path.isfile(os.path.join(old_store, n))),
    "old_root": os.path.normpath(old_root) != root
        and os.path.exists(os.path.join(old_root, "config.json")),
}
version = zip_text(code, ".taskmgr-code-version")
out["code_version"] = version.strip() if version is not None else None
try:
    # the link is made anew at every switch, so its own mtime is the time of the install
    out["code_version_mtime"] = os.lstat(code).st_mtime
except OSError:
    out["code_version_mtime"] = None
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
    """The `task host add` options (taskmgr.Args has them)."""

    name: str
    ssh: str | None
    port: int | None
    identity_file: str | None
    ssh_option: list[str] | None
    enabled: bool | None
    pyz: str | None
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
    code_target: str | None
    code_store: list[str]
    legacy_code: bool
    code_version: str | None
    code_version_mtime: float | None
    unit: str | None
    unit_mtime: float | None
    old_code_dirs: list[str]
    old_code_link: bool
    old_code_files: list[str]
    old_root: bool
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
    """One entry of the step summary (the command's result)."""

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


# The server's remote_root for `task host add` (default: the existing entry's, else
# ~/.taskmgr-remote). `~` and relative paths are resolved on the server.
REMOTE_ROOT_ENV = "TASKMGR_REMOTE_ROOT"


def remote_root_from_env() -> str:
    return os.environ.get(REMOTE_ROOT_ENV, "").strip()


BUILD_HINT = "build it with `python3 -m taskmgr.pyz -o dist` in the checkout"


def prebuilt_code(path: str) -> tuple[str, str, int]:
    """Check the taskmgr-remote.pyz to upload: (code version, absolute path, files inside).

    `task host add` never builds the archive: it uploads one built by `python3 -m taskmgr.pyz`
    and requires its code version to equal the version of the running `task`, so the server
    runs the code the CLI was built from.
    """
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        raise StepFailed("%s not found; %s (or pass --pyz FILE)" % (path, BUILD_HINT))
    try:
        version, files = pyz.remote_archive_info(path)
    except (OSError, ValueError) as e:
        raise StepFailed("%s; %s" % (e, BUILD_HINT)) from None
    local = pyz.code_version()
    if version != local:
        raise StepFailed(
            "%s has code version %s, this task has %s; rebuild it: %s"
            % (path, version[:12], local[:12], BUILD_HINT)
        )
    return version, path, files


def systemd_quote(arg: str) -> str:
    """Quote one ExecStart argument (systemd expands %, $ and splits on spaces)."""
    arg = arg.replace("%", "%%").replace("$", "$$")
    if re.search(r"[\s\"'\\;]", arg) or not arg:
        return '"%s"' % arg.replace("\\", "\\\\").replace('"', '\\"')
    return arg


def unit_text(python: str, root: str) -> str:
    """The user unit for this server (absolute paths; same settings as
    deploy/taskmgr-remote.user.service, which a test keeps in sync)."""
    exec_start = " ".join(
        systemd_quote(a) for a in (python, "%s/%s" % (root, CODE_FILE), "--root", root, "serve")
    )
    return (
        "# Generated by `task host add`; run it again instead of editing this file.\n"
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


# Color of a step's result line.
STATUS_SGR = {"ok": GREEN, "changed": GREEN, "skipped": YELLOW, "declined": YELLOW, "failed": RED}


class Ui:
    """Progress and prompts on stderr; answers from stdin."""

    def __init__(self, yes: bool) -> None:
        self.yes: bool = yes
        # TASKMGR_TEST_INTERACTIVE=1: test-only, answers come from a pipe instead of a TTY.
        self.interactive: bool = sys.stdin.isatty() or (
            os.environ.get("TASKMGR_TEST_INTERACTIVE") == "1"
        )

    @staticmethod
    def say(text: str = "", sgr: str | None = None) -> None:
        _ = sys.stderr.write(paint(text, sgr) + "\n")
        _ = sys.stderr.flush()

    def info(self, text: str, sgr: str | None = None) -> None:
        for line in text.splitlines() or [""]:
            self.say("      " + paint(line, sgr))

    def confirm(self, change: str) -> None:
        """Describe a change and ask; raises Declined or NonInteractive."""
        self.info("change: " + change, YELLOW)
        if self.yes:
            self.say("      %s y (--yes)" % paint("proceed? [y/N]", BOLD))
            return
        if not self.interactive:
            raise NonInteractive(
                "a change is needed but stdin is not a terminal; run it in a terminal or "
                + "pass --yes (nothing was changed by this step)"
            )
        _ = sys.stderr.write("      %s " % paint("proceed? [y/N]", BOLD))
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
        self.version: str = ""
        self.pyz: str = ""  # the local taskmgr-remote.pyz to upload
        self.files: int = 0
        self.staged: str | None = None  # uploaded archive waiting for the data check
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
            ("code", "code in <remote_root>/%s" % CODE_FILE, self.step_code, True),
            ("remote_root", "remote_root and config.json", self.step_root, True),
            ("data", "job directories match the current layout", self.step_data, True),
            ("unit", "systemd user unit", self.step_unit, True),
            ("service", "taskmgr-remote service", self.step_service, True),
            ("linger", "lingering (start at boot, survive logout)", self.step_linger, False),
            ("verify", "the server answers", self.step_verify, True),
            ("machines", "local machine list", self.step_machines, True),
            ("old_code", "code of older versions outside remote_root", self.step_old_code, False),
        ]
        self.ui.say("task host add %s: %s" % (self.args.name, self.destination()), BOLD)
        stopped_by: str | None = None
        try:
            for i, (name, title, fn, required) in enumerate(plan):
                if stopped_by is not None:
                    self.steps.append(
                        {"name": name, "status": "skipped", "detail": "not run: " + stopped_by}
                    )
                    continue
                self.ui.say("[%d/%d] %s: %s" % (i + 1, len(plan), name, title), CYAN)
                try:
                    status, detail = fn()
                except Declined as e:
                    status, detail = "declined", str(e)
                except StepFailed as e:
                    status, detail = "failed", str(e)
                except TaskmgrError as e:
                    status, detail = "failed", e.message
                self.ui.info("-> %s: %s" % (status, detail), STATUS_SGR.get(status))
                self.steps.append({"name": name, "status": status, "detail": detail})
                if status in ("declined", "failed") and required:
                    stopped_by = "step %s %s" % (name, status)
        finally:
            if self.staged is not None:
                self._discard_staged(stopped_by or "the run ended early")
        complete = all(s["status"] in ("ok", "changed") for s in self.steps)
        self.ui.say(
            "done: %s" % ("the server is set up" if complete else "NOT complete, see the steps"),
            GREEN if complete else RED,
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
            raise StepFailed("the server probe printed no JSON: %r" % out[-500:]) from None
        if not isinstance(obj, dict):
            raise StepFailed("the server probe printed no JSON object")
        self.probe = cast(Probe, cast(object, obj))
        return self.probe

    def p_home(self) -> str:
        return self.probe.get("home") or ""

    def code_path(self) -> str:
        return "%s/%s" % (self.root(), CODE_FILE)

    def store_path(self) -> str:
        return "%s/%s" % (self.root(), CODE_STORE)

    def archive_name(self) -> str:
        return "%s.pyz" % self.version[:16]

    def unit_path(self) -> str:
        return "%s/%s/%s.service" % (self.p_home(), UNIT_DIR, UNIT)

    def root(self) -> str:
        return self.probe.get("root") or ""

    def wanted_unit(self) -> str:
        return unit_text(self.probe.get("python") or "python3", self.root())

    def server_entry(self, code_file: str | None = None) -> Server:
        e: Server = cast(Server, json.loads(json.dumps(self.entry)))
        e["remote_root"] = self.root()
        e["remote_command"] = code_file or self.code_path()
        return e

    def systemctl(self, *argv: str) -> None:
        _ = self.sh(shlex.join(["systemctl", "--user"] + list(argv)), timeout=120)

    # -- 1. ssh + environment (read-only) -------------------------------------
    def step_ssh(self) -> tuple[str, str]:
        spec = remote_root_from_env() or (self.existing or {}).get("remote_root") or ""
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
                + "Set %s to a directory on a local disk " % REMOTE_ROOT_ENV
                + "(e.g. /scratch/<user>/taskmgr-remote) and run again."
            )
        if p.get("old_root"):
            self.ui.info(
                "WARNING: %s/%s holds the data of an older setup; this run uses %s. "
                % (p.get("home"), OLD_REMOTE_ROOT, root)
                + "To keep using the old data, stop here and run again with "
                + "%s=~/%s" % (REMOTE_ROOT_ENV, OLD_REMOTE_ROOT),
                YELLOW,
            )
        return "ok", "%s, Python %s, systemd user instance ok" % (self.destination(), vtext)

    # -- 2. code ----------------------------------------------------------------
    def step_code(self) -> tuple[str, str]:
        self.version, self.pyz, self.files = prebuilt_code(
            self.args.pyz or pyz.default_remote_pyz()
        )
        self.ui.info("archive %s (version %s)" % (self.pyz, self.version[:12]))
        installed = self.probe.get("code_version")
        short = self.version[:12]
        self.ui.info("local version %s; installed: %s" % (short, (installed or "none")[:12]))
        if installed == self.version:
            return "ok", "already up to date (%s)" % short
        path = self.code_path()
        store = self.store_path()
        final = "%s/%s" % (store, self.archive_name())
        size = os.path.getsize(self.pyz)
        if self.probe.get("code_exists"):
            what = "update %s from version %s to %s" % (path, (installed or "unknown")[:12], short)
        else:
            what = "install version %s as %s" % (short, path)
        self.ui.confirm(
            "%s: upload %s (%d KB, %d files inside) to %s/, and after step data point %s at "
            % (what, CODE_FILE, size // 1024, self.files, store, path)
            + "%s (the previous archive is kept, older ones are removed)" % final
        )
        with open(self.pyz, "rb") as f:
            data = f.read()
        script = (
            "set -e\n"
            + "mkdir -p %s\n" % q(store)
            + "new=$(mktemp %s)\n" % q("%s/%s.new-XXXXXX" % (store, self.archive_name()))
            + 'if ! cat > "$new"; then rm -f "$new"; exit 1; fi\n'
            + 'chmod 755 "$new"\n'
            + 'echo "$new"\n'
        )
        out = self.sh(script, data).strip().splitlines()
        staged = out[-1] if out else ""
        if not staged.startswith(final + ".new-"):
            raise StepFailed("unexpected staging file %r" % staged)
        self.staged = staged
        self.ui.info("uploaded to %s" % staged)
        # Status is finalized when the staged file is moved into place (after step data).
        return "changed", "uploaded version %s to %s (installed after step data)" % (short, staged)

    def _install_staged(self) -> None:
        staged = self.staged
        assert staged is not None
        path, store, name = self.code_path(), self.store_path(), self.archive_name()
        final = "%s/%s" % (store, name)
        # The archive gets its final name (an existing file of this version stays as it is:
        # a process may be reading it), then a new link replaces `path` with one rename, so
        # `path` exists at every moment. Archives other than this one and the one the link
        # pointed to before are removed (nothing reads them after its start).
        script = (
            "set -e\n"
            + "prev=$(readlink %s 2>/dev/null || true)\n" % q(path)
            + "if [ -e %s ]; then rm -f %s; else mv %s %s; fi\n"
            % (q(final), q(staged), q(staged), q(final))
            + "ln -s %s %s\n" % (q("%s/%s" % (CODE_STORE, name)), q(path + ".link-new"))
            + "mv -f %s %s\n" % (q(path + ".link-new"), q(path))
            + 'keep=$(basename "${prev:-none}")\n'
            + "for f in %s/*; do\n" % q(store)
            + '  b=$(basename "$f")\n'
            + '  if [ "$b" != %s ] && [ "$b" != "$keep" ]; then rm -f "$f"; fi\n' % q(name)
            + "done\n"
        )
        _ = self.sh(script)
        self.staged = None
        self.code_changed = True
        p = self.run_probe()
        if p.get("code_version") != self.version:
            raise StepFailed(
                "after the switch %s reports version %r" % (path, p.get("code_version"))
            )
        detail = "installed version %s: %s -> %s" % (self.version[:12], path, final)
        previous = [n for n in p.get("code_store") or [] if n != name]
        if previous:
            detail += "; previous archive kept: %s/%s" % (store, previous[0])
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
            _ = self.sh(
                "rm -f %s\nrmdir %s %s 2>/dev/null || true\n"
                % (q(staged), q(self.store_path()), q(self.root()))
            )
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
                    + "stop it, then run `task host add` again"
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
        backup = "%s/%s/%s" % (self.root(), BACKUP_DIR, utc_stamp())
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
            + "%s/%s/<time>/, then run `task host add` again" % (self.root(), BACKUP_DIR)
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
                    + "stop it, then run `task host add` again"
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

    # -- 10. code that older versions installed outside remote_root -------------------
    def step_old_code(self) -> tuple[str, str]:
        p = self.run_probe()
        home = self.p_home()
        trees = ["%s/%s" % (home, LEGACY_DIR)] if p.get("legacy_code") else []
        trees += ["%s/%s" % (home, d) for d in p.get("old_code_dirs") or []]
        files = ["%s/%s" % (home, OLD_CODE_FILE)] if p.get("old_code_link") else []
        old_store = "%s/%s" % (home, OLD_CODE_STORE)
        files += ["%s/%s" % (old_store, n) for n in p.get("old_code_files") or []]
        if not trees and not files:
            return "ok", "no code of an older version"
        self.ui.info(
            "the service runs %s; this code from older versions is not used any more:"
            % self.code_path()
        )
        for t in trees + files:
            self.ui.info("  " + t)
        self.ui.confirm(
            "delete %s. Machine lists on other computers that still name "
            % ", ".join(trees + files)
            + "it stop working; run `task host add %s` there" % self.args.name
        )
        script = "rm -rf -- %s\n" % " ".join(q(t) for t in trees + files)
        if old_store != self.root():
            script += "rmdir %s 2>/dev/null || true\n" % q(old_store)
        _ = self.sh(script)
        left = self.run_probe()
        if (
            left.get("legacy_code")
            or left.get("old_code_dirs")
            or left.get("old_code_link")
            or left.get("old_code_files")
        ):
            raise StepFailed("still there after rm -rf: %s" % ", ".join(trees + files))
        return "changed", "deleted %s" % ", ".join(trees + files)


def _canon(s: Server) -> str:
    return json.dumps(s, sort_keys=True)


def _str(v: JsonValue) -> str:
    return v if isinstance(v, str) else ""


def _strs(v: JsonValue) -> list[str]:
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _paths(entries: list[JsonDict]) -> str:
    return ", ".join(_str(e.get("path")) for e in entries)


def add_host(args: AddHostArgs, home: str) -> tuple[Mapping[str, object], int]:
    """`task host add`: (step summary, exit code). `home` is the local state dir."""
    return HostSetup(args, home).run()
