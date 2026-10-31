---
title: Failure recovery
tags: [taskmgr, recovery, unknown]
lang: en
up: "[[taskmgr/README]]"
---

# Failure recovery

Other language: [[docs/recovery.zh-CN]]

On start the daemon takes `daemon.lock`, removes `incoming/` entries it can lock that are
older than 1 hour, then under the state lock checks every job. The same check runs every
`control_poll_seconds` for running/stopping jobs whose shim is dead (assumption: the spec
only requires it at startup), and `task resolve` runs it for unknown jobs.

1. `runner.json` final → restore the terminal state (regenerate `result.json._taskmgr` from
   `runner.json` if missing); `interrupted` → step 5.
2. `exited` but not final → finish finalization (leftover check, merge, final). No re-run.
   Exception, GPU busy: if the exit code is `gpu_busy_exit_code` and the job was not
   stopped for cancel or timeout, the entry did no work. Once the group is gone (after a
   takeover if members are alive) the job goes back to `queued` with the **same** jobid
   and `enqueue_seq`: `workdir/` is kept (the next dispatch reuses it), `runner.json` is renamed to
   `runner.prev-N.json`, `dispatched_at` is cleared, the logs are kept. This is not an
   interruption: `auto_restarts` does not change and `max_auto_restarts` does not apply.
   With `cancel_requested` the job ends `cancelled` instead. The same applies to
   `runner.json` status `waiting_gpu` with the shim dead (it died while waiting to
   relaunch; the group was already gone).
3. queued → keep waiting. A queued job whose `runner.json` shows such a GPU-busy exit (a
   crash while it was being queued again) has the record set aside and is dispatched.
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
5. Re-run: `runner.json` → `interrupted`; `cancel_requested` → cancelled;
   `auto_restarts` at `max_auto_restarts` → `failed` / `too_many_interruptions`; else the
   job ends `failed` with `error.kind = "interrupted"` and a new job is created from its
   package (hard links to the same files; an automatic retry): it keeps the original `enqueue_seq`, has
   `auto_restarts` + 1 and `retry_of` = the interrupted job, whose `retried_as` gets the new
   jobid. A crash in the middle of this step reuses the re-run job already created instead
   of creating a second one. `task status --wait` follows the re-run.

Two errors end a job without these rules:

- `shim_error`: the shim failed (an exception) before it started the entry. It ends the
  job `failed` itself (`cancelled` if a cancel was requested). This is not an
  interruption: no re-run is created and `auto_restarts` does not change. If the entry may
  already have started, the shim leaves the job to the checks above.
- `recovery_error`: an error in the daemon's per-job work (startup recovery, the
  periodic health check, a takeover tick) is logged and the other jobs carry on. After 3
  errors in a row for the same job the daemon stops tracking it and marks it `unknown`
  with `error.kind = "recovery_error"`; it keeps its GPUs until resolved (below). A job
  that cannot even be marked unknown is skipped until the daemon restarts.

## Resolving `unknown` manually

`unknown` jobs never re-run and keep their GPUs (also after a GPU-busy exit: resolving
such a job ends it `failed` with exit code 75).

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
5. Re-run with `task retry <jobid> --host H` if wanted (this creates a new job).
