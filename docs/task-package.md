---
title: Task packages
tags: [taskmgr, task-package, schema]
lang: en
up: "[[taskmgr/README]]"
---

# Task packages

Other language: [[docs/task-package.zh-CN]]

| File | Required | Notes |
| --- | --- | --- |
| `task.json` | yes | execution config (below) |
| `TASK.md` | yes | for humans/agents; taskmgr never reads it |
| `progress.json` | yes | initial progress |
| entry script, code, inputs | as needed | |
| `taskmgr_task.py` | template | stdlib helper: `write_progress`, `update_progress`, `write_result`, `update_outputs` (flock + atomic replace); `claim_gpu`, `gpu_busy` (GPU contract below) |

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
  job's workdir, must be executable); otherwise a command name looked up on the entry
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
is kept as `result.json.corrupt` and reported in `validation_errors`; a task-written
`_taskmgr` is dropped with warning `task_wrote_taskmgr`.

`error.kind` values: `setup`, `shim_start_failed`, `exit_code`, `signal`, `exit_code_lost`,
`interrupted` (the run was cut off and an automatic re-run was created),
`too_many_interruptions`, `operator_marked_stopped`, `stop_unconfirmed` (job in unknown),
`record_incomplete` / `undeterminable` (unknown after recovery).

## Entry process environment

1. `PATH=/usr/local/bin:/usr/bin:/bin`, `HOME`, `USER` (of the daemon user), `LANG=C.UTF-8`,
   then `base_env` from `config.json`.
2. With `environment.venv`: `VIRTUAL_ENV=<venv>`, `PATH=<venv>/bin:$PATH`.
3. `env` from `task.json`.
4. Runner: `CUDA_DEVICE_ORDER=PCI_BUS_ID`, `CUDA_VISIBLE_DEVICES=<uuids in declaration
   order>` (empty string for `[]`), `TASKMGR_JOBID`, `TASKMGR_TASK_ID`,
   `TASKMGR_WORKDIR`, `TASKMGR_PROGRESS_FILE`, `TASKMGR_RESULT_FILE`.

stdin is `/dev/null`; stdout/stderr go to `stdout.log` / `stderr.log` in the job directory.

A job runs once. `retry_of` is the jobid this job re-runs (`task retry` or an automatic
re-run after an interruption), `null` for a submission.

## GPU contract: exit code 75

The GPU servers run in Exclusive_Process compute mode, and processes outside taskmgr can
hold a GPU; then no CUDA context can be created on it. taskmgr does not query nvidia-smi
for this. Instead the task reports it:

- Exit code **75** (`EX_TEMPFAIL`, `taskmgr_task.GPU_BUSY_EXIT_CODE`) from the entry
  process means "I could not get the GPU and did no work; start me again later". The
  shim then waits `gpu_retry_interval_seconds` (default 2) and starts the entry again
  in a fresh copy of the package (logs are appended, with a separator line). The job
  stays `running`, keeps its GPUs, and `task jobs` shows it as `pending (gpu busy)`.
  Waiting does not count toward `timeout_seconds`; each launch is timed from its own start.
  `task cancel` works while it waits.
- Exit 75 before producing side effects (files outside the workdir, results); the workdir
  of a busy launch is thrown away.
- Never exit 75 for any other reason: such a task is started again forever, until it is
  cancelled. The server can turn the convention off (`gpu_busy_exit_code: null`), and
  then 75 is an ordinary failure.
- `taskmgr_task.claim_gpu()` (the template's `run.py` calls it first): if
  `CUDA_VISIBLE_DEVICES` is empty or unset it does nothing; otherwise it loads
  `libcuda.so.1` with ctypes, calls `cuInit` and, for every visible device, `cuDeviceGet` and
  `cuDevicePrimaryCtxRetain`. The primary contexts stay retained for the life of the
  process, and frameworks such as torch reuse them. If a call returns
  `CUDA_ERROR_DEVICE_UNAVAILABLE` (46) it calls `gpu_busy("GPU <i> (<uuid>) is held by
  another process ...")`; other CUDA errors and a missing `libcuda.so.1` raise
  `RuntimeError` (the task declared GPUs, so that is a setup problem). Call it before
  slow imports or compilation. Its `load` argument replaces the library loader in tests.
- `taskmgr_task.gpu_busy(message="")` prints the message to stderr and exits 75. Call it
  when your own CUDA code sees "device busy/unavailable".
- If the CUDA work runs in a subprocess, do not call `claim_gpu()` in the parent: in
  exclusive-process mode the parent's context would block the child. Let the child exit 75
  and forward it with `gpu_busy()`.
