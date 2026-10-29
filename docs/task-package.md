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
