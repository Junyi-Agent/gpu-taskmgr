---
title: Assumptions and limitations
tags: [taskmgr, assumptions, limitations]
lang: en
up: "[[taskmgr/README]]"
---

# Assumptions and limitations

Other language: [[docs/assumptions.zh-CN]]

## Assumptions (where the spec is silent)

1. An uploaded archive may contain the package at its root or inside one top-level
   directory (e.g. `tar czf AA-01.tar.gz AA-01/`).
2. `task init` writes a valid `progress.json` (current time) and puts `<REQUIRED...>`
   markers in `task.json` (`gpus`, `environment.venv`, `result.required_outputs`) and in
   every `TASK.md` section; it fills `task_id` from the argument. `--output` picks the
   directory (default `./<task_id>`).
3. Without `--submission-key` the CLI generates `cli-<32 hex>`.
4. The submission-key mapping is written before the rename into `queue/`, both under the
   state lock. A mapping whose job does not exist (crash in between) is treated as stale
   and overwritten.
5. `task cancel` on a running job only sets `cancel_requested`; the shim (or the daemon
   during takeover) moves it to `stopping` within `control_poll_seconds`. A cancel that
   arrives after the entry has already exited does not change its outcome. A cancel seen
   before the entry starts goes running → stopping → cancelled without running it.
6. `task status` of a finished job prints `result.json` with two added top-level keys,
   `host`, `complete`, `retry_of` and `retried_as`. A job that ended without running
   (cancelled while queued) prints a synthesized `{"schema_version", "_taskmgr": {...}}`.
7. `--wait --timeout` expiry prints the current status object plus an `error`
   (`wait_timeout`) and exits 2. Ctrl-C prints an `interrupted` error and exits 2.
8. Protocol compatibility means equal integer `protocol_version` (currently 2; 1 was the
   attempt-based protocol). ssh exit status 255 is treated as a connection failure; ssh runs
   with `BatchMode=yes`.
9. Leftover cleanup after the entry exits moves the job to `stopping` with
   `stop_reason: "leftover"`; a confirmed stop finalizes from the real exit code, an
   unconfirmed one puts the job into `unknown` (same as cancel/timeout).
10. `resolve` with live group members: if the unknown came from a check that could not be
    decided (unreadable `/proc`, incomplete record), the job goes back to running/stopping
    and the daemon takes over tracking (case 4.4). If SIGKILL already failed
    (`stop_unconfirmed_reason` set) the job stays unknown for the operator. A cancel
    recorded on an unknown job makes it cancelled once the stop is confirmed.
11. `_taskmgr.paths` point to the job's final location (`done/...`); while running,
    `TASKMGR_WORKDIR` and friends point to `in-progress/...` (the directory moves when the
    job ends).
12. `logs --follow` ends when the run is final or the job is terminal or unknown; on a
    queued job it first waits for the dispatch.
13. `fetch` skips unsafe entries (absolute/escaping symlinks a task created, special files)
    with a warning and lists them in `skipped`. It downloads `job.json`, `runner.json`, the
    three logs and `workdir/`, not `package/`, `package.tar.gz` or `shim.lock`.
14. `max_cpu_jobs` counts CPU-only jobs in running, stopping and unknown.
15. jobid uniqueness is checked against all job directories on the server.
    `receive` answers a known submission key (dedup or conflict) right after extraction and
    hashing, before GPU resolution and validation, so a retry after a lost ACK succeeds even
    if the machine's GPUs changed. A corrupt `result.json` is kept as
    `result.json.corrupt` (or `.corrupt.N` if that name is taken).
16. `_taskmgr.host` is the remote machine's hostname.
17. Extra config key `scheduler_poll_seconds`; extra command `task hosts`.
18. A retry or automatic re-run job gets its own `submitted_at` (the time it was created),
    so `task jobs` lists it as a separate row after the jobs it re-runs. It copies the
    resolved GPU UUIDs of the old job and is not re-validated. A queued job that already
    has a `runner.json` with an execution identity (not expected) becomes `unknown` with
    `error.kind = "record_incomplete"` instead of being dispatched.

## Limitations

- Only the task's process group is managed. A process that calls `setsid()`/`setpgid()`
  itself leaves the group and is neither tracked nor stopped by taskmgr.
- No MIG support; GPU usage by processes outside taskmgr is not detected or prevented.
- `remote_root` must be on a local filesystem (flock is unreliable on NFS).
- No checkpoint/resume: an automatic re-run starts from the original package.
- No installation of software or environments; venvs and paths must exist on the server.
