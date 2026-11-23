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
2. `task create package` writes a valid `progress.json` (current time) and puts `<REQUIRED...>`
   markers in `task.json` (`gpus`, `environment.venv`, `result.required_outputs`) and in
   every `TASK.md` section; it fills `task_id` from the argument. `--dest` picks the
   directory (default `./<task_id>`).
3. Without `--submission-key` the CLI generates `cli-<32 hex>`.
4. The submission-key mapping is written before the rename into `queue/`, both under the
   state lock. A mapping whose job does not exist (crash in between) is treated as stale
   and overwritten.
5. `task cancel job` on a running job only sets `cancel_requested`; the shim (or the daemon
   during takeover) moves it to `stopping` within `control_poll_seconds` and
   sends SIGKILL with no grace period, so the entry gets no chance to clean up. A cancel that
   arrives after the entry has already exited does not change its outcome. A cancel seen
   before the entry starts goes running → stopping → cancelled without running it.
6. `task get job` of a finished job prints `result.json` with these top-level keys added:
   `host`, `complete`, `retry_of` and `retried_as`. A job that ended without running
   (cancelled while queued) prints a synthesized `{"schema_version", "_taskmgr": {...}}`.
7. `task wait job --timeout S` waits S seconds in total: each status query (and the lookup of the
   jobid on every server) gets at most the time left, so a server that does not answer
   cannot extend the wait. Expiry prints the last status object (empty when no query
   answered) plus an `error` (`wait_timeout`) and exits 2. When a query that returns after
   the deadline shows a terminal state (or `unknown`), that state wins and the exit code is
   the one of that state. Ctrl-C prints an `interrupted` error and exits 2.
8. Protocol compatibility means equal integer `protocol_version` (currently 2; 1 was the
   attempt-based protocol). Additions that keep protocol 2 are announced in the reply key
   `features` ([[docs/task-package]], Server features); a client checks a feature before it
   relies on it. ssh exit status 255 is treated as a connection failure for
   `taskmgr-remote` subcommands. For the shell scripts `task create host` runs, a script may
   exit 255 itself, so 255 counts as a connection failure only when stderr reads like an
   ssh error (`ssh:`, `connection refused`, `permission denied (`, ...). ssh runs with
   `BatchMode=yes` and, unless the machine entry sets them, `ConnectTimeout=10`,
   `ServerAliveInterval=15` and `ServerAliveCountMax=3` (a server that stops answering
   drops the connection after about 45 s).
9. Leftover cleanup after the entry exits moves the job to `stopping` with
   `stop_reason: "leftover"`; a confirmed stop finalizes from the real exit code, an
   unconfirmed one puts the job into `unknown` (same as cancel/timeout).
10. `task resolve job` with live group members: if the unknown came from a check that could not be
    decided (unreadable `/proc`, incomplete record), the job goes back to running/stopping
    and the daemon takes over tracking (case 4.4). If SIGKILL already failed
    (`stop_unconfirmed_reason` set) the job stays unknown for the operator. A cancel
    recorded on an unknown job makes it cancelled once the stop is confirmed.
11. `_taskmgr.paths` point to the job's final location (`done/...`); while running,
    `TASKMGR_WORKDIR` and friends point to `in-progress/...` (the directory moves when the
    job ends).
12. `task logs job --follow` ends when the run is final or the job is terminal or unknown; on a
    queued job it first waits for the dispatch.
13. `task fetch job` skips unsafe entries (absolute/escaping symlinks a task created, special files)
    with a warning and lists them in `skipped`. The server leaves special files (FIFOs,
    sockets, devices) out of the stream and names them in the stream's last member,
    `taskmgr-fetch.json`; the client merges that list into `skipped` and does not keep the
    file. It downloads `job.json`, `runner.json`, the
    three logs and `workdir/`, not `package/`, `package.tar.gz` or `shim.lock`.
14. `max_cpu_jobs` counts CPU-only jobs in running, stopping and unknown.
15. jobid uniqueness is checked against all job directories on the server.
    `receive` answers a known submission key (dedup or conflict) right after extraction and
    hashing, before GPU resolution and validation, so a retry after a lost ACK succeeds even
    if the machine's GPUs changed. A corrupt `result.json`, or one that is not a regular
    file, is kept as `result.json.corrupt` (or `.corrupt.N` if that name is taken).
16. `_taskmgr.host` is the remote machine's hostname.
17. Extra config key `scheduler_poll_seconds`; extra commands `task get hosts`, `task update|delete|enable|disable|ping host`, `task whoami`, `task update`, `task version`.
18. A retry or automatic re-run job gets its own `submitted_at` (the time it was created),
    so `task get jobs` lists it as a separate row after the jobs it re-runs. It copies the
    resolved GPU UUIDs of the old job (or its `gpu_request`, and chooses again) and is not
    re-validated. A queued job that already
    has a `runner.json` with an execution identity (not expected) becomes `unknown` with
    `error.kind = "record_incomplete"` instead of being dispatched.
19. GPU busy (desc-2 D17): `pending_reason` is removed once a relaunch has kept running
    for `gpu_retry_interval_seconds`, not at the moment it starts, so a job that keeps
    getting exit 75 does not flicker between `running` and `pending-gpu`. A
    leftover cleanup after exit 75 keeps the `leftover_processes` warning for the final
    result. The pre-start checks are not repeated for a relaunch. When the shim died
    between exit 75 and the relaunch, `runner.json` is kept as `runner.prev-N.json`. An
    `unknown` job is never queued again, also after exit 75. `claim_gpu()` does nothing
    when `CUDA_VISIBLE_DEVICES` is unset (a run outside taskmgr), as when it is empty.
20. Disabled servers (by user decision, departs from desc-1 §14): `task get jobs` and the jobid
    lookup without `--host` (also `whoami`, `version`) ask only enabled servers, so a server
    that is down and disabled with `task disable host` costs no timeout. The output names
    the skipped servers; `--host` still reaches a disabled server. desc-1 queried disabled
    servers too, because they may still hold jobs.
21. Command line (by user decision, departs from desc-1): kubectl-style
    `task <verb> <resource> [args]` (`task create job PKG`, `task get jobs`,
    `task create host NAME`), every resource also under its plural name, and `-o text|json`
    for the output format. A command name of desc-1 is a usage error that names the new
    form; [[docs/usage]] (Command changes) lists them.

## Limitations

- Only the task's process group is managed. A process that calls `setsid()`/`setpgid()`
  itself leaves the group and is neither tracked nor stopped by taskmgr.
- No MIG support; GPU usage by processes outside taskmgr is not detected or prevented by
  taskmgr itself. A task that follows the GPU contract (exit 75, [[docs/task-package]])
  waits for such a GPU instead of failing; one that does not fails as before.
- A task that exits 75 for another reason is started again until it is cancelled.
- `remote_root` must be on a local filesystem (flock is unreliable on NFS).
- No checkpoint/resume: an automatic re-run starts from the original package.
- No installation of software or environments; venvs and paths must exist on the server.
  `task create host` installs only taskmgr itself (code, unit, `remote_root`).
- Two `task create host` runs for the same server user are serialized: from the end of step
  `ssh` to the end of the run a process on the server holds
  `$XDG_RUNTIME_DIR/taskmgr-hostsetup-<uid>.lock` (`/tmp/...` without
  `XDG_RUNTIME_DIR`), and a second run fails while it is held. `task create host` trusts the code version recorded inside `<remote_root>/taskmgr-remote.pyz`
  (an installed archive is never modified, see [[docs/install]]).
