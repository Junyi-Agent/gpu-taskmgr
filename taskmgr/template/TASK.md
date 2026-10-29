# {{TASK_ID}}

<!--
Filling instructions (for humans and AI agents):
- Replace every REQUIRED marker (angle-bracket placeholder) here and in task.json. The package is
  rejected by `task validate` / `task submit` while any marker remains.
- task.json fields: entry.argv (the command; relative paths with "/" are package files,
  bare names like python3 are looked up on PATH remotely), gpus (nvidia-smi indices or
  GPU-... UUIDs, [] for none), environment.venv / required_paths (absolute remote paths),
  env (no CUDA_VISIBLE_DEVICES, CUDA_DEVICE_ORDER, VIRTUAL_ENV or TASKMGR_*),
  timeout_seconds (optional), result.required_outputs (keys that must be in outputs).
- The entry script writes progress and results with taskmgr_task.py:
      import taskmgr_task as tt
      tt.write_progress("profiling", percent=40, message="kernel 3/8")
      tt.update_outputs(best_config={"block": 128}, speedup=1.37)
- Put artifacts (profiles, CSVs, modified kernels) under ./artifacts/.
- GPU contract: exit code 75 means "the GPU is held by another process and I did no
  work"; taskmgr then starts the entry again after a short wait (the job shows as
  "pending (gpu busy)") instead of failing it. Call tt.claim_gpu() before any work
  (run.py does). If your own CUDA code later sees "device busy/unavailable", call
  tt.gpu_busy("why") before producing side effects. Never exit 75 for any other reason:
  such a task would be started again forever (until cancelled). If the CUDA work runs in
  a subprocess, do not call claim_gpu() in the parent (on exclusive-process GPUs the
  parent's context would block the child); let the child exit 75 and forward it with
  tt.gpu_busy().
- taskmgr only runs entry.argv; it never reads this file. If an AI agent should do the
  work, make entry.argv start that agent and point it at TASK.md.
-->

## Objective

<REQUIRED: what this task must achieve, in one or two sentences>

## Background

<REQUIRED: kernel / model / hardware context the executor needs>

## Steps

<REQUIRED: concrete steps the entry command (or the agent it starts) performs>

## Expected outputs

<REQUIRED: each key written to result.json "outputs" and its meaning; keep in sync with result.required_outputs>

## Success criteria

<REQUIRED: how to judge the result, e.g. speedup >= 1.1 with identical numerics>
