---
title: Task packages
tags: [taskmgr, task-package, schema]
lang: en
up: "[[taskmgr/README]]"
---

# Task packages

Other language: [[docs/task-package.zh-CN]]

`task init` writes a template to fill in; `task pack` builds a package from project files and
options ([[docs/usage]], Packing).

| File | Required | Notes |
| --- | --- | --- |
| `task.json` | yes | execution config (below) |
| `TASK.md` | yes | for humans/agents; taskmgr never reads it |
| `progress.json` | yes | initial progress |
| entry script, code, inputs | as needed | |
| `precompile.py`, `cutecache.py` | template | compile CuTe DSL kernels on the CPU and load them on the GPU (see Precompile); delete them if the task compiles nothing |
| `taskmgr_task.py` | template | stdlib helper: `write_progress`, `update_progress`, `write_result`, `update_outputs` (flock + atomic replace); `claim_gpu`, `gpu_busy` (GPU contract below) |

`task.json`:

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

- Keys: only the ones shown here (and `precompile`). Any other key, at the top level or
  inside `entry`, `precompile`, `environment` or `result`, is an error (code
  `unknown_key`) in `task validate`, `task submit` and on the server: a key this taskmgr does
  not know may be a feature of a newer taskmgr, or a typo, and is never ignored. Keys
  starting with `x-` are annotations and are ignored (`"x-owner": "junyi"`).
- `task_id` matches `^[A-Z]{2}-[0-9]{2,4}$`.
- `entry.argv[0]`: contains `/` → path (relative = package file, resolved against the
  job's workdir, must be an executable file in the package); otherwise a command name
  looked up on the entry process PATH on the remote side. No shell is involved; use
  `["bash", "-c", "..."]`.
- `precompile` (optional): `{"argv": [...], "timeout_seconds": N}`, see Precompile.
  `argv` follows the rules of `entry.argv`; `timeout_seconds` is optional.
- `gpus`: nvidia-smi indices (PCI bus order) or full `GPU-...` UUIDs; `[]` = no GPU. MIG
  UUIDs and duplicates (also an index and a UUID naming the same GPU) are rejected. Or an
  object that lets the server choose the GPUs when the job is launched (see GPU selection
  below; needs the server feature `gpu_select`):
  `{"count": 1}` (any idle GPU), `{"count": 2, "min_free_gb": 40}`,
  `{"count": 1, "min_free_gb": 40, "from": [4, 5, 6, 7]}`.
- `env` names match `^[A-Za-z_][A-Za-z0-9_]*$`; values are strings. `env` may not set
  `CUDA_VISIBLE_DEVICES`, `CUDA_DEVICE_ORDER`, `VIRTUAL_ENV`, `TASKMGR_*`.

`progress.json`: `{"schema_version": 1, "stage": str, "updated_at": "UTC ISO 8601",
"percent"?: 0..100, "message"?: str}`. Written only by the task.

`result.json` (written by the task during the run, merged by the runner after the process
group has exited):

```json
{"schema_version": 1, "outputs": {...}, "<other business keys>": "...",
 "_taskmgr": {"jobid", "task_id", "retry_of", "host", "gpus": [{"declared", "uuid"}],
              "status", "return_code", "signal", "started_at", "ended_at",
              "validation_errors": [], "error": {"kind", "message"} | null,
              "warnings": ["task_wrote_taskmgr" | "leftover_processes"],
              "paths": {"job_dir", "workdir", "stdout", "stderr", "shim_log",
                        "progress", "result", "artifacts"},
              "final": true}}
```

Result is valid when it is a JSON object with an `outputs` object containing every
`required_outputs` key. A missing file yields `{"schema_version", "_taskmgr"}`; a corrupt one
(including JSON nested too deeply to parse), or a `result.json` that is not a regular file
(directory, FIFO, symlink: links are not followed), is kept as `result.json.corrupt` and
reported in `validation_errors`. A `progress.json` that is not a regular file shows as
`{"error": ...}` in `task status`. A task-written
`_taskmgr` is dropped with warning `task_wrote_taskmgr`.

`error.kind` values: `setup`, `precompile` (the precompile command failed), `shim_start_failed`, `shim_error` (the shim failed before
it started the entry; the job ends `failed` and is not re-run), `exit_code`, `signal`,
`exit_code_lost`, `interrupted` (the run was cut off; an automatic re-run was created unless
`task resolve` ended an `unknown` job),
`too_many_interruptions`, `operator_marked_stopped`, `stop_unconfirmed` (job in unknown),
`record_incomplete` / `undeterminable` (unknown after recovery), `recovery_error` (unknown:
the daemon's recovery or tracking of the job failed repeatedly, see [[docs/recovery]]).

## Packing and validation

`task validate` and `task submit` check the package locally; the server checks it again
after the upload.

### Server features

Some `task.json` keys need a server whose code implements them. Every `taskmgr-remote`
reply carries `"features"`, the sorted list of what its code implements (a server without
the key runs code older than the list and implements none of them):

| Feature | Meaning |
| --- | --- |
| `strict_task_json` | the server rejects task.json keys it does not know (`unknown_key`) |
| `precompile` | `task.json` `precompile` |
| `gpu_select` | `task.json` `gpus` as an object (GPU selection) |
| `gpu_holders` | `status` of a job waiting for GPUs lists the processes holding them ([[docs/usage]]) |
| `cancel_by` | `cancel` records who asked (`cancel_request`, [[docs/usage]]) |

`task validate --detail` lists the features a package needs (`"features"`). `task submit`
asks the server for its features (`taskmgr-remote version`) before the upload when the
package needs any, and refuses a server that lacks one: error `unsupported_feature` (exit
2, nothing uploaded), e.g. `server gpu1 runs code 3f9a1c0e7b2d without precompile support,
needed by this package; run `task host add gpu1` to update it`; `details` has `missing` and
`server_features`. `task host ping --detail` and `task version --detail` show each server's
`features`.

- Left out of the packed archive and of the content hash: every path with a component
  named `__pycache__`, `.git` or `.DS_Store`, ending in `.pyc` or `.json.lock` (flock
  files of `taskmgr_task.py`, e.g. from a local test run) or starting with `.tmp-`
  (unfinished atomic writes); at the package root also `result.json` and names starting
  with `result.json.corrupt`. Other names such as `result.jsonl` are packed. The server
  deletes these entries from an uploaded archive before it computes the content hash, so
  a stale `result.json` in an archive never becomes a run's result. `task validate` lists
  every left-out path as a warning (code `excluded`; below an excluded directory only the
  directory).
- Size: the server refuses a package whose regular files total more than its
  `max_package_bytes` while extracting (error `package_too_large`), and stops receiving an
  upload that is larger than the limit plus 1/16 plus 1 MiB. `task submit` does not check
  the size locally. `task validate` warns (code `package_too_large`) when the package is
  larger than the server default (1073741824 bytes); a server may allow more or less.
- Symlinks must be relative and resolve inside the package (also through other links);
  special files (FIFOs, devices, sockets) are rejected.
- A relative `entry.argv[0]` must name a regular file inside the package that is not left
  out of packing and has the executable bit (error `entry`; fix with `chmod +x` or run it
  through an interpreter, e.g. `["python", "run.py"]`).
- Extracting an archive (on the server, and `task validate` of a `.tar.gz`) refuses
  absolute paths, `..`, duplicate entries (compared case-insensitively), hard links,
  device files, FIFOs, and symlinks that point or resolve outside the package. Nothing is
  written through a symlink an earlier entry created, and no existing file is
  overwritten. `task validate` lists every unsafe entry; the server stops at the first
  (`unsafe_package`).

## Precompile

A job holds its GPUs while its entry runs, and on exclusive GPUs nothing else can use them.
Compile before that. With `precompile` in `task.json`:

```json
"precompile": {"argv": ["python3", "precompile.py", "kernels.py"], "timeout_seconds": 1800}
```

| STATE shown | job state | What happens | GPUs |
| --- | --- | --- | --- |
| `queued` | queued | waits for a CPU slot (`max_cpu_jobs`) | none |
| `precompiling` | running | the precompile command runs | none, `CUDA_VISIBLE_DEVICES=""` |
| `pending-gpu` | queued | waits for its GPUs at its original queue position | none |
| `running` | running | the entry runs | yes |

- Both commands run in the same workdir, so the entry finds what the precompile built.
- Exit 0 moves the job on. Any other exit, including 75, fails it with `error.kind =
  "precompile"` and the entry never runs. `TASKMGR_GPU_BUSY_EXIT_CODE` is not set.
- `precompile.timeout_seconds` limits the precompile; `timeout_seconds` limits only the entry.
  The time in `pending-gpu` counts toward neither.
- `task cancel` works in every phase. `task logs` shows the output of both commands.
- A retry or an automatic re-run is a new job with an empty workdir: it precompiles again.

`task init` adds `precompile.py` and `cutecache.py`. `precompile.py` runs the file's
`_*compile_all()` steps on the CPU and stores the kernels in `kernel-cache/` in the workdir
(`CUTECACHE_DIR` overrides it). The entry loads them with
`cutecache.load_precompiled("kernels.py")`; a kernel that was not precompiled fails the run
instead of compiling on the GPU. The defaults describe an H200; `PRECOMPILE_ARCH`,
`PRECOMPILE_QUACK_ARCH`, `PRECOMPILE_N_SM` and `PRECOMPILE_CAPABILITY` change them.

## GPU selection

With `gpus` as an object the job names no GPUs; the server chooses them each time the job is
about to start:

| Key | Required | Meaning |
| --- | --- | --- |
| `count` | yes | how many GPUs, an integer >= 1 |
| `min_free_gb` | no | each GPU must have at least this much memory free (GiB, 1024 MiB as nvidia-smi counts; a number > 0) |
| `from` | no | the candidates: indices or `GPU-...` UUIDs, as in the list form (default: every GPU) |

A GPU qualifies when, at that moment (nvidia-smi on the server):

- no taskmgr job holds it (running, stopping, unknown) and no earlier queued job waits for it;
- its compute mode is not `Prohibited`, and in `Exclusive_Process` mode no process holds a
  CUDA context on it (none could be created);
- with `min_free_gb`: its free memory is at least that; without: no process holds a CUDA
  context on it at all ("any idle GPU").

Among the qualifying GPUs the ones with the most free memory are taken, ties by lowest
index. The choice is recorded in `job.json` `gpus` (`{"declared": <index>, "uuid"}`), so
`task status` and `_taskmgr.gpus` in `result.json` show which GPUs the job got;
`CUDA_VISIBLE_DEVICES` has their UUIDs as usual. `task status` also shows the request as
`gpu_request` (`from` resolved to UUIDs as `from_uuids`).

When fewer than `count` GPUs qualify, the job stays `queued` (shown `pending-gpu`) with a
`pending_reason` such as `waiting for 2 GPUs with >= 40 GiB free (1 qualify now)`, and the
scheduler tries again on every pass (`scheduler_poll_seconds`, default 1 s), like the
GPU-busy relaunch. It reads nvidia-smi for this at most every 5 s, and again as soon as a
taskmgr job with GPUs starts or ends; when nvidia-smi fails or takes more than 10 s, these
jobs wait (`pending_reason` `cannot choose GPUs: ...`) and the next read is 30 s later,
while other jobs are scheduled as usual. A waiting job reserves no GPU: a later job with fixed GPUs that are free
can start before it. If the entry exits 75 on a chosen GPU (another process took it after the
choice), the job is queued again at its place with its GPUs cleared, and the first pass after
`gpu_retry_interval_seconds` chooses again (until then `pending_reason` reads
`entry exited 75 (GPU busy, #N); choosing GPUs again from <time>`, N counting these
requeues); the workdir is kept as for any GPU-busy relaunch, and the run record of such an
exit is kept as one `runner.prev-N.json` however often it happens. A retry or an automatic
re-run chooses its GPUs again. With `precompile`, GPUs are chosen after the precompile.

At submit the server refuses a request this machine can never meet (`invalid_gpus`): more GPUs
than it has (or than `from` lists), or `min_free_gb` above the total memory of enough GPUs.

## Entry process environment

1. `PATH=/usr/local/bin:/usr/bin:/bin`, `HOME`, `USER` (of the daemon user), `LANG=C.UTF-8`,
   then `base_env` from `config.json`.
2. With `environment.venv`: `VIRTUAL_ENV=<venv>`, `PATH=<venv>/bin:$PATH`.
3. `env` from `task.json`.
4. Runner: `CUDA_DEVICE_ORDER=PCI_BUS_ID`, `CUDA_VISIBLE_DEVICES=<uuids in declaration
   order>` (empty string for `[]`), `TASKMGR_JOBID`, `TASKMGR_TASK_ID`,
   `TASKMGR_WORKDIR`, `TASKMGR_PROGRESS_FILE`, `TASKMGR_RESULT_FILE`, and
   `TASKMGR_GPU_BUSY_EXIT_CODE` (the server's `gpu_busy_exit_code`; when it is `null` the
   variable is removed, also when `base_env` sets it).

stdin is `/dev/null`; stdout/stderr go to `stdout.log` / `stderr.log` in the job directory.

A job runs once. `retry_of` is the jobid this job re-runs (`task retry` or an automatic
re-run after an interruption), `null` for a submission.

## GPU contract: exit code 75

The GPU servers run in Exclusive_Process compute mode, and processes outside taskmgr can
hold a GPU; then no CUDA context can be created on it. taskmgr does not query nvidia-smi
for this. Instead the task reports it:

- Exit code **75** (`EX_TEMPFAIL`, `taskmgr_task.GPU_BUSY_EXIT_CODE`) from the entry
  process means "I could not get the GPU and did no work; start me again later". The
  shim then waits `gpu_retry_interval_seconds` (default 1) and starts the entry again
  in the same workdir (logs are appended, with a separator line). The job
  stays `running`, keeps its GPUs, and `task jobs` shows it as `pending-gpu`.
  Waiting does not count toward `timeout_seconds`; each launch is timed from its own start.
  `task cancel` works while it waits.
- Exit 75 before producing side effects (files outside the workdir, results). The workdir
  is kept: a relaunch sees the files the previous launch left there (e.g. a compiled
  binary), so anything written before the exit 75 must be safe to reuse.
- Never exit 75 for any other reason: such a task is started again forever, until it is
  cancelled. The server can turn the convention off (`gpu_busy_exit_code: null`), and
  then 75 is an ordinary failure.
- Create the CUDA context first, before importing torch or any other heavy library. The
  template's `run.py` shows the order:

  ```python
  import taskmgr_task as tt   # module level: the standard library and taskmgr_task only

  def main():
      tt.claim_gpu()          # first: the CUDA context in milliseconds, or exit 75
      import torch            # now; reuses the primary context claim_gpu created
  ```

  A busy GPU is then found in milliseconds and the relaunch about 1 s later is cheap; with
  `import torch` first, every retry pays the import (seconds) again. taskmgr does not check
  whether a GPU is free before it starts the entry, and the entry should not either: another
  process can take the GPU between such a check and the moment the entry creates its
  context. Creating the context is the check.
- `taskmgr_task.claim_gpu()`: if `CUDA_VISIBLE_DEVICES` is empty or unset it does nothing;
  otherwise it loads `libcuda.so.1` with ctypes (no CUDA Python package, no torch), calls
  `cuInit` and, for every visible device, `cuDeviceGet` and `cuDevicePrimaryCtxRetain`, which
  creates the device's primary context. The primary contexts stay retained for the life of
  the process. The CUDA runtime uses the primary context of each device, so torch, triton,
  cupy and CuTe imported later in the same process reuse these contexts and create none of
  their own. If a call returns `CUDA_ERROR_DEVICE_UNAVAILABLE` (46) it calls
  `gpu_busy("GPU <i> (<uuid>) is held by another process ...")`; other CUDA errors and a
  missing `libcuda.so.1` raise `RuntimeError` (the task declared GPUs, so that is a setup
  problem). Its `load` argument replaces the library loader in tests. An entry that is not
  Python (a compiled binary) does the same itself: create the context first
  (`cudaFree(0)` or `cuDevicePrimaryCtxRetain`) and exit 75 on error 46.
- `taskmgr_task.gpu_busy(message="")` prints the message to stderr and exits with
  `taskmgr_task.gpu_busy_exit_code()`: the value of `TASKMGR_GPU_BUSY_EXIT_CODE`, or 75
  when it is unset or not an integer from 1 to 255. Call it when your own CUDA code sees
  "device busy/unavailable".
- If the CUDA work runs in a subprocess, do not call `claim_gpu()` in the parent: in
  exclusive-process mode the parent's context would block the child. Let the child create
  its context first, exit 75 when the GPU is busy, and forward that in the parent with
  `gpu_busy()`. The same holds for a parent that forks after `claim_gpu()`: a forked child
  cannot use the parent's CUDA context.
