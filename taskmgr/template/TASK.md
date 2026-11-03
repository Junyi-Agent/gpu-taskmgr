# {{TASK_ID}}

<!--
Filling instructions (for humans and AI agents):
- Replace every REQUIRED marker (angle-bracket placeholder) here and in task.json. The package is
  rejected by `task validate` / `task submit` while any marker remains.
- Compile before you take a GPU. A job holds its GPUs only while entry.argv runs, and
  other jobs wait for them, so never spend that time compiling. Put every compile step
  (CuTe kernels, torch extensions, binaries) in task.json `precompile`: taskmgr runs it
  first with CUDA_VISIBLE_DEVICES="" and no GPU, in the same workdir the entry runs in,
  then queues the job for its GPUs. The job shows as "precompiling", then "pending-gpu",
  then "running". A precompile that does not exit 0 fails the job. precompile.py and
  cutecache.py do this for CuTe DSL kernels: the kernels file has _*compile_all()
  functions that compile with fake tensors, and run.py loads it with
  cutecache.load_precompiled, which fails on a kernel that was not precompiled rather
  than compiling it on the GPU. If nothing compiles, delete the `precompile` key and
  both files.
- task.json fields: precompile.argv and precompile.timeout_seconds (optional, separate
  from timeout_seconds), entry.argv (the command; relative paths with "/" are package files,
  bare names like python3 are looked up on PATH remotely), gpus (nvidia-smi indices or
  GPU-... UUIDs, [] for none; or {"count": N, "min_free_gb": X, "from": [...]} to take any
  N GPUs that are free, with X GiB free each, when the job starts), environment.venv / required_paths (absolute remote paths),
  env (no CUDA_VISIBLE_DEVICES, CUDA_DEVICE_ORDER, VIRTUAL_ENV or TASKMGR_*),
  timeout_seconds (optional), result.required_outputs (keys that must be in outputs).
- The entry script writes progress and results with taskmgr_task.py:
      import taskmgr_task as tt
      tt.write_progress("profiling", percent=40, message="kernel 3/8")
      tt.update_outputs(best_config={"block": 128}, speedup=1.37)
- Put artifacts (profiles, CSVs, modified kernels) under ./artifacts/.
- GPU contract: exit code 75 means "the GPU is held by another process and I did no
  work"; taskmgr then starts the entry again after about 1 s (the job shows as
  "pending-gpu") instead of failing it. Create the CUDA context first: call
  tt.claim_gpu() as the first line of main(), BEFORE importing torch or any other heavy
  library (run.py does). It creates the GPU's primary CUDA context through libcuda in
  milliseconds, or exits 75 when the GPU is busy, so each retry is cheap; torch imported
  afterwards reuses that context, so the GPU cannot be taken in between. Do not replace it
  with a "is the GPU free?" check. If your own CUDA code later sees "device busy/unavailable", call
  tt.gpu_busy("why") before producing side effects. The relaunch reuses the workdir, so
  files written before the exit 75 must be safe to reuse. Never exit 75 for any other reason:
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
