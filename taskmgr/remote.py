"""`taskmgr-remote`: remote subcommands, the scheduler daemon and the shim entry point.

Invoked by the local CLI as
    ssh <host> taskmgr-remote --root <remote_root> --protocol-version N <subcommand> ...
Every subcommand except `logs`, `fetch`, `serve` and `shim` prints one JSON object.
"""

import argparse
import json
import os
import secrets
import shutil
import stat
import sys
import tarfile
import time

from . import package
from .common import (PROTOCOL_VERSION, SCHEMA_VERSION, SUBMISSION_KEY_RE, TERMINAL_STATES,
                     TaskmgrError, atomic_write_json, fsync_dir, read_json, try_flock_fd,
                     try_read_json, utcnow_iso)
from .gpu import resolve_declarations
from .monitor import assess, identity_of
from .store import Store, end_without_result, job_summary, remove_tree


def remote_protocol_version():
    # TASKMGR_TEST_PROTOCOL_VERSION: test-only override to simulate a mismatched remote.
    v = os.environ.get("TASKMGR_TEST_PROTOCOL_VERSION")
    return int(v) if v else PROTOCOL_VERSION


def emit(obj, code=0):
    obj = dict(obj)
    obj["protocol_version"] = remote_protocol_version()
    sys.stdout.write(json.dumps(obj, sort_keys=True) + "\n")
    sys.stdout.flush()
    return code


# ---------------------------------------------------------------------------
# receive (Section 5.2)

def cmd_receive(store, args):
    store.ensure_layout()
    key = args.submission_key or ("auto-" + secrets.token_hex(12))
    if not SUBMISSION_KEY_RE.match(key):
        raise TaskmgrError("bad_submission_key", "submission key must match "
                           "^[A-Za-z0-9._-]{1,128}$")
    updir = store.path("incoming", "up-%s-%s" % (time.strftime("%Y%m%d%H%M%S"),
                                                 secrets.token_hex(6)))
    os.mkdir(updir)
    lock_fd = try_flock_fd(updir, directory=True)
    try:
        tar_path = os.path.join(updir, "package.tar.gz")
        with open(tar_path, "wb") as f:
            shutil.copyfileobj(sys.stdin.buffer, f, 1 << 20)
            f.flush()
            os.fsync(f.fileno())
        if os.environ.get("TASKMGR_TEST_RECEIVE_PAUSE"):  # test-only: hold the upload open
            time.sleep(float(os.environ["TASKMGR_TEST_RECEIVE_PAUSE"]))
        ext = os.path.join(updir, "extract")
        package.safe_extract(tar_path, ext, store.config.get("max_package_bytes"))
        root = package.package_root(ext)
        errors, task = package.validate_dir(root)
        if errors:
            raise TaskmgrError("invalid_package", "package validation failed (%d errors)"
                               % len(errors), details=errors)
        chash = package.content_hash(root)
        gpus = resolve_declarations(task.get("gpus") or [])
        pkg = os.path.join(updir, "package")
        os.rename(root, pkg)
        if os.path.exists(ext):
            remove_tree(ext)
        _make_read_only(pkg)
        with store.state_lock():
            kpath = store.path("submission-keys", key + ".json")
            mapping = read_json(kpath)
            if mapping and store.find_job_dir(mapping.get("jobid")):
                if mapping.get("content_hash") != chash:
                    raise TaskmgrError("submission_key_conflict",
                                       "submission key %s was already used for different "
                                       "content (job %s)" % (key, mapping["jobid"]))
                _, old = store.load_job(mapping["jobid"])
                return emit({"ok": True, "jobid": mapping["jobid"], "task_id": old["task_id"],
                             "submission_key": key, "deduplicated": True,
                             "content_hash": chash, "state": old["state"]})
            jobid = store.new_jobid()
            now = utcnow_iso()
            job = {
                "schema_version": SCHEMA_VERSION, "jobid": jobid, "task_id": task["task_id"],
                "state": "queued", "submitted_at": now, "updated_at": now,
                "enqueue_seq": store.next_seq(), "content_hash": chash,
                "submission_key": key, "gpus": gpus, "cancel_requested": False,
                "auto_restarts": 0, "attempts": [], "current_attempt": None,
                "stop_reason": None, "error": None, "retries": 0,
            }
            atomic_write_json(os.path.join(updir, "job.json"), job)
            # The key mapping is written first; a mapping whose job does not exist is stale.
            atomic_write_json(kpath, {"submission_key": key, "jobid": jobid,
                                      "content_hash": chash, "created_at": now})
            os.rename(updir, store.path("queue", jobid))
            fsync_dir(store.path("incoming"))
            fsync_dir(store.path("queue"))
        return emit({"ok": True, "jobid": jobid, "task_id": task["task_id"],
                     "submission_key": key, "deduplicated": False, "content_hash": chash,
                     "state": "queued"})
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if os.path.exists(updir):
            remove_tree(updir)


def _make_read_only(root):
    for dirpath, dirnames, filenames in os.walk(root):
        for f in filenames:
            p = os.path.join(dirpath, f)
            if not os.path.islink(p):
                os.chmod(p, stat.S_IMODE(os.lstat(p).st_mode) & ~0o222)


# ---------------------------------------------------------------------------
# queries

def cmd_jobs(store, args):
    jobs = [job_summary(j) for _, j in store.iter_jobs()
            if not args.state or j.get("state") == args.state]
    jobs.sort(key=lambda j: (j.get("submitted_at") or "", j["jobid"]))
    return emit({"jobs": jobs})


def synthesized_result(job):
    return {"schema_version": SCHEMA_VERSION, "_taskmgr": {
        "jobid": job["jobid"], "task_id": job.get("task_id"), "attempt": None,
        "status": job["state"], "return_code": None, "signal": None, "error": job.get("error"),
        "gpus": job.get("gpus", []), "validation_errors": [], "warnings": [],
        "note": "the job ended without running an attempt", "final": True}}


def job_status(store, jobid):
    jd = store.find_job_dir(jobid)
    if jd is None:
        return {"found": False, "jobid": jobid}
    _, job = store.load_job(jobid)
    out = {"found": True, "job": job_summary(job), "attempts": store.existing_attempts(jd),
           "terminal": job["state"] in TERMINAL_STATES}
    n = job.get("current_attempt")
    adir = store.attempt_dir(jd, n) if n else None
    if out["terminal"]:
        res = None
        if adir:
            res, _ = try_read_json(os.path.join(adir, "workdir", "result.json"))
        out["result"] = res if isinstance(res, dict) else synthesized_result(job)
        return out
    if adir:
        runner, _ = store.read_runner(adir)
        runner = runner or {}
        info = {k: runner.get(k) for k in ("attempt", "status", "dispatched_at", "started_at",
                                           "exited", "return_code", "signal", "takeovers")}
        info["identity"] = identity_of(runner)
        out["attempt_info"] = info
        prog, err = try_read_json(os.path.join(adir, "workdir", "progress.json"))
        out["progress"] = prog if prog is not None else (None if err == "missing" else
                                                         {"error": err})
    return out


def cmd_status(store, args):
    return emit(job_status(store, args.jobid))


# ---------------------------------------------------------------------------
# control (Section 13)

def cmd_cancel(store, args):
    with store.state_lock():
        jd, job = store.load_job(args.jobid)
        changed = False
        if job["state"] in TERMINAL_STATES:
            pass
        elif job["state"] == "queued":
            job["cancel_requested"] = True
            end_without_result(store, jd, job, None, "cancelled", None, None)
            changed = True
        elif not job.get("cancel_requested"):
            job["cancel_requested"] = True  # shim / daemon picks it up within control_poll
            store.save_job(jd, job)
            changed = True
    return emit({"jobid": job["jobid"], "state": job["state"], "changed": changed,
                 "cancel_requested": job.get("cancel_requested", False)})


def cmd_retry(store, args):
    with store.state_lock():
        jd, job = store.load_job(args.jobid)
        if job["state"] == "unknown":
            raise TaskmgrError("resolve_required", "job %s is unknown; run `task resolve` first"
                               % args.jobid)
        if job["state"] not in TERMINAL_STATES:
            raise TaskmgrError("not_terminal", "job %s is %s; only terminal jobs can be retried"
                               % (args.jobid, job["state"]))
        job.update({"state": "queued", "enqueue_seq": store.next_seq(), "cancel_requested": False,
                    "auto_restarts": 0, "error": None, "stop_reason": None,
                    "current_attempt": None, "retries": job.get("retries", 0) + 1,
                    "ended_at": None})
        store.save_job(jd, job)
    return emit({"jobid": job["jobid"], "state": "queued", "changed": True,
                 "enqueue_seq": job["enqueue_seq"]})


def cmd_resolve(store, args):
    if args.as_ is not None:
        if args.as_ != "stopped":
            raise TaskmgrError("bad_args", "--as only accepts 'stopped'")
        if args.confirm != args.jobid:
            raise TaskmgrError("bad_args", "--confirm must repeat the jobid %s" % args.jobid)
        with store.state_lock():
            jd, job = store.load_job(args.jobid)
            if job["state"] != "unknown":
                raise TaskmgrError("not_unknown", "job %s is %s, not unknown"
                                   % (args.jobid, job["state"]))
            n = job.get("current_attempt")
            runner = None
            if n:
                adir = store.attempt_dir(jd, n)
                runner, _ = store.read_runner(adir)
                runner = runner if isinstance(runner, dict) else {"attempt": n}
                runner.update({"final": True, "status": "interrupted",
                               "interrupted_reason": "operator_marked_stopped",
                               "ended_at": runner.get("ended_at") or utcnow_iso()})
                if os.path.isdir(adir):
                    store.write_runner(adir, runner)
                for a in job.get("attempts", []):
                    if a.get("attempt") == n:
                        a["status"] = "interrupted"
            if job.get("cancel_requested"):
                end_without_result(store, jd, job, n, "cancelled", None, runner)
            else:
                end_without_result(store, jd, job, n, "failed", {
                    "kind": "operator_marked_stopped",
                    "message": "operator confirmed all processes are gone"}, runner)
        return emit({"jobid": args.jobid, "state": job["state"], "changed": True})
    with store.state_lock():
        _, job = store.load_job(args.jobid)
        if job["state"] != "unknown":
            return emit({"jobid": args.jobid, "state": job["state"], "changed": False})
        res = assess(store, args.jobid, resolve=True)
        _, job = store.load_job(args.jobid)
    out = {"jobid": args.jobid, "state": job["state"], "changed": job["state"] != "unknown",
           "decision": res["decision"]}
    for k in ("identity", "alive_pids", "reason"):
        if k in res:
            out[k] = res[k]
    if job["state"] == "unknown":
        jd = store.find_job_dir(args.jobid)
        runner, _ = store.read_runner(store.attempt_dir(jd, job["current_attempt"])) \
            if job.get("current_attempt") else (None, None)
        out["identity"] = identity_of(runner)
        out["hint"] = ("still undecided; check `ps -o pid,pgid,lstart,cmd -g <pgid>` and "
                       "nvidia-smi, kill leftovers, then run resolve --as stopped --confirm "
                       "<jobid>")
    return emit(out)


# ---------------------------------------------------------------------------
# logs and fetch (Section 15)

def _pick_attempt(store, jd, job, attempt):
    have = store.existing_attempts(jd)
    if attempt is not None:
        if attempt not in have:
            raise TaskmgrError("no_such_attempt", "attempt %d does not exist" % attempt)
        return attempt
    if job.get("current_attempt"):
        return job["current_attempt"]
    return have[-1] if have else None


def cmd_logs(store, args):
    jd, job = store.load_job(args.jobid)
    n = _pick_attempt(store, jd, job, args.attempt)
    while n is None:
        if not args.follow or job["state"] in TERMINAL_STATES:
            raise TaskmgrError("no_attempt", "job %s has no attempt yet" % args.jobid)
        time.sleep(0.5)
        jd, job = store.load_job(args.jobid)
        n = _pick_attempt(store, jd, job, None)
    name = {"stdout": "stdout.log", "stderr": "stderr.log", "shim": "shim.log"}[args.stream]
    out = sys.stdout.buffer
    f = None
    while True:
        jd, job = store.load_job(args.jobid)
        adir = store.attempt_dir(jd, n)
        if f is None:
            try:
                f = open(os.path.join(adir, name), "rb")
            except FileNotFoundError:
                f = None
        if f is not None:
            while True:
                chunk = f.read(1 << 16)
                if not chunk:
                    break
                out.write(chunk)
            out.flush()
        if not args.follow:
            return 0
        runner, _ = store.read_runner(adir)
        ended = (isinstance(runner, dict) and runner.get("final")) \
            or job["state"] in TERMINAL_STATES or job["state"] == "unknown" \
            or job.get("current_attempt") != n
        if ended:
            if f is None:
                try:
                    f = open(os.path.join(adir, name), "rb")
                except FileNotFoundError:
                    return 0
            out.write(f.read())
            out.flush()
            return 0
        time.sleep(0.5)


def cmd_fetch(store, args):
    jd, job = store.load_job(args.jobid)
    have = store.existing_attempts(jd)
    if args.all_attempts:
        nums = have
    else:
        n = _pick_attempt(store, jd, job, args.attempt)
        nums = [n] if n else []
    if not nums:
        raise TaskmgrError("no_attempt", "job %s has no attempt to fetch" % args.jobid)
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as tar:
        for n in nums:
            package.add_tree_to_tar(tar, store.attempt_dir(jd, n), "attempts/%04d" % n)
    sys.stdout.buffer.flush()
    return 0


# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="taskmgr-remote")
    p.add_argument("--root", required=True, help="remote storage root (from the machine list)")
    p.add_argument("--protocol-version", type=int, default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("version")
    r = sub.add_parser("receive")
    r.add_argument("--submission-key")
    j = sub.add_parser("jobs")
    j.add_argument("--state")
    for name in ("status", "cancel", "retry"):
        s = sub.add_parser(name)
        s.add_argument("jobid")
    s = sub.add_parser("resolve")
    s.add_argument("jobid")
    s.add_argument("--as", dest="as_")
    s.add_argument("--confirm")
    lg = sub.add_parser("logs")
    lg.add_argument("jobid")
    lg.add_argument("--attempt", type=int)
    lg.add_argument("--stream", choices=("stdout", "stderr", "shim"), default="stdout")
    lg.add_argument("--follow", action="store_true")
    fe = sub.add_parser("fetch")
    fe.add_argument("jobid")
    fe.add_argument("--attempt", type=int)
    fe.add_argument("--all-attempts", action="store_true")
    sub.add_parser("serve")
    sh = sub.add_parser("shim")
    sh.add_argument("jobid")
    sh.add_argument("attempt", type=int)
    sh.add_argument("--lock-fd", type=int, required=True)
    return p


COMMANDS = {"receive": cmd_receive, "jobs": cmd_jobs, "status": cmd_status,
            "cancel": cmd_cancel, "retry": cmd_retry, "resolve": cmd_resolve,
            "logs": cmd_logs, "fetch": cmd_fetch}


def main(argv=None):
    args = build_parser().parse_args(argv)
    stream_cmd = args.cmd in ("logs", "fetch")
    try:
        store = Store(args.root)
        if args.cmd == "shim":
            from . import shim
            return shim.main(store, args.jobid, args.attempt, args.lock_fd)
        if args.cmd == "serve":
            from .daemon import Daemon
            return Daemon(store).run()
        if args.protocol_version is not None and args.protocol_version != \
                remote_protocol_version():
            raise TaskmgrError("protocol_mismatch", "client protocol %s is incompatible with "
                               "remote protocol %s" % (args.protocol_version,
                                                       remote_protocol_version()))
        if args.cmd == "version":
            return emit({"ok": True})
        store.ensure_layout()
        return COMMANDS[args.cmd](store, args)
    except TaskmgrError as e:
        if stream_cmd:
            sys.stderr.write(json.dumps(dict(e.to_json(), protocol_version=PROTOCOL_VERSION))
                             + "\n")
            return e.exit_code
        return emit(e.to_json(), e.exit_code)
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
