"""`taskmgr-remote`: remote subcommands, the scheduler daemon and the shim entry point.

Invoked by the local CLI as
    ssh <host> taskmgr-remote --root <remote_root> --protocol-version N <subcommand> ...
Every subcommand except `logs`, `fetch`, `serve` and `shim` prints one JSON object.
`inspect` is read-only and creates nothing, so `task create host` can run it on any root.
"""

import argparse
import getpass
import io
import json
import os
import secrets
import socket
import sys
import tarfile
import time
from collections.abc import Callable, Mapping
from typing import IO, cast

from . import package
from .common import (
    CODE_ROOT,
    FEATURES,
    GPU_HOLDING_STATES,
    JOB_DIRS,
    JOBID_RE,
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    SUBMISSION_KEY_RE,
    TERMINAL_STATES,
    VERSION_FILE,
    TaskmgrError,
    atomic_write_json,
    read_code_file,
    read_json,
    try_read_json,
    utcnow_iso,
)
from .gpu import gpu_holders, request_from_task, resolve_declarations
from .monitor import assess, identity_of
from .records import CancelRequest, Job, JsonValue, KeyMapping, Runner, SkippedEntry
from .store import (
    OPERATOR_MARKED_STOPPED,
    OPERATOR_STOPPED_ERROR,
    PRIVATE_FILE,
    Store,
    clone_job,
    end_without_result,
    enqueue_staged,
    has_run,
    initial_phase,
    interrupted_runner,
    job_summary,
    make_read_only,
    meta_block,
    new_job_record,
    precompiling,
    remove_tree,
    stage_package,
    staging_dir,
    sync_tree,
)


class Args(argparse.Namespace):
    """Typed view of the parsed command line; attributes exist only for their subcommand."""

    # Class-level defaults equal the argparse defaults: argparse only fills a top-level
    # default when the attribute is missing, and copies every subcommand value over.
    root: str = ""
    protocol_version: int | None = None
    cmd: str = ""
    submission_key: str | None = None
    state: str | None = None
    limit: int | None = None
    jobid: str = ""
    as_: str | None = None
    by: str | None = None
    confirm: str | None = None
    stream: str = "stdout"
    follow: bool = False
    lock_fd: int = -1


def remote_protocol_version() -> int:
    # TASKMGR_TEST_PROTOCOL_VERSION: test-only override to simulate a mismatched remote.
    v = os.environ.get("TASKMGR_TEST_PROTOCOL_VERSION")
    return int(v) if v else PROTOCOL_VERSION


def remote_features() -> list[str]:
    # TASKMGR_TEST_FEATURES: test-only override (comma-separated; "-" for none) to simulate
    # a server whose code lacks a feature.
    v = os.environ.get("TASKMGR_TEST_FEATURES")
    if v is not None:
        return sorted(f for f in v.split(",") if f and f != "-")
    return sorted(FEATURES)


def emit(obj: Mapping[str, object], code: int = 0) -> int:
    out = dict(obj)
    out["protocol_version"] = remote_protocol_version()
    out["features"] = remote_features()
    _ = sys.stdout.write(json.dumps(out, sort_keys=True) + "\n")
    _ = sys.stdout.flush()
    return code


# ---------------------------------------------------------------------------
# receive (Section 5.2)


def cmd_receive(store: Store, args: Args) -> int:
    store.ensure_layout()

    key = args.submission_key or ("auto-" + secrets.token_hex(12))
    if not SUBMISSION_KEY_RE.match(key):
        raise TaskmgrError(
            "bad_submission_key", "submission key must match ^[A-Za-z0-9._-]{1,128}$"
        )

    # Flocked for the whole upload and reception.
    with staging_dir(store, "up-%s" % time.strftime("%Y%m%d%H%M%S")) as updir:
        tar_path = os.path.join(updir, "package.tar.gz")
        fd = os.open(tar_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE)
        with os.fdopen(fd, "wb") as f:
            receive_upload(sys.stdin.buffer, f, store.config["max_package_bytes"])
        if os.environ.get("TASKMGR_TEST_RECEIVE_PAUSE"):  # test-only: hold the upload open
            time.sleep(float(os.environ["TASKMGR_TEST_RECEIVE_PAUSE"]))

        ext = os.path.join(updir, "extract")
        _ = package.safe_extract(tar_path, ext, store.config["max_package_bytes"])
        root = package.package_root(ext)
        # What a directory upload would not have carried (a stale result.json from an
        # earlier run would otherwise become this run's result).
        _ = package.remove_excluded(root)
        chash = package.content_hash(root)

        # A retry of an already enqueued submission answers from the key mapping before
        # any check that depends on the machine's current state (e.g. GPU visibility).
        with store.state_lock():
            ack = _dedup_ack(store, key, chash)
        if ack:
            return emit(ack)

        # Extraction already enforced this server's max_package_bytes.
        errors, task = package.validate_dir(root, max_bytes=None)
        if errors:
            raise TaskmgrError(
                "invalid_package",
                "package validation failed (%d errors)" % len(errors),
                details=errors,
            )
        assert task is not None  # validate_dir returns the task when there are no errors

        spec = task.get("gpus")
        request = request_from_task(spec) if isinstance(spec, dict) else None
        gpus = resolve_declarations(spec) if isinstance(spec, list) else []

        pkg = os.path.join(updir, "package")
        os.rename(root, pkg)
        if os.path.exists(ext):
            remove_tree(ext)
        make_read_only(pkg)
        sync_tree(updir)  # package/ is the source of the run: durable before the ACK

        with store.state_lock():
            ack = _dedup_ack(store, key, chash)
            if ack:
                return emit(ack)
            jobid = store.new_jobid()
            job = new_job_record(
                jobid,
                task["task_id"],
                store.next_seq(),
                chash,
                key,
                gpus,
                phase=initial_phase(task),
                gpu_request=request,
            )

            # The key mapping is written first; a mapping whose job does not exist is stale.
            atomic_write_json(
                store.path("submission-keys", key + ".json"),
                {
                    "submission_key": key,
                    "jobid": jobid,
                    "content_hash": chash,
                    "created_at": job["submitted_at"],
                },
            )
            enqueue_staged(store, updir, job)

        return emit(
            {
                "ok": True,
                "jobid": jobid,
                "task_id": task["task_id"],
                "submission_key": key,
                "deduplicated": False,
                "content_hash": chash,
                "state": "queued",
            }
        )


def receive_upload(src: IO[bytes], dst: IO[bytes], max_bytes: int | None) -> None:
    """Copy the uploaded archive to dst (then fsync it), refusing one that is clearly
    too large before it fills the disk. The limit applies to the extracted size, so a
    compressed archive of incompressible files may be a little larger: allow 1/16 more
    plus 1 MiB of tar and gzip overhead; safe_extract enforces the exact limit."""
    cap = None if max_bytes is None else max_bytes + max_bytes // 16 + (1 << 20)
    total = 0
    while True:
        chunk = src.read(1 << 20)
        if not chunk:
            break
        total += len(chunk)
        if cap is not None and total > cap:
            raise TaskmgrError(
                "package_too_large",
                package.size_exceeded("upload of more than", cap, max_bytes or 0),
            )
        _ = dst.write(chunk)
    dst.flush()
    os.fsync(dst.fileno())


def _dedup_ack(store: Store, key: str, chash: str) -> dict[str, object] | None:
    """Section 6.2 lookup; needs the state lock. Returns an ACK, None, or raises."""
    mapping = cast(KeyMapping | None, read_json(store.path("submission-keys", key + ".json")))
    if not mapping or not store.find_job_dir(mapping.get("jobid")):
        return None  # unused key, or a stale mapping whose job was never enqueued
    if mapping.get("content_hash") != chash:
        raise TaskmgrError(
            "submission_key_conflict",
            "submission key %s was already used for different content (job %s)"
            % (key, mapping["jobid"]),
        )
    _, old = store.load_job(mapping["jobid"])
    return {
        "ok": True,
        "jobid": mapping["jobid"],
        "task_id": old["task_id"],
        "submission_key": key,
        "deduplicated": True,
        "content_hash": chash,
        "state": old["state"],
    }


# ---------------------------------------------------------------------------
# queries


def cmd_jobs(store: Store, args: Args) -> int:
    """The jobs in this store, oldest first; --limit N: only the N most recently submitted
    (`task get jobs -n N` needs no more from each server)."""
    if args.limit is not None and args.limit < 1:
        raise TaskmgrError("bad_args", "--limit must be at least 1")

    # A directory rename during the scan can show a job twice; keep its newest record.
    latest: dict[str, Job] = {}
    for _, j in store.iter_jobs():
        old = latest.get(j["jobid"])
        if old is None or (j.get("updated_at") or "") >= (old.get("updated_at") or ""):
            latest[j["jobid"]] = j
    jobs = [
        job_summary(j) for j in latest.values() if not args.state or j.get("state") == args.state
    ]
    jobs.sort(key=lambda j: (j.get("submitted_at") or "", j["jobid"]))
    if args.limit is not None:
        jobs = jobs[-args.limit :]
    return emit({"jobs": jobs})


def synthesized_result(job: Job) -> dict[str, object]:
    meta = meta_block(job, job["state"], job.get("error"))
    meta["note"] = "the job ended without running"
    return {"schema_version": SCHEMA_VERSION, "_taskmgr": meta}


def waits_for_gpu(job: Job) -> bool:
    """Whether the job waits for GPUs now: queued with GPUs (also in phase gpu), or running
    and waiting to relaunch after a GPU-busy exit (`task jobs` shows both as pending-gpu
    or queued)."""
    if job["state"] == "running":
        return bool(job.get("pending_reason")) and not precompiling(job)
    wants = job.get("gpus") or job.get("gpu_request")
    return job["state"] == "queued" and not precompiling(job) and bool(wants)


def waited_gpus(job: Job) -> list[str] | None:
    """The UUIDs a waiting job needs: its GPUs, the candidates of its request, or None
    (every GPU)."""
    gpus = [g["uuid"] for g in job.get("gpus", [])]
    if gpus:
        return gpus
    request = job.get("gpu_request")
    return (request.get("from_uuids") or None) if request else None


def job_status(store: Store, jobid: str, _retry: bool = True) -> dict[str, object]:
    try:
        jd, job = store.load_job(jobid)  # also follows a directory that moves meanwhile
    except TaskmgrError as e:
        if e.code == "not_found":
            return {"found": False, "jobid": jobid}
        raise

    terminal = job["state"] in TERMINAL_STATES
    ran = has_run(job)
    out: dict[str, object] = {
        "found": True,
        "job": job_summary(job),
        "has_run": ran,
        "terminal": terminal,
    }

    if terminal:
        res: JsonValue = None
        if ran:
            res, _ = try_read_json(
                os.path.join(jd, "workdir", "result.json"), follow_symlinks=False
            )
            if not isinstance(res, dict) and _retry:
                return job_status(store, jobid, False)
        out["result"] = res if isinstance(res, dict) else synthesized_result(job)
        return out

    if waits_for_gpu(job):
        try:
            out["gpu_holders"] = gpu_holders(waited_gpus(job))
        except TaskmgrError as e:
            out["gpu_holders"] = None
            out["gpu_holders_error"] = e.message

    if ran:
        found, _ = store.read_runner(jd)
        runner: Mapping[str, object] = found or {}
        info: dict[str, object] = {
            k: runner.get(k)
            for k in (
                "status",
                "dispatched_at",
                "started_at",
                "exited",
                "return_code",
                "signal",
                "takeovers",
                "gpu_busy_retries",
                "last_gpu_busy_at",
            )
        }
        info["identity"] = identity_of(runner)
        out["run"] = info

        prog, err = try_read_json(
            os.path.join(jd, "workdir", "progress.json"), follow_symlinks=False
        )
        out["progress"] = (
            prog if prog is not None else (None if err == "missing" else {"error": err})
        )
    return out


def cmd_status(store: Store, args: Args) -> int:
    return emit(job_status(store, args.jobid))


# ---------------------------------------------------------------------------
# control (Section 13)


def cancel_request(by: str | None) -> CancelRequest:
    """Who asks for a cancel: `by` (the CLI's user@hostname, sent with --by), else the
    user and hostname this command runs as on the server."""
    if by:
        return {"by": by, "at": utcnow_iso(), "via": "cli"}
    who = "%s@%s" % (getpass.getuser(), socket.gethostname())
    return {"by": who, "at": utcnow_iso(), "via": "remote"}


def cmd_cancel(store: Store, args: Args) -> int:
    request = cancel_request(args.by)

    with store.state_lock():
        jd, job = store.load_job(args.jobid)
        changed = False
        if job["state"] in TERMINAL_STATES:
            pass
        elif job["state"] == "queued":
            job["cancel_requested"] = True
            job["cancel_request"] = request
            _ = end_without_result(store, jd, job, "cancelled", None, None)
            changed = True
        elif not job.get("cancel_requested"):
            job["cancel_requested"] = True  # shim / daemon picks it up within control_poll
            job["cancel_request"] = request
            _ = store.save_job(jd, job)
            changed = True

    out: dict[str, object] = {
        "jobid": job["jobid"],
        "state": job["state"],
        "changed": changed,
        "cancel_requested": job.get("cancel_requested", False),
    }
    if "cancel_request" in job:
        out["cancel_request"] = job["cancel_request"]
    return emit(out)


def cmd_retry(store: Store, args: Args) -> int:
    # The package never changes: stage its copy before taking the state lock.
    with staging_dir(store, "retry") as updir:
        jd, _ = store.load_job(args.jobid)
        try:
            stage_package(jd, updir)
            staged: str | None = updir
        except OSError:  # the job directory moved (the job was still running)
            staged = None

        with store.state_lock():
            jd, job = store.load_job(args.jobid)
            if job["state"] == "unknown":
                raise TaskmgrError(
                    "resolve_required",
                    "job %s is unknown; run `task resolve job %s` first" % (args.jobid, args.jobid),
                )
            if job["state"] not in TERMINAL_STATES:
                raise TaskmgrError(
                    "not_terminal",
                    "job %s is %s; only terminal jobs can be retried" % (args.jobid, job["state"]),
                )
            new = clone_job(store, jd, job, automatic=False, staged=staged)

    return emit(
        {
            "jobid": new["jobid"],
            "retry_of": job["jobid"],
            "state": new["state"],
            "changed": True,
            "enqueue_seq": new["enqueue_seq"],
        }
    )


def cmd_resolve(store: Store, args: Args) -> int:
    if args.as_ is not None:
        if args.as_ != "stopped":
            raise TaskmgrError("bad_args", "--as only accepts 'stopped'")
        if args.confirm != args.jobid:
            raise TaskmgrError("bad_args", "--confirm must repeat the jobid %s" % args.jobid)

        with store.state_lock():
            jd, job = store.load_job(args.jobid)
            if job["state"] != "unknown":
                raise TaskmgrError(
                    "not_unknown", "job %s is %s, not unknown" % (args.jobid, job["state"])
                )

            # The run record is made final in memory and written only by and after
            # end_without_result: if that fails, runner.json is unchanged and the job
            # stays unknown. (A run record marked stopped is never re-run, see assess.)
            runner: Runner | None = None
            if has_run(job):
                runner = interrupted_runner(store, jd, job["jobid"], OPERATOR_MARKED_STOPPED)
            if job.get("cancel_requested"):
                jd = end_without_result(store, jd, job, "cancelled", None, runner)
            else:
                jd = end_without_result(store, jd, job, "failed", OPERATOR_STOPPED_ERROR, runner)
            if runner is not None:
                store.write_runner(jd, runner)
        return emit({"jobid": args.jobid, "state": job["state"], "changed": True})

    with store.state_lock():
        _, job = store.load_job(args.jobid)
        if job["state"] != "unknown":
            return emit({"jobid": args.jobid, "state": job["state"], "changed": False})
        res = assess(store, args.jobid, resolve=True)
        _, job = store.load_job(args.jobid)

    out: dict[str, object] = {
        "jobid": args.jobid,
        "state": job["state"],
        "changed": job["state"] != "unknown",
        "decision": res["decision"],
    }

    if "identity" in res:
        out["identity"] = res["identity"]
    if "alive_pids" in res:
        out["alive_pids"] = res["alive_pids"]
    if "reason" in res:
        out["reason"] = res["reason"]

    if job["state"] == "unknown":
        jd = store.find_job_dir(args.jobid)
        runner, _ = store.read_runner(jd) if jd else (None, None)
        out["identity"] = identity_of(runner)
        out["hint"] = (
            "still undecided; check `ps -o pid,pgid,lstart,cmd -g <pgid>` and nvidia-smi, "
            + "kill leftovers, then run resolve --as stopped --confirm <jobid>"
        )
    return emit(out)


# ---------------------------------------------------------------------------
# logs and fetch (Section 15)


def _no_run(jobid: str) -> TaskmgrError:
    return TaskmgrError("no_run", "job %s has not run (nothing to show)" % jobid)


def cmd_logs(store: Store, args: Args) -> int:
    jd, job = store.load_job(args.jobid)
    while not has_run(job):
        if not args.follow or job["state"] != "queued":
            raise _no_run(args.jobid)
        time.sleep(0.5)  # --follow on a queued job waits until it starts
        jd, job = store.load_job(args.jobid)

    name = {"stdout": "stdout.log", "stderr": "stderr.log", "shim": "shim.log"}[args.stream]
    out = sys.stdout.buffer
    f: IO[bytes] | None = None
    runner: Runner | None

    while True:
        jd, job = store.load_job(args.jobid)
        if f is None:
            try:
                f = open(os.path.join(jd, name), "rb")
            except FileNotFoundError:
                f = None
        if f is not None:
            while True:
                chunk = f.read(1 << 16)
                if not chunk:
                    break
                _ = out.write(chunk)
            _ = out.flush()

        if not args.follow:
            return 0
        runner, _ = store.read_runner(jd)
        ended = (
            (isinstance(runner, dict) and runner.get("final"))
            or job["state"] in TERMINAL_STATES
            or job["state"] == "unknown"
        )

        if ended:
            if f is None:
                try:
                    f = open(os.path.join(store.find_job_dir(args.jobid) or jd, name), "rb")
                except FileNotFoundError:
                    return 0
            _ = out.write(f.read())
            _ = out.flush()
            return 0
        time.sleep(0.5)


# What `fetch` downloads from a job directory (not package/, package.tar.gz, shim.lock).
FETCH_FILES = ("job.json", "runner.json", "stdout.log", "stderr.log", "shim.log", "workdir")


def cmd_fetch(store: Store, args: Args) -> int:
    jd, job = store.load_job(args.jobid)
    if not has_run(job):
        raise _no_run(args.jobid)

    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as tar:
        skipped: list[SkippedEntry] = []
        for name in FETCH_FILES:
            skipped += package.add_tree_to_tar(tar, os.path.join(jd, name), name)
        # Last member: what the walk left out, for the client's `skipped` (desc-2 D12).
        data = json.dumps({"skipped": skipped}, sort_keys=True).encode()
        ti = tarfile.TarInfo(package.FETCH_MANIFEST)
        ti.size, ti.mode = len(data), 0o644
        tar.addfile(ti, io.BytesIO(data))
    _ = sys.stdout.buffer.flush()
    return 0


# ---------------------------------------------------------------------------
# inspect (read-only; used by `task create host`, desc-2 D18)

# Keys every job.json of the current layout has (protocol 2, desc-2 D16).
JOB_REQUIRED_KEYS: tuple[str, ...] = tuple(sorted(Job.__required_keys__))
# job.json keys that only the protocol-1 layout (attempts) wrote.
PROTOCOL1_JOB_KEYS = ("attempts", "current_attempt", "retries")


def job_dir_problems(jobdir: str) -> tuple[str | None, list[str]]:
    """(state or None, reasons the directory does not match the current layout)."""
    reasons: list[str] = []
    if os.path.isdir(os.path.join(jobdir, "attempts")):
        reasons.append("has attempts/ (protocol 1 layout)")

    obj, err = try_read_json(os.path.join(jobdir, "job.json"))
    if not isinstance(obj, dict):
        reasons.append("job.json is %s" % (err or "not a JSON object"))
        return None, reasons

    missing = [k for k in JOB_REQUIRED_KEYS if k not in obj]
    if missing:
        reasons.append("job.json lacks %s" % ", ".join(missing))
    old = [k for k in PROTOCOL1_JOB_KEYS if k in obj]
    if old:
        reasons.append("job.json has protocol 1 keys %s" % ", ".join(old))

    state = obj.get("state")
    return (state if isinstance(state, str) else None), reasons


def inspect_root(root: str) -> dict[str, object]:
    """Counts per state and the job directories the current code cannot handle.

    Reads only; a job is `live` when its state or location says it may still run
    (running, stopping, unknown, or anything under in-progress/).
    """
    counts: dict[str, int] = {}
    incompatible: list[dict[str, object]] = []
    for d in JOB_DIRS:
        try:
            names = sorted(os.listdir(os.path.join(root, d)))
        except OSError:
            continue
        for name in names:
            jobdir = os.path.join(root, d, name)
            if not JOBID_RE.match(name) or not os.path.isdir(jobdir):
                continue
            state, reasons = job_dir_problems(jobdir)
            if not reasons:
                key = state or "?"
                counts[key] = counts.get(key, 0) + 1
                continue
            incompatible.append(
                {
                    "path": "%s/%s" % (d, name),
                    "jobid": name,
                    "state": state,
                    "live": d == "in-progress" or state in GPU_HOLDING_STATES,
                    "reasons": reasons,
                    "submission_keys": [],
                }
            )

    by_jobid = {cast(str, e["jobid"]): e for e in incompatible}
    try:
        key_files = sorted(os.listdir(os.path.join(root, "submission-keys")))
    except OSError:
        key_files = []
    for name in key_files:
        if not name.endswith(".json") or not SUBMISSION_KEY_RE.match(name[: -len(".json")]):
            continue
        mapping, _ = try_read_json(os.path.join(root, "submission-keys", name))
        jobid = mapping.get("jobid") if isinstance(mapping, dict) else None
        entry = by_jobid.get(jobid) if isinstance(jobid, str) else None
        if entry is not None:
            cast(list[str], entry["submission_keys"]).append("submission-keys/" + name)

    return {
        "root": root,
        "exists": os.path.isdir(root),
        "config_exists": os.path.lexists(os.path.join(root, "config.json")),
        "counts": counts,
        "incompatible": incompatible,
    }


def version_info() -> dict[str, object]:
    """`version`: the installed code version (written by `task create host`) and who we run as."""
    # Written by `task create host` and by the .pyz build; missing in a plain checkout (tests).
    code_version = (read_code_file(VERSION_FILE) or "").strip() or None
    return {
        "ok": True,
        "code_version": code_version,
        "code_dir": CODE_ROOT,
        "user": getpass.getuser(),
        "hostname": socket.gethostname(),
    }


def cmd_inspect(store: Store, _args: Args) -> int:
    return emit(inspect_root(store.root))


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """The server side of `task`: run by the CLI over ssh with fixed arguments, by systemd
    (serve) and by the daemon (shim); an operator runs `version` or `serve` by hand."""
    p = argparse.ArgumentParser(
        prog="taskmgr-remote",
        description="Server side of taskmgr: one JSON object per call on stdout (except "
        + "logs, fetch, serve and shim).",
    )
    _ = p.add_argument("--root", required=True, help="remote storage root (from the machine list)")
    _ = p.add_argument(
        "--protocol-version",
        type=int,
        default=None,
        metavar="N",
        help="the client's protocol version; a different one is refused (protocol_mismatch)",
    )
    sub = p.add_subparsers(dest="cmd", required=True, metavar="<subcommand>")
    jobid_help = "the job's id (16 hex digits)"

    _ = sub.add_parser("version", help="installed code version, features, user and hostname")
    _ = sub.add_parser("inspect", help="read-only: job counts and incompatible job directories")
    r = sub.add_parser("receive", help="read a package (tar.gz) on stdin and queue it as a job")
    _ = r.add_argument(
        "--submission-key",
        metavar="KEY",
        help="idempotency key: a key seen before returns its job (default: a new key)",
    )
    j = sub.add_parser("jobs", help="list the jobs, oldest first")
    _ = j.add_argument("--state", help="only jobs in this state")
    _ = j.add_argument(
        "--limit", type=int, metavar="N", help="only the N most recently submitted jobs"
    )
    s = sub.add_parser("status", help="one job, with its run, progress and result")
    _ = s.add_argument("jobid", help=jobid_help)
    s = sub.add_parser("cancel", help="cancel a job (queued: at once; running: stop it)")
    _ = s.add_argument("jobid", help=jobid_help)
    _ = s.add_argument("--by", help="user@hostname of the client asking for the cancel")
    s = sub.add_parser("retry", help="queue a finished job again as a new job")
    _ = s.add_argument("jobid", help=jobid_help)
    s = sub.add_parser("resolve", help="settle a job in the unknown state")
    _ = s.add_argument("jobid", help=jobid_help)
    _ = s.add_argument(
        "--as",
        dest="as_",
        metavar="STATE",
        help="stopped: declare that the job's processes are gone (needs --confirm)",
    )
    _ = s.add_argument("--confirm", metavar="JOBID", help="the jobid again, to confirm --as")
    lg = sub.add_parser("logs", help="print a job's log (raw text)")
    _ = lg.add_argument("jobid", help=jobid_help)
    _ = lg.add_argument(
        "--stream",
        choices=("stdout", "stderr", "shim"),
        default="stdout",
        help="which log (default stdout)",
    )
    _ = lg.add_argument("--follow", action="store_true", help="keep printing until the job ends")
    fe = sub.add_parser("fetch", help="write a job's directory as tar.gz on stdout")
    _ = fe.add_argument("jobid", help=jobid_help)
    _ = sub.add_parser("serve", help="run the scheduler daemon (the systemd service runs this)")
    sh = sub.add_parser("shim", help="internal: supervise one job's run (started by the daemon)")
    _ = sh.add_argument("jobid", help=jobid_help)
    _ = sh.add_argument(
        "--lock-fd", type=int, required=True, help="inherited fd of the job's lock file"
    )
    return p


COMMANDS: dict[str, Callable[[Store, Args], int]] = {
    "receive": cmd_receive,
    "jobs": cmd_jobs,
    "status": cmd_status,
    "cancel": cmd_cancel,
    "retry": cmd_retry,
    "resolve": cmd_resolve,
    "logs": cmd_logs,
    "fetch": cmd_fetch,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv, namespace=Args())
    stream_cmd = args.cmd in ("logs", "fetch")
    try:
        store = Store(args.root)
        if args.cmd == "shim":
            from . import shim

            return shim.main(store, args.jobid, args.lock_fd)
        if args.cmd == "serve":
            from .daemon import Daemon

            return Daemon(store).run()
        if args.protocol_version is not None and args.protocol_version != remote_protocol_version():
            raise TaskmgrError(
                "protocol_mismatch",
                "client protocol %s is incompatible with remote protocol %s"
                % (args.protocol_version, remote_protocol_version()),
            )
        if args.cmd == "version":
            return emit(version_info())
        if args.cmd == "inspect":  # read-only: no ensure_layout
            return cmd_inspect(store, args)
        store.ensure_layout()
        return COMMANDS[args.cmd](store, args)
    except TaskmgrError as e:
        if stream_cmd:
            _ = sys.stderr.write(
                json.dumps(dict(e.to_json(), protocol_version=PROTOCOL_VERSION)) + "\n"
            )
            return e.exit_code
        return emit(e.to_json(), e.exit_code)
    except BrokenPipeError:
        return 0
    except Exception as e:  # noqa: BLE001 - always answer with JSON
        import traceback

        _ = sys.stderr.write(traceback.format_exc())
        err: dict[str, object] = {
            "error": {"code": "internal_error", "message": "%s: %s" % (type(e).__name__, e)}
        }
        if stream_cmd:
            _ = sys.stderr.write(json.dumps(dict(err, protocol_version=PROTOCOL_VERSION)) + "\n")
            return 2
        return emit(err, 2)


if __name__ == "__main__":
    sys.exit(main())
