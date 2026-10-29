# taskmgr

English | [[taskmgr/README.zh-CN|简体中文]]

Task manager for GPU performance tuning. A local CLI (`task`) stores and submits task
packages. Each remote GPU server runs `taskmgr-remote`, which keeps its own queue, schedules
jobs onto GPUs, runs them, and recovers after restarts. There is no central scheduler.
Requirements: Python 3.10+ standard library; Linux on the remote side.

Spec: [[agent/desc-1.md]]. Design decisions: [[agent/desc-2.md]].

## Quick start

```sh
# local
uv tool install --editable /path/to/gpu-taskmgr
mkdir -p ~/.taskmgr && cp taskmgr/deploy/machines.example.json ~/.taskmgr/machines.json

# remote (no root): systemd user service, see docs/install.md
systemctl --user enable --now taskmgr-remote && loginctl enable-linger "$USER"

# use
task init AA-01                      # fill every <REQUIRED...> marker
task validate AA-01/
task submit AA-01/                   # prints {"host", "jobid", ...}
task status <jobid> --wait
task fetch <jobid> --host <host> --output ./results/
```

## Documentation

| Doc | Content |
| --- | --- |
| [[docs/install]] | local CLI, remote server, systemd user service, `config.json` |
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
- Test-only overrides are listed and marked in [[docs/testing]], which also has the
  optional smoke test against real SSH.

## Code layout

```
taskmgr/
  taskmgr.py      local CLI entry (`task`)
  remote.py       remote entry (`taskmgr-remote`): subcommands, `serve`, `shim`
  daemon.py       scheduler daemon: queue scan, GPU reservation, shim spawn, recovery loop
  shim.py         per-attempt supervisor: pre-start checks, exec, wait, stop, finalize
  monitor.py      stop procedure, restart-recovery checks, daemon takeover tracking
  store.py        remote storage root, state lock, job/runner records, result merge
  package.py      validation, deterministic packing, content hash, safe extraction
  gpu.py          nvidia-smi query and GPU declaration resolution
  procutil.py     /proc: boot_id, start times, process-group members
  common.py       constants, atomic JSON writes, flock helpers
  template/       package template (`task init`) incl. taskmgr_task.py
  examples/       succeed/ (EX-01, CPU-only) and multi-gpu/ (MG-01, gpus [0, 1])
  deploy/         taskmgr-remote.user.service (default), taskmgr-remote.service (root),
                  machines.example.json, config.example.json
  tests/          unittest suite: python -m unittest discover -s taskmgr/tests
bin/task, bin/taskmgr-remote   wrappers that run from a checkout
docs/            documentation (English *.md, Chinese *.zh-CN.md)
```
