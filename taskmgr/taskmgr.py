"""`task`: the local taskmgr CLI (entry module).

Local state lives in $TASKMGR_HOME (default ~/.taskmgr):
    machines.json   machine list and connection configuration
    rr.json         round-robin cursor (updated under rr.lock)
"""

import getpass
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from typing import cast

from . import hostcmds, package, render
from .cliparse import Args, Command, build_parser
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
    try_read_json,
    utcnow_iso,
)
from .pyz import code_version as local_code_version
from .records import JsonDict, JsonValue, Problem, Server, SkippedEntry, TaskSpec
from .textout import (
    CYAN,
    GRAY,
    STATE_COLORS,
    YELLOW,
    CmdResult,
    as_dict,
    color_on,
    error_text,
    json_text,
    note,
    rows,
    say,
    set_color,
    warn,
)
from .transport import (
    ConnError,
    Remote,
    load_machines,
    query_all,
    query_timeout,
    reply_features,
    require_features,
)
from .transport import json_obj as _obj


def _arr(v: JsonValue) -> list[JsonValue]:
    """A JSON value the remote protocol guarantees to be an array."""
    if not isinstance(v, list):
        raise TypeError("expected a JSON array in the remote reply, got %r" % (v,))
    return v


# ---------------------------------------------------------------------------
# Machine list and transports


def home_dir(args: Args) -> str:
    h = args.home or os.environ.get("TASKMGR_HOME") or "~/.taskmgr"
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
        # A missing, unreadable or invalid cursor file starts over at 0.
        state, _ = try_read_json(os.path.join(home, "rr.json"))
        raw_cursor = state.get("cursor", 0) if isinstance(state, dict) else 0
        try:
            cursor = int(raw_cursor) if isinstance(raw_cursor, (int, float, str)) else 0
        except (ValueError, OverflowError):
            cursor = 0
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


def _package_warnings(root: str) -> list[Problem]:
    """What `task validate` warns about: entries left out of the package, and a size over
    the server default max_package_bytes (each server enforces its own limit)."""
    warnings: list[Problem] = [
        {"code": "excluded", "message": "left out of the package", "path": rel}
        for rel in package.excluded_entries(root)
    ]
    limit = package.DEFAULT_MAX_PACKAGE_BYTES
    size = package.packed_size(root)
    if limit is not None and size > limit:
        msg = package.size_exceeded("package size of", size, limit)
        warnings.append({"code": "package_too_large", "message": msg + " (the server default)"})
    return warnings


def validate_path(
    path: str,
) -> tuple[list[Problem], TaskSpec | None, str | None, list[Problem]]:
    """Validate a package directory or archive without a size limit.
    Returns (errors, task, content_hash, warnings)."""
    if not os.path.exists(path):
        raise TaskmgrError("not_found", "path does not exist: %s" % path)
    if os.path.isdir(path):
        errors, task = package.validate_dir(path)
        warnings = _package_warnings(path)
        return errors, task, (None if errors else package.content_hash(path)), warnings
    with tempfile.TemporaryDirectory(prefix="taskmgr-validate-") as tmp:
        try:
            skipped = package.safe_extract(path, tmp, skip_unsafe=True)
        except TaskmgrError as e:
            return [{"code": e.code, "message": e.message}], None, None, []
        unsafe: list[Problem] = [
            {
                "code": "unsafe_package",
                "message": "unsafe archive entry: %s" % s["reason"],
                "path": s["path"],
            }
            for s in skipped
        ]
        root = package.package_root(tmp)
        warnings = _package_warnings(root)
        _ = package.remove_excluded(root)  # as the server does before validating
        errors, task = package.validate_dir(root)
        errors = unsafe + errors  # report every error, not only the first unsafe entry
        return errors, task, (None if errors else package.content_hash(root)), warnings


def cmd_validate(args: Args) -> CmdResult:
    errors, task, chash, warnings = validate_path(args.path)
    for w in warnings:
        warn("%s%s" % (w["path"] + ": " if "path" in w else "", w["message"]))
    if errors:
        return {"valid": False, "errors": errors, "warnings": warnings}, 1
    assert task is not None  # validate_dir returns the task when there are no errors
    return {
        "valid": True,
        "task_id": task["task_id"],
        "content_hash": chash,
        "features": package.task_features(task),
        "warnings": warnings,
    }, 0


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
    errors, task, _, _ = validate_path(path)  # the server checks the size
    if errors:
        raise TaskmgrError(
            "invalid_package", "package validation failed (%d errors)" % len(errors), details=errors
        )
    home = home_dir(args)
    servers = load_machines(home)
    server = find_server(servers, args.host) if args.host else pick_round_robin(home, servers)
    key = args.submission_key or ("cli-" + secrets.token_hex(16))
    remote = Remote(server)
    needed = package.task_features(task)
    if needed:  # before the upload: a server without the feature must not get the package
        require_features(remote.call(["version"]), needed, server["name"], "this package")
    with tempfile.TemporaryDirectory(prefix="taskmgr-submit-") as tmp:
        if os.path.isdir(path):
            upload = os.path.join(tmp, "package.tar.gz")
            package.pack_dir(path, upload)
        else:
            upload = path
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


def _strip(reply: JsonDict) -> JsonDict:
    """A remote reply without the protocol fields."""
    _ = reply.pop("protocol_version", None)
    _ = reply.pop("ok", None)
    return reply


def query_hosts(
    servers: list[Server], host: str | None, rargs: Sequence[object]
) -> tuple[list[tuple[str, JsonDict]], dict[str, object], int]:
    """Call rargs on the query targets: ([(host, reply)] in machine-list order, the
    {"errors", "skipped", "complete"} part of the result, exit code).

    The exit code is 2 (and the result has an all_hosts_failed error) when no target answered.
    """
    servers, skipped = query_targets(servers, host)
    results, errors = query_all(servers, rargs)
    replies = [(s["name"], _strip(results[s["name"]])) for s in servers if s["name"] in results]
    out: dict[str, object] = {"errors": errors, "skipped": skipped, "complete": not errors}
    if servers and not results:
        out["error"] = {"code": "all_hosts_failed", "message": "no server could be queried"}
        return replies, out, 2
    return replies, out, 0


def report_partial(out: Mapping[str, object], what: str) -> None:
    """stderr lines for the skipped (disabled) and unreachable servers of a query."""
    skipped = out.get("skipped")
    names = [str(n) for n in cast(list[object], skipped)] if isinstance(skipped, list) else []
    if names:
        note("skipped", YELLOW, "disabled, not queried: %s" % ", ".join(names))
    for e in rows(out, "errors"):
        warn("%s unreachable, %s: %s" % (e.get("host"), what, e.get("error")))


def cmd_jobs(args: Args) -> CmdResult:
    rargs = ["jobs"] + (["--state", args.state] if args.state else [])
    replies, out, code = query_hosts(load_machines(home_dir(args)), args.host, rargs)
    jobs = [{"host": h, **_obj(j)} for h, r in replies for j in _arr(r.get("jobs", []))]
    jobs.sort(key=lambda j: (str(j.get("submitted_at")), str(j["host"]), str(j.get("jobid"))))
    out["jobs"] = jobs[-args.limit :]
    report_partial(out, "its jobs are not listed")
    return out, code


def render_status(host: str | None, st: JsonDict, complete: bool) -> dict[str, object]:
    j = _obj(st["job"])
    if st["terminal"]:
        out: dict[str, object] = dict(_obj(st["result"]))
        out["host"] = host
        out["complete"] = complete
        out["retry_of"] = j.get("retry_of")
        out["retried_as"] = j.get("retried_as", [])
        out["cancel_request"] = j.get("cancel_request")
        out["gpu_request"] = j.get("gpu_request")
        return out
    out = {
        "host": host,
        "jobid": j["jobid"],
        "task_id": j["task_id"],
        "state": j["state"],
        "pending_reason": j.get("pending_reason"),
        "phase": j.get("phase"),
        "cancel_requested": j["cancel_requested"],
        "run": st.get("run"),
        "progress": st.get("progress"),
        "submitted_at": j["submitted_at"],
        "gpus": j["gpus"],
        "gpu_request": j.get("gpu_request"),
        "error": j.get("error"),
        "retry_of": j.get("retry_of"),
        "retried_as": j.get("retried_as", []),
        "cancel_request": j.get("cancel_request"),
        "gpu_holders": st.get("gpu_holders"),
        "complete": complete,
    }
    if "gpu_holders_error" in st:
        out["gpu_holders_error"] = st["gpu_holders_error"]
    return out


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


def locate(
    args: Args, servers: list[Server], timeout: float | None = None
) -> tuple[Server, JsonDict, bool]:
    """Return (host, status, complete) per the table in Section 14.2.

    timeout: the limit of each status query (default query_timeout())."""
    if args.host:
        server = find_server(servers, args.host)
        st = Remote(server).call(["status", args.jobid], timeout=timeout)
        if not st.get("found"):
            raise TaskmgrError(
                "not_found", "job %s not found on %s" % (args.jobid, args.host), 2, args.host
            )
        return server, st, True
    servers, skipped = query_targets(servers, None)
    results, errors = query_all(servers, ["status", args.jobid], timeout)
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
    """--timeout is the total wait: each query gets at most the time left, so a server that
    does not answer cannot extend it. A query that returns a terminal state (or unknown)
    wins over an expired --timeout."""
    jobid: str = args.jobid  # changes when the wait follows an automatic re-run
    t_start = time.monotonic()
    deadline: float | None = t_start + args.timeout if args.timeout is not None else None
    fail_since: float | None = None
    st: JsonDict | None = None
    server: Server | None = None
    complete = True
    while True:
        limit = query_timeout()
        if deadline is not None:
            limit = max(0.0, min(limit, deadline - time.monotonic()))
        try:
            if server is None:
                server, st, complete = locate(args, servers, limit)
            else:
                st = Remote(server).call(["status", jobid], timeout=limit)
                if not st.get("found"):
                    raise TaskmgrError(
                        "not_found",
                        "job %s disappeared from %s" % (jobid, server["name"]),
                        2,
                        server["name"],
                    )
            fail_since = None
        except TaskmgrError as e:
            # Unreachable servers are retried within --retry-timeout, with or without --host:
            # the lookup on every server fails as "incomplete" while a server is down.
            if e.code not in ("connection_error", "incomplete"):
                raise
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
                    "message": "--timeout expired without an answer from the server: %s"
                    % e.message,
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
    out = _strip(Remote(server).call(rargs))
    out["host"] = server["name"]
    return out, 0


def local_actor() -> str:
    """user@hostname of this CLI, recorded by the server as who asked for a cancel."""
    try:
        user = getpass.getuser()
    except (KeyError, OSError):  # no user name for the uid (some containers)
        user = str(os.getuid())
    return "%s@%s" % (user, socket.gethostname())


def cmd_cancel(args: Args) -> CmdResult:
    server, st = job_server(args)
    rargs: list[object] = ["cancel", args.jobid]
    if "cancel_by" in reply_features(st):  # an older server has no --by
        rargs += ["--by", local_actor()]
    out = _strip(Remote(server).call(rargs))
    out["host"] = server["name"]
    return out, 0


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


def _fetch_manifest(extracted: str) -> list[SkippedEntry]:
    """Remove package.FETCH_MANIFEST from a fetched tree; returns the special files the
    server left out (listed in it)."""
    path = os.path.join(extracted, package.FETCH_MANIFEST)
    if not os.path.lexists(path):
        return []
    obj, _ = (
        try_read_json(path) if os.path.isfile(path) and not os.path.islink(path) else (None, None)
    )
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.unlink(path)
    rows = obj.get("skipped") if isinstance(obj, dict) else None
    out: list[SkippedEntry] = []
    for r in cast(list[object], rows) if isinstance(rows, list) else []:
        if isinstance(r, dict):
            d = cast(JsonDict, r)
            out.append({"path": str(d.get("path")), "reason": str(d.get("reason"))})
    return out


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
    created: list[str] = []  # the directories of DIR/<host> this fetch creates, deepest first
    d = parent
    while not os.path.isdir(d):
        created.append(d)
        d = os.path.dirname(d)
    os.makedirs(parent, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".fetch-", dir=parent)
    try:
        tar_path = os.path.join(staging, "stream.tar.gz")
        with open(tar_path, "wb") as f:
            _ = remote.stream(["fetch", jobid], f)
        extracted = os.path.join(staging, "x")
        unsafe = package.safe_extract(tar_path, extracted, skip_unsafe=True)
        skipped = sorted(_fetch_manifest(extracted) + unsafe, key=lambda s: s["path"])
        for s in skipped:
            warn("skipped %s (%s)" % (s["path"], s["reason"]))
        if os.path.lexists(target):
            raise TaskmgrError("exists", "target directory already exists: %s" % target)
        os.rename(extracted, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        for d in created:  # leave no empty DIR/<host> behind
            try:
                os.rmdir(d)
            except OSError:
                break
        raise
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


def _host_versions(args: Args) -> tuple[dict[str, object], int]:
    """`version` on the query targets: {"hosts": [reply + host], "errors", "skipped",
    "complete"}."""
    replies, out, code = query_hosts(load_machines(home_dir(args)), args.host, ["version"])
    out["hosts"] = [{"host": h, **r} for h, r in replies]
    return out, code


def cmd_whoami(args: Args) -> CmdResult:
    out, code = _host_versions(args)
    report_partial(out, "not shown")
    return out, code


def _git(*argv: str) -> subprocess.CompletedProcess[str] | None:
    """Run git in the checkout; None when git is not installed or this is a .pyz."""
    if code_is_zipapp():
        return None
    try:
        return subprocess.run(
            ["git", "-C", CODE_ROOT, *argv], capture_output=True, text=True, timeout=300
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
        "path": CODE_ROOT,
    }


def cmd_version(args: Args) -> CmdResult:
    local = local_version()
    out, code = _host_versions(args)
    report_partial(out, "version unknown")
    for h in rows(out, "hosts"):
        h["up_to_date"] = h.get("code_version") == local["code_version"]
    return {"local": local, **out}, code


def cmd_update(_args: Args) -> CmdResult:
    """Update this checkout with `git pull --ff-only`; servers are updated by `task host add`."""
    if code_is_zipapp():
        raise TaskmgrError(
            "not_a_checkout",
            "%s is a zipapp; build a new one from a checkout (python3 -m taskmgr.pyz)" % CODE_ROOT,
        )
    before = _git_out("rev-parse", "--short=12", "HEAD")
    if before is None:
        raise TaskmgrError(
            "not_a_checkout", "%s is not a git checkout (or git is missing)" % CODE_ROOT
        )
    p = _git("pull", "--ff-only")
    assert p is not None  # git ran just above
    if p.returncode != 0:
        raise TaskmgrError(
            "update_failed", "git pull --ff-only failed in %s:\n%s" % (CODE_ROOT, p.stderr.strip())
        )
    after = _git_out("rev-parse", "--short=12", "HEAD")
    return {"path": CODE_ROOT, "before": before, "after": after, "changed": before != after}, 0


# ---------------------------------------------------------------------------
# Command line


COMMANDS = [
    Command("init", "tasks", "create a task package template", cmd_init, render.text_init),
    Command(
        "validate",
        "tasks",
        "check a task package directory or tar.gz",
        cmd_validate,
        render.text_validate,
    ),
    Command("submit", "tasks", "submit a task package to a server", cmd_submit, render.text_submit),
    Command("jobs", "tasks", "list the jobs on every server", cmd_jobs, render.text_jobs),
    Command(
        "status", "tasks", "show one job, or wait for it to end", cmd_status, render.text_status
    ),
    Command("logs", "tasks", "print a job's stdout, stderr or shim log", cmd_logs, None),
    Command("fetch", "tasks", "download a job's results", cmd_fetch, render.text_fetch),
    Command("cancel", "tasks", "cancel a job", cmd_cancel, render.text_cancel),
    Command(
        "retry", "tasks", "run a finished job again as a new job", cmd_retry, render.text_retry
    ),
    Command(
        "resolve", "tasks", "resolve a job in the unknown state", cmd_resolve, render.text_resolve
    ),
    Command("host", "hosts", "manage the machine list", cmd_host, render.text_host),
    Command(
        "whoami",
        "others",
        "the user and hostname each server runs as",
        cmd_whoami,
        render.text_whoami,
    ),
    Command(
        "update",
        "others",
        "update this local CLI (git pull --ff-only)",
        cmd_update,
        render.text_update,
    ),
    Command(
        "version",
        "others",
        "local version and the code version on each server",
        cmd_version,
        render.text_version,
    ),
]
BY_NAME = {c.name: c for c in COMMANDS}


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Before parsing, so help and usage errors follow --plain too (no option abbreviations:
    # the exact spelling is the only one).
    _ = set_color("--plain" in argv)
    parser = build_parser(COMMANDS)
    try:
        args = parser.parse_args(argv, namespace=Args())
    except SystemExit as e:  # argparse errors are parameter errors: exit 2
        if e.code == 0:
            return 0
        if "--detail" in argv:
            err = {"error": {"code": "bad_args", "message": "invalid arguments (see stderr)"}}
            _ = sys.stdout.write(json_text(err, indent=None) + "\n")
        return 2
    _ = set_color(args.plain)
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
        return fail(as_dict(e.to_json()["error"]), e.exit_code)
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
    err = as_dict(out.get("error"))
    if code and "code" in err:
        _ = sys.stderr.write(error_text(err, color_on()))
    return code


if __name__ == "__main__":
    sys.exit(main())
