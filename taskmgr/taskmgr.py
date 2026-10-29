"""`task`: the local taskmgr CLI (entry module).

Local state lives in $TASKMGR_HOME (default ~/.taskmgr):
    machines.json   machine list and connection configuration
    rr.json         round-robin cursor (updated under rr.lock)
"""

import argparse
import concurrent.futures
import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from typing import IO, TypedDict, cast

from . import package
from .common import (
    PROTOCOL_VERSION,
    SUBMISSION_KEY_RE,
    TASK_ID_RE,
    TaskmgrError,
    atomic_write_json,
    flocked,
    read_json,
    utcnow_iso,
)
from .records import JsonDict, JsonValue, LocalInjection, Problem, Server, SshConfig, TaskSpec

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "template")

# What every local command returns: (JSON object for stdout or None, exit code).
CmdResult = tuple[Mapping[str, object] | None, int]


class Args(argparse.Namespace):
    """Parsed command line: the union of every subcommand's attributes.

    The class defaults equal the parser defaults; argparse overwrites the attributes of the
    chosen subcommand (plus `home` and `cmd`).
    """

    home: str | None = None
    cmd: str = ""
    task_id: str = ""
    output: str | None = None
    path: str = ""
    host: str | None = None
    submission_key: str | None = None
    state: str | None = None
    jobid: str = ""
    wait: bool = False
    poll_interval: float = 5.0
    retry_timeout: float = 300.0
    timeout: float | None = None
    as_: str | None = None
    confirm: str | None = None
    stream: str = "stdout"
    follow: bool = False
    detail: bool = False
    color: str = "auto"


class HostError(TypedDict):
    """One unreachable server in a multi-server query."""

    host: str
    error: str
    code: str


class ConnError(TaskmgrError):
    def __init__(self, message: str, host: str | None = None) -> None:
        super().__init__("connection_error", message, 2, host)


def eprint(*a: object) -> None:
    print(*a, file=sys.stderr)


def _obj(v: JsonValue) -> JsonDict:
    """A JSON value the remote protocol guarantees to be an object."""
    if not isinstance(v, dict):
        raise TypeError("expected a JSON object in the remote reply, got %r" % (v,))
    return v


def _arr(v: JsonValue) -> list[JsonValue]:
    """A JSON value the remote protocol guarantees to be an array."""
    if not isinstance(v, list):
        raise TypeError("expected a JSON array in the remote reply, got %r" % (v,))
    return v


def _str(v: JsonValue, default: str) -> str:
    return v if isinstance(v, str) else default


def _remote_error(reply: JsonDict, host: str, with_details: bool) -> TaskmgrError:
    """Turn a remote {"error": {...}} reply into a TaskmgrError."""
    e = _obj(reply["error"])
    details = e.get("details") if with_details else None
    return TaskmgrError(
        _str(e.get("code", "remote_error"), "remote_error"),
        _str(e.get("message", ""), ""),
        2,
        host,
        details,
    )


# ---------------------------------------------------------------------------
# Machine list and transports


def home_dir(args: Args | None = None) -> str:
    h = (args.home if args is not None else None) or os.environ.get("TASKMGR_HOME") or "~/.taskmgr"
    return os.path.abspath(os.path.expanduser(h))


def load_machines(home: str) -> list[Server]:
    path = os.path.join(home, "machines.json")
    try:
        data = read_json(path)
    except ValueError as e:
        raise TaskmgrError("bad_machines", "%s is not valid JSON: %s" % (path, e))
    if data is None:
        raise TaskmgrError(
            "no_machines",
            "machine list %s does not exist (see taskmgr/deploy/machines.example.json)" % path,
        )
    servers = data.get("servers") if isinstance(data, dict) else data
    if not isinstance(servers, list):
        raise TaskmgrError("bad_machines", '%s must contain a "servers" array' % path)
    names: set[object] = set()
    for entry in servers:
        s = entry if isinstance(entry, dict) else None
        if s is None or not s.get("name") or s["name"] in names:
            raise TaskmgrError("bad_machines", "every server needs a unique name: %r" % (entry,))
        names.add(s["name"])
        if s.get("transport", "ssh") not in ("ssh", "local"):
            raise TaskmgrError(
                "bad_machines", "server %s: transport must be ssh or local" % s["name"]
            )
        if not s.get("remote_root") or not s.get("remote_command"):
            raise TaskmgrError(
                "bad_machines", "server %s needs remote_root and remote_command" % s["name"]
            )
    # Checked above: every entry is an object with name, remote_root and remote_command.
    return cast(list[Server], servers)


def find_server(servers: list[Server], name: str | None) -> Server:
    for s in servers:
        if s["name"] == name:
            return s
    raise TaskmgrError("unknown_host", "host %r is not in the machine list" % name, host=name)


def pick_round_robin(home: str, servers: list[Server]) -> Server:
    """Choose the next enabled server; cursor read+update happen under one flock."""
    enabled = [s for s in servers if s.get("enabled", True)]
    if not enabled:
        raise TaskmgrError("no_enabled_hosts", "no enabled server in the machine list")
    with flocked(os.path.join(home, "rr.lock")):
        state = read_json(os.path.join(home, "rr.json"), {}) or {}
        raw_cursor = state.get("cursor", 0) if isinstance(state, dict) else 0
        cursor = int(raw_cursor) if isinstance(raw_cursor, (int, float, str)) else 0
        chosen = enabled[cursor % len(enabled)]
        atomic_write_json(
            os.path.join(home, "rr.json"),
            {"cursor": cursor + 1, "last": chosen["name"], "updated_at": utcnow_iso()},
        )
    return chosen


class Remote:
    def __init__(self, server: Server) -> None:
        self.server: Server = server
        self.name: str = server["name"]

    def argv(self, args: Sequence[object]) -> list[str]:
        s = self.server
        base = shlex.split(s["remote_command"]) + [
            "--root",
            s["remote_root"],
            "--protocol-version",
            str(PROTOCOL_VERSION),
        ]
        base += [str(a) for a in args]
        if s.get("transport", "ssh") == "local":
            return base
        ssh: SshConfig = s.get("ssh") or {}
        cmd = ["ssh", "-o", "BatchMode=yes"]
        port = ssh.get("port")
        if port:
            cmd += ["-p", str(port)]
        identity_file = ssh.get("identity_file")
        if identity_file:
            cmd += ["-i", os.path.expanduser(identity_file)]
        cmd += list(ssh.get("options") or [])
        host = ssh.get("host") or self.name
        user = ssh.get("user")
        cmd += ["%s@%s" % (user, host) if user else host]
        cmd += ["--", shlex.join(base)]
        return cmd

    def _inject(self) -> None:
        # Test-only failure injection for transport "local" (Section 18.1).
        if self.server.get("transport") != "local":
            return
        inj: LocalInjection = self.server.get("local") or {}
        fail = inj.get("fail")
        if fail:
            raise ConnError(str(fail), self.name)
        f = inj.get("fail_if_exists")
        if f and os.path.exists(f):
            raise ConnError("injected connection failure (%s exists)" % f, self.name)

    def _is_conn_failure(self, rc: int) -> bool:
        return self.server.get("transport", "ssh") == "ssh" and rc == 255

    def call(
        self,
        args: Sequence[object],
        stdin_path: str | None = None,
        timeout: float | None = None,
    ) -> JsonDict:
        """Run a JSON subcommand and return its object; raise TaskmgrError/ConnError."""
        self._inject()
        upload = open(stdin_path, "rb") if stdin_path else None
        try:
            p = subprocess.run(
                self.argv(args),
                stdin=upload if upload is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise ConnError("remote call timed out", self.name)
        except OSError as e:
            raise ConnError("cannot run transport: %s" % e, self.name)
        finally:
            if upload is not None:
                upload.close()
        err = p.stderr.decode(errors="replace").strip()
        if self._is_conn_failure(p.returncode):
            raise ConnError("ssh: %s" % (err or "connection failed"), self.name)
        obj: JsonValue = None
        for line in reversed(p.stdout.decode(errors="replace").splitlines()):
            if line.strip():
                try:
                    obj = cast(JsonValue, json.loads(line))
                except ValueError:
                    pass
                break
        if not isinstance(obj, dict):
            raise ConnError(
                "no valid JSON from remote (exit %d): %s" % (p.returncode, err[-2000:]), self.name
            )
        reply = obj
        check_protocol(reply, self.name)
        if "error" in reply:
            raise _remote_error(reply, self.name, with_details=True)
        return reply

    def stream(self, args: Sequence[object], stdout: IO[bytes]) -> int:
        """Run a streaming subcommand (logs/fetch) writing to stdout; returns exit code."""
        self._inject()
        try:
            p = subprocess.run(
                self.argv(args), stdin=subprocess.DEVNULL, stdout=stdout, stderr=subprocess.PIPE
            )
        except OSError as e:
            raise ConnError("cannot run transport: %s" % e, self.name)
        err = p.stderr.decode(errors="replace").strip()
        if self._is_conn_failure(p.returncode):
            raise ConnError("ssh: %s" % (err or "connection failed"), self.name)
        if p.returncode != 0:
            obj: JsonValue = None
            for line in reversed(err.splitlines()):
                try:
                    obj = cast(JsonValue, json.loads(line))
                    break
                except ValueError:
                    continue
            if isinstance(obj, dict) and "error" in obj:
                check_protocol(obj, self.name)
                raise _remote_error(obj, self.name, with_details=False)
            raise TaskmgrError(
                "remote_error", "remote exited %d: %s" % (p.returncode, err[-2000:]), 2, self.name
            )
        return 0


def check_protocol(obj: JsonDict, host: str | None) -> None:
    v = obj.get("protocol_version")
    if v != PROTOCOL_VERSION:
        raise TaskmgrError(
            "protocol_mismatch",
            "remote protocol_version %r is incompatible with local %d" % (v, PROTOCOL_VERSION),
            2,
            host,
        )


def query_all(
    servers: list[Server], args: Sequence[object]
) -> tuple[dict[str, JsonDict], list[HostError]]:
    """Call a JSON subcommand on every server in parallel: ({name: obj}, [errors])."""
    results: dict[str, JsonDict] = {}
    errors: list[HostError] = []
    if not servers:
        return results, errors
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(servers))) as ex:
        futs: dict[concurrent.futures.Future[JsonDict], str] = {
            ex.submit(Remote(s).call, args): s["name"] for s in servers
        }
        for f in concurrent.futures.as_completed(futs):
            name = futs[f]
            try:
                results[name] = f.result()
            except TaskmgrError as e:
                errors.append({"host": name, "error": e.message, "code": e.code})
    order: list[str] = [s["name"] for s in servers]
    errors.sort(key=lambda e: order.index(e["host"]))
    return results, errors


# ---------------------------------------------------------------------------
# init / validate


def cmd_init(args: Args) -> CmdResult:
    if not TASK_ID_RE.match(args.task_id):
        raise TaskmgrError("bad_task_id", "task_id must match ^[A-Z]{2}-[0-9]{2,4}$")
    target = os.path.abspath(args.output or args.task_id)
    if os.path.lexists(target):
        raise TaskmgrError("exists", "target %s already exists; nothing was written" % target)
    _ = shutil.copytree(TEMPLATE_DIR, target, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("task.json", "TASK.md"):
        p = os.path.join(target, name)
        with open(p) as f:
            text = f.read().replace("{{TASK_ID}}", args.task_id)
        with open(p, "w") as f:
            _ = f.write(text)
    prog = os.path.join(target, "progress.json")
    with open(prog, "w") as f:
        json.dump(
            {
                "schema_version": 1,
                "stage": "not_started",
                "updated_at": utcnow_iso(),
                "percent": 0,
                "message": "not started",
            },
            f,
            indent=2,
        )
        _ = f.write("\n")
    return {"path": target, "task_id": args.task_id}, 0


def validate_path(path: str) -> tuple[list[Problem], TaskSpec | None, str | None]:
    """Validate a package directory or archive. Returns (errors, task, content_hash)."""
    if not os.path.exists(path):
        raise TaskmgrError("not_found", "path does not exist: %s" % path)
    if os.path.isdir(path):
        errors, task = package.validate_dir(path)
        return errors, task, (None if errors else package.content_hash(path))
    with tempfile.TemporaryDirectory(prefix="taskmgr-validate-") as tmp:
        try:
            skipped = package.safe_extract(path, tmp, skip_unsafe=True)
        except TaskmgrError as e:
            return [{"code": e.code, "message": e.message}], None, None
        unsafe: list[Problem] = [
            {
                "code": "unsafe_package",
                "message": "unsafe archive entry: %s" % s["reason"],
                "path": s["path"],
            }
            for s in skipped
        ]
        root = package.package_root(tmp)
        errors, task = package.validate_dir(root)
        errors = unsafe + errors  # report every error, not only the first unsafe entry
        return errors, task, (None if errors else package.content_hash(root))


def cmd_validate(args: Args) -> CmdResult:
    errors, task, chash = validate_path(args.path)
    if errors:
        return {"valid": False, "errors": errors}, 1
    assert task is not None  # validate_dir returns the task when there are no errors
    return {"valid": True, "task_id": task["task_id"], "content_hash": chash}, 0


# ---------------------------------------------------------------------------
# submit


def cmd_submit(args: Args) -> CmdResult:
    if args.submission_key and not args.host:
        raise TaskmgrError(
            "bad_args",
            "--submission-key requires --host (a round-robin retry could land on another server)",
        )
    if args.submission_key and not SUBMISSION_KEY_RE.match(args.submission_key):
        raise TaskmgrError("bad_args", "--submission-key must match ^[A-Za-z0-9._-]{1,128}$")
    path = os.path.abspath(args.path)
    errors, _, _ = validate_path(path)
    if errors:
        raise TaskmgrError(
            "invalid_package", "package validation failed (%d errors)" % len(errors), details=errors
        )
    home = home_dir(args)
    servers = load_machines(home)
    server = find_server(servers, args.host) if args.host else pick_round_robin(home, servers)
    key = args.submission_key or ("cli-" + secrets.token_hex(16))
    with tempfile.TemporaryDirectory(prefix="taskmgr-submit-") as tmp:
        if os.path.isdir(path):
            upload = os.path.join(tmp, "package.tar.gz")
            package.pack_dir(path, upload)
        else:
            upload = path
        remote = Remote(server)
        try:
            ack = remote.call(["receive", "--submission-key", key], stdin_path=upload)
        except (ConnError, KeyboardInterrupt) as e:
            msg = e.message if isinstance(e, ConnError) else "interrupted during upload"
            eprint(
                "submission outcome is uncertain on host %s (submission key %s): %s"
                % (server["name"], key, msg)
            )
            eprint("do NOT resubmit elsewhere; retry safely with:")
            eprint(
                "  task submit --host %s --submission-key %s %s"
                % (shlex.quote(server["name"]), shlex.quote(key), shlex.quote(args.path))
            )
            raise TaskmgrError(
                "submission_uncertain", msg, 2, server["name"], {"submission_key": key}
            )
    return {
        "host": server["name"],
        "jobid": ack["jobid"],
        "task_id": ack["task_id"],
        "submission_key": ack["submission_key"],
        "deduplicated": ack["deduplicated"],
        "content_hash": ack.get("content_hash"),
        "state": ack.get("state"),
    }, 0


# ---------------------------------------------------------------------------
# jobs / status


def cmd_jobs(args: Args) -> CmdResult:
    servers = load_machines(home_dir(args))
    if args.host:
        servers = [find_server(servers, args.host)]
    rargs = ["jobs"] + (["--state", args.state] if args.state else [])
    results, errors = query_all(servers, rargs)
    jobs: list[JsonDict] = []
    for s in servers:
        for j in _arr((results.get(s["name"]) or {}).get("jobs", [])):
            jobs.append({"host": s["name"], **_obj(j)})
    out: dict[str, object] = {"complete": not errors, "errors": errors, "jobs": jobs}
    code = 0
    if not results:
        out["error"] = {"code": "all_hosts_failed", "message": "no server could be queried"}
        code = 2
    if args.detail:
        return out, code
    # Table mode (the default): the table on stdout, problems on stderr.
    for e in errors:
        eprint("warning: %s unreachable, its jobs are not listed: %s" % (e["host"], e["error"]))
    if code:
        eprint("error: no server could be queried")
        return None, code
    _ = sys.stdout.write(render_jobs_table(jobs, use_color(args.color)))
    return None, 0


JOBS_COLUMNS = ("JOBID", "TASK_ID", "SUBMITTED", "HOST", "STATE")


# ANSI SGR codes per job state; anything not listed stays uncolored.
STATE_COLORS = {
    "queued": "33",  # yellow
    "running": "36",  # cyan
    "stopping": "35",  # magenta
    "unknown": "1;35",  # bold magenta: needs an operator
    "succeeded": "32",  # green
    "failed": "31",  # red
    "invalid_result": "31",
    "timed_out": "31",
    "cancelled": "90",  # gray
}


def use_color(mode: str) -> bool:
    """--color auto: only on a terminal, and not when NO_COLOR is set or TERM=dumb."""
    if mode in ("always", "never"):
        return mode == "always"
    return (
        sys.stdout.isatty()
        and "NO_COLOR" not in os.environ
        and os.environ.get("TERM", "") != "dumb"
    )


def _paint(text: str, sgr: str | None, color: bool) -> str:
    return "\033[%sm%s\033[0m" % (sgr, text) if color and sgr else text


def render_jobs_table(jobs: list[JsonDict], color: bool = False) -> str:
    """`task jobs` table: one row per job, oldest submission first."""
    rows = sorted(
        (
            (
                _str(j.get("jobid"), "-"),
                _str(j.get("task_id"), "-"),
                _str(j.get("submitted_at"), "-"),
                _str(j.get("host"), "-"),
                _str(j.get("state"), "-"),
            )
            for j in jobs
        ),
        key=lambda r: (r[2], r[3], r[0]),
    )
    widths = [max([len(c)] + [len(r[i]) for r in rows]) for i, c in enumerate(JOBS_COLUMNS)]

    def line(cells: tuple[str, ...], sgr: list[str | None]) -> str:
        # Pad the plain text first so escape codes never affect the alignment; the last
        # column is not padded, so there is no trailing whitespace.
        padded = [c.ljust(w) for c, w in zip(cells[:-1], widths)] + [cells[-1]]
        return "  ".join(_paint(c, s, color) for c, s in zip(padded, sgr)).rstrip() + "\n"

    out = line(JOBS_COLUMNS, ["1"] * len(JOBS_COLUMNS))  # bold header
    for r in rows:
        out += line(r, [None, None, None, None, STATE_COLORS.get(r[4])])
    return out


def render_status(host: str | None, st: JsonDict, complete: bool) -> dict[str, object]:
    j = _obj(st["job"])
    if st["terminal"]:
        out: dict[str, object] = dict(_obj(st["result"]))
        out["host"] = host
        out["complete"] = complete
        out["retry_of"] = j.get("retry_of")
        out["retried_as"] = j.get("retried_as", [])
        return out
    return {
        "host": host,
        "jobid": j["jobid"],
        "task_id": j["task_id"],
        "state": j["state"],
        "cancel_requested": j["cancel_requested"],
        "run": st.get("run"),
        "progress": st.get("progress"),
        "submitted_at": j["submitted_at"],
        "gpus": j["gpus"],
        "error": j.get("error"),
        "retry_of": j.get("retry_of"),
        "retried_as": j.get("retried_as", []),
        "complete": complete,
    }


def automatic_rerun(job: JsonDict) -> str | None:
    """The jobid of the automatic re-run of an interrupted job, if it has one."""
    err, later = job.get("error"), job.get("retried_as")
    if isinstance(err, dict) and err.get("kind") == "interrupted" and isinstance(later, list):
        if later and isinstance(later[-1], str):
            return later[-1]
    return None


def locate(args: Args, servers: list[Server]) -> tuple[Server, JsonDict, bool]:
    """Return (host, status, complete) per the table in Section 14.2."""
    if args.host:
        server = find_server(servers, args.host)
        st = Remote(server).call(["status", args.jobid])
        if not st.get("found"):
            raise TaskmgrError(
                "not_found", "job %s not found on %s" % (args.jobid, args.host), 2, args.host
            )
        return server, st, True
    results, errors = query_all(servers, ["status", args.jobid])
    matches = [(s, results[s["name"]]) for s in servers if results.get(s["name"], {}).get("found")]
    if len(matches) > 1:
        hosts = [s["name"] for s, _ in matches]
        raise TaskmgrError(
            "ambiguous",
            "job %s exists on several servers %s; use --host" % (args.jobid, hosts),
            2,
            details={"hosts": hosts},
        )
    if len(matches) == 1:
        if errors:
            eprint(
                "warning: some servers were unreachable (%s); uniqueness of %s could not be fully checked"
                % (", ".join(e["host"] for e in errors), args.jobid)
            )
        return matches[0][0], matches[0][1], not errors
    if errors:
        raise TaskmgrError(
            "incomplete",
            "job %s not found on reachable servers; unreachable: %s"
            % (args.jobid, ", ".join(e["host"] for e in errors)),
            2,
            details={"errors": errors},
        )
    raise TaskmgrError("not_found", "job %s not found on any server" % args.jobid)


def cmd_status(args: Args) -> CmdResult:
    servers = load_machines(home_dir(args))
    if not args.wait:
        server, st, complete = locate(args, servers)
        return render_status(server["name"], st, complete), 0
    return wait_status(args, servers)


def wait_status(args: Args, servers: list[Server]) -> CmdResult:
    jobid: str = args.jobid  # changes when the wait follows an automatic re-run
    t_start = time.monotonic()
    deadline: float | None = t_start + args.timeout if args.timeout is not None else None
    fail_since: float | None = None
    st: JsonDict | None = None
    server: Server | None = None
    complete = True
    while True:
        try:
            if server is None:
                server, st, complete = locate(args, servers)
            else:
                st = Remote(server).call(["status", jobid])
                if not st.get("found"):
                    raise TaskmgrError(
                        "not_found",
                        "job %s disappeared from %s" % (jobid, server["name"]),
                        2,
                        server["name"],
                    )
            fail_since = None
        except ConnError as e:
            if not args.host and server is None:
                raise  # the cross-server lookup happens once
            now = time.monotonic()
            fail_since = fail_since or now
            if now - fail_since >= args.retry_timeout:
                raise TaskmgrError(
                    "connection_error",
                    "giving up after %.0fs of connection failures: %s"
                    % (now - fail_since, e.message),
                    2,
                    e.host,
                )
            if deadline is not None and now >= deadline:
                host = server["name"] if server else args.host
                out: dict[str, object] = render_status(host, st, complete) if st else {}
                out["error"] = {
                    "code": "wait_timeout",
                    "message": "--timeout expired while the server was unreachable: %s" % e.message,
                    "host": host,
                }
                return out, 2
            eprint("connection failed (%s); retrying" % e.message)
            time.sleep(
                args.poll_interval
                if deadline is None
                else max(0.0, min(args.poll_interval, deadline - now))
            )
            continue
        job = _obj(st["job"])
        state = job["state"]
        if st["terminal"]:
            rerun = automatic_rerun(job)
            if rerun is not None:
                # Section 12.2 step 5: the re-run is a new job; keep waiting on it.
                eprint("job %s was interrupted; following automatic re-run %s" % (jobid, rerun))
                jobid = rerun
                continue
            out = render_status(server["name"], st, complete)
            return out, (0 if state == "succeeded" else 1)
        if state == "unknown":
            out = render_status(server["name"], st, complete)
            eprint("job %s is in the unknown state; see `task resolve`" % jobid)
            return out, 3
        if deadline is not None and time.monotonic() >= deadline:
            out = render_status(server["name"], st, complete)
            out["error"] = {
                "code": "wait_timeout",
                "message": "job still %s after %ss" % (state, args.timeout),
                "host": server["name"],
            }
            return out, 2
        time.sleep(
            args.poll_interval
            if deadline is None
            else max(0.0, min(args.poll_interval, deadline - time.monotonic()))
        )


# ---------------------------------------------------------------------------
# cancel / retry / resolve


def _host_call(args: Args, rargs: Sequence[object]) -> CmdResult:
    servers = load_machines(home_dir(args))
    server = find_server(servers, args.host)
    out = Remote(server).call(rargs)
    _ = out.pop("protocol_version", None)
    out["host"] = server["name"]
    return out, 0


def cmd_cancel(args: Args) -> CmdResult:
    return _host_call(args, ["cancel", args.jobid])


def cmd_retry(args: Args) -> CmdResult:
    return _host_call(args, ["retry", args.jobid])


def cmd_resolve(args: Args) -> CmdResult:
    rargs: list[object] = ["resolve", args.jobid]
    if args.as_:
        if args.confirm is None:
            raise TaskmgrError("bad_args", "--as stopped requires --confirm <jobid>")
        if args.confirm != args.jobid:
            raise TaskmgrError("bad_args", "--confirm must repeat the same jobid")
        rargs += ["--as", args.as_, "--confirm", args.confirm]
    return _host_call(args, rargs)


# ---------------------------------------------------------------------------
# logs / fetch


def cmd_logs(args: Args) -> CmdResult:
    servers = load_machines(home_dir(args))
    server = find_server(servers, args.host)
    rargs: list[object] = ["logs", args.jobid, "--stream", args.stream]
    if args.follow:
        rargs.append("--follow")
    _ = sys.stdout.flush()
    _ = Remote(server).stream(rargs, sys.stdout.buffer)
    return None, 0


def cmd_fetch(args: Args) -> CmdResult:
    servers = load_machines(home_dir(args))
    server = find_server(servers, args.host)
    remote = Remote(server)
    host: str = server["name"]
    jobid: str = args.jobid
    st = remote.call(["status", jobid])
    if not st.get("found"):
        raise TaskmgrError("not_found", "job %s not found on %s" % (jobid, host), 2, host)
    state = _obj(st["job"])["state"]
    if not st.get("has_run"):
        raise TaskmgrError(
            "no_run", "job %s has not run (%s); nothing to fetch" % (jobid, state), 2, host
        )
    output = args.output
    assert output is not None  # --output is required for fetch
    parent = os.path.join(os.path.abspath(output), host)
    target = os.path.join(parent, jobid)
    if os.path.lexists(target):
        raise TaskmgrError("exists", "target directory already exists: %s" % target)
    partial = not st["terminal"]
    if partial:
        eprint("warning: job %s is %s; fetched results may be incomplete" % (jobid, state))
    os.makedirs(parent, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".fetch-", dir=parent)
    try:
        tar_path = os.path.join(staging, "stream.tar.gz")
        with open(tar_path, "wb") as f:
            _ = remote.stream(["fetch", jobid], f)
        extracted = os.path.join(staging, "x")
        skipped = package.safe_extract(tar_path, extracted, skip_unsafe=True)
        for s in skipped:
            eprint("warning: skipped unsafe entry %s (%s)" % (s["path"], s["reason"]))
        if os.path.lexists(target):
            raise TaskmgrError("exists", "target directory already exists: %s" % target)
        os.rename(extracted, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {
        "host": host,
        "jobid": jobid,
        "state": state,
        "partial": partial,
        "path": target,
        "skipped": skipped,
    }, 0


def cmd_hosts(args: Args) -> CmdResult:
    servers = load_machines(home_dir(args))
    return {
        "servers": [
            {
                "name": s["name"],
                "enabled": s.get("enabled", True),
                "transport": s.get("transport", "ssh"),
                "remote_root": s["remote_root"],
            }
            for s in servers
        ]
    }, 0


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="task", description="GPU task manager (local CLI)")
    _ = p.add_argument("--home", help="local state directory (default $TASKMGR_HOME or ~/.taskmgr)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create a task package template")
    _ = s.add_argument("task_id")
    _ = s.add_argument("--output", help="target directory (default ./<task_id>)")
    s = sub.add_parser("validate", help="validate a task package directory or tar.gz")
    _ = s.add_argument("path")
    s = sub.add_parser("submit", help="submit a task package")
    _ = s.add_argument("path")
    _ = s.add_argument("--host")
    _ = s.add_argument("--submission-key")
    s = sub.add_parser("jobs", help="list jobs (a table; --detail for JSON)")
    _ = s.add_argument("--host")
    _ = s.add_argument("--state")
    _ = s.add_argument("--detail", action="store_true", help="print the full JSON object")
    _ = s.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="color the table (auto: only on a terminal, off with NO_COLOR)",
    )
    s = sub.add_parser("status", help="show (or wait for) one job")
    _ = s.add_argument("jobid")
    _ = s.add_argument("--host")
    _ = s.add_argument("--wait", action="store_true")
    _ = s.add_argument("--poll-interval", type=float, default=5.0)
    _ = s.add_argument("--retry-timeout", type=float, default=300.0)
    _ = s.add_argument("--timeout", type=float, default=None)
    for name in ("cancel", "retry"):
        s = sub.add_parser(name)
        _ = s.add_argument("jobid")
        _ = s.add_argument("--host", required=True)
    s = sub.add_parser("resolve", help="resolve a job in the unknown state")
    _ = s.add_argument("jobid")
    _ = s.add_argument("--host", required=True)
    _ = s.add_argument("--as", dest="as_", choices=("stopped",))
    _ = s.add_argument("--confirm")
    s = sub.add_parser("logs")
    _ = s.add_argument("jobid")
    _ = s.add_argument("--host", required=True)
    _ = s.add_argument("--stream", choices=("stdout", "stderr", "shim"), default="stdout")
    _ = s.add_argument("--follow", action="store_true")
    s = sub.add_parser("fetch")
    _ = s.add_argument("jobid")
    _ = s.add_argument("--host", required=True)
    _ = s.add_argument("--output", required=True)
    _ = sub.add_parser("hosts", help="list configured servers")
    return p


COMMANDS: dict[str, Callable[[Args], CmdResult]] = {
    "init": cmd_init,
    "validate": cmd_validate,
    "submit": cmd_submit,
    "jobs": cmd_jobs,
    "status": cmd_status,
    "cancel": cmd_cancel,
    "retry": cmd_retry,
    "resolve": cmd_resolve,
    "logs": cmd_logs,
    "fetch": cmd_fetch,
    "hosts": cmd_hosts,
}


def text_output(args: Args) -> bool:
    """Commands whose stdout is text (logs, the jobs table): errors go to stderr only."""
    return args.cmd == "logs" or (args.cmd == "jobs" and not args.detail)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv, namespace=Args())
    except SystemExit as e:  # argparse errors are parameter errors: exit 2
        if e.code == 0:
            return 0
        _ = sys.stdout.write(
            json.dumps({"error": {"code": "bad_args", "message": "invalid arguments (see stderr)"}})
            + "\n"
        )
        return 2
    try:
        out, code = COMMANDS[args.cmd](args)
    except TaskmgrError as e:
        if text_output(args):
            eprint(json.dumps(e.to_json()))
            return e.exit_code
        out, code = e.to_json(), e.exit_code
        eprint("error: %s" % e.message)
    except KeyboardInterrupt:
        eprint("interrupted (remote jobs are not affected)")
        if not text_output(args):
            _ = sys.stdout.write(
                json.dumps({"error": {"code": "interrupted", "message": "interrupted locally"}})
                + "\n"
            )
        return 2
    except Exception as e:  # noqa: BLE001 - keep the JSON / exit-code contract
        import traceback

        eprint(traceback.format_exc())
        err = {"error": {"code": "internal_error", "message": "%s: %s" % (type(e).__name__, e)}}
        if text_output(args):
            eprint(json.dumps(err))
        else:
            _ = sys.stdout.write(json.dumps(err) + "\n")
        return 2
    if out is not None:
        _ = sys.stdout.write(json.dumps(out, indent=2, sort_keys=True) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
