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

## Resolving `unknown` manually

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
