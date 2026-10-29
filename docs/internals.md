---
title: Remote layout and state machine
tags: [taskmgr, internals, state-machine, schema]
lang: en
up: "[[taskmgr/README]]"
---

# Remote layout and state machine

Other language: [[docs/internals.zh-CN]]

## Remote directory layout

```
<remote_root>/
  config.json
  locks/state.lock          serializes every state change (all processes, all clients)
  locks/daemon.lock         daemon instance lock (holds the daemon pid)
  locks/counters.json       next enqueue_seq (assumption: location not in the spec)
  incoming/up-*/            uploads in flight (flock held on the directory); never scheduled
  incoming/retry-<jobid>/   a retry or re-run being built (same rules)
  submission-keys/<key>.json  {"submission_key", "jobid", "content_hash", "created_at"}
  queue/<jobid>/            queued
  in-progress/<jobid>/      running / stopping / unknown
  done/<jobid>/             terminal
  daemon/daemon.log         daemon log (also written to stderr / journald)
```

```
<jobid>/
  job.json          state record (below)
  package.tar.gz    original upload, immutable
  package/          read-only extracted copy; the run starts from it
  runner.json       authoritative run record, written only by the runner
  shim.lock         flock held by the shim for its whole life
  stdout.log  stderr.log  shim.log
  workdir/          fresh copy of package/ + progress.json, result.json, artifacts/
```

A job runs at most once. A retry (`task retry`) or an automatic re-run after an interruption
is a new job with its own jobid and directory, built from copies of `package.tar.gz` and
`package/` under `incoming/retry-<jobid>/` and renamed into `queue/`.

`job.json`:

```json
{"schema_version": 1, "jobid": "3f9a1c0e7b2d4a56", "task_id": "AA-01", "state": "running",
 "submitted_at": "...", "updated_at": "...", "ended_at": "...", "enqueue_seq": 17,
 "content_hash": "<sha256>", "submission_key": "cli-...",
 "gpus": [{"declared": 0, "uuid": "GPU-..."}],
 "cancel_requested": false, "stop_reason": null | "cancel" | "timeout" | "leftover",
 "auto_restarts": 0, "dispatched_at": null | "...",
 "retry_of": null | "<jobid this job re-runs>", "retried_as": ["<jobids of its re-runs>"],
 "error": null}
```

`auto_restarts` counts the automatic re-runs in a chain (0 for a submission or a manual
retry, the predecessor's count + 1 for an automatic re-run). `dispatched_at` is set when the
job is dispatched; a job without it never ran and has no run files.

`runner.json`:

```json
{"schema_version": 1, "jobid": "...", "status": "dispatched|starting|running|
  <terminal>|interrupted", "gpus": [...], "dispatched_at": "...",
 "boot_id": "...", "shim_pid": 1234, "shim_start_time": 98765, "pgid": 1240,
 "leader_pid": 1240, "leader_start_time": 98770, "argv_hash": "<sha256 of argv JSON>",
 "argv": [...], "started_at": "...", "started_at_epoch": 1761725700.1,
 "exited": true, "return_code": 0, "signal": null, "ended_at": "...",
 "final": true, "taskmgr": {<the _taskmgr block>}, "takeovers": [{"at", "by_pid"}],
 "stop_unconfirmed_reason": null, "interrupted_reason": null}
```

Every JSON write goes to a temp file in the same directory, is fsynced, `os.replace`d, and
the directory is fsynced.

## State machine

| State | Dir | Terminal | Holds GPUs |
| --- | --- | --- | --- |
| queued | queue/ | no | no |
| running | in-progress/ | no | yes |
| stopping | in-progress/ | no | yes |
| unknown | in-progress/ | no | yes |
| succeeded / failed / invalid_result / cancelled / timed_out | done/ | yes | no |

```
queued --dispatch--> running --exit--> succeeded | failed | invalid_result
queued --cancel--> cancelled
running --shim fork/exec failure--> failed(shim_start_failed)
running --pre-start check failure--> failed(setup)
running --cancel seen / timeout--> stopping --group gone--> cancelled | timed_out
stopping --still alive after SIGKILL--> unknown
running/stopping --restart recovery--> see below
unknown --resolve (check concludes)--> see below
unknown --resolve --as stopped--> failed(operator_marked_stopped) | cancelled
terminal --retry--> (unchanged; a new queued job with retry_of = this job)
```

Transitions happen under `locks/state.lock`: `job.json` is written first, then the
directory is renamed; if they disagree, `job.json` wins and daemon startup moves the
directory. Run status (`runner.json`) is a terminal state or `interrupted`.

### Scheduling

Each pass (under the state lock) walks queued jobs by `enqueue_seq` keeping a reserved set:
a job starts only if all its GPUs are free and unreserved, otherwise its GPUs are added to
the reserved set; a job touching a GPU held by an `unknown` job neither starts nor reserves.
Jobs with `gpus: []` only obey `max_cpu_jobs`. Dispatch writes `runner.json`, then
`job.json` (running) and the rename, and only then double-forks the shim (new session,
reparented to init) and hands it the already-locked `shim.lock`. A failed fork/exec ends
the job as `failed` (`shim_start_failed`). A queued job that already has a `runner.json`
(a crash between the two writes) is dispatched again if the record has no execution
identity; if it has one, the job becomes `unknown` and keeps its GPUs.

### Shim

1. Copy `package/` to `workdir/`, build the environment, run pre-start checks (venv and
   required paths exist, entry resolves and is executable, declared UUIDs visible via
   nvidia-smi). Failure → `failed`, `error.kind=setup`, `return_code: null`.
2. Fork; the child `setsid()`s and blocks on a pipe. The shim writes the identity record
   (`boot_id`, shim pid/start time, pgid, leader pid/start time, argv hash) with fsync, then
   releases the child, which execs the entry. No identity ⇒ the entry never ran.
3. Poll `waitpid`, re-read `job.json` every `control_poll_seconds` for `cancel_requested`,
   enforce `timeout_seconds` (from entry start).
4. On exit: write `exited/return_code/signal/ended_at`; stop leftover group members
   (warning `leftover_processes`); merge `result.json`; mark `runner.json` final; update
   `job.json`.

### Stop procedure

SIGTERM to the process group → wait `stop_grace_seconds` → SIGKILL → wait
`kill_confirm_seconds`. Only when no member of the group is alive are GPUs released and the
terminal state entered; otherwise the job becomes `unknown` and keeps its GPUs. Membership
is "pgid equals the recorded one and start time ≥ the leader's start time" via `/proc`;
zombies count as gone.
