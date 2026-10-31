"""`task`: the local taskmgr CLI (entry module).

Local state lives in $TASKMGR_HOME (default ~/.taskmgr):
    machines.json   machine list and connection configuration
    rr.json         round-robin cursor (updated under rr.lock)
"""

import argparse
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from collections.abc import Callable, Mapping, Sequence
from typing import NamedTuple, NoReturn, cast

from . import hostcmds, package
from .common import (
    CODE_ROOT,
    PROTOCOL_VERSION,
    SUBMISSION_KEY_RE,
    TASK_ID_RE,
    TaskmgrError,
    atomic_write_json,
    code_is_zipapp,
    copy_package_tree,
    flocked,
    read_code_file,
    read_json,
    utcnow_iso,
)
from .pyz import code_version as local_code_version
from .records import JsonDict, JsonValue, Problem, Server, TaskSpec, override
from .textout import (
    BOLD,
    CYAN,
    GPU_BUSY_STATE,
    GRAY,
    GREEN,
    RED,
    STATE_COLORS,
    YELLOW,
    CmdResult,
    color_on,
    error_text,
    hint,
    json_text,
    message,
    note,
    paint,
    render_fields,
    render_table,
    say,
    set_color,
    warn,
)
from .textout import (
    message as message_line,
)
from .transport import ConnError, Remote, load_machines, query_all
from .transport import json_obj as _obj
from .transport import json_str as _str


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
    plain: bool = False
    # host add
    host_cmd: str = ""
    name: str = ""
    ssh: str | None = None
    port: int | None = None
    identity_file: str | None = None
    remote_root: str | None = None
    ssh_option: list[str] | None = None
    enabled: bool | None = None
    yes: bool = False


def _arr(v: JsonValue) -> list[JsonValue]:
    """A JSON value the remote protocol guarantees to be an array."""
    if not isinstance(v, list):
        raise TypeError("expected a JSON array in the remote reply, got %r" % (v,))
    return v


# ---------------------------------------------------------------------------
# Machine list and transports


def home_dir(args: Args | None = None) -> str:
    h = (args.home if args is not None else None) or os.environ.get("TASKMGR_HOME") or "~/.taskmgr"
    return os.path.abspath(os.path.expanduser(h))


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


# ---------------------------------------------------------------------------
# init / validate


def cmd_init(args: Args) -> CmdResult:
    if not TASK_ID_RE.match(args.task_id):
        raise TaskmgrError("bad_task_id", "task_id must match ^[A-Z]{2}-[0-9]{2,4}$")
    target = os.path.abspath(args.output or args.task_id)
    if os.path.lexists(target):
        raise TaskmgrError("exists", "target %s already exists; nothing was written" % target)
    _ = copy_package_tree("template", target, ("__pycache__", "*.pyc"))
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
            note(
                "uncertain",
                YELLOW,
                "submission outcome on host %s (submission key %s): %s"
                % (server["name"], key, msg),
            )
            note(
                "hint",
                CYAN,
                "do NOT resubmit elsewhere; retry safely with `task submit --host %s "
                % shlex.quote(server["name"])
                + "--submission-key %s %s`" % (shlex.quote(key), shlex.quote(args.path)),
            )
            raise TaskmgrError(
                "submission_uncertain", msg, 2, server["name"], {"submission_key": key}
            ) from e
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


def query_targets(servers: list[Server], host: str | None) -> tuple[list[Server], list[str]]:
    """The servers a query asks, and the names of the disabled ones it skips: --host names
    one server (disabled or not); otherwise every enabled server."""
    if host:
        return [find_server(servers, host)], []
    enabled = [s for s in servers if s.get("enabled", True)]
    skipped = [s["name"] for s in servers if not s.get("enabled", True)]
    if servers and not enabled:
        raise TaskmgrError(
            "no_enabled_hosts",
            "every server is disabled (%s); use --host NAME or `task host enable NAME`"
            % ", ".join(skipped),
        )
    return enabled, skipped


def cmd_jobs(args: Args) -> CmdResult:
    servers, skipped = query_targets(load_machines(home_dir(args)), args.host)
    rargs = ["jobs"] + (["--state", args.state] if args.state else [])
    results, errors = query_all(servers, rargs)
    jobs: list[JsonDict] = []
    for s in servers:
        for j in _arr((results.get(s["name"]) or {}).get("jobs", [])):
            jobs.append({"host": s["name"], **_obj(j)})
    out: dict[str, object] = {
        "complete": not errors,
        "errors": errors,
        "skipped": skipped,
        "jobs": jobs,
    }
    code = 0
    if not results:
        out["error"] = {"code": "all_hosts_failed", "message": "no server could be queried"}
        code = 2
    return out, code


JOBS_COLUMNS = ("JOBID", "TASK_ID", "SUBMITTED", "HOST", "STATE")


def display_state(job: JsonDict) -> str:
    """The STATE cell: the job state, or GPU_BUSY_STATE for a running job that waits."""
    state = _str(job.get("state"), "-")
    if state == "running" and job.get("pending_reason"):
        return GPU_BUSY_STATE
    return state


def render_jobs_table(jobs: list[JsonDict], color: bool = False) -> str:
    """`task jobs` table: one row per job, oldest submission first."""
    rows = sorted(
        (
            (
                _str(j.get("jobid"), "-"),
                _str(j.get("task_id"), "-"),
                _str(j.get("submitted_at"), "-"),
                _str(j.get("host"), "-"),
                display_state(j),
            )
            for j in jobs
        ),
        key=lambda r: (r[2], r[3], r[0]),
    )
    return render_table(JOBS_COLUMNS, rows, color)


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
        "pending_reason": j.get("pending_reason"),
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
    if (
        isinstance(err, dict)
        and err.get("kind") == "interrupted"
        and isinstance(later, list)
        and later
        and isinstance(later[-1], str)
    ):
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
    servers, skipped = query_targets(servers, None)
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
            warn(
                "some servers were unreachable (%s); uniqueness of %s could not be fully checked"
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
    if skipped:
        raise TaskmgrError(
            "not_found",
            "job %s not found on any enabled server; disabled, not searched: %s (use --host)"
            % (args.jobid, ", ".join(skipped)),
            details={"skipped": skipped},
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
                ) from e
            if deadline is not None and now >= deadline:
                host = server["name"] if server else args.host
                out: dict[str, object] = render_status(host, st, complete) if st else {}
                out["error"] = {
                    "code": "wait_timeout",
                    "message": "--timeout expired while the server was unreachable: %s" % e.message,
                    "host": host,
                }
                return out, 2
            note("retrying", YELLOW, "connection failed (%s)" % e.message)
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
                note(
                    "following",
                    CYAN,
                    "job %s was interrupted; following automatic re-run %s" % (jobid, rerun),
                )
                jobid = rerun
                continue
            out = render_status(server["name"], st, complete)
            return out, (0 if state == "succeeded" else 1)
        if state == "unknown":
            out = render_status(server["name"], st, complete)
            note(
                "unknown",
                STATE_COLORS["unknown"],
                "job %s is in the unknown state; see `task resolve`" % jobid,
            )
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


def job_server(args: Args) -> tuple[Server, JsonDict]:
    """The server holding args.jobid (--host, or a lookup on every server) and its status."""
    server, st, _ = locate(args, load_machines(home_dir(args)))
    return server, st


def _host_call(args: Args, rargs: Sequence[object]) -> CmdResult:
    server, _ = job_server(args)
    out = Remote(server).call(rargs)
    _ = out.pop("protocol_version", None)
    out["host"] = server["name"]
    return out, 0


def cmd_cancel(args: Args) -> CmdResult:
    return _host_call(args, ["cancel", args.jobid])


RETRY_COLUMNS = ("JOBID", "RETRY_OF", "HOST", "STATE")


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
    server, _ = job_server(args)
    rargs: list[object] = ["logs", args.jobid, "--stream", args.stream]
    if args.follow:
        rargs.append("--follow")
    _ = sys.stdout.flush()
    _ = Remote(server).stream(rargs, sys.stdout.buffer)
    return None, 0


def cmd_fetch(args: Args) -> CmdResult:
    server, st = job_server(args)
    remote = Remote(server)
    host: str = server["name"]
    jobid: str = args.jobid
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
        warn("job %s is %s; fetched results may be incomplete" % (jobid, state))
    os.makedirs(parent, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".fetch-", dir=parent)
    try:
        tar_path = os.path.join(staging, "stream.tar.gz")
        with open(tar_path, "wb") as f:
            _ = remote.stream(["fetch", jobid], f)
        extracted = os.path.join(staging, "x")
        skipped = package.safe_extract(tar_path, extracted, skip_unsafe=True)
        for s in skipped:
            warn("skipped unsafe entry %s (%s)" % (s["path"], s["reason"]))
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


# ---------------------------------------------------------------------------
# host / whoami / update / version


def cmd_host(args: Args) -> CmdResult:
    return hostcmds.run(args, home_dir(args))


def _per_host(
    servers: list[Server], host: str | None, rargs: Sequence[object], key: str
) -> CmdResult:
    """Call rargs on the query targets: {key: [reply + host], "errors", "skipped",
    "complete"}."""
    servers, skipped = query_targets(servers, host)
    results, errors = query_all(servers, rargs)
    rows: list[dict[str, object]] = []
    for s in servers:
        r = results.get(s["name"])
        if r is not None:
            _ = r.pop("protocol_version", None)
            _ = r.pop("ok", None)
            rows.append({"host": s["name"], **r})
    out: dict[str, object] = {
        key: rows,
        "errors": errors,
        "skipped": skipped,
        "complete": not errors,
    }
    if servers and not results:
        out["error"] = {"code": "all_hosts_failed", "message": "no server could be queried"}
        return out, 2
    return out, 0


def cmd_whoami(args: Args) -> CmdResult:
    return _per_host(load_machines(home_dir(args)), args.host, ["version"], "hosts")


REPO_DIR = CODE_ROOT  # the checkout, or the .pyz file


def _git(*argv: str) -> subprocess.CompletedProcess[str] | None:
    """Run git in the checkout; None when git is not installed or this is a .pyz."""
    if code_is_zipapp():
        return None
    try:
        return subprocess.run(
            ["git", "-C", REPO_DIR, *argv], capture_output=True, text=True, timeout=300
        )
    except FileNotFoundError:
        return None


def _git_out(*argv: str) -> str | None:
    p = _git(*argv)
    return p.stdout.strip() if p is not None and p.returncode == 0 else None


def local_version() -> dict[str, object]:
    """Version of this checkout: pyproject version, git commit, and the code version
    `task host add` installs (comparable with a server's code_version)."""
    version = None
    pyproject = read_code_file("pyproject.toml")  # a .pyz carries a copy
    if pyproject is not None:
        m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M)
        version = m.group(1) if m else None
    commit = _git_out("rev-parse", "--short=12", "HEAD")
    if commit is None:  # a .pyz records the commit it was built from
        commit = (read_code_file(".taskmgr-commit") or "").strip() or None
    dirty = _git_out("status", "--porcelain", "--untracked-files=no")
    code_version = local_code_version()
    return {
        "version": version,
        "commit": commit,
        "dirty": None if dirty is None else bool(dirty),
        "code_version": code_version,
        "protocol_version": PROTOCOL_VERSION,
        "path": REPO_DIR,
    }


def cmd_version(args: Args) -> CmdResult:
    local = local_version()
    out, code = _per_host(load_machines(home_dir(args)), args.host, ["version"], "hosts")
    assert out is not None
    for h in _rows(out, "hosts"):
        h["up_to_date"] = h.get("code_version") == local["code_version"]
    return {"local": local, **out}, code


def cmd_update(_args: Args) -> CmdResult:
    """Update this checkout with `git pull --ff-only`; servers are updated by `task host add`."""
    if code_is_zipapp():
        raise TaskmgrError(
            "not_a_checkout",
            "%s is a zipapp; build a new one from a checkout (python3 -m taskmgr.pyz)" % REPO_DIR,
        )
    before = _git_out("rev-parse", "--short=12", "HEAD")
    if before is None:
        raise TaskmgrError(
            "not_a_checkout", "%s is not a git checkout (or git is missing)" % REPO_DIR
        )
    p = _git("pull", "--ff-only")
    assert p is not None  # git ran just above
    if p.returncode != 0:
        raise TaskmgrError(
            "update_failed", "git pull --ff-only failed in %s:\n%s" % (REPO_DIR, p.stderr.strip())
        )
    after = _git_out("rev-parse", "--short=12", "HEAD")
    return {"path": REPO_DIR, "before": before, "after": after, "changed": before != after}, 0


def _rows(out: Mapping[str, object], key: str) -> list[dict[str, object]]:
    """out[key] as a list of objects (JSON arrays of objects in command results)."""
    v = out.get(key)
    if not isinstance(v, list):
        return []
    return [cast(dict[str, object], r) for r in cast(list[object], v) if isinstance(r, dict)]


def _as_dict(v: object) -> dict[str, object]:
    return cast(dict[str, object], v) if isinstance(v, dict) else {}


# ---------------------------------------------------------------------------
# Text output (the default; --detail prints the JSON object)


def _s(v: object, default: str = "-") -> str:
    if v is None or v == "" or v == []:
        return default
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (list, dict)):
        return json.dumps(v, sort_keys=True)
    return str(v)


def _note_skipped(out: Mapping[str, object]) -> None:
    v = out.get("skipped")
    names = [str(n) for n in cast(list[object], v)] if isinstance(v, list) else []
    if names:
        note("skipped", YELLOW, "disabled, not queried: %s" % ", ".join(names))


def _warn_unreachable(out: Mapping[str, object], what: str, color: bool) -> None:
    for e in _rows(out, "errors"):
        _ = color  # warn() follows the run's color setting
        warn("%s unreachable, %s: %s" % (e.get("host"), what, e.get("error")))


Fields = list[tuple[str, str, str | None]]


def text_init(_args: Args, out: Mapping[str, object], color: bool) -> str:
    return message("created", GREEN, "%s in %s" % (out["task_id"], out["path"]), color) + hint(
        "fill every <REQUIRED...> marker, then run `task validate %s`"
        % shlex.quote(str(out["path"])),
        color,
    )


def text_validate(_args: Args, out: Mapping[str, object], color: bool) -> str:
    if out.get("valid"):
        return message(
            "valid",
            GREEN,
            "%s, content_hash %s" % (out.get("task_id"), out.get("content_hash")),
            color,
        )
    errors = _rows(out, "errors")
    lines = [
        message(
            "invalid", RED, "%d error%s" % (len(errors), "" if len(errors) == 1 else "s"), color
        ).rstrip("\n")
    ]
    for e in errors:
        where = " %s:" % e["path"] if e.get("path") else ""
        lines.append("  %s%s %s" % (paint(_s(e.get("code")), BOLD, color), where, e.get("message")))
    return "\n".join(lines) + "\n"


def text_submit(_args: Args, out: Mapping[str, object], color: bool) -> str:
    row = [_s(out.get(k)) for k in ("jobid", "task_id", "host", "state")]
    text = render_table(("JOBID", "TASK_ID", "HOST", "STATE"), [row], color)
    if out.get("deduplicated"):
        text += message(
            "note",
            YELLOW,
            "already submitted with submission key %s; no new job" % out.get("submission_key"),
            color,
        )
    return text


def text_jobs(_args: Args, out: Mapping[str, object], color: bool) -> str:
    _note_skipped(out)
    _warn_unreachable(out, "its jobs are not listed", color)
    if out.get("error"):
        return ""
    return render_jobs_table(cast(list[JsonDict], _rows(out, "jobs")), color)


def _error_field(err: object) -> Fields:
    e = _as_dict(err)
    return [("error", "%s: %s" % (e.get("kind"), e.get("message")), RED)] if "kind" in e else []


def text_status(_args: Args, out: Mapping[str, object], color: bool) -> str:
    meta = _as_dict(out.get("_taskmgr"))
    if meta:  # terminal: the job's result.json
        state = _s(meta.get("status"))
        fields: Fields = [
            ("jobid", _s(meta.get("jobid")), None),
            ("task_id", _s(meta.get("task_id")), None),
            ("host", _s(out.get("host")), None),
            ("state", state, STATE_COLORS.get(state)),
            ("return_code", _s(meta.get("return_code")), None),
            ("started_at", _s(meta.get("started_at")), None),
            ("ended_at", _s(meta.get("ended_at")), None),
        ] + _error_field(meta.get("error"))
        for k in ("validation_errors", "warnings"):
            if meta.get(k):
                fields.append((k, _s(meta.get(k)), YELLOW))
        skip = ("host", "complete", "retry_of", "retried_as")
        keys = sorted(k for k in out if not k.startswith("_") and k not in skip)
        fields.append(("result", ", ".join(keys) or "-", None))
    else:
        state = display_state(cast(JsonDict, out))
        fields = [
            ("jobid", _s(out.get("jobid")), None),
            ("task_id", _s(out.get("task_id")), None),
            ("host", _s(out.get("host")), None),
            ("state", state, STATE_COLORS.get(state)),
            ("submitted_at", _s(out.get("submitted_at")), None),
            ("gpus", _s(out.get("gpus")), None),
        ]
        if out.get("pending_reason"):
            fields.append(("pending_reason", _s(out.get("pending_reason")), YELLOW))
        if out.get("cancel_requested"):
            fields.append(("cancel_requested", "yes", YELLOW))
        prog = _as_dict(out.get("progress"))
        if prog:
            pct = prog.get("percent")
            parts = (_s(prog.get("stage"), ""), "" if pct is None else "%s%%" % pct)
            msg = " ".join(x for x in parts + (_s(prog.get("message"), ""),) if x)
            fields.append(("progress", msg or "-", None))
        fields += _error_field(out.get("error"))
    if out.get("retry_of"):
        fields.append(("retry_of", _s(out.get("retry_of")), None))
    retried = out.get("retried_as")
    if isinstance(retried, list) and retried:
        fields.append(("retried_as", ", ".join(map(str, cast(list[object], retried))), None))
    if out.get("complete") is False:
        fields.append(("note", "some servers were unreachable", YELLOW))
    return render_fields(fields, color)


def text_cancel(_args: Args, out: Mapping[str, object], color: bool) -> str:
    state = _s(out.get("state"))
    if out.get("changed"):
        label, sgr = "cancel requested", GREEN
    elif out.get("cancel_requested"):
        label, sgr = "cancel already requested", YELLOW
    else:
        label, sgr = "nothing to cancel", YELLOW
    job = "job %s on %s is %s" % (
        out.get("jobid"),
        out.get("host"),
        paint(state, STATE_COLORS.get(state), color),
    )
    return message(label, sgr, job, color)


def text_retry(_args: Args, out: Mapping[str, object], color: bool) -> str:
    row = [_s(out.get(k)) for k in ("jobid", "retry_of", "host", "state")]
    return render_table(("JOBID", "RETRY_OF", "HOST", "STATE"), [row], color)


def text_resolve(_args: Args, out: Mapping[str, object], color: bool) -> str:
    state = _s(out.get("state"))
    fields: Fields = [
        ("jobid", _s(out.get("jobid")), None),
        ("host", _s(out.get("host")), None),
        ("state", state, STATE_COLORS.get(state)),
        ("changed", _s(out.get("changed")), None),
    ]
    for k, v in sorted(out.items()):
        if k not in ("jobid", "host", "state", "changed"):
            fields.append((k, _s(v), None))
    return render_fields(fields, color)


def text_fetch(_args: Args, out: Mapping[str, object], color: bool) -> str:
    where = "job %s from %s into %s" % (out.get("jobid"), out.get("host"), out.get("path"))
    text = message("fetched", GREEN, where, color)
    if out.get("partial"):
        text += message("partial", YELLOW, "the job is still %s" % out.get("state"), color)
    for s in _rows(out, "skipped"):
        why = "unsafe entry %s (%s)" % (s.get("path"), s.get("reason"))
        text += message("skipped", YELLOW, why, color)
    return text


def text_whoami(_args: Args, out: Mapping[str, object], color: bool) -> str:
    _note_skipped(out)
    _warn_unreachable(out, "not shown", color)
    hosts = _rows(out, "hosts")
    rows = [[_s(h.get("host")), _s(h.get("user")), _s(h.get("hostname"))] for h in hosts]
    text = render_table(("HOST", "USER", "HOSTNAME"), rows, color, {}) if rows else ""
    if any("user" not in h for h in hosts):
        text += hint("- means the server runs older code; `task host add NAME` updates it", color)
    return text


def text_version(_args: Args, out: Mapping[str, object], color: bool) -> str:
    loc = _as_dict(out.get("local"))
    commit = _s(loc.get("commit"))
    if loc.get("dirty"):
        commit += " (uncommitted changes)"
    text = render_fields(
        [
            ("local", "taskmgr %s" % _s(loc.get("version")), None),
            ("commit", commit, YELLOW if loc.get("dirty") else None),
            ("code_version", _s(loc.get("code_version"))[:12], None),
            ("protocol", _s(loc.get("protocol_version")), None),
        ],
        color,
    )
    _note_skipped(out)
    _warn_unreachable(out, "version unknown", color)
    hosts = _rows(out, "hosts")
    if hosts:
        rows = [
            [
                _s(h.get("host")),
                _s(h.get("code_version"), "none")[:12],
                "up to date" if h.get("up_to_date") else "differs",
            ]
            for h in hosts
        ]
        text += "\n" + render_table(
            ("HOST", "CODE_VERSION", "STATUS"),
            rows,
            color,
            {2: {"up to date": GREEN, "differs": YELLOW}},
        )
        if any(not h.get("up_to_date") for h in hosts):
            text += hint("`task host add NAME` installs the local version on a server", color)
    return text


def text_update(_args: Args, out: Mapping[str, object], color: bool) -> str:
    if not out.get("changed"):
        return message("up to date", GREEN, "%s at %s" % (out["path"], out["after"]), color)
    return message(
        "updated", GREEN, "%s from %s to %s" % (out["path"], out["before"], out["after"]), color
    ) + hint("servers keep their code until `task host add NAME` (see `task version`)", color)


def text_host(args: Args, out: Mapping[str, object], color: bool) -> str:
    return hostcmds.render(args, out, color)


# ---------------------------------------------------------------------------
# Command line


class Command(NamedTuple):
    name: str
    group: str  # heading in `task -h`
    help: str
    run: Callable[[Args], CmdResult]
    text: Callable[[Args, Mapping[str, object], bool], str] | None  # None: streams text itself


COMMANDS = [
    Command("init", "tasks", "create a task package template", cmd_init, text_init),
    Command(
        "validate", "tasks", "check a task package directory or tar.gz", cmd_validate, text_validate
    ),
    Command("submit", "tasks", "submit a task package to a server", cmd_submit, text_submit),
    Command("jobs", "tasks", "list the jobs on every server", cmd_jobs, text_jobs),
    Command("status", "tasks", "show one job, or wait for it to end", cmd_status, text_status),
    Command("logs", "tasks", "print a job's stdout, stderr or shim log", cmd_logs, None),
    Command("fetch", "tasks", "download a job's results", cmd_fetch, text_fetch),
    Command("cancel", "tasks", "cancel a job", cmd_cancel, text_cancel),
    Command("retry", "tasks", "run a finished job again as a new job", cmd_retry, text_retry),
    Command("resolve", "tasks", "resolve a job in the unknown state", cmd_resolve, text_resolve),
    Command("host", "hosts", "manage the machine list", cmd_host, text_host),
    Command(
        "whoami", "others", "the user and hostname each server runs as", cmd_whoami, text_whoami
    ),
    Command(
        "update", "others", "update this local CLI (git pull --ff-only)", cmd_update, text_update
    ),
    Command(
        "version",
        "others",
        "local version and the code version on each server",
        cmd_version,
        text_version,
    ),
]
BY_NAME = {c.name: c for c in COMMANDS}


HelpEntry = tuple[str, str]  # (name column, help text)
HELP_WIDTH = 88


def render_help(
    usage: str, description: str, sections: Sequence[tuple[str, Sequence[HelpEntry]]], footer: str
) -> str:
    """The layout of every `-h`: usage line, description, then bold sections of cyan names
    and help text in one aligned column, then a footer line."""
    width = max((len(n) for _, entries in sections for n, _ in entries), default=0) + 2
    text = usage_line(usage) + "\n"
    if description:
        text += textwrap.fill(description, HELP_WIDTH) + "\n\n"
    for title, entries in sections:
        if not entries:
            continue
        text += paint(title, BOLD) + "\n"
        for name, help_text in entries:
            body = textwrap.wrap(help_text, max(20, HELP_WIDTH - width - 2)) or [""]
            text += "  %s%s\n" % (paint(name.ljust(width), CYAN), body[0])
            text += "".join("  %s%s\n" % (" " * width, more) for more in body[1:])
        text += "\n"
    return text + footer + "\n"


def usage_line(usage: str) -> str:
    """`usage: <usage>`; long lines wrap under the first argument."""
    words = _usage_words(usage)
    n_prog = next((i for i, w in enumerate(words) if w.startswith(("<", "["))), len(words))
    indent = " " * (len(" ".join(words[:n_prog])) + 1)
    lines: list[str] = []
    cur = ""
    for w in words:
        if cur.strip() and len(cur) + 1 + len(w) > HELP_WIDTH - 7:
            lines.append(cur)
            cur = indent + w
        else:
            cur = (cur + " " + w) if cur else w
    lines.append(cur)
    return "%s %s\n" % (paint("usage:", BOLD), ("\n" + " " * 7).join(lines))


def _usage_words(usage: str) -> list[str]:
    """Split a usage string into words, keeping `[--opt VALUE]` groups together."""
    words: list[str] = []
    depth = 0
    for w in usage.split(" "):
        if depth and words:
            words[-1] += " " + w
        else:
            words.append(w)
        depth += w.count("[") - w.count("]")
    return words


def top_help() -> str:
    """`task -h`: the commands by group."""
    groups: dict[str, list[HelpEntry]] = {}
    for c in COMMANDS:
        entries = groups.setdefault(c.group, [])
        if c.name == "host":
            entries.extend(("host " + n, h) for n, h in hostcmds.HELP)
        else:
            entries.append((c.name, c.help))
    groups["options"] = [
        ("--detail", DETAIL_HELP + " (all but logs)"),
        ("--plain", PLAIN_HELP),
        ("--home DIR", "local state directory (default $TASKMGR_HOME or ~/.taskmgr)"),
        ("-h, --help", "show this help"),
    ]
    return render_help(
        "task <command> [args]",
        "Run task packages on GPU servers and follow their jobs.",
        list(groups.items()),
        "`task <command> -h` shows a command's arguments and options.",
    )


DETAIL_HELP = "print the full JSON object instead of text"
PLAIN_HELP = "no colors (also NO_COLOR=1)"


def _opt_value(a: argparse.Action) -> str:
    """The value placeholder of an option: its choices, metavar or DEST."""
    if a.nargs == 0:
        return ""
    if a.choices is not None:
        choices = cast(Sequence[object], a.choices)
        return "|".join(str(c) for c in choices)
    if isinstance(a.metavar, str):
        return a.metavar
    return a.dest.upper()


def sentence(text: str) -> str:
    """Capitalized and ending with a period, like the description in `task -h`."""
    text = text.strip()
    if not text:
        return text
    return text[0].upper() + text[1:] + ("" if text.endswith(".") else ".")


class Parser(argparse.ArgumentParser):
    """argparse with the help layout of `task -h` and colored usage errors."""

    def _parts(self) -> tuple[list[str], list[HelpEntry], list[HelpEntry], list[HelpEntry]]:
        """(usage words, commands, arguments, options) of this parser."""
        usage: list[str] = []
        commands: list[HelpEntry] = []
        arguments: list[HelpEntry] = []
        options: list[HelpEntry] = []
        for a in self._actions:
            if a.help == argparse.SUPPRESS and not a.option_strings:
                continue
            help_text = a.help if isinstance(a.help, str) and a.help != argparse.SUPPRESS else ""
            if isinstance(a, argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage]
                usage.append("<subcommand>")
                for c in a._get_subactions():
                    commands.append((c.dest, c.help if isinstance(c.help, str) else ""))
            elif not a.option_strings:
                name = "<%s>" % (a.metavar if isinstance(a.metavar, str) else a.dest)
                usage.append("[%s]" % name if a.nargs == "?" else name)
                arguments.append((name, help_text))
            elif a.help != argparse.SUPPRESS:
                value = _opt_value(a)
                flag = max(a.option_strings, key=len)
                shown = (flag + " " + value).strip()
                if not isinstance(a, argparse._HelpAction):  # pyright: ignore[reportPrivateUsage]
                    usage.append(shown if a.required else "[%s]" % shown)
                names = ", ".join(sorted(a.option_strings, key=len))
                if isinstance(a, argparse._HelpAction):  # pyright: ignore[reportPrivateUsage]
                    help_text = "show this help"
                options.append(((names + " " + value).strip(), help_text))
        # Like `task -h`: the command's own options, then --detail, --plain, and -h last.
        order = {"--detail": 1, "--plain": 2, "-h, --help": 3}
        options.sort(key=lambda e: order.get(e[0], 0))
        usage.sort(key=lambda w: (w.startswith("["), order.get(w.strip("[]"), 0)))
        return usage, commands, arguments, options

    def _usage(self) -> str:
        words, _, _, _ = self._parts()
        return " ".join([self.prog] + words)

    @override
    def format_help(self) -> str:
        words, commands, arguments, options = self._parts()
        footer = (
            "`%s <subcommand> -h` shows a subcommand's arguments and options." % self.prog
            if commands
            else "`task -h` lists all commands."
        )
        return render_help(
            " ".join([self.prog] + words),
            sentence(self.description or ""),
            [("commands", commands), ("arguments", arguments), ("options", options)],
            footer,
        )

    @override
    def format_usage(self) -> str:
        return usage_line(self._usage())

    @override
    def error(self, message: str) -> NoReturn:
        _ = sys.stderr.write(self.format_usage())
        _ = sys.stderr.write(message_line("error", RED, message))
        if self.prog == "task":
            what = "lists all commands"
        elif self._parts()[1]:
            what = "lists its subcommands"
        else:
            what = "shows its arguments and options"
        _ = sys.stderr.write(hint("`%s -h` %s" % (self.prog, what)))
        sys.exit(2)


class TopParser(Parser):
    @override
    def format_help(self) -> str:
        return top_help()

    @override
    def format_usage(self) -> str:
        return usage_line("task <command> [args]")


def build_parser() -> argparse.ArgumentParser:
    p = TopParser(prog="task")
    _ = p.add_argument("--home", help="local state directory (default $TASKMGR_HOME or ~/.taskmgr)")
    # --plain anywhere on the line; SUPPRESS keeps a subcommand from resetting it.
    plain = Parser(add_help=False)
    _ = plain.add_argument(
        "--plain",
        action="store_true",
        default=argparse.SUPPRESS,
        help=PLAIN_HELP,
    )
    _ = p.add_argument("--plain", action="store_true", default=argparse.SUPPRESS, help=PLAIN_HELP)
    # --detail / --plain on every subcommand that prints a result.
    common = Parser(add_help=False, parents=[plain])
    _ = common.add_argument("--detail", action="store_true", help=DETAIL_HELP)
    sub = p.add_subparsers(dest="cmd", required=True, metavar="<command>", parser_class=Parser)

    def add(name: str) -> argparse.ArgumentParser:
        c = BY_NAME[name]
        # `host` puts the options on its subcommands; logs prints the log itself.
        parents = [plain] if name == "host" or c.text is None else [common]
        return sub.add_parser(name, help=c.help, description=c.help, parents=parents)

    jobid_help = "the job's id (16 hex digits)"
    host_help = "server name (default: look the jobid up on every server)"
    s = add("init")
    _ = s.add_argument("task_id", help="task id, e.g. AA-01 (two capitals, dash, 2-4 digits)")
    _ = s.add_argument("--output", metavar="DIR", help="target directory (default ./<task_id>)")
    s = add("validate")
    _ = s.add_argument("path", help="package directory or .tar.gz")
    s = add("submit")
    _ = s.add_argument("path", help="package directory or .tar.gz")
    _ = s.add_argument("--host", help="server name (default: round-robin over enabled servers)")
    _ = s.add_argument(
        "--submission-key",
        metavar="KEY",
        help="idempotency key: the same key on the same server returns the same job "
        + "(needs --host)",
    )
    s = add("jobs")
    _ = s.add_argument("--host", help="only this server (default: every server)")
    _ = s.add_argument("--state", help="only jobs in this state, e.g. running")
    s = add("status")
    _ = s.add_argument("jobid", help=jobid_help)
    _ = s.add_argument("--host", help=host_help)
    _ = s.add_argument(
        "--wait", action="store_true", help="wait until the job ends; the exit code tells how"
    )
    _ = s.add_argument(
        "--poll-interval",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="--wait: seconds between polls (default 5)",
    )
    _ = s.add_argument(
        "--retry-timeout",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help="--wait: give up after this long without a connection (default 300)",
    )
    _ = s.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="--wait: give up after this long (default: no limit)",
    )
    s = add("logs")
    _ = s.add_argument("jobid", help=jobid_help)
    _ = s.add_argument("--host", help=host_help)
    _ = s.add_argument(
        "--stream",
        choices=("stdout", "stderr", "shim"),
        default="stdout",
        help="which log (default stdout)",
    )
    _ = s.add_argument("--follow", action="store_true", help="keep printing until the job ends")
    s = add("fetch")
    _ = s.add_argument("jobid", help=jobid_help)
    _ = s.add_argument("--host", help=host_help)
    _ = s.add_argument(
        "--output",
        required=True,
        metavar="DIR",
        help="download into DIR/<host>/<jobid>/ (must not exist yet)",
    )
    for name in ("cancel", "retry"):
        s = add(name)
        _ = s.add_argument("jobid", help=jobid_help)
        _ = s.add_argument("--host", help=host_help)
    s = add("resolve")
    _ = s.add_argument("jobid", help=jobid_help)
    _ = s.add_argument("--host", help=host_help)
    _ = s.add_argument(
        "--as",
        dest="as_",
        choices=("stopped",),
        help="declare that the job's processes are gone (needs --confirm)",
    )
    _ = s.add_argument("--confirm", metavar="JOBID", help="repeat the jobid to confirm --as")
    hostcmds.add_parsers(add("host"), common)
    s = add("whoami")
    _ = s.add_argument("--host", help="only this server (default: every server)")
    _ = add("update")
    s = add("version")
    _ = s.add_argument("--host", help="only this server (default: every server)")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _ = set_color("--plain" in argv)  # before parsing: help and usage errors are colored too
    parser = build_parser()
    try:
        args = parser.parse_args(argv, namespace=Args())
    except SystemExit as e:  # argparse errors are parameter errors: exit 2
        if e.code == 0:
            return 0
        if "--detail" in argv:
            err = {"error": {"code": "bad_args", "message": "invalid arguments (see stderr)"}}
            _ = sys.stdout.write(json_text(err, indent=None) + "\n")
        return 2
    cmd = BY_NAME[args.cmd]
    detail = args.detail and cmd.text is not None

    def fail(err: Mapping[str, object], code: int) -> int:
        """An error: JSON on stdout with --detail (and a line on stderr), else text on stderr."""
        if detail:
            _ = sys.stdout.write(json_text({"error": err}, indent=None) + "\n")
        _ = sys.stderr.write(error_text(err, color_on()))
        return code

    try:
        out, code = cmd.run(args)
    except TaskmgrError as e:
        return fail(_as_dict(e.to_json()["error"]), e.exit_code)
    except KeyboardInterrupt:
        note("interrupted", YELLOW, "remote jobs are not affected")
        if detail:
            err = {"error": {"code": "interrupted", "message": "interrupted locally"}}
            _ = sys.stdout.write(json_text(err, indent=None) + "\n")
        return 2
    except Exception as e:  # noqa: BLE001 - keep the JSON / exit-code contract
        import traceback

        say(traceback.format_exc().rstrip(), GRAY)
        return fail({"code": "internal_error", "message": "%s: %s" % (type(e).__name__, e)}, 2)
    if out is None:
        return code
    if detail or cmd.text is None:
        _ = sys.stdout.write(json_text(out) + "\n")
        return code
    _ = sys.stdout.write(cmd.text(args, out, color_on()))
    # An error that comes with a result (jobs: every server failed; status --wait: timeout).
    err = _as_dict(out.get("error"))
    if code and "code" in err:
        _ = sys.stderr.write(error_text(err, color_on()))
    return code


if __name__ == "__main__":
    sys.exit(main())
