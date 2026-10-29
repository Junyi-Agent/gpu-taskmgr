---
title: Installation and deployment
tags: [taskmgr, install, deploy, systemd]
lang: en
up: "[[taskmgr/README]]"
---

# Installation and deployment

Other language: [[docs/install.zh-CN]]

## Installation

### Local CLI

```sh
uv tool install --editable /path/to/gpu-taskmgr   # installs `task` and `taskmgr-remote`
# or, without pip:
ln -s /path/to/gpu-taskmgr/bin/task ~/.local/bin/task

task add host gpu1 --ssh tuner@10.0.0.12   # sets up the server and adds it to machines.json
task hosts
```

`task add host` (below) creates `~/.taskmgr/machines.json` and its entries. You can also
write the file by hand from `taskmgr/deploy/machines.example.json`.

Local state lives in `$TASKMGR_HOME` (default `~/.taskmgr`, or `task --home DIR ...`):

| File | Content |
| --- | --- |
| `machines.json` | `{"servers": [...]}`, the machine list (Section 4 of the spec) |
| `rr.json` | round-robin cursor `{"cursor": N, "last": name}` |
| `rr.lock` | flock serializing read+update of `rr.json` across concurrent CLIs |

Task packages are ordinary local directories (or `.tar.gz` files) that you keep wherever you
like; the local side keeps no jobid index and no run state.

### Remote server: `task add host` (no root required)

The daemon runs as a **systemd user service** under the account that owns the jobs; no
root access is needed. `task add host` sets up a server, or updates one, over ssh:

```sh
task add host NAME --ssh USER@HOST [--port N] [--identity-file PATH]
              [--remote-root DIR] [--ssh-option KEY=VALUE ...] [--disabled | --enabled] [--yes]
task add host gpu1 --ssh tuner@10.0.0.12          # new server
task add host gpu1                                # existing entry: check and update
```

It goes through nine steps. Each step prints what it checks and what it found. A step that
needs to change something describes the change and asks `proceed? [y/N]`; anything but
`y` stops the run there (exit 2), and the steps after it are not run. Read-only checks
never ask. `--yes` answers every question with yes. Without `--yes` and without a terminal
on stdin, the run stops at the first change it would make (exit 2) and nothing is changed.

The command is idempotent: on an up-to-date server every step reports `ok`, nothing is
changed and nothing is asked. To update a server after `git pull` (or after editing the
code), run `task add host NAME` again: only the code step and a service restart run.

| Step | Checks | Changes (each asked) |
| --- | --- | --- |
| `ssh` | ssh works (BatchMode), Linux, `python3` >= 3.10, `systemctl --user` works, linger, installed code version, unit file, service state, filesystem of `remote_root` (warns on NFS, CIFS, FUSE, ...) | none; a failed check stops the run |
| `code` | `~/taskmgr/.taskmgr-code-version` against the local package | upload `taskmgr/` (without `tests/` and `__pycache__`) and `bin/taskmgr-remote` to `~/taskmgr.new-*`; after the `data` step it replaces `~/taskmgr`, the old tree is kept as `~/taskmgr.old-<time>` (older `~/taskmgr.old-*` are removed) |
| `remote_root` | `remote_root` and `config.json` exist | `mkdir -p`, write `config.json` with the defaults when it is missing; an existing `config.json` is never overwritten |
| `data` | `taskmgr-remote inspect` (read-only): job directories that the new code cannot handle (protocol-1 `attempts/`, `job.json` missing current fields) | stop the service, then move those directories (and their `submission-keys/*.json`) into `<remote_root>.backup-<UTC time>/`; nothing is deleted. Jobs that may still run (running, stopping, unknown, or in `in-progress/`) are never moved: the step fails and lists them |
| `unit` | `~/.config/systemd/user/taskmgr-remote.service` against the generated unit (shows a diff) | write it, `systemctl --user daemon-reload` |
| `service` | enabled, active, and whether the running daemon is older than the installed code or unit | `enable`; `start`; `restart` (running tasks keep running, `KillMode=process`); then waits until the daemon holds `locks/daemon.lock` |
| `linger` | `loginctl show-user` | `loginctl enable-linger <user>`; if refused, the step fails with the command an admin has to run once, and the run continues |
| `verify` | `taskmgr-remote --protocol-version N version` answers; the service's main PID holds `locks/daemon.lock` | none |
| `machines` | the entry in `~/.taskmgr/machines.json` | add or update the entry (shows the change); other entries are left as they are |

Stdout gets one JSON object: `{"host", "steps": [{"name", "status", "detail"}],
"complete"}`, with `status` one of `ok`, `changed`, `skipped`, `declined`, `failed`. Exit
code 0 when every step is `ok` or `changed`, otherwise 2.

The new machine entry gets absolute paths (`remote_root`, `remote_command =
<home>/taskmgr/bin/taskmgr-remote`) and these ssh options, merged with `--ssh-option`
(same key replaces): `ServerAliveInterval=15`, `ConnectTimeout=10`, `ControlMaster=auto`,
`ControlPath=~/.ssh/cm-%r@%h:%p`, `ControlPersist=10m`. For an existing entry the stored
settings are the defaults for omitted flags; its ssh options are kept, and `--ssh-option`
adds to them. `--disabled` / `--enabled` set `enabled`; otherwise a new entry is enabled
and an existing one keeps its value.

`remote_root` must be on a local filesystem (not NFS): all coordination uses `flock`. The
default is `~/taskmgr-root`; if `$HOME` is on NFS, pass `--remote-root
/scratch/tuner/taskmgr-root` (a path on local disk). `~` and relative paths are resolved
on the server.

Notes:

- The generated unit runs the daemon with the `python3` that the ssh session finds,
  given as an absolute path, so the version checked in step `ssh` is the one that runs.
- Data in `remote_root` is never deleted. Incompatible job directories are moved after you
  confirm; live ones stay where they are until they end.
- Two `add host` runs for the same server at the same time are not supported.
- The code version is the content hash of the shipped files. Files edited by hand in
  `~/taskmgr` are not noticed while the version file matches.

### What `add host` does, and the manual fallback

The same setup by hand, for user `tuner`. It installs the shipped unit
(`taskmgr/deploy/taskmgr-remote.user.service`, which uses `%h` paths) instead of the
generated one, so a later `task add host` replaces the unit file (it shows the diff).

```sh
git archive HEAD | ssh gpu1 'mkdir -p ~/taskmgr && tar -x -C ~/taskmgr'   # on your machine
mkdir -p ~/taskmgr-root                                                   # on the server
cp ~/taskmgr/taskmgr/deploy/config.example.json ~/taskmgr-root/config.json
```

In the machine list use absolute paths (no `~`):
`"remote_root": "/home/tuner/taskmgr-root"`,
`"remote_command": "/home/tuner/taskmgr/bin/taskmgr-remote"`. If `remote_root` is not
`~/taskmgr-root`, change `--root` in the unit accordingly.

#### Start / stop / enable at boot (systemd user service)

```sh
mkdir -p ~/.config/systemd/user
cp ~/taskmgr/taskmgr/deploy/taskmgr-remote.user.service \
   ~/.config/systemd/user/taskmgr-remote.service
systemctl --user daemon-reload
systemctl --user enable --now taskmgr-remote     # start now and with the user manager
loginctl enable-linger "$USER"                   # start at boot, survive logout
loginctl show-user "$USER" -p Linger             # must print Linger=yes
systemctl --user stop taskmgr-remote             # running tasks keep running
systemctl --user restart taskmgr-remote          # recovery runs on start
journalctl --user -u taskmgr-remote              # daemon log (also <root>/daemon/daemon.log)
```

**Lingering is required.** Without it the user manager starts only when you log in and
stops at your last logout. The daemon then would not start at boot, and stopping the user
manager stops this unit's whole cgroup, including shims and tasks. Whether a user may
enable lingering for themselves depends on the host's polkit policy; if
`loginctl enable-linger` is refused, an admin has to run `loginctl enable-linger tuner`
once. That is the only step that may need root. Also check that logind does not kill user
processes at logout (`KillUserProcesses=no`, the default on most distributions).

The unit uses **`KillMode=process`**. Shims are double-forked into their own sessions, but
they remain in the unit's cgroup, so the default `KillMode=control-group` would kill every
shim and task on `systemctl --user stop`. With `KillMode=process` systemd only signals the
daemon's main PID; the daemon exits on SIGTERM without touching shims, and on the next
start runs restart recovery. We chose this over `systemd-run --user --scope` per shim
because it needs no D-Bus access from the daemon and keeps the daemon a plain process that
also runs outside systemd (tmux, nohup): `nohup ~/taskmgr/bin/taskmgr-remote --root
~/taskmgr-root serve > /dev/null 2>&1 &`; stop with `kill <pid>` (pid in
`locks/daemon.lock`).

A second `serve` on the same root exits with code 2 (it cannot take `locks/daemon.lock`).

## Remote `config.json`

`<remote_root>/config.json`; every key is optional.

| Key | Default | Meaning |
| --- | --- | --- |
| `max_package_bytes` | 1073741824 | max total extracted size of a received package |
| `max_cpu_jobs` | `null` (unlimited) | max concurrent jobs with `gpus: []` (running/stopping/unknown) |
| `max_auto_restarts` | 2 | automatic re-runs after reboot/never-started interruptions |
| `stop_grace_seconds` | 30 | SIGTERM → SIGKILL delay in the stop procedure |
| `kill_confirm_seconds` | 10 | wait after SIGKILL before declaring `unknown` |
| `control_poll_seconds` | 2 | how often shim / daemon re-read `job.json` for cancel; health-check period |
| `scheduler_poll_seconds` | 1 | scheduling pass interval (assumption: not in the spec) |
| `gpu_busy_exit_code` | 75 | entry exit code meaning "GPU busy, no work done": the shim starts the entry again instead of finishing the job; `null` turns this off (75 is then an ordinary failure) |
| `gpu_retry_interval_seconds` | 1 | wait between a GPU-busy exit and the relaunch |
| `base_env` | `{}` | overrides/extends the entry process base environment |
