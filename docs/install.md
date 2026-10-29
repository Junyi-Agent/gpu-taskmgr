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

mkdir -p ~/.taskmgr
cp taskmgr/deploy/machines.example.json ~/.taskmgr/machines.json   # then edit
task hosts
```

Local state lives in `$TASKMGR_HOME` (default `~/.taskmgr`, or `task --home DIR ...`):

| File | Content |
| --- | --- |
| `machines.json` | `{"servers": [...]}`, the machine list (Section 4 of the spec) |
| `rr.json` | round-robin cursor `{"cursor": N, "last": name}` |
| `rr.lock` | flock serializing read+update of `rr.json` across concurrent CLIs |

Task packages are ordinary local directories (or `.tar.gz` files) that you keep wherever you
like; the local side keeps no jobid index and no run state.

### Remote server (no root required)

The daemon runs as a **systemd user service** under the account that owns the jobs; no
root access is needed. Example for user `tuner`:

```sh
git clone <repo> ~/taskmgr                 # provides ~/taskmgr/bin/taskmgr-remote
# or, without git credentials on the server, from your machine:
#   git archive HEAD | ssh gpu1 'mkdir -p ~/taskmgr && tar -x -C ~/taskmgr'
mkdir -p ~/taskmgr-root
cp ~/taskmgr/taskmgr/deploy/config.example.json ~/taskmgr-root/config.json
```

`remote_root` must be on a local filesystem (not NFS): all coordination uses `flock`. If
`$HOME` is on NFS, use local disk (e.g. `/scratch/tuner/taskmgr-root`) and change `--root`
in the unit accordingly. In the machine list use absolute paths (no `~`):
`"remote_root": "/home/tuner/taskmgr-root"`,
`"remote_command": "/home/tuner/taskmgr/bin/taskmgr-remote"`.

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
| `base_env` | `{}` | overrides/extends the entry process base environment |
