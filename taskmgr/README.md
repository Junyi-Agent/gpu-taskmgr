# taskmgr

English | [[taskmgr/README.zh-CN|简体中文]]

Task manager for GPU performance tuning. A local CLI (`task`) stores and submits task
packages. Each remote GPU server runs `taskmgr-remote`, which keeps its own queue, schedules
jobs onto GPUs, runs them, and recovers after restarts. There is no central scheduler.
Requirements: Python 3.10+ standard library; Linux on the remote side.

Spec: [[agent/desc-1.md]]. Design decisions: [[agent/desc-2.md]].

## Quick start

Below, `gpu1` is the name you give the GPU server in the machine list, `tuner@10.0.0.12`
its ssh address, and `tuner` your user name on it. No step needs root.

**1. Your machine: install the CLI**

```sh
uv tool install --editable /path/to/gpu-taskmgr     # provides the `task` command
```

**2. Your machine: set up the server**

```sh
task add host gpu1 --ssh tuner@10.0.0.12
```

This copies the code to `~/taskmgr` on the server, creates `~/taskmgr-root` with a
`config.json`, installs and starts the systemd user service, enables lingering, and adds
`gpu1` to `~/.taskmgr/machines.json`. Each step says what it found; each change asks
`[y/N]` first (`--yes` answers yes). If `loginctl enable-linger` is refused, the step
reports the command an admin has to run once. If `$HOME` is on NFS, add
`--remote-root /scratch/tuner/taskmgr-root` (local disk). Details: [[docs/install]].

To update the server later (after `git pull`), run `task add host gpu1` again. It is
idempotent: on an up-to-date server it changes nothing and asks nothing.

The CLI and every server must speak the same `protocol_version`; a mismatch fails with
`protocol_mismatch` (exit 2). Protocol 2 removed attempts (a job runs once; a retry is a new
job). Job directories written by protocol 1 (`attempts/NNNN/`) are not migrated: `task add
host` moves them into `<remote_root>.backup-<time>/` after asking, and refuses while such a
job may still be running.

**3. Use it**

```sh
task hosts                           # check the machine list
task init AA-01                      # template; fill every <REQUIRED...> marker
task validate AA-01/
task submit AA-01/                   # prints {"host", "jobid", ...}
task status <jobid> --wait
task fetch <jobid> --host gpu1 --output ./results/
```

## Documentation

| Doc | Content |
| --- | --- |
| [[docs/install]] | local CLI, `task add host`, systemd user service, `config.json` |
| [[docs/usage]] | commands, JSON output, exit codes |
| [[docs/task-package]] | `task.json`, `progress.json`, `result.json`, entry environment |
| [[docs/internals]] | remote directory layout, `job.json` / `runner.json`, state machine, scheduling, shim, stop procedure |
| [[docs/recovery]] | restart recovery, resolving `unknown` by hand |
| [[docs/assumptions]] | every assumption where the spec is silent; limitations |
| [[docs/testing]] | test command, test-only overrides, SSH smoke test |
| [[docs/development]] | uv setup, pre-commit (ruff format, basedpyright) |

Points the spec asks the README to state:

- The systemd units use `KillMode=process`, so stopping the daemon does not stop running
  tasks. This needs no D-Bus access, unlike `systemd-run --scope` per shim
  ([[docs/install]]).
- Only the task's process group is managed. A process that calls `setsid()` itself
  escapes ([[docs/assumptions]]).
- A task whose GPU is held by another process exits 75; the shim starts it again after a
  short wait and `task jobs` shows `pending (gpu busy)` ([[docs/task-package]], GPU
  contract).
- Test-only overrides are listed and marked in [[docs/testing]], which also has the
  optional smoke test against real SSH.

## Code layout

```
taskmgr/
  taskmgr.py      local CLI entry (`task`)
  hostsetup.py    `task add host`: server setup and update over ssh, step by step
  transport.py    machine list; ssh / local transport (`Remote`)
  remote.py       remote entry (`taskmgr-remote`): subcommands, `serve`, `shim`
  daemon.py       scheduler daemon: queue scan, GPU reservation, shim spawn, recovery loop
  shim.py         per-run supervisor: pre-start checks, exec, wait, stop, finalize,
                  relaunch after a GPU-busy exit (75)
  monitor.py      stop procedure, restart-recovery checks, daemon takeover tracking
  store.py        remote storage root, state lock, job/runner records, result merge
  package.py      validation, deterministic packing, content hash, safe extraction
  gpu.py          nvidia-smi query and GPU declaration resolution
  procutil.py     /proc: boot_id, start times, process-group members
  common.py       constants, atomic JSON writes, flock helpers
  template/       package template (`task init`) incl. taskmgr_task.py
  examples/       succeed/ (EX-01, CPU-only) and multi-gpu/ (MG-01, gpus [0, 1])
  deploy/         taskmgr-remote.user.service (systemd user unit), machines.example.json,
                  config.example.json
  tests/          unittest suite: python -m unittest discover -s taskmgr/tests
bin/task, bin/taskmgr-remote   wrappers that run from a checkout
docs/            documentation (English *.md, Chinese *.zh-CN.md)
```
