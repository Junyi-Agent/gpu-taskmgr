# taskmgr

Task manager for GPU performance tuning. A local CLI (`task`) stores and submits task
packages; each remote GPU server runs `taskmgr-remote`, which persists its own queue,
schedules jobs onto GPUs, runs them, and recovers after restarts. There is no central
scheduler. Requirements: [[agent/desc-1.md]] (Python 3.10+ standard library, Linux on the
remote side).

```
taskmgr/
  taskmgr.py      local CLI entry (`task`)
  remote.py       remote entry (`taskmgr-remote`): subcommands, `serve`, `shim`
  daemon.py       scheduler daemon: queue scan, GPU reservation, shim spawn, recovery loop
  shim.py         per-attempt supervisor: pre-start checks, exec, wait, stop, finalize
  monitor.py      stop procedure, restart-recovery checks, daemon takeover tracking
  store.py        remote storage root, state lock, job/runner records, result merge
  package.py      validation, deterministic packing, content hash, safe extraction
  gpu.py          nvidia-smi query and GPU declaration resolution
  procutil.py     /proc: boot_id, start times, process-group members
  common.py       constants, atomic JSON writes, flock helpers
  template/       package template (`task init`) incl. taskmgr_task.py
  examples/       succeed/ (EX-01, CPU-only) and multi-gpu/ (MG-01, gpus [0, 1])
  deploy/         taskmgr-remote.service, machines.example.json, config.example.json
  tests/          unittest suite
bin/task, bin/taskmgr-remote   wrappers that run from a checkout
```

## Installation

### Local CLI

```sh
pip install -e /path/to/gpu-taskmgr        # installs `task` and `taskmgr-remote`
# or, without pip:
ln -s /path/to/gpu-taskmgr/bin/task ~/.local/bin/task

mkdir -p ~/.taskmgr
cp taskmgr/deploy/machines.example.json ~/.taskmgr/machines.json   # then edit
task hosts
```

Local state lives in `$TASKMGR_HOME` (default `~/.taskmgr`, or `task --home DIR ...`):

| File | Content |
| --- | --- |
| `machines.json` | `{"servers": [...]}`, the machine list (Section 4 of the spec) |
| `rr.json` | round-robin cursor `{"cursor": N, "last": name}` |
| `rr.lock` | flock serializing read+update of `rr.json` across concurrent CLIs |

Task packages are ordinary local directories (or `.tar.gz` files) that you keep wherever you
like; the local side keeps no jobid index and no run state.

### Remote server

```sh
sudo mkdir -p /opt/taskmgr /data/taskmgr && sudo chown tuner: /opt/taskmgr /data/taskmgr
git clone <repo> /opt/taskmgr            # provides /opt/taskmgr/bin/taskmgr-remote
cp /opt/taskmgr/taskmgr/deploy/config.example.json /data/taskmgr/config.json
```

`remote_root` (`/data/taskmgr` above) must be on a local filesystem (not NFS): all
coordination uses `flock`. The machine-list entry's `remote_command` must point to the
`taskmgr-remote` wrapper (or the pip-installed script).

#### Start / stop / enable at boot (systemd)

```sh
sudo cp /opt/taskmgr/taskmgr/deploy/taskmgr-remote.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now taskmgr-remote   # start now and at boot
sudo systemctl stop taskmgr-remote           # running tasks keep running
journalctl -u taskmgr-remote                 # daemon log
```

The unit uses **`KillMode=process`**. Shims are double-forked into their own sessions, but
they remain in the unit's cgroup, so the default `KillMode=control-group` would kill every
shim and task on `systemctl stop`. With `KillMode=process` systemd only signals the daemon's
main PID; the daemon exits on SIGTERM without touching shims, and on the next start runs
restart recovery. We chose this over `systemd-run --scope` per shim because it needs no
extra privileges or D-Bus access and keeps the daemon a plain process that also runs
outside systemd (tmux, nohup). Without systemd: `nohup taskmgr-remote --root /data/taskmgr
serve > /dev/null 2>&1 &` (log: `daemon/daemon.log`); stop with `kill <pid>` (pid in
`locks/daemon.lock`).

A second `serve` on the same root exits with code 2 (it cannot take `locks/daemon.lock`).

## Remote `config.json`

`<remote_root>/config.json`; every key is optional.

| Key | Default | Meaning |
| --- | --- | --- |
| `max_package_bytes` | 1073741824 | max total extracted size of a received package |
| `max_cpu_jobs` | `null` (unlimited) | max concurrent jobs with `gpus: []` (running/stopping/unknown) |
| `max_auto_restarts` | 2 | automatic re-runs after reboot/never-started interruptions |
| `stop_grace_seconds` | 30 | SIGTERM → SIGKILL delay in the stop procedure |
| `kill_confirm_seconds` | 10 | wait after SIGKILL before declaring `unknown` |
| `control_poll_seconds` | 2 | how often shim / daemon re-read `job.json` for cancel; health-check period |
| `scheduler_poll_seconds` | 1 | scheduling pass interval (assumption: not in the spec) |
| `base_env` | `{}` | overrides/extends the entry process base environment |

## Usage

```sh
task init AA-01                        # ./AA-01/ template; fill every <REQUIRED...> marker
task validate AA-01/                   # exit 0 {"valid": true}, 1 with all errors, 2 bad path
task submit AA-01/                     # round-robin over enabled servers
task submit --host nebius-h200-2 AA-01.tar.gz
task submit --host nebius-h200-2 --submission-key tune-aa01-v3 AA-01/   # idempotent retry
task jobs [--host H] [--state running]
task status <jobid> [--host H]
task status <jobid> [--host H] --wait [--poll-interval 5] [--retry-timeout 300] [--timeout S]
task cancel  <jobid> --host H
task retry   <jobid> --host H
task resolve <jobid> --host H [--as stopped --confirm <jobid>]
task logs  <jobid> --host H [--attempt N] [--stream stdout|stderr|shim] [--follow]
task fetch <jobid> --host H --output ./results/ [--attempt N | --all-attempts]
task hosts                             # list configured servers (extra)
```

All commands except `logs` print one JSON object on stdout; errors are
`{"error": {"code", "message", "host"?, "details"?}}` with diagnostics on stderr.

| Exit | Meaning |
| --- | --- |
| 0 | success; `--wait`: job succeeded; `jobs`: at least one server answered |
| 1 | `--wait`: job ended failed / invalid_result / cancelled / timed_out; `validate`: invalid |
| 2 | parameter, connection, query or operation error; `jobs` with every server failing |
| 3 | `--wait` found the job in `unknown` |

Submit receipt: `{"host", "jobid", "task_id", "submission_key", "deduplicated",
"content_hash", "state"}`. If the outcome is uncertain (connection failure or no valid
reply), stderr names the chosen host and the submission key and prints the retry command
`task submit --host H --submission-key K PATH`; the CLI never resubmits to another server.

`task status` for an unfinished job prints
`{"host", "jobid", "task_id", "state", "cancel_requested", "attempt": {attempt, status,
dispatched_at, started_at, exited, return_code, signal, takeovers, identity}, "progress":
<progress.json or null>, "submitted_at", "gpus", "error", "complete"}`. For a finished job it
prints the final `result.json`.

## Task packages

| File | Required | Notes |
| --- | --- | --- |
| `task.json` | yes | execution config (below) |
| `TASK.md` | yes | for humans/agents; taskmgr never reads it |
| `progress.json` | yes | initial progress |
| entry script, code, inputs | as needed | |
| `taskmgr_task.py` | template | stdlib helper: `write_progress`, `update_progress`, `write_result`, `update_outputs` (flock + atomic replace) |

`task.json` (unknown extra keys are allowed and ignored):

```json
{
  "schema_version": 1,
  "task_id": "AA-01",
  "entry": {"argv": ["python", "run.py", "--config", "cfg.yaml"]},
  "gpus": [0, 1],
  "environment": {"venv": "/opt/venvs/tune", "required_paths": ["/usr/local/cuda-12.4"]},
  "env": {"NCCL_DEBUG": "WARN"},
  "timeout_seconds": 7200,
  "result": {"required_outputs": ["best_config", "speedup"]}
}
```

- `task_id` matches `^[A-Z]{2}-[0-9]{2,4}$`.
- `entry.argv[0]`: contains `/` → path (relative = package file, resolved against the
  attempt workdir, must be executable); otherwise a command name looked up on the entry
  process PATH on the remote side. No shell is involved; use `["bash", "-c", "..."]`.
- `gpus`: nvidia-smi indices (PCI bus order) or full `GPU-...` UUIDs; `[]` = no GPU. MIG
  UUIDs and duplicates (also an index and a UUID naming the same GPU) are rejected.
- `env` may not set `CUDA_VISIBLE_DEVICES`, `CUDA_DEVICE_ORDER`, `VIRTUAL_ENV`, `TASKMGR_*`.

`progress.json`: `{"schema_version": 1, "stage": str, "updated_at": "UTC ISO 8601",
"percent"?: 0..100, "message"?: str}`. Written only by the task.

`result.json` (written by the task during the run, merged by the runner after the process
group has exited):

```json
{"schema_version": 1, "outputs": {...}, "<other business keys>": "...",
 "_taskmgr": {"jobid", "task_id", "attempt", "host", "gpus": [{"declared", "uuid"}],
              "status", "return_code", "signal", "started_at", "ended_at",
              "validation_errors": [], "error": {"kind", "message"} | null,
              "warnings": ["task_wrote_taskmgr" | "leftover_processes"],
              "paths": {"attempt_dir", "workdir", "stdout", "stderr", "shim_log",
                        "progress", "result", "artifacts"},
              "final": true}}
```

Result is valid when it is a JSON object with an `outputs` object containing every
`required_outputs` key. A missing file yields `{"schema_version", "_taskmgr"}`; a corrupt one
is kept as `result.json.corrupt` and reported in `validation_errors`; a task-written
`_taskmgr` is dropped with warning `task_wrote_taskmgr`.

`error.kind` values: `setup`, `shim_start_failed`, `exit_code`, `signal`, `exit_code_lost`,
`too_many_interruptions`, `operator_marked_stopped`, `stop_unconfirmed` (job in unknown),
`record_incomplete` / `undeterminable` (unknown after recovery).

### Entry process environment

1. `PATH=/usr/local/bin:/usr/bin:/bin`, `HOME`, `USER` (of the daemon user), `LANG=C.UTF-8`,
   then `base_env` from `config.json`.
2. With `environment.venv`: `VIRTUAL_ENV=<venv>`, `PATH=<venv>/bin:$PATH`.
3. `env` from `task.json`.
4. Runner: `CUDA_DEVICE_ORDER=PCI_BUS_ID`, `CUDA_VISIBLE_DEVICES=<uuids in declaration
   order>` (empty string for `[]`), `TASKMGR_JOBID`, `TASKMGR_TASK_ID`, `TASKMGR_ATTEMPT`,
   `TASKMGR_WORKDIR`, `TASKMGR_PROGRESS_FILE`, `TASKMGR_RESULT_FILE`.

stdin is `/dev/null`; stdout/stderr go to `stdout.log` / `stderr.log` of the attempt.

## Remote directory layout

```
<remote_root>/
  config.json
  locks/state.lock          serializes every state change (all processes, all clients)
  locks/daemon.lock         daemon instance lock (holds the daemon pid)
  locks/counters.json       next enqueue_seq (assumption: location not in the spec)
  incoming/up-*/            uploads in flight (flock held on the directory); never scheduled
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
  package/          read-only extracted copy; every attempt starts from it
  attempts/0001/
    runner.json     authoritative attempt record, written only by the runner
    shim.lock       flock held by the shim for its whole life
    stdout.log  stderr.log  shim.log
    workdir/        fresh copy of package/ + progress.json, result.json, artifacts/
```

`job.json`:

```json
{"schema_version": 1, "jobid": "3f9a1c0e7b2d4a56", "task_id": "AA-01", "state": "running",
 "submitted_at": "...", "updated_at": "...", "ended_at": "...", "enqueue_seq": 17,
 "content_hash": "<sha256>", "submission_key": "cli-...",
 "gpus": [{"declared": 0, "uuid": "GPU-..."}],
 "cancel_requested": false, "stop_reason": null | "cancel" | "timeout",
 "auto_restarts": 0, "retries": 0, "current_attempt": 1,
 "attempts": [{"attempt": 1, "status": "running", "dispatched_at": "...", "ended_at": "..."}],
 "error": null}
```

`runner.json`:

```json
{"schema_version": 1, "attempt": 1, "jobid": "...", "status": "dispatched|starting|running|
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
terminal --retry--> queued
```

Transitions happen under `locks/state.lock`: `job.json` is written first, then the
directory is renamed; if they disagree, `job.json` wins and daemon startup moves the
directory. Attempt status (`runner.json`) is a terminal state or `interrupted`.

### Scheduling

Each pass (under the state lock) walks queued jobs by `enqueue_seq` keeping a reserved set:
a job starts only if all its GPUs are free and unreserved, otherwise its GPUs are added to
the reserved set; a job touching a GPU held by an `unknown` job neither starts nor reserves.
Jobs with `gpus: []` only obey `max_cpu_jobs`. Dispatch writes `runner.json`, then
`job.json` (running) and the rename, and only then double-forks the shim (new session,
reparented to init) and hands it the already-locked `shim.lock`. A failed fork/exec ends
the job as `failed` (`shim_start_failed`).

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

## Failure recovery

On start the daemon takes `daemon.lock`, removes `incoming/` entries it can lock that are
older than 1 hour, then under the state lock checks every job. The same check runs every
`control_poll_seconds` for running/stopping jobs whose shim is dead (assumption: the spec
only requires it at startup), and `task resolve` runs it for unknown jobs.

1. `runner.json` final → restore the terminal state (regenerate `result.json._taskmgr` from
   `runner.json` if missing); `interrupted` → step 5.
2. `exited` but not final → finish finalization (leftover check, merge, final). No re-run.
3. queued → keep waiting.
4. running/stopping without `exited`:
   1. `shim.lock` held → shim alive, nothing to do.
   2. no identity → entry never ran → step 5.
   3. `boot_id` changed → interrupted by reboot → step 5.
   4. same boot, group members alive → **takeover**: the daemon polls the group, applies
      cancel and timeout (timeout measured from `started_at_epoch`) using the stop
      procedure, and when the group is gone ends the job `failed` /
      `exit_code_lost` (or cancelled / timed_out / unknown per the stop).
   5. same boot, no member → `failed` / `exit_code_lost`, business fields kept; if the job
      was stopping (or cancel was requested) → cancelled / timed_out.
   6. undeterminable (`/proc` unreadable, record corrupt/incomplete) → `unknown`.
5. Re-run: attempt → `interrupted`; `cancel_requested` → cancelled; auto-restart count at
   `max_auto_restarts` → `failed` / `too_many_interruptions`; else back to queued with the
   original `enqueue_seq`; the next attempt starts from `package/`.

### Resolving `unknown` manually

`unknown` jobs never re-run and keep their GPUs.

1. `task resolve <jobid> --host H` re-runs the check. If the group is gone the job ends by
   the rules above (cancelled if cancel was requested). If processes are still alive, or
   the record cannot be read, the job stays unknown and the output shows `identity`
   (`boot_id`, `pgid`, `leader_pid`, `leader_start_time`, `shim_pid`, ...) and
   `alive_pids`.
2. On the server, inspect: `ps -o pid,pgid,lstart,stat,cmd -g <pgid>`,
   `cat /proc/sys/kernel/random/boot_id`, `nvidia-smi` (processes on the job's GPUs).
3. Kill what is left: `kill -TERM -<pgid>`, then `kill -KILL -<pgid>`. Processes in `D`
   state may need the underlying device/driver issue fixed or a reboot.
4. Run `task resolve <jobid> --host H` again, or, once you have confirmed everything is
   gone, `task resolve <jobid> --host H --as stopped --confirm <jobid>` → `failed`
   (`operator_marked_stopped`) or `cancelled`; GPUs are released; nothing re-runs.
5. Re-run with `task retry <jobid> --host H` if wanted.

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
   before the entry starts ends the attempt as cancelled without running it.
6. `task status` of a finished job prints `result.json` with two added top-level keys,
   `host` and `complete`. A job that ended without any attempt (cancelled while queued)
   prints a synthesized `{"schema_version", "_taskmgr": {...}}`.
7. `--wait --timeout` expiry prints the current status object plus an `error`
   (`wait_timeout`) and exits 2. Ctrl-C prints an `interrupted` error and exits 2.
8. Protocol compatibility means equal integer `protocol_version` (currently 1). ssh exit
   status 255 is treated as a connection failure; ssh runs with `BatchMode=yes`.
9. Leftover cleanup that cannot be confirmed puts the job into `unknown` (same as
   cancel/timeout).
10. `resolve` never starts a takeover: if processes are alive it leaves the job unknown.
11. `_taskmgr.paths` point to the attempt's final location (`done/...`); while running,
    `TASKMGR_WORKDIR` and friends point to `in-progress/...` (the directory moves when the
    job ends).
12. `logs --follow` ends when the attempt is final, the job is terminal or unknown, or a
    newer attempt replaced it.
13. `fetch` skips unsafe entries (absolute/escaping symlinks a task created, special files)
    with a warning and lists them in `skipped`; it includes `shim.lock`.
14. `max_cpu_jobs` counts CPU-only jobs in running, stopping and unknown.
15. jobid uniqueness is checked against all job directories on the server.
16. `_taskmgr.host` is the remote machine's hostname.
17. Extra config key `scheduler_poll_seconds`; extra command `task hosts`.

## Limitations

- Only the task's process group is managed. A process that calls `setsid()`/`setpgid()`
  itself leaves the group and is neither tracked nor stopped by taskmgr.
- No MIG support; GPU usage by processes outside taskmgr is not detected or prevented.
- `remote_root` must be on a local filesystem (flock is unreliable on NFS).
- No checkpoint/resume: an automatic re-run starts from the original package.
- No installation of software or environments; venvs and paths must exist on the server.

## Tests

```sh
python -m unittest discover -s taskmgr/tests      # Linux, no GPU, no network
# from macOS:
docker run --rm --init -v "$PWD":/src -w /src python:3.11-slim \
    python -m unittest discover -s taskmgr/tests
```

Run with `--init` (or any init that reaps orphans) in containers: tests kill shims and the
orphaned task processes must be reaped.

**Test-only overrides** (do not use in production):

| Variable / setting | Effect |
| --- | --- |
| `TASKMGR_NVIDIA_SMI` | path of a fake `nvidia-smi` printing `index, uuid` lines |
| `TASKMGR_BOOT_ID_FILE` | read boot_id from this file (simulated reboot) |
| machine entry `"local": {"fail": "msg"}` | `transport: local` raises a connection error |
| machine entry `"local": {"fail_if_exists": "/path"}` | connection error while the file exists |
| `TASKMGR_TEST_PROTOCOL_VERSION` | remote reports this protocol version |
| `TASKMGR_TEST_SKIP_DAEMON_LOCK=1` | daemon skips `daemon.lock` (multi-daemon test) |
| `TASKMGR_SHIM_EXECUTABLE` | interpreter used to exec the shim (shim start failure) |
| `TASKMGR_TEST_SHIM_CRASH=<point>` | shim exits at `before_identity`, `before_exec`, `after_exit_recorded`, `before_finalize` |
| `TASKMGR_TEST_IGNORE_KILL=1` | stop procedure sends no signals (unconfirmed stop) |
| `TASKMGR_TEST_RECEIVE_PAUSE=<s>` | `receive` sleeps while holding the upload lock |

The systemd behaviour is tested by checking `KillMode=process` in the unit and by stopping
the daemon with SIGTERM to its whole process group: shim and task keep running and the
real exit code is recorded.

### Optional smoke test against real SSH

```sh
# on the server
git clone <repo> /opt/taskmgr && mkdir -p /data/taskmgr
nohup /opt/taskmgr/bin/taskmgr-remote --root /data/taskmgr serve >/tmp/taskmgr.log 2>&1 &
# locally, machines.json with transport "ssh" pointing at it
task submit --host gpu1 taskmgr/examples/succeed
task status <jobid> --host gpu1 --wait --poll-interval 2      # exit 0, outputs present
task submit --host gpu1 taskmgr/examples/multi-gpu            # needs >= 2 GPUs
task logs <jobid> --host gpu1 --follow
task fetch <jobid> --host gpu1 --output ./results/
```
