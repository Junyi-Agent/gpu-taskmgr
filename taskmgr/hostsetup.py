"""`task create host`: set up or update a GPU server over ssh, one step at a time (desc-2 D18).

Every step checks the server, prints what it found, and changes something only when the
server differs from the wanted state; each change is confirmed with [y/N] (`--yes` answers
yes). A run on an up-to-date server changes nothing and asks nothing. Nothing needs root.
Deleting something outside the install (step old_code) is destructive: --yes does not
answer it; it needs `--prune-old-code` or a "y" typed in a terminal, else it is kept.
`--plan` runs every check and reports every change as "planned" without making it (and
without taking the setup lock).

Steps: ssh (read-only checks), code, remote_root, data, unit, service, linger, verify
(read-only), machines (the local machine list), old_code (removes code that older versions
installed outside remote_root; see hostlegacy). Everything on the server lives in
remote_root (default ~/.taskmgr-remote, or $TASKMGR_REMOTE_ROOT): config.json, the job data,
the code versions/<version>.pyz (built by `task build pyz`) and the symlink taskmgr-remote.pyz to
it. An installed archive is never modified: an update adds a file and switches the link (see
pyz.MAIN_PY for why). What runs on the server is in hostserver. From the end of step ssh to
the end of the run, a lock on the server keeps a second run for the same server user out.
Progress and prompts go to stderr; the result is a summary of the steps (text on stdout, or
JSON with -o json; see hostcmds).
"""

import datetime
import difflib
import json
import os
import re
import select
import shlex
import subprocess
import time
from collections.abc import Callable, Mapping
from typing import Protocol, TypedDict, cast

from . import hostlegacy, pyz
from .common import DEFAULT_REMOTE_CONFIG, TaskmgrError, read_code_file
from .hostlegacy import OLD_REMOTE_ROOT, LegacyProbe
from .prompt import Declined, Kept, NonInteractive, Planned, Ui
from .records import JsonDict, JsonValue, Server, SshConfig
from .textout import BOLD, CYAN, GREEN, RED, YELLOW
from .transport import Remote, edit_machines, load_machines

# Under remote_root:
CODE_FILE = pyz.REMOTE_PYZ  # the installed code, a symlink into CODE_STORE
CODE_STORE = "versions"  # <version>.pyz files: the current one, the previous one, in use
BACKUP_DIR = "backups"  # backups/<UTC time>/: job directories the new code cannot handle
STALE_UPLOAD_MINUTES = 10  # CODE_STORE/*.new-* older than this: an interrupted upload
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

# hostserver.py, the script run on the server (as `python3 -c`).
SERVER_SCRIPT = "taskmgr/hostserver.py"


class AddHostArgs(Protocol):
    """The `task create host` options (taskmgr.Args has them)."""

    name: str
    ssh: str | None
    port: int | None
    identity_file: str | None
    ssh_option: list[str] | None
    enabled: bool | None
    pyz: str | None
    yes: bool
    plan: bool
    prune_old_code: bool


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
    unit_root: str | None  # the --root of the installed unit
    data_roots: list[str]  # other directories with a config.json (see probe_params)
    code_exists: bool
    code_store: list[str]
    code_version: str | None
    code_version_mtime: float | None
    unit: str | None
    unit_mtime: float | None
    legacy: LegacyProbe
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


class Step(TypedDict):
    """One entry of the step summary (the command's result)."""

    name: str
    status: str  # ok | changed | planned | kept | skipped | declined | failed
    detail: str


class StepFailed(Exception):
    """The step cannot reach the wanted state; the message says why."""


def utc_stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def q(s: str) -> str:
    return shlex.quote(s)


# ---------------------------------------------------------------------------
# What gets installed


# The server's remote_root for `task create host` (default: the existing entry's, else
# ~/.taskmgr-remote). `~` and relative paths are resolved on the server.
REMOTE_ROOT_ENV = "TASKMGR_REMOTE_ROOT"


def remote_root_from_env() -> str:
    return os.environ.get(REMOTE_ROOT_ENV, "").strip()


BUILD_HINT = "build it with `task build pyz`"


def prebuilt_code(path: str) -> tuple[str, str, int]:
    """Check the taskmgr-remote.pyz to upload: (code version, absolute path, files inside).

    `task create host` never builds the archive: it uploads one built by `task build pyz`
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
        "# Generated by `task create host`; run it again instead of editing this file.\n"
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


def destination(s: Server) -> str:
    """user@host[:port] of an ssh entry; "local" for the test-only local transport."""
    if s.get("transport", "ssh") == "local":
        return "local"
    ssh = s.get("ssh") or {}
    dest = ssh.get("host", "")
    user, port = ssh.get("user"), ssh.get("port")
    if user:
        dest = "%s@%s" % (user, dest)
    if port:
        dest += ":%d" % port
    return dest


# Color of a step's status (progress on stderr and the summary table).
STEP_COLORS = {
    "ok": GREEN,
    "changed": "1;32",  # bold green: done, and something was changed
    "planned": CYAN,
    "kept": YELLOW,
    "skipped": YELLOW,
    "declined": YELLOW,
    "failed": RED,
}


def server_script() -> str:
    text = read_code_file(SERVER_SCRIPT)
    if text is None:
        raise StepFailed("%s is missing from this copy of taskmgr" % SERVER_SCRIPT)
    return text


# ---------------------------------------------------------------------------
# The setup run


class HostSetup:
    def __init__(self, args: AddHostArgs, home: str, command: str = "create") -> None:
        self.args: AddHostArgs = args
        self.command: str = command  # the verb, "create" or "update": names the command
        self.ui: Ui = Ui(args.yes, args.plan, args.prune_old_code)
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
        self.previous_code: str | None = None  # the archive the link pointed to before
        self.uploads_removed: str = ""  # detail suffix of step code: removed stale uploads
        self.unit_changed: bool = False
        self.locker: subprocess.Popen[bytes] | None = None  # holds the server's setup lock
        self.steps: list[Step] = []
        self.planned: set[str] = set()  # --plan: the steps with a change to make

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
        return destination(self.entry)

    def host_name(self) -> str:
        return self.args.name

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
        self.ui.say(
            "task %s host %s: %s" % (self.command, self.args.name, self.destination()), BOLD
        )
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
                except Planned as e:
                    status, detail = "planned", str(e)
                    self.planned.add(name)
                except Kept as e:
                    status = "kept"
                    detail = "not changed: %s. Destructive, so --yes does not do it; " % e + (
                        "run `task %s host %s --prune-old-code` to do it"
                        % (self.command, self.args.name)
                    )
                except Declined as e:
                    status, detail = "declined", str(e)
                except (StepFailed, NonInteractive) as e:
                    status, detail = "failed", str(e)
                except TaskmgrError as e:
                    status, detail = "failed", e.message
                self.ui.info("-> %s: %s" % (status, detail), STEP_COLORS.get(status))
                self.steps.append({"name": name, "status": status, "detail": detail})
                if status in ("declined", "failed") and required:
                    stopped_by = "step %s %s" % (name, status)
        finally:
            if self.staged is not None:
                self._discard_staged(stopped_by or "the run ended early")
            _ = self._unlock()
        failed = any(s["status"] in ("failed", "declined") for s in self.steps)
        complete = not failed and all(s["status"] in ("ok", "changed", "kept") for s in self.steps)
        out: dict[str, object] = {"host": self.args.name, "steps": self.steps, "complete": complete}
        if self.ui.plan:
            out["plan"] = True
            out["planned"] = [s["name"] for s in self.steps if s["status"] == "planned"]
            n = len(self.planned)
            self.ui.say(
                "plan: %s" % ("failed, see the steps" if failed else "%d steps would change" % n),
                RED if failed else (YELLOW if n else GREEN),
            )
            # 0: nothing to change, 1: changes planned, 2: a check failed
            return out, (2 if failed else 1 if n else 0)
        self.ui.say(
            "done: %s" % ("the server is set up" if complete else "NOT complete, see the steps"),
            GREEN if complete else RED,
        )
        return out, (0 if complete else 2)

    # -- remote helpers ---------------------------------------------------------
    def sh(self, script: str, stdin: bytes | None = None, timeout: float = 300) -> str:
        """Run a script that must succeed; returns stdout. Raises StepFailed otherwise."""
        rc, out, err = self.remote.shell(PRELUDE + script, stdin, timeout)
        if rc != 0:
            raise StepFailed("remote command failed (exit %d): %s" % (rc, err.strip()[-1500:]))
        return out

    def server_command(self, params: Mapping[str, object]) -> str:
        """Shell command that runs hostserver.py with PARAMS on the server."""
        return "exec python3 -c %s %s\n" % (q(server_script()), q(json.dumps(params)))

    def probe_params(self, spec: str) -> dict[str, object]:
        # Where data may already be: the existing entry's remote_root, else the default
        # of older versions (hostserver adds the installed unit's --root).
        old = self.existing
        data_roots = [old["remote_root"]] if old is not None else ["~/" + OLD_REMOTE_ROOT]
        return {
            "mode": "probe",
            "root": spec,
            "code": CODE_FILE,
            "store": CODE_STORE,
            "version_file": pyz.VERSION_FILE,
            "unit": "%s/%s.service" % (UNIT_DIR, UNIT),
            "unit_name": UNIT,
            "data_roots": data_roots,
            "legacy": hostlegacy.probe_params(),
        }

    def run_probe(self, root_spec: str | None = None) -> Probe:
        spec = root_spec or self.probe.get("root") or DEFAULT_REMOTE_ROOT
        script = (
            "if ! command -v python3 >/dev/null 2>&1; then\n"
            + '  printf \'{"python": null, "uname": "%s"}\\n\' "$(uname -s)"; exit 0\n'
            + "fi\n"
            + self.server_command(self.probe_params(spec))
        )
        out = self.sh(script, timeout=120)
        try:
            obj = cast(JsonValue, json.loads(out.strip().splitlines()[-1]))
        except (ValueError, IndexError):
            raise StepFailed("the server probe printed no JSON: %r" % out[-500:]) from None
        if not isinstance(obj, dict):
            raise StepFailed("the server probe printed no JSON object")
        self.probe = cast(Probe, cast(object, obj))
        return self.probe

    def legacy_probe(self) -> LegacyProbe:
        return self.run_probe().get("legacy") or {}

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
        self._check_root_switch()
        if not self.ui.plan:  # a plan changes nothing, not even the lock file
            self._lock()
        return "ok", "%s, Python %s, systemd user instance ok" % (self.destination(), vtext)

    def _check_root_switch(self) -> None:
        """Data in another remote_root (the existing entry's, the installed unit's, or the
        default of older versions) would be orphaned: the unit is rewritten to this run's
        remote_root and the daemon restarted there. That needs a confirmation by hand;
        --yes does not give it."""
        p = self.probe
        root = self.root()
        others = list(p.get("data_roots") or [])
        old = os.path.normpath("%s/%s" % (self.p_home(), OLD_REMOTE_ROOT))
        if p.get("unit_root") not in (None, old):
            # a unit for another remote_root: the old default was left behind on purpose
            others = [d for d in others if d != old]
        if not others:
            return
        where = ", ".join(others)
        problem = (
            "%s already holds taskmgr data (config.json), but this run sets up remote_root " % where
            + "%s: the service would run on %s and no longer see the jobs in %s"
            % (root, root, where)
        )
        keep = "to keep using it, run again with %s=%s" % (REMOTE_ROOT_ENV, others[0])
        if self.ui.yes and not self.ui.plan:
            raise StepFailed(
                "%s. Nothing was changed; %s, or to use %s anyway run without --yes and "
                % (problem, keep, root)
                + "confirm"
            )
        self.ui.info("WARNING: %s; %s" % (problem, keep), YELLOW)
        self.ui.confirm(
            "use %s as remote_root; the data in %s stays where it is, unused" % (root, where)
        )

    # -- the server's setup lock --------------------------------------------------
    def _lock(self) -> None:
        """Take the setup lock on the server, held by a process there until _unlock."""
        script = PRELUDE + self.server_command({"mode": "lock"})
        env = self.remote._env()  # pyright: ignore[reportPrivateUsage]
        p = subprocess.Popen(
            self.remote.shell_argv(script),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self.locker = p
        assert p.stdout is not None
        ready, _, _ = select.select([p.stdout], [], [], 120)
        line = cast(bytes, p.stdout.readline()).decode(errors="replace").strip() if ready else ""
        if line.startswith("locked "):
            self.ui.info("holding the setup lock %s" % line[len("locked ") :])
            return
        err = self._unlock()
        if line.startswith("busy "):
            _, path, pid = (line.split(" ", 2) + ["?", "?"])[:3]
            raise StepFailed(
                "another `task create host` or `task update host` for this server is running "
                + "(pid %s on the server " % pid
                + "holds %s); wait for it to end, then run again" % path
            )
        raise StepFailed("could not take the setup lock on the server: %s" % (line or err))

    def _unlock(self) -> str:
        """Release the setup lock (if held); returns the locker's stderr."""
        p, self.locker = self.locker, None
        if p is None:
            return ""
        try:
            _, err = p.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()
            _, err = p.communicate()
        return err.decode(errors="replace").strip()[-500:]

    # -- 2. code ----------------------------------------------------------------
    def step_code(self) -> tuple[str, str]:
        self.version, self.pyz, self.files = prebuilt_code(
            self.args.pyz or pyz.default_remote_pyz()
        )
        self.ui.info("archive %s (version %s)" % (self.pyz, self.version[:12]))
        installed = self.probe.get("code_version")
        short = self.version[:12]
        self.ui.info("local version %s; installed: %s" % (short, (installed or "none")[:12]))
        stale = self._stale_uploads()
        if installed == self.version:  # no change, so no question: leftovers are only named
            left = (
                "; interrupted uploads left in %s: %s (removed with the next update)"
                % (self.store_path(), ", ".join(stale))
                if stale
                else ""
            )
            return "ok", "already up to date (%s)%s" % (short, left)
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
            + "%s (the previous archive is kept; once the service runs the new code, " % final
            + "older ones that no running job uses are removed)"
            + ("; remove interrupted uploads %s" % ", ".join(stale) if stale else "")
        )
        cleaned = self.uploads_removed = self._remove_uploads(stale)
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
        return "changed", "uploaded version %s to %s (installed after step data)%s" % (
            short,
            staged,
            cleaned,
        )

    def _stale_uploads(self) -> list[str]:
        """The names of what interrupted uploads left in CODE_STORE (the setup lock keeps
        uploads of other runs out; the age is a second guard)."""
        store = self.store_path()
        out = self.sh(
            "if [ -d %s ]; then find %s -maxdepth 1 -type f -name '*.new-*' -mmin +%d "
            % (q(store), q(store), STALE_UPLOAD_MINUTES)
            + "-print; fi\n"
        )
        return sorted(os.path.basename(line) for line in out.splitlines() if line.strip())

    def _remove_uploads(self, names: list[str]) -> str:
        """Remove the confirmed _stale_uploads(). Returns a detail suffix."""
        if not names:
            return ""
        store = self.store_path()
        _ = self.sh("rm -f -- %s\n" % " ".join(q("%s/%s" % (store, n)) for n in names))
        self.ui.info("removed interrupted uploads: %s" % ", ".join(names))
        return "; removed interrupted uploads %s" % ", ".join(names)

    def _install_staged(self) -> None:
        staged = self.staged
        assert staged is not None
        path, store, name = self.code_path(), self.store_path(), self.archive_name()
        final = "%s/%s" % (store, name)
        # The archive gets its final name (an existing file of this version stays as it is:
        # a process may be reading it), then a new link replaces `path` with one rename, so
        # `path` exists at every moment. Old archives are removed only in step service, once
        # the daemon runs this one (see _prune_code).
        script = (
            "set -e\n"
            + "prev=$(readlink %s 2>/dev/null || true)\n" % q(path)
            + "if [ -e %s ]; then rm -f %s; else mv %s %s; fi\n"
            % (q(final), q(staged), q(staged), q(final))
            # rm -f: a run cut off before the mv leaves the .link-new behind
            + "rm -f %s\n" % q(path + ".link-new")
            + "ln -s %s %s\n" % (q("%s/%s" % (CODE_STORE, name)), q(path + ".link-new"))
            + "mv -f %s %s\n" % (q(path + ".link-new"), q(path))
            + 'echo "prev=$prev"\n'
        )
        last = (self.sh(script).strip().splitlines() or [""])[-1]
        self.staged = None
        self.code_changed = True
        prev = os.path.basename(last[len("prev=") :]) if last.startswith("prev=") else ""
        p = self.run_probe()
        if p.get("code_version") != self.version:
            raise StepFailed(
                "after the switch %s reports version %r" % (path, p.get("code_version"))
            )
        detail = "installed version %s: %s -> %s%s" % (
            self.version[:12],
            path,
            final,
            self.uploads_removed,
        )
        if prev and prev != name and prev in (p.get("code_store") or []):
            self.previous_code = prev
            detail += "; previous archive kept: %s/%s" % (store, prev)
        self._set_code_detail(detail)

    def _set_code_detail(self, detail: str) -> None:
        self.ui.info("code: " + detail)
        for s in self.steps:
            if s["name"] == "code":
                s["detail"] = detail

    def _prune_code(self) -> None:
        """After the daemon was (re)started on the code installed in this run: remove the
        archives that are neither that code, the previous one, nor in use by a process
        (a running job's shim imports from the archive it was started with)."""
        keep = [self.archive_name()] + ([self.previous_code] if self.previous_code else [])
        params = {"mode": "prune", "store": self.store_path(), "keep": keep}
        out = self.sh(self.server_command(params), timeout=120)
        try:
            reply = cast(JsonValue, json.loads(out.strip().splitlines()[-1]))
        except (ValueError, IndexError):
            raise StepFailed("removing old archives printed no JSON: %r" % out[-500:]) from None
        removed = _strs(reply.get("removed") if isinstance(reply, dict) else None)
        if removed:
            detail = next(s["detail"] for s in self.steps if s["name"] == "code")
            self._set_code_detail("%s; removed older archives %s" % (detail, ", ".join(removed)))

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
            + "umask 077\n"  # remote_root is owner-only (docs/install.md)
            + "mkdir -p %s\n" % q(root)
            + "if [ -O %s ]; then chmod go-rwx %s; fi\n" % (q(root), q(root))
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
        if self.ui.plan and ({"code", "remote_root"} & self.planned):
            # The check runs `taskmgr-remote inspect` of the new code in the new root.
            return "skipped", "plan: checked once steps code and remote_root are done"
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
                    + "stop it, then run `task %s host %s` again" % (self.command, self.args.name)
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
            + "%s/%s/<time>/, then run `task %s host %s` again"
            % (self.root(), BACKUP_DIR, self.command, self.args.name)
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

    def _service_plan(self, p: Probe) -> list[str]:
        """--plan: what step service would do."""
        todo: list[str] = []
        if p.get("enabled") != "enabled":
            todo.append("`systemctl --user enable %s`" % UNIT)
        if p.get("active") != "active":
            todo.append("`systemctl --user start %s`" % UNIT)
        else:
            reason = self._stale_reason()
            if reason is None and {"code", "unit"} & self.planned:
                reason = "step %s would change" % " and ".join(
                    s for s in ("code", "unit") if s in self.planned
                )
            if reason is not None:
                todo.append("`systemctl --user restart %s` (%s)" % (UNIT, reason))
        if "code" in self.planned:
            todo.append("remove archives in %s that no running job uses" % self.store_path())
        return todo

    def step_service(self) -> tuple[str, str]:
        p = self.run_probe()
        if self.ui.plan:
            todo = self._service_plan(p)
            if todo:
                self.ui.info("would change: " + "; ".join(todo))
                raise Planned("; ".join(todo))
            return "ok", "%s is enabled and active (pid %s)" % (UNIT, p.get("main_pid"))
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
                    + "stop it, then run `task %s host %s` again" % (self.command, self.args.name)
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
        if self.code_changed:
            self._prune_code()
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
        if self.ui.plan and self.planned:
            return "skipped", "plan: verified after the planned changes are made"
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
        new = cast(JsonValue, json.loads(json.dumps(want)))

        def upsert(servers: list[JsonValue]) -> None:
            for i, s in enumerate(servers):
                if isinstance(s, dict) and s.get("name") == self.args.name:
                    servers[i] = new
                    return
            servers.append(new)

        edit_machines(self.home, upsert)
        return "changed", "%s %s in %s" % (
            "added" if old is None else "updated",
            self.args.name,
            self.machines_path,
        )

    # -- 10. code that older versions installed outside remote_root -------------------
    def step_old_code(self) -> tuple[str, str]:
        return hostlegacy.step_old_code(self)


def _canon(s: Server) -> str:
    return json.dumps(s, sort_keys=True)


def _str(v: JsonValue) -> str:
    return v if isinstance(v, str) else ""


def _strs(v: JsonValue) -> list[str]:
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _paths(entries: list[JsonDict]) -> str:
    return ", ".join(_str(e.get("path")) for e in entries)


def add_host(args: AddHostArgs, home: str) -> tuple[Mapping[str, object], int]:
    """`task create host`: (step summary, exit code). `home` is the local state dir."""
    return HostSetup(args, home).run()


def update_host(args: AddHostArgs, home: str) -> tuple[Mapping[str, object], int]:
    """`task update host`: `task create host` for a server already in the machine list, with its
    entry as it is (no ssh options; the update parser leaves them None). It installs the
    taskmgr-remote.pyz of this task's code version and restarts the service on it; the other
    steps only check, unless something drifted. (step summary, exit code)."""
    if not any(s["name"] == args.name for s in load_machines(home)):
        raise TaskmgrError(
            "unknown_host",
            "host %r is not in the machine list; `task create host %s --ssh USER@HOST` sets it up"
            % (args.name, args.name),
            host=args.name,
        )
    return HostSetup(args, home, command="update").run()
