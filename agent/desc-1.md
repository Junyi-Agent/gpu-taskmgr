# taskmgr Requirements Specification

Oct 30, 2026 · @Junyi Hou

## Goal and scope

Implement a task management system for GPU performance tuning under the `taskmgr/` directory. It provides a local CLI named `task` and a program named `taskmgr-remote` that runs on each remote server. The code may be organized as a Python package with `taskmgr.py` as the entry module; a single file is not required.

Goal: store and submit task packages locally, and choose a remote execution server through the local CLI. Each remote server independently persists its own tasks and state, and is responsible for queuing, execution, queries, and restart recovery.

Throughout this document, "local" always means the task submission side. Where the implementation needs a key behavior that this document does not specify, state the assumption explicitly in the README.

## 1. Architecture, process model, and persistence

The system consists of a local CLI and multiple remote execution servers (for example, Nebius GPU servers). There is no central scheduler.

### 1.1 Remote process model

Each remote server runs three kinds of processes, with responsibilities divided exactly as follows:

1. **Remote subcommand** `taskmgr-remote <subcommand>`: invoked by the local CLI as `ssh <host> taskmgr-remote --root <remote_root> <subcommand> ...`. It handles upload reception, validation and enqueueing, queries, cancellation, retry, and manual resolution. It writes JSON to stdout and exits when done.
2. **Scheduler daemon** `taskmgr-remote serve`: a long-running process that scans the queue, allocates GPUs, starts attempts, and performs restart recovery.
3. **Attempt supervisor (shim)**: one per attempt, started by the daemon in detached mode (a new session, not an ordinary child of the daemon). The shim starts the task's process group, obtains the real exit code with `wait`, runs the stop procedure for timeout and cancellation, writes the runner record and the final result, and then exits.

Rationale: if the daemon exits or restarts, the shim and the task are not killed, and the shim can still obtain the real exit code. The daemon must never be the direct parent of the task process.

Processes coordinate only through files and file locks (`fcntl.flock`) under the remote storage root. Do not introduce a socket service or a message queue. When the daemon is not running, the remote subcommand can still complete submission and enqueueing; the task runs once the daemon starts.

Every call from the local CLI to the remote subcommand carries a `protocol_version`, and the remote returns its own version in its JSON output. If the versions are incompatible, report an error (exit code 2) and do not attempt to continue.

### 1.2 Local persistence

- Concrete task packages, including their code, descriptions, and input files.
- The machine list and connection configuration.
- The round-robin position.

The local side does not persist task run state and does not keep a jobid index. jobids and task state are always queried from the remote side.

### 1.3 Remote persistence

Each remote server independently persists:

- The original task packages it received.
- The task queue, jobids, execution state, and the submission-key mapping.
- The working directory, progress, results, logs, artifacts, and runner record of every execution.

Local exit, network disconnection, or giving up on waiting does not affect remote tasks that have already been submitted.

## 2. Task packages and templates

A task package contains:

- `task.json`: structured execution configuration.
- `TASK.md`: a task description for humans or AI agents to read.
- `progress.json`: the initial progress file.
- The entry script and any code and input files it needs.

`result.json` is produced during execution and is not required at submission time.

### 2.1 task\_id and jobid

A task\_id has the form `AA-BB` and must match the regex `^[A-Z]{2}-[0-9]{2,4}$`. AA is two uppercase letters denoting the task type; BB is 2 to 4 digits denoting the subtask number, for example `AA-01` or `MM-0123`.

The task\_id is a business identifier; the jobid identifies one submission for execution. The same task\_id may be submitted many times, and each submission gets a new jobid, except when a submission is retried with the same submission key (see Section 6).

### 2.2 task.json

`task.json` contains at least the following fields:

```json
{
  "schema_version": 1,
  "task_id": "AA-01",
  "entry": {"argv": ["python", "run.py", "--config", "cfg.yaml"]},
  "gpus": [0, 1],
  "environment": {
    "venv": "/opt/venvs/tune",
    "required_paths": ["/usr/local/cuda-12.4"]
  },
  "env": {"NCCL_DEBUG": "WARN"},
  "timeout_seconds": 7200,
  "result": {"required_outputs": ["best_config", "speedup"]}
}
```

- `entry.argv`: the entry command, a non-empty argv array. If `argv[0]` contains `/`, it is a path; a relative path refers to a file in the package and is resolved against the working directory. If it contains no `/`, it is a command name (such as `bash` or `python`) and is looked up on the entry process's PATH (see Section 3.2).
- `gpus`: the GPU list. Each element is a local GPU index (integer) or a GPU UUID (string); see Section 7 for how they are interpreted. An empty array is allowed and means the task uses no GPU.
- `environment`: optional. `venv` is the absolute path of a virtual environment; `required_paths` is a list of absolute paths that must exist before start.
- `env`: extra environment variables; keys and values are strings. It must not set `CUDA_VISIBLE_DEVICES`, `CUDA_DEVICE_ORDER`, `VIRTUAL_ENV`, or any variable starting with `TASKMGR_`; the runner sets those.
- `timeout_seconds`: optional positive integer; omitted means no time limit.
- `result.required_outputs`: optional list of keys that must be present in `outputs`.

### 2.3 Templates and required placeholders

`task init AA-01` generates a task package template with a stable structure and filling instructions, for other agents to complete.

Every required item in the template is marked with the literal placeholder `<REQUIRED>` or `<REQUIRED: explanation>`, both for field values in `task.json` and for sections of `TASK.md`. If the string `<REQUIRED` still appears anywhere in `task.json`, `TASK.md`, or `progress.json`, the package is incomplete and must not be submitted.

The template also ships a helper module `taskmgr_task.py` that depends only on the standard library and provides functions for atomically writing progress.json and result.json (see Section 10).

### 2.4 Validation

`task validate task-package/` checks the following and reports all errors as JSON:

- Required files exist, and the JSON files parse and conform to their schemas.
- The task\_id matches the format in Section 2.1.
- No `<REQUIRED` placeholder remains.
- A relative-path `argv[0]` (Section 2.2) is an existing file in the package; bare command names are checked remotely (Section 3.1).
- The package contains no absolute symlinks, no symlinks pointing outside the package, and no special files such as device files.

`task submit` runs the same validation before uploading.

taskmgr only executes the entry command in `task.json` and never interprets TASK.md itself. If an AI agent is needed to complete the task, the entry command starts that agent and has it read TASK.md.

## 3. Execution environment

The first version assumes the remote runtime environment is already prepared. taskmgr does not install system software or Python packages, and does not create container environments. Remote servers must run Linux; the implementation relies on `/proc`.

The task working directory is a separate directory for each attempt. Relative file paths in the task configuration are resolved against it; remote environment paths are absolute. By default the entry command does not go through a shell; a task that needs shell syntax uses an entry such as `bash -c` explicitly.

### 3.1 Pre-start checks

Before starting the entry command, the shim checks that:

- All paths in `environment.venv` and `environment.required_paths` exist.
- The entry command resolves as defined in Section 2.2 (using the PATH built in Section 3.2) and is executable.
- Every declared GPU UUID is visible on this machine (queried via `nvidia-smi`).

If any check fails, the attempt does not start the entry process. The task ends as `failed` with `return_code` null, `_taskmgr.error.kind` set to `setup`, and the specific reason recorded. The check does not test whether a GPU is being used by external processes.

### 3.2 Environment of the entry process

The entry process does not inherit the daemon's own environment variables. Its environment is built in this order:

1. Base environment: `PATH=/usr/local/bin:/usr/bin:/bin`, `HOME`, `USER`, `LANG=C.UTF-8`. These can be overridden or extended by `base_env` in `config.json`.
2. If `environment.venv` is declared, set `VIRTUAL_ENV` and prepend `<venv>/bin` to PATH.
3. Overlay the `env` from `task.json`.
4. Finally, the runner sets the variables listed below.

The entry process's stdin is connected to `/dev/null`; stdout and stderr are written to separate log files in the attempt directory. Variables set by the runner:

- `CUDA_DEVICE_ORDER=PCI_BUS_ID`
- `CUDA_VISIBLE_DEVICES`: the GPU UUIDs in declaration order, comma-separated; an empty string when the GPU list is empty.
- `TASKMGR_JOBID`, `TASKMGR_TASK_ID`, `TASKMGR_ATTEMPT`, `TASKMGR_WORKDIR`, `TASKMGR_PROGRESS_FILE`, `TASKMGR_RESULT_FILE`.

## 4. Local machine list and server selection

The machine list is stored locally. Each server entry contains at least:

```json
{
  "name": "nebius-h200-2",
  "enabled": true,
  "transport": "ssh",
  "ssh": {"host": "10.0.0.12", "user": "tuner", "port": 22, "identity_file": "~/.ssh/id_ed25519", "options": ["-o", "ServerAliveInterval=15"]},
  "remote_root": "/data/taskmgr",
  "remote_command": "/opt/taskmgr/bin/taskmgr-remote"
}
```

- `remote_root` is the single source of truth for the remote storage root. Both the remote subcommand and the daemon receive it via `--root`. Other remote settings live in `<remote_root>/config.json`, which does not repeat the root.
- `transport` is `ssh` or `local`. `local` invokes `remote_command` directly as a local subprocess and is used for testing (see Section 18).

### 4.1 Selection rules

- With `--host`, use that server directly, even if it is marked as disabled.
- Without `--host`, choose among enabled servers in the persisted round-robin order.
- Whether a remote server is busy does not affect the round-robin choice; once uploaded, the task enters that server's own queue.
- When multiple local CLIs run concurrently, reading and updating the round-robin position must happen under the same file lock.

If the connection fails or the submission outcome is uncertain, report the chosen server and the error. Do not automatically resubmit to another server, to avoid duplicate execution.

## 5. Submission and ACK

Supported forms:

```
task submit task-package/
task submit task-package.tar.gz
task submit --host nebius-h200-2 task-package.tar.gz
```

`--host` means upload directly to the named execution server and run there.

### 5.1 Packaging

When a directory is submitted, the tool packs it into a tar.gz in a temporary location without modifying the original local task content. Packing must be deterministic: entries sorted by relative path, mtime, uid, and gid fixed at 0, empty user and group names, and a gzip header timestamp of 0.

### 5.2 Remote reception

1. Upload into `<remote_root>/incoming/<temporary name>/`. The scheduler never scans incoming, so in-flight uploads are invisible to the execution queue.
2. Extract safely: reject absolute paths, paths containing `..`, symlinks pointing outside the package, hard links, device files, and FIFOs; reject packages whose extracted size exceeds `max_package_bytes` in `config.json`.
3. Run the validation in Section 2.4 independently, and resolve GPU declarations to UUIDs (see Section 7).
4. Under the state lock: allocate a jobid, check the submission key, write `job.json`, then atomically rename the directory to `queue/<jobid>/`.
5. Only after it is durably enqueued, return the ACK and the jobid.

Each upload uses its own temporary directory in incoming and holds a flock on that directory for the whole upload and reception. On startup the daemon only removes leftover directories whose lock it can acquire and whose modification time is more than 1 hour old, so it never deletes an upload in progress.

The jobid is generated remotely as a 16-character lowercase hexadecimal string; it must not collide with any existing jobid on that server.

### 5.3 Submission receipt

On success, the local CLI writes a JSON receipt to stdout containing at least `host`, `jobid`, `task_id`, `submission_key`, and `deduplicated` (whether it matched an existing submission). It does not persist a jobid index.

## 6. Submission idempotency

Supported form:

```
task submit --host HOST --submission-key KEY task-package/
```

`--submission-key` must be used together with `--host`; otherwise it is a parameter error, because a round-robin retry could land on a different server. KEY must match `^[A-Za-z0-9._-]{1,128}$`.

### 6.1 Content hash

"Same task content" is decided by a normalized content hash, not by the archive bytes. For every regular file and symlink in the extracted package, sorted by relative POSIX path, write (path, type, executable bit, SHA-256 of the content or the link target) into a manifest, then compute the SHA-256 of the manifest. Packing the same directory twice, or submitting a directory and its tar.gz, yields the same hash.

### 6.2 Rules

- The remote side persists the mapping from submission key to (jobid, content hash).
- If a server receives the same key with the same content hash, it returns the original jobid with `deduplicated` set to true and does not enqueue again. This holds regardless of the original job's current state.
- The same key with a different content hash is an error (exit code 2).
- If the server has enqueued the job but the ACK was lost, the user can retry against the original server with the same key.

### 6.3 Submissions without a key

When no submission key is given, the local CLI generates a random key for this invocation and sends it with the submission. If the outcome is uncertain, stderr reports the chosen server and this key, along with a ready-to-run retry command (`--host` plus the key).

This is still an independent submission: separate CLI invocations are never deduplicated by task\_id or task content.

## 7. GPU declaration and concurrent scheduling

GPUs must be declared in the `gpus` field of `task.json`; writing them only in TASK.md is not enough.

### 7.1 Index interpretation and device mapping

- Integer indices follow the `nvidia-smi` index (PCI bus order), not CUDA's default FASTEST\_FIRST order.
- Strings must be full GPU UUIDs (starting with `GPU-`).
- On receipt, the remote side resolves every declaration to its actual UUID and stores both the original declaration and the UUID in `job.json`. Reject the task if an index or UUID does not exist, or if the list contains duplicates.
- The first version does not support MIG instances; declaring a MIG UUID is rejected.
- At run time, `CUDA_VISIBLE_DEVICES` is set to the UUID list in declaration order, so `cuda:0` inside the task is the first declared GPU, `cuda:1` the second, and so on.

### 7.2 Scheduling rules

Queue order is by the enqueue sequence number `enqueue_seq`. It is assigned at first submission and reassigned on `task retry` (moving the job to the back of the queue); automatic re-runs during restart recovery keep the original number.

Each scheduling pass walks queued jobs in queue order under the state lock, maintaining a "reserved set":

1. If all GPUs the job needs are free and none of them is in the reserved set → acquire all of them at once and start an attempt.
2. Otherwise → do not start it, and add all GPUs it needs to the reserved set.

Exception: if any GPU the job needs is held by a job in the unknown state, the job neither starts nor joins the reserved set. Otherwise an unknown job would, through reservation, also lock up other idle GPUs.

Effects:

- Jobs with overlapping GPUs run serially, strictly first-come first-served per GPU, so large multi-GPU jobs are not starved by a stream of later small jobs.
- Jobs with disjoint GPUs may run concurrently; a blocked job does not affect GPUs unrelated to it.
- A multi-GPU job must acquire all its GPUs at once before starting; partial allocation is not allowed.
- Jobs with an empty GPU list take no part in GPU reservation and are limited only by the optional `max_cpu_jobs` concurrency cap in `config.json`.

A GPU is free when no job in a GPU-holding state (Section 8.1) uses it. These rules constrain only tasks managed by this system; they do not prevent external processes from using the GPUs.

## 8. State machine

A job's state is defined by the `state` field in `job.json`. The directory location follows from the state; if the two disagree, `job.json` wins and recovery repairs the directory location. All state transitions happen under the state lock: first atomically write `job.json`, then rename the directory.

### 8.1 Job states

| State | Directory | Terminal | Holds GPUs | Meaning |
| --- | --- | --- | --- | --- |
| queued | queue/ | No | No | Waiting to run |
| running | in-progress/ | No | Yes | Dispatched; shim starting or running |
| stopping | in-progress/ | No | Yes | Stopping due to cancellation or timeout |
| unknown | in-progress/ | No | Yes | Cannot confirm whether it is still running; needs manual resolution |
| succeeded | done/ | Yes | No | Exit code 0 and valid result |
| failed | done/ | Yes | No | Non-zero exit, killed by signal, pre-start check failure, etc. |
| invalid\_result | done/ | Yes | No | Exit code 0 but invalid result |
| cancelled | done/ | Yes | No | Cancellation with confirmed stop |
| timed\_out | done/ | Yes | No | Timeout with confirmed stop |

Each attempt also has its own attempt status in its `runner.json`: in addition to the terminal states above, it can be `interrupted`, meaning the execution was cut off without a final result.

### 8.2 State transitions

| Current state | Event | New state |
| --- | --- | --- |
| queued | Scheduler acquires all GPUs and dispatches | running |
| queued | cancel | cancelled |
| running | Shim fails to start (fork or exec failure) | failed (`error.kind` = `shim_start_failed`) |
| running | Pre-start check fails | failed |
| running | Entry process exits | succeeded / failed / invalid\_result |
| running | cancel or timeout | stopping |
| stopping | Whole process group confirmed stopped | cancelled / timed\_out |
| stopping | Stop still unconfirmed after forced kill | unknown |
| running / stopping | Restart recovery | Per Section 12.2 |
| unknown | `task resolve` automatic check reaches a conclusion | Per Section 12.2 |
| unknown | `task resolve --as stopped --confirm` | failed (or cancelled if cancellation was requested) |
| Any terminal state | `task retry` | queued |

No transition outside this table is allowed.

## 9. Remote directory layout and execution isolation

### 9.1 Directory layout

```
<remote_root>/
  config.json
  locks/state.lock          state lock (see Section 9.3)
  locks/daemon.lock         daemon instance lock
  incoming/                 packages being uploaded; invisible to the scheduler
  submission-keys/<key>.json
  queue/<jobid>/
  in-progress/<jobid>/
  done/<jobid>/
  daemon/                   daemon logs
```

Each job directory:

```
<jobid>/
  job.json                  state, task_id, submission time, enqueue_seq, content hash, submission key,
                            GPU declarations and UUIDs, cancel_requested, auto-restart count, attempt list
  package.tar.gz            original task package, immutable
  package/                  read-only extracted copy, the source for every attempt
  attempts/0001/
    runner.json             authoritative attempt record, owned exclusively by the runner
    shim.lock               held by the shim for its entire lifetime
    stdout.log
    stderr.log
    shim.log
    workdir/                working directory of this execution: progress.json, result.json, task artifacts
  attempts/0002/
```

### 9.2 Execution isolation

- Each attempt copies a fresh `workdir/` from `package/`; it never reuses intermediate files left by a previous execution.
- Each attempt's progress.json, result.json, logs, and artifacts are kept separately and must not overwrite history.
- Task artifacts are written inside `workdir/`, preferably under `workdir/artifacts/`.

### 9.3 Concurrency and multiple instances

- `state.lock` serializes every state-changing operation, from any process or client: state transitions, jobid allocation, submission-key checks and mapping writes, the rename into queue/, and GPU allocation in each scheduling pass.
- On startup the daemon acquires `daemon.lock` non-blockingly and exits with an error if it fails, so an accidentally started second instance never runs.
- Even if that lock were bypassed, dispatch decisions are made under `state.lock`: first record the state and GPU allocation in `job.json` and `runner.json`, then start the shim. If the job is no longer queued at that point, abandon the dispatch.
- Every JSON write uses a temporary file in the same directory, fsync, and `os.replace`, fsyncing the directory where necessary.

`remote_root` must be on a local filesystem, not on NFS or another network filesystem, because flock is unreliable there.

## 10. Progress, results, and field ownership

### 10.1 Schemas

progress.json:

```json
{"schema_version": 1, "stage": "profiling", "updated_at": "2026-10-29T08:15:00Z", "percent": 40, "message": "kernel 3/8"}
```

`stage` (string) and `updated_at` (UTC ISO 8601) are required; `percent` (a number from 0 to 100) and `message` are optional.

result.json:

```json
{
  "schema_version": 1,
  "outputs": {"best_config": {"block": 128}, "speedup": 1.37},
  "_taskmgr": {"...": "written by the runner"}
}
```

The task's business results go in the `outputs` object. `_taskmgr` is written by the runner and must not be modified by the task.

### 10.2 Writers and timing

- **progress.json is written only by the task.** The runner copies the initial file when creating the workdir and never writes it during the run.
- **During the run, result.json is written only by the task.** The runner writes it only after the whole process group has exited (Section 10.4), so the runner and the task never write the same file concurrently.
- If several processes inside the task write the same file, the task coordinates them; `taskmgr_task.py` in the template provides flock-protected read-modify-write and atomic replacement.

### 10.3 Runner result merge

After the entry process exits, the shim handles result.json as follows:

- The file exists and is a valid JSON object: keep every field except `_taskmgr`. If the task wrote its own `_taskmgr`, discard it and record the warning `task_wrote_taskmgr`.
- The file does not exist: create a minimal result file containing only `schema_version` and `_taskmgr`.
- The file is corrupt: rename the original to `result.json.corrupt` to keep it, then generate a final result that includes the error.

`_taskmgr` contains at least: `jobid`, `task_id`, `attempt`, `host`, `gpus` (declared values and UUIDs), `status`, `return_code`, `signal`, `started_at`, `ended_at`, `validation_errors`, `error`, `warnings`, `paths` (logs, workdir, artifact locations), and `final: true`.

### 10.4 Authoritative record

`runner.json` is the authoritative record owned exclusively by the runner; the task cannot write it because it lies outside the workdir. After the entry process exits, the shim writes in this order:

1. Immediately after `wait` returns, write `exited: true`, `return_code`, `signal`, and `ended_at` to `runner.json`.
2. Check the process group for leftover processes. If any remain, clean them up with the stop procedure in Section 13.1 and record `leftover_processes` in `_taskmgr.warnings`.
3. Merge and write result.json per Section 10.3.
4. Mark `runner.json` as `final: true`.
5. Update `job.json` under the state lock.

If recovery finds `exited: true` but not yet final, it resumes from step 2 and finishes; it does not re-run the task. If the shim crashes after `wait` returns but before step 1 is durable, the exit code cannot be recovered; this is handled as `exit_code_lost` per Section 12.2.

Whether an attempt is complete is decided only by `final` in `runner.json`, never by whether result.json exists. If `runner.json` is final but result.json lacks a complete `_taskmgr`, recovery regenerates it from `runner.json`.

## 11. Success, failure, and output validation

A business result is valid when result.json is a valid JSON object, `outputs` exists and is an object, and it contains every key listed in `result.required_outputs` in `task.json`.

When the entry process ends normally (not by cancellation or timeout):

| Condition | State |
| --- | --- |
| Exit code 0, valid result | succeeded |
| Non-zero exit code | failed |
| Killed by a signal not sent by the runner | failed |
| Exit code 0, invalid result | invalid\_result |

- `return_code` records the real exit code from `waitpid`. If the process was killed by a signal, `return_code` is null and `signal` records the signal number.
- If the real exit code cannot be obtained, use null; never fabricate an exit code.
- Output validation never rewrites the real exit code. For example, an invalid\_result job still has `return_code` 0, with the reason recorded in `validation_errors`.
- Ordinary failed and invalid\_result jobs are not retried automatically.

## 12. Restart recovery

### 12.1 Execution identity record

When the shim starts the entry process, it writes the following to `runner.json`:

- The system boot identifier `boot_id` (`/proc/sys/kernel/random/boot_id`).
- The shim's PID and process start time (field 22 of `/proc/<pid>/stat`).
- The task's process group ID, the group leader's PID, the leader's start time, and an argv hash.

To close the window where the command has been exec'd but its identity is not yet on disk: after forking, the child first calls `setsid` and then blocks on a pipe. Only after the shim has written and fsynced the identity record does it signal the child to exec the entry command. Therefore, if `runner.json` contains no identity record, the entry command was never executed.

### 12.2 Recovery procedure

After starting and acquiring `daemon.lock`, the daemon cleans up incoming and then scans every `job.json` under the state lock:

1. **The attempt's `runner.json` is final**: restore the corresponding terminal state without re-running; repair the directory location and result.json if needed.
2. **`exited` is recorded but not yet final**: finish finalization from step 2 of Section 10.4; do not re-run.
3. **queued**: resume waiting.
4. **running / stopping, with no `exited` recorded**: check in this order:
   1. Try to acquire the attempt's `shim.lock` non-blockingly. If that fails, the shim is still running → keep tracking it; do not start it again.
   2. `runner.json` has no execution identity → the entry command never ran → go to step 5.
   3. The current `boot_id` differs from the recorded one → the machine rebooted and the task was genuinely interrupted → go to step 5.
   4. Within the same boot, search `/proc` for all processes whose process group ID equals the recorded one and whose start time is not earlier than this attempt's start. The group leader may already have exited, so checking only the leader is not enough. If any process matches → the daemon takes over tracking: it polls every `control_poll_seconds` and is responsible for this attempt's timeout and cancellation (the stop procedure in Section 13.1). When the whole process group has exited, the job ends as failed with `return_code` null and `error.kind` = `exit_code_lost`; if cancellation or timeout was carried out meanwhile, mark it cancelled, timed\_out, or unknown according to the stop procedure's outcome.
   5. Within the same boot, no process matches → the task has ended but its exit code is lost. End it as failed with `error.kind` = `exit_code_lost`, keep the business fields in result.json for manual review, and do not re-run automatically. If the previous state was stopping, mark it cancelled or timed\_out according to the stop reason.
   6. Anything that cannot be determined, such as `/proc` being unreadable or the record being incomplete → mark unknown; do not re-run automatically and do not release the GPUs.
5. **Automatic re-run (only for cases 4.2 and 4.3)**: mark the attempt's `runner.json` as `interrupted`. If `cancel_requested` is true, the job becomes cancelled. If the auto-restart count has reached `max_auto_restarts` in `config.json` (default 2), the job becomes failed with `error.kind` = `too_many_interruptions`. Otherwise it returns to queued, keeps its original `enqueue_seq`, and at the next dispatch a new attempt re-runs from the original task package.

progress.json only reports progress; the first version does not require tasks to support resuming from checkpoints.

## 13. Cancellation, timeout, retry, and manual resolution

### 13.1 Stop procedure

Cancellation, timeout, and leftover-process cleanup all use the same stop procedure:

1. Send SIGTERM to the whole task process group.
2. Wait `stop_grace_seconds` from `config.json` (default 30).
3. If processes remain, send SIGKILL and wait `kill_confirm_seconds` (default 10).
4. Only after confirming that no process in the group is alive, release the GPUs and enter the terminal state. Otherwise enter unknown and keep the GPUs held.

The first version manages only the task's process group. Processes that leave the group via their own `setsid` are out of scope; the README must document this limitation.

### 13.2 Cancellation

`task cancel <jobid> --host HOST`

- The cancel request is first persisted as `cancel_requested: true` under the state lock, so it is never auto-re-run after a restart.
- queued: becomes cancelled immediately and never runs.
- running: enters stopping; the shim runs the stop procedure and marks cancelled once the stop is confirmed.
- unknown: only records the request; the state does not change. Once resolve later confirms the stop, it becomes cancelled.
- Already terminal: nothing changes; output the current state with `changed` false and exit code 0.

Notification: the cancel command only writes job.json under the state lock and never signals processes directly. The shim reads job.json every `control_poll_seconds` (default 2 seconds) and runs the stop procedure when it sees `cancel_requested`. For an attempt under daemon takeover tracking, the daemon does the same.

### 13.3 Timeout

`timeout_seconds` counts from the moment the entry process of this attempt starts, and the shim keeps the time. On timeout it runs the stop procedure and marks timed\_out once the stop is confirmed. Without it, there is no time limit.

### 13.4 Explicit retry

`task retry <jobid> --host HOST`

- Allowed only for jobs in a terminal state. The job returns to queued with a newly assigned `enqueue_seq`, and `cancel_requested` and the auto-restart count are reset. A new attempt is created at the next dispatch, and all history is kept.
- A job in unknown must be resolved first; a direct retry is an error (exit code 2) that points to resolve.
- The original task package is never modified; if the task content changes, submit it again.

### 13.5 Manually resolving unknown

`task resolve <jobid> --host HOST`

Re-runs the automatic check in Section 12.2. If it can now decide, it proceeds by the normal recovery rules; if it still cannot, the job stays unknown and the command prints the recorded execution identity (boot\_id, process group ID, PID, start times) for manual investigation.

`task resolve <jobid> --host HOST --as stopped --confirm <jobid>`

Used after an operator has manually confirmed that all related processes are gone (for example by checking `ps` and `nvidia-smi`). `--confirm` must repeat the same jobid. Effect: the attempt is marked interrupted, the job becomes failed (`error.kind` = `operator_marked_stopped`) or cancelled (if cancellation was requested), the GPUs are released, and nothing re-runs automatically. To run it again, use `task retry`.

There is no option to mark the job as still running: if the processes really are still alive, the operator should kill them manually first and then use `--as stopped`.

## 14. Job discovery and status queries

### 14.1 Job list

`task jobs --host HOST` queries the named server. `task jobs` queries every server in the local machine list, including disabled ones, because a disabled server may still hold jobs; "enabled" only affects round-robin selection. `--state` optionally filters by state.

Output format:

```json
{
  "complete": false,
  "errors": [{"host": "nebius-h200-3", "error": "ssh: connect timed out"}],
  "jobs": [{"host": "nebius-h200-2", "jobid": "3f9a1c0e7b2d4a56", "task_id": "AA-01", "state": "running", "attempt": 1, "submitted_at": "2026-10-29T08:00:00Z"}]
}
```

When some servers are unreachable, results from the others are still returned, with `complete` false and the failures listed in `errors`.

### 14.2 Single job status

`task status <jobid> --host HOST` queries that server directly.

`task status <jobid>` queries every server in the machine list, including disabled ones, to locate the jobid:

| Case | Output | Exit code |
| --- | --- | --- |
| Exactly one match, all servers reachable | The job, `complete: true` | 0 |
| Exactly one match, some servers unreachable | The job, `complete: false`; stderr notes that uniqueness could not be fully checked | 0 |
| The same jobid on several servers | Ambiguity error listing the matching servers, asking for `--host` | 2 |
| All reachable, no match | `not_found` error | 2 |
| Some unreachable, no match | `incomplete` error; does not assert the job does not exist | 2 |

A plain status returns immediately: for an unfinished job it outputs the job state, current attempt info, and the latest progress.json; for a finished job it outputs the final result.json directly.

### 14.3 Waiting

`task status <jobid> --wait`, optionally with `--host`, waits until the job reaches a terminal state and then outputs the final result.json.

- `--poll-interval` (default 5 seconds): polling interval.
- `--retry-timeout` (default 300 seconds): the longest period of consecutive connection failures to retry through. Beyond it, return a connection error (exit code 2) without changing the remote job state.
- `--timeout` (default unlimited): total wait time. On expiry, output the current state with exit code 2.
- On finding the unknown state, return immediately with the anomaly (exit code 3); never wait indefinitely.
- Interrupting the wait locally does not cancel the remote job.
- Without `--host`, the cross-server lookup happens once at the start; after that only the located server is polled.

## 15. Logs and artifact retrieval

```
task logs <jobid> --host HOST [--attempt N] [--stream stdout|stderr|shim]
task logs <jobid> --host HOST --follow
task fetch <jobid> --host HOST --output ./results/ [--attempt N | --all-attempts]
```

- `--attempt` defaults to the latest execution; `--stream` defaults to stdout.
- `logs` writes log text directly to stdout. `--follow` keeps printing new content and exits with code 0 after the attempt ends and the remaining content has been printed.
- `fetch` downloads everything under the attempt directory: runner.json, logs, and the progress.json, result.json, and task artifacts in the workdir (profiles, CSVs, modified kernels, and so on), leaving the remote files in place.
- Downloads go to `<output>/<host>/<jobid>/attempts/<NNNN>/`, preserving the directory structure. If the target directory already exists, refuse the download (exit code 2) and do not overwrite existing local files.
- Transfer uses a tar stream over SSH; local extraction applies the same path safety checks as Section 5.2.
- When fetching a running job, stderr warns that the results may be incomplete, and the stdout receipt has `partial` set to true.

## 16. CLI output and exit codes

All commands except `logs` write one valid JSON object to stdout; hints and diagnostics go to stderr. On error, stdout carries `{"error": {"code": "...", "message": "...", "host": "..."}}` and detailed diagnostics go to stderr.

| Exit code | Meaning |
| --- | --- |
| 0 | Operation or plain query succeeded; for `--wait`, the job ended as succeeded |
| 1 | `--wait` saw the job end as failed, invalid\_result, cancelled, or timed\_out |
| 2 | Parameter, connection, query, or other CLI operation error (query-specific cases are in Section 14) |
| 3 | `--wait` stopped because the job is in the unknown state |

- A plain status that successfully finds a failed job still returns 0; whether the job succeeded is expressed by the state in the JSON.
- `task jobs` returns 0 if at least one server was queried successfully, with the `complete` field indicating completeness; it returns 2 if all fail.
- cancel, retry, and resolve return 0 when they execute successfully, including when `changed` is false.

Other commands:

- `task validate`: returns 0 on success with `{"valid": true}` on stdout; returns 1 when validation fails, listing all errors on stdout; returns 2 for operational errors such as a nonexistent path.
- `task init`: returns 0 on success and prints the generated path on stdout; if the target directory already exists, it refuses, returns 2, and overwrites nothing.

## 17. Implementation and deliverables

- Use the Python 3.10+ standard library plus existing infrastructure such as SSH and tar. The first version introduces no web UI, central scheduling service, external message queue, or third-party Python dependencies.
- Remote servers must run Linux.

Deliverables:

- Complete source code.
- A README, including every assumption the implementation adds where this document is silent.
- An example local machine list and an example remote `config.json` (`max_package_bytes`, `max_cpu_jobs`, `max_auto_restarts`, `stop_grace_seconds`, `kill_confirm_seconds`, `control_poll_seconds`, `base_env`, with defaults).
- The task package template and at least two runnable examples: one task that succeeds and one that declares multiple GPUs.
- Installation instructions for the local CLI.
- Instructions for starting, stopping, and enabling the remote service at boot, with an example systemd unit. Stopping the daemon must not stop running tasks: the unit must set `KillMode=process`, or the daemon must place each shim in its own scope via `systemd-run --scope`. The README states which approach is used and why.
- Documentation of the state machine, directory layout, JSON schemas, and failure recovery, including the manual steps for resolving unknown.

## 18. Verification

### 18.1 Test environment

The automated tests must run on a single Linux machine with no GPU and no network:

- **Fake GPUs**: the environment variable `TASKMGR_NVIDIA_SMI` points to a substitute script that returns configured GPU indices and UUIDs.
- **Multiple servers**: the machine list uses `transport: local`, with a different `remote_root` for each "server".
- **Simulated machine reboot**: the environment variable `TASKMGR_BOOT_ID_FILE` overrides where boot\_id is read from.
- **Simulated unreachability**: the local transport supports injecting connection failures through configuration.
- These overrides are for testing only and are marked as such in the README.
- Use the standard library `unittest`; one command runs all tests: `python -m unittest discover -s taskmgr/tests`.
- The README also gives an optional smoke test against real SSH.

When simulating a reboot by swapping the boot\_id, the test must first kill the "pre-reboot" shim and task processes. Otherwise recovery creates a new attempt while the old processes are still alive, and the duplicate execution it observes comes from the test environment itself.

### 18.2 Coverage

Cover at least:

- Directory and archive submission; packing the same directory twice yields the same content hash.
- Template validation; leftover placeholders and invalid packages are rejected, including archives with paths escaping the package; command-name entries such as `bash` pass validation and are resolved on PATH remotely.
- Round-robin, explicit server selection, and no lost round-robin position under concurrent CLIs.
- Concurrent submission and submission idempotency; the same key with different content is an error; `--submission-key` without `--host` is rejected.
- Restarting the daemon during an upload does not clean up the in-progress upload.
- Incompatible protocol versions produce an error.
- Queuing on GPU conflict, concurrency for disjoint GPUs, and no starvation of multi-GPU jobs by later small jobs; GPUs held by unknown jobs do not lock up other GPUs through reservation.
- Pre-start check failure; shim fails to start.
- Success, non-zero exit code, killed by signal, and invalid\_result.
- The runner keeps business fields when updating results; a `_taskmgr` written by the task is discarded; result.json missing or corrupt.
- Tasks keep running across a daemon restart and the real exit code is still obtained; stopping the daemon with the example systemd configuration does not affect running tasks.
- A restart after the exit code is recorded but before finalization does not re-run the task.
- Takeover when the shim is killed but the task is still running, including cancellation and timeout during takeover; a process group whose leader has exited but whose members are a[Ilive is not mistaken for stopped.
- Within the same boot, when both shim and task have exited, the job is recorded as `exit_code_lost` and not re-run.
- A new attempt after a server reboot; failed after exceeding the auto-restart limit.
- The unknown state never re-runs and never releases GPUs; resolve's automatic check and the `--as stopped` flow.
- Cancellation (queued, running, unknown), timeout, and process-group cleanup; a cancel request stays effective across restarts.
- Multiple remote daemon instances never dispatch the same job twice.
- Cross-server job discovery (including disabled servers), jobid ambiguity, and the various outputs when some servers are unreachable.
- `--wait` for success, failure, unknown, connection retry, and timeout.
- Exit codes of `validate` and `init`.
- Viewing and following logs, downloading results and artifacts, and refusing to overwrite an existing local directory.

