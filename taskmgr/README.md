# taskmgr

English | [[taskmgr/README.zh-CN|简体中文]]

Task manager for GPU performance tuning. A local CLI (`task`) stores and submits task
packages. Each remote GPU server runs `taskmgr-remote`, which keeps its own queue, schedules
jobs onto GPUs, runs them, and recovers after restarts. There is no central scheduler.
Requirements: Python 3.10+ standard library; Linux on the remote side.

Spec: [[agent/desc-1.md]]. Design decisions: [[agent/desc-2.md]].

## Quick start

Below, `gpu1` stands for the GPU server's ssh address (e.g. `tuner@10.0.0.12`) and `tuner`
for your user name on it. No step needs root.

**1. Your machine: install the CLI**

```sh
uv tool install --editable /path/to/gpu-taskmgr     # provides the `task` command
```

**2. Your machine → server: copy the code to `~/taskmgr` on the server**

Run this in the repository root on your machine. `git archive` packs only committed files
(no `.venv`, no `.git`) and ssh unpacks them into `~/taskmgr` on the server, so the server
needs no git credentials. Run it again to update the code.

```sh
git archive HEAD | ssh gpu1 'mkdir -p ~/taskmgr && tar -x -C ~/taskmgr'
```

The CLI and every server must speak the same `protocol_version`; a mismatch fails with
`protocol_mismatch` (exit 2). Protocol 2 removed attempts (a job runs once; a retry is a new
job). Job directories written by protocol 1 (`attempts/NNNN/`) are not migrated: before
upgrading a server, let its jobs finish, then restart the daemon on a new, empty
`remote_root` (or move the old one aside).

**3. Server: install and start the systemd user service**

`ssh gpu1`, then:

```sh
mkdir -p ~/taskmgr-root ~/.config/systemd/user
cp ~/taskmgr/taskmgr/deploy/config.example.json ~/taskmgr-root/config.json        # remote config
cp ~/taskmgr/taskmgr/deploy/taskmgr-remote.user.service \
   ~/.config/systemd/user/taskmgr-remote.service                                 # user unit
systemctl --user daemon-reload                     # make systemd see the new unit
systemctl --user enable --now taskmgr-remote       # start now and with the user manager
loginctl enable-linger "$USER"                     # start at boot, survive logout (may need an admin once)
systemctl --user status taskmgr-remote             # should be active (running)
```

The unit hard-codes `%h/taskmgr/bin/taskmgr-remote` and `--root %h/taskmgr-root`, which is
why step 2 copies to `~/taskmgr`. If `$HOME` is on NFS, use local disk instead; see
[[docs/install]].

**4. Your machine: register the server**

```sh
mkdir -p ~/.taskmgr
cp /path/to/gpu-taskmgr/taskmgr/deploy/machines.example.json ~/.taskmgr/machines.json
```

Edit `~/.taskmgr/machines.json`, one entry per server, with absolute paths (no `~`):

```json
{"servers": [{"name": "gpu1", "enabled": true, "transport": "ssh",
              "ssh": {"host": "10.0.0.12", "user": "tuner"},
              "remote_root": "/home/tuner/taskmgr-root",
              "remote_command": "/home/tuner/taskmgr/bin/taskmgr-remote"}]}
```

**5. Use it**

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
- A task whose GPU is held by another process exits 75; the shim starts it again after a
  short wait and `task jobs` shows `pending (gpu busy)` ([[docs/task-package]], GPU
  contract).
- Test-only overrides are listed and marked in [[docs/testing]], which also has the
  optional smoke test against real SSH.

## Code layout

```
taskmgr/
  taskmgr.py      local CLI entry (`task`)
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
