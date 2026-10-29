---
title: taskmgr design decisions (supplement to desc-1)
date: 2026-10-30
supplements: "[[desc-1]]"
implementation: "[[taskmgr/README]]"
tags: [taskmgr, design, decisions]
---

# taskmgr design decisions

This document adds to [[desc-1]]. It records the decisions the first implementation made
where desc-1 leaves room, and the reason for each. Section numbers in parentheses refer to
desc-1. If this document and desc-1 conflict, desc-1 wins and the code should be fixed.
User-facing details (commands, schemas, config defaults) are in [[taskmgr/README]] and
`docs/` ([[docs/usage]], [[docs/task-package]], [[docs/install]], ...).

## D1. Code layout (§1, §17)

| Module | Role |
| --- | --- |
| `taskmgr/taskmgr.py` | local CLI entry (`task`) |
| `taskmgr/remote.py` | remote entry (`taskmgr-remote`): subcommands, `serve`, `shim` |
| `taskmgr/daemon.py` | scheduler loop, shim spawn, startup recovery, dead-shim detection |
| `taskmgr/shim.py` | per-attempt supervisor |
| `taskmgr/monitor.py` | stop procedure, recovery check (§12.2), takeover tracker |
| `taskmgr/store.py` | remote root, state lock, job/runner records, result merge, finalization |
| `taskmgr/package.py` | validation, deterministic packing, content hash, safe extraction |

The shim is the same program as the remote CLI (`taskmgr-remote shim <jobid> <n>`), so a
server has one installation and one version of the code.

## D2. Shim start: double fork and lock handover (§1.1, §9.3, §12.2)

- The daemon forks, the child calls `setsid()` and forks again, and the grandchild execs the
  shim. The intermediate child exits at once, so the shim is reparented to init and is not
  the daemon's child.
- Before forking, the daemon creates `attempts/NNNN/shim.lock` and takes the flock itself.
  The locked fd is inherited by the shim (`--lock-fd N`), and the daemon then closes its copy.
  *Reason:* if the shim had to take the lock itself, the daemon could see an unlocked
  `shim.lock` between spawn and lock and conclude that the shim was dead.
- The shim marks the lock fd close-on-exec, so the task never inherits it. Otherwise a
  task that outlives its shim would keep the lock and hide the shim's death.
- An exec failure is reported to the daemon through a close-on-exec pipe: if the pipe gets
  data, the job fails with `shim_start_failed`, and if it closes empty, the exec worked.

## D3. Dead-shim detection while the daemon is running (§12.2)

desc-1 requires the recovery check at daemon startup. The daemon also runs the same check
every `control_poll_seconds` for running/stopping jobs whose `shim.lock` is free. Without
this check, a shim crash during normal operation would leave the job `running` until the
next daemon restart.

## D4. Takeover runs inside the daemon loop (§12.2 case 4.4)

Takeover is a non-blocking state machine (`monitor.Takeover`) that advances on each
daemon tick through watch → SIGTERM sent → SIGKILL sent. A blocking stop procedure would
stall scheduling for up to `stop_grace_seconds + kill_confirm_seconds`. While tracking, the
daemon holds the attempt's `shim.lock`. The timeout during takeover is measured by wall
clock from `runner.json.started_at_epoch`, because the shim's monotonic clock is gone.

## D5. Cancel only writes `cancel_requested` (§13.2, §8.2)

`task cancel` on a running job sets `cancel_requested: true` and leaves the state as
`running`. The supervisor (the shim, or the daemon under takeover) moves the job to
`stopping` with `stop_reason: "cancel"` within `control_poll_seconds`. *Reason:* only the
supervisor knows whether the entry has already exited, so it is the only process that can
make running → stopping without racing finalization. Consequences:

- A cancel that arrives after the entry has exited does not change its outcome.
- A cancel seen before the entry starts goes running → stopping → cancelled without
  executing the entry.
- A cancel recorded on an `unknown` job makes it `cancelled` once a later check confirms
  that the stop happened, whether or not the entry had exited.
- A cancel request survives daemon restarts and reboots (recovery treats it as the stop
  reason).

## D6. Leftover cleanup goes through `stopping` (§10.4 step 2, §8.2)

When the entry exits and members of its process group remain, the shim writes
`stop_reason: "leftover"`, moves the job running → stopping, and runs the stop procedure.
If the stop is confirmed, the outcome comes from the real exit code, with the warning
`leftover_processes`. If not, the job becomes `unknown`. desc-1 §8.2 allows only
stopping → unknown, so the shim never goes running → unknown.

## D7. `resolve` and live processes (§13.5)

- If the processes are gone, the job ends by the normal recovery rules.
- If processes are alive and `runner.json.stop_unconfirmed_reason` is set (SIGKILL already
  failed), the job stays `unknown` and the output shows the identity record and
  `alive_pids` for the operator.
- If processes are alive and the job became unknown because a check could not be decided
  (`/proc` unreadable, incomplete record), the job returns to running/stopping and the
  daemon takes over (case 4.4).

## D8. Submission (§5, §6)

- An archive may hold the package at its root or inside one top-level directory.
- Without `--submission-key`, the CLI generates `cli-<32 hex>`.
- The remote side checks the submission key immediately after extraction and hashing,
  before validation and GPU resolution. *Reason:* a retry after a lost ACK must return the
  original jobid even if the machine's GPUs or nvidia-smi changed in the meantime.
- Under the state lock, the key mapping is written before the rename into `queue/`. A
  mapping whose job directory does not exist is treated as stale and overwritten.
- Before the ACK, the extracted `package/` is synced to disk (`os.sync()`), because every
  attempt starts from it.
- The upload takes a blocking flock on its own incoming directory.
- `package/` files and directories are made read-only.
- The local CLI treats ssh exit status 255 as a connection error and runs ssh with
  `BatchMode=yes`. Ctrl-C during an upload is reported as an uncertain outcome, together
  with the retry command.

## D9. Validation details (§2.4, §5.2)

- Symlinks are checked twice: a lexical check of the link target, and a physical check with
  `realpath` that the link resolves inside the package. The lexical check alone accepts
  chains such as `x -> .`, `y -> x/..`.
- `task validate` on an archive reports every unsafe entry together with the schema
  errors. An unreadable archive is a validation error (exit 1, `bad_archive`).
- Regexes match the whole string (`\Z`), so a trailing newline is rejected.
- Extra keys in `task.json` are allowed.
- `task init` writes a valid `progress.json` with the current time. `<REQUIRED...>`
  markers go into `task.json` (`gpus`, `environment.venv`, `result.required_outputs`) and
  into every `TASK.md` section.

## D10. Records and results (§9, §10, §11)

- The enqueue counter is stored in `locks/counters.json` and updated under the state lock.
- `runner.json` stores the full `_taskmgr` block (`runner.taskmgr`) at finalization, so
  recovery can regenerate `result.json` from it.
- Warnings found in finalization steps 2–3 are written to `runner.json` before
  `result.json` is written, so a shim crash in between does not lose them.
- On re-finalization, a `_taskmgr` block that was written for the same jobid and attempt is
  recognized as the runner's own and does not trigger `task_wrote_taskmgr`.
- A corrupt `result.json` is renamed to `result.json.corrupt`, or to `.corrupt.N` if the
  task already created a file with that name.
- `_taskmgr.paths` point to the attempt's final location (`done/...`). While the job runs,
  `TASKMGR_WORKDIR` and the other paths point to `in-progress/...`, because the directory
  moves when the job ends.
- `_taskmgr.host` is the remote hostname.
- `error.kind` values: `setup`, `shim_start_failed`, `exit_code`, `signal`,
  `exit_code_lost`, `too_many_interruptions`, `operator_marked_stopped`,
  `stop_unconfirmed`, `record_incomplete`, `undeterminable`.

## D11. Scheduling (§7.2)

- `max_cpu_jobs` (default `null` = unlimited) counts CPU-only jobs in the running,
  stopping, and unknown states.
- An extra config key, `scheduler_poll_seconds` (default 1), sets how often the scheduler
  runs a pass.
- A new attempt's number is `max(len(attempts), existing attempt dirs) + 1`, so a crash
  that leaves an orphan attempt directory never causes reuse of that number.

## D12. CLI output (§14, §16)

- `task jobs` prints a table by default (JOBID, TASK_ID, SUBMITTED, HOST, STATE, sorted by
  submission time, STATE colored on a terminal); `--detail` prints the §14.1 JSON object.
  This departs from §16 ("all commands except logs print one JSON object"); user decision
  2026-10-30. Exit codes are unchanged; in table mode errors go to stderr only.

- `task status` on a finished job prints `result.json` with two top-level keys added:
  `host` and `complete`. A job that ended without any attempt (cancelled while queued)
  prints a synthesized `{"schema_version", "_taskmgr"}`.
- If `--wait --timeout` expires, the CLI prints the current status with
  `error.code = "wait_timeout"` and exits 2. The deadline also applies while connection
  failures are being retried.
- Ctrl-C prints an `interrupted` error and exits 2. Unexpected exceptions print
  `internal_error` and exit 2. For `logs`, errors go to stderr only.
- Protocol compatibility means the integer `protocol_version` values are equal (currently
  1).
- `logs --follow` ends when the attempt is final, the job is terminal or unknown, or a
  newer attempt has replaced it.
- `fetch` skips unsafe entries that a task created (absolute or escaping symlinks, special
  files), warns about them, and lists them in `skipped`. Files that change or disappear
  during the walk of a running attempt are skipped, and temporary `.tmp-*` files are
  excluded.
- `jobs` deduplicates by jobid if a concurrent directory move shows a job twice.

## D13. Deployment (§17)

- We assume an unprivileged user on the GPU servers, so the only deployment is a **systemd
  user service** (`deploy/taskmgr-remote.user.service`) under the account that owns the jobs,
  with code in `~/taskmgr` and `remote_root` in `~/taskmgr-root` (or local scratch disk if
  `$HOME` is on NFS). The machine list uses absolute paths, because `~` is not expanded.
- Lingering is required: `loginctl enable-linger "$USER"`. Without it the user manager
  stops at the last logout and takes the unit's cgroup (shims and tasks) with it, and
  nothing starts at boot. Enabling it may need an admin once, depending on the host's
  polkit policy; that is the only step that may need root. logind must not have
  `KillUserProcesses=yes`.
- The unit uses `KillMode=process`; the alternative was `systemd-run --user --scope` per
  shim. `KillMode=process` needs no D-Bus access from the daemon, and the daemon stays a
  plain process that also runs under tmux or nohup.
- No system-level unit is shipped (an earlier `deploy/taskmgr-remote.service` was
  removed): nothing in taskmgr needs root.
- The daemon exits on SIGTERM without touching shims and logs to stderr (journald) and to
  `daemon/daemon.log`.

## D14. Test environment (§18)

- Tests run in Linux (Docker with `--init`, so orphaned task processes are reaped). A test
  stands in for systemd stop by sending SIGTERM to the daemon's whole process group.
- In addition to the overrides desc-1 requires (`TASKMGR_NVIDIA_SMI`,
  `TASKMGR_BOOT_ID_FILE`, local-transport failure injection), the tests use these test-only
  hooks:

| Hook | Purpose |
| --- | --- |
| `TASKMGR_TEST_PROTOCOL_VERSION` | remote reports a different protocol version |
| `TASKMGR_TEST_SKIP_DAEMON_LOCK=1` | several daemons; checks that state.lock prevents double dispatch |
| `TASKMGR_SHIM_EXECUTABLE` | shim exec failure |
| `TASKMGR_TEST_SHIM_CRASH=<point>` | shim exits at `before_identity`, `before_exec`, `after_exit_recorded`, `before_finalize` |
| `TASKMGR_TEST_IGNORE_KILL=1` | stop procedure sends no signals (models a process that survives SIGKILL) |
| `TASKMGR_TEST_RECEIVE_PAUSE=<s>` | holds an upload open across a daemon restart |
| `"local": {"fail" \| "fail_if_exists"}` | static or toggled connection failure |

## D15. Known limitations

- Only the task's process group is managed; a process that calls `setsid()` itself escapes.
- No MIG support. Use of a GPU by processes outside taskmgr is not detected.
- `remote_root` must be on a local filesystem.
- An automatic re-run starts from the original package, with no checkpoint resume.
- Not yet run against real SSH or real GPUs; see the smoke test in [[docs/testing]].
