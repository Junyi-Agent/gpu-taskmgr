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

task host add gpu1 --ssh tuner@10.0.0.12   # sets up the server and adds it to machines.json
task host list
task host ping                             # checks that the server answers
```

`task host add` (below) creates `~/.taskmgr/machines.json` and its entries. You can also
write the file by hand from `taskmgr/deploy/machines.example.json`.

Local state lives in `$TASKMGR_HOME` (default `~/.taskmgr`, or `task --home DIR ...`):

| File | Content |
| --- | --- |
| `machines.json` | `{"servers": [...]}`, the machine list (Section 4 of the spec) |
| `rr.json` | round-robin cursor `{"cursor": N, "last": name}` |
| `rr.lock` | flock serializing read+update of `rr.json` across concurrent CLIs |

Task packages are ordinary local directories (or `.tar.gz` files) that you keep wherever you
like; the local side keeps no jobid index and no run state.

### Single-file zipapps (`.pyz`)

`python3 -m taskmgr.pyz` (from a checkout) builds two files that run with any `python3`
>= 3.10 and need nothing else:

```sh
python3 -m taskmgr.pyz -o dist                    # writes dist/task.pyz and dist/taskmgr-remote.pyz
ln -s "$PWD/dist/task.pyz" ~/.local/bin/task      # the local CLI as one file
task version                                      # commit and code_version come from the archive
```

Each archive holds the package (`taskmgr/` without `tests/` and `__pycache__`), plus `pyproject.toml`, `.taskmgr-commit` and `.taskmgr-code-version` at the
top. The code version is a hash of the files in `taskmgr/` that change what runs: every
`.py` file (not those in `deploy/`, `examples/` and `tests/`) and everything in
`template/`. Docs, README files and the deploy examples do not change it, so editing them
does not make servers restart. It is the same for the checkout and
for every archive built from it, so `task.pyz host add` installs the same code as a checkout
would, and `task version` shows the servers as `up_to_date`.
`taskmgr-remote.pyz` runs the daemon and the shim (the daemon puts the archive on the
shim's `PYTHONPATH`); `taskmgr-remote.pyz version` reports the archive path as `code_dir`.
`task update` refuses inside a `.pyz` (`not_a_checkout`): build a new one instead.
`task host add` never builds anything: it uploads a `taskmgr-remote.pyz` built by this
command (see below). By default it takes `dist/taskmgr-remote.pyz` in the checkout, or,
run from `task.pyz`, the `taskmgr-remote.pyz` in the same directory as `task.pyz` (symlinks
resolved, so the `ln -s` above finds `dist/`); `--pyz FILE` names another file. The archive's
code version must equal the version of the running `task`; otherwise the `code` step fails
and asks you to rebuild with `python3 -m taskmgr.pyz -o dist`.

### Remote server: `task host add` (no root required)

The daemon runs as a **systemd user service** under the account that owns the jobs; no
root access is needed. `task host add` sets up a server, or updates one, over ssh:

```sh
task host add NAME --ssh USER@HOST [--port N] [--identity-file PATH]
              [--ssh-option KEY=VALUE ...] [--disabled | --enabled] [--pyz FILE] [--yes]
task host add gpu1 --ssh tuner@10.0.0.12          # new server
task host add gpu1                                # existing entry: check and update
```

It goes through ten steps. Each step prints what it checks and what it found. A step that
needs to change something describes the change and asks `proceed? [y/N]`; anything but
`y` stops the run there (exit 2), and the steps after it are not run. Read-only checks
never ask. `--yes` answers every question with yes. Without `--yes` and without a terminal
on stdin, the run stops at the first change it would make (exit 2) and nothing is changed.

The command is idempotent: on an up-to-date server every step reports `ok`, nothing is
changed and nothing is asked. To update a server after `git pull` (or after editing the
code), rebuild with `python3 -m taskmgr.pyz -o dist` and run `task host add NAME` again:
only the code step and a service restart run.

| Step | Checks | Changes (each asked) |
| --- | --- | --- |
| `ssh` | ssh works (BatchMode), Linux, `python3` >= 3.10, `systemctl --user` works, linger, installed code version, unit file, service state, filesystem of `remote_root` (warns on NFS, CIFS, FUSE, ...) | none; a failed check stops the run |
| `code` | the prebuilt `taskmgr-remote.pyz` (`--pyz`, default see above) exists and has the code version of the running `task` (otherwise the step fails: rebuild with `python3 -m taskmgr.pyz -o dist`); the code version inside `<remote_root>/taskmgr-remote.pyz` against it | upload that file to `<remote_root>/versions/<version>.pyz.new-*`; after the `data` step it is renamed to `<remote_root>/versions/<version>.pyz` and the symlink `<remote_root>/taskmgr-remote.pyz` is switched to it (one rename). The archive the link pointed to before is kept. Upload files `*.new-*` older than 10 minutes (left by an interrupted run) are removed |
| `remote_root` | `remote_root` and `config.json` exist | `mkdir -p`, write `config.json` with the defaults when it is missing; an existing `config.json` is never overwritten |
| `data` | `taskmgr-remote inspect` (read-only): job directories that the new code cannot handle (protocol-1 `attempts/`, `job.json` missing current fields) | stop the service, then move those directories (and their `submission-keys/*.json`) into `<remote_root>/backups/<UTC time>/`; nothing is deleted. Jobs that may still run (running, stopping, unknown, or in `in-progress/`) are never moved: the step fails and lists them |
| `unit` | `~/.config/systemd/user/taskmgr-remote.service` against the generated unit (shows a diff) | write it, `systemctl --user daemon-reload` |
| `service` | enabled, active, and whether the running daemon is older than the installed code or unit | `enable`; `start`; `restart` (running tasks keep running, `KillMode=process`); then waits until the daemon holds `locks/daemon.lock`. After a code change, once the daemon runs the new code, removes the archives in `versions/` other than the current one, the previous one and any a running process uses (a running job's shim imports from the archive it started with) |
| `linger` | `loginctl show-user` | `loginctl enable-linger <user>`; if refused, the step fails with the command an admin has to run once, and the run continues |
| `verify` | `taskmgr-remote --protocol-version N version` answers; the service's main PID holds `locks/daemon.lock` | none |
| `machines` | the entry in `~/.taskmgr/machines.json` | add or update the entry (shows the change); other entries are left as they are |
| `old_code` | code that older versions installed outside `remote_root`: the tree `~/taskmgr` (only when it holds the marker file `.taskmgr-code-version` and has no `.git`) and the `~/taskmgr.old-*` directories without `.git`; the first zipapp layout `~/taskmgr-remote.pyz` (a symlink) and `~/.taskmgr-remote/<version>.pyz` | delete them. Machine lists on other computers that still name them stop working; run `task host add NAME` there. If declined, the run reports it and the next run asks again |

Progress and prompts go to stderr. Stdout gets a table of the steps (STEP, STATUS, DETAIL)
and a `done:` or `incomplete:` line; with `--detail` it gets the JSON object
`{"host", "steps": [{"name", "status", "detail"}], "complete"}` instead. `status` is one of
`ok`, `changed`, `skipped`, `declined`, `failed`. Exit code 0 when every step is `ok` or
`changed`, otherwise 2.

The new machine entry gets absolute paths (`remote_root`, `remote_command =
<remote_root>/taskmgr-remote.pyz`) and these ssh options, merged with `--ssh-option`
(same key replaces): `ServerAliveInterval=15`, `ConnectTimeout=10`, `ControlMaster=auto`,
`ControlPath=~/.ssh/cm-%r@%h:%p`, `ControlPersist=10m`. For an existing entry the stored
settings are the defaults for omitted flags; its ssh options are kept, and `--ssh-option`
adds to them. `--disabled` / `--enabled` set `enabled`; otherwise a new entry is enabled
and an existing one keeps its value.

Everything `taskmgr-remote` keeps on the server is in one directory, `remote_root`
(default `~/.taskmgr-remote`); only the systemd unit is outside it:

```
~/.taskmgr-remote/
  config.json
  taskmgr-remote.pyz -> versions/<version>.pyz    # the code the service runs
  versions/<version>.pyz                          # current, previous, and any in use
  locks/ incoming/ queue/ in-progress/ done/ submission-keys/ daemon/
  backups/<UTC time>/                             # job directories moved by step data
```

To use another directory, set `TASKMGR_REMOTE_ROOT` when running `task host add` (for
example `TASKMGR_REMOTE_ROOT=/scratch/tuner/taskmgr-remote task host add gpu1`); `~` and
relative paths are resolved on the server. Without it, an existing entry keeps its
`remote_root`. `remote_root` must be on a local filesystem (not NFS): all coordination
uses `flock`. Data in another `remote_root` is never moved. When step `ssh` finds a
`config.json` in a directory this run would stop using (the existing entry's
`remote_root`, the `--root` of the installed unit, or for a new entry `~/taskmgr-root`,
the default of older versions), the service would no longer see those jobs: without
`--yes` it warns and asks; with `--yes` the run fails and changes nothing. To keep using
that directory, run again with `TASKMGR_REMOTE_ROOT` set to it (e.g.
`TASKMGR_REMOTE_ROOT=~/taskmgr-root`).

Notes:

- The generated unit runs the daemon with the `python3` that the ssh session finds,
  given as an absolute path, so the version checked in step `ssh` is the one that runs.
- Data in `remote_root` is never deleted. Incompatible job directories are moved after you
  confirm; live ones stay where they are until they end.
- Two `host add` runs for the same server user do not overlap: from the end of step `ssh`
  to the end of the run a process on the server holds a lock file
  (`$XDG_RUNTIME_DIR/taskmgr-hostsetup-<uid>.lock`, else in `/tmp`), and a second run
  fails at step `ssh` while it is held.
- The code on the server is `<remote_root>/taskmgr-remote.pyz`, a symlink to
  `versions/<version>.pyz`; the code version is read from `.taskmgr-code-version`
  inside the archive. An installed archive is never modified. Python's zipimport reads an
  archive by path, once per module, with the offsets of its first read, so replacing the
  file under a starting process (a shim the daemon just launched) breaks its imports. An
  update therefore adds a new file and switches the link, and each process imports from
  the resolved path (`sys.path[0]` is replaced by its realpath; the daemon gives shims the
  resolved path on `PYTHONPATH`). Rollback by hand: `ln -sfn versions/<old>.pyz
  ~/.taskmgr-remote/taskmgr-remote.pyz`, then `systemctl --user restart taskmgr-remote`.

### Machine list: `task host list`, `enable`, `disable`, `remove`, `ping`

```sh
task host list                # NAME, ENABLED, DESTINATION (user@host[:port]), REMOTE_ROOT
task host ping                # every server; or `task host ping gpu1` for one
task host disable gpu1        # gpu1 is down: leave it out of submits and queries
task host enable gpu1         # back
task host remove gpu1         # asks [y/N]; --yes skips the question
```

`task host disable NAME` / `enable NAME` set `enabled` in the entry (rewritten atomically
under `machines.lock`, like `remove`). A disabled server is skipped by round-robin submits
and by the queries over every server (`jobs`, the jobid lookup, `whoami`, `version`);
`--host NAME` still reaches it. See [[docs/usage]].

`task host ping` runs `taskmgr-remote version` on each enabled server in parallel and prints one row
per server: `ok` or `unreachable`, the round-trip time, `protocol_version`, the installed
code version (`none` when the server runs a checkout that `task host add` did not install),
and the user and hostname the remote side runs as. The error of each unreachable server
follows the table. Exit code 0 when every pinged server answered, otherwise 2. Disabled
servers are skipped and named after the table; `task host ping NAME` pings one even when it
is disabled.

`task host remove NAME` removes the entry from `machines.json` (rewritten atomically under
`machines.lock`; other entries are kept). It changes nothing on the server: code, service,
`remote_root` and its jobs stay, and `task host add NAME --ssh ...` adds the server again.
Without a terminal it needs `--yes`; an unknown name fails with `unknown_host` (exit 2).
To stop the server's service as well, see the manual steps below.

### What `host add` does, and the manual fallback

The same setup by hand, for user `tuner`. It installs the shipped unit
(`taskmgr/deploy/taskmgr-remote.user.service`, which uses `%h` paths) instead of the
generated one, so a later `task host add` replaces the unit file (it shows the diff).

```sh
python3 -m taskmgr.pyz -o dist                                    # on your machine
ssh gpu1 mkdir -p .taskmgr-remote/versions
scp dist/taskmgr-remote.pyz gpu1:.taskmgr-remote/versions/<version>.pyz
scp taskmgr/deploy/config.example.json gpu1:.taskmgr-remote/config.json
scp taskmgr/deploy/taskmgr-remote.user.service gpu1:
ln -s versions/<version>.pyz ~/.taskmgr-remote/taskmgr-remote.pyz   # on the server
```

`<version>` is any name; `task host add` uses the first 16 characters of the code version.
The shipped unit starts `/usr/bin/python3`: replace it in `ExecStart` with the output of
`command -v python3` in an ssh session (Python >= 3.10), as the generated unit does.
In the machine list use absolute paths (no `~`):
`"remote_root": "/home/tuner/.taskmgr-remote"`,
`"remote_command": "/home/tuner/.taskmgr-remote/taskmgr-remote.pyz"`. If `remote_root` is
not `~/.taskmgr-remote`, change both paths in the unit's `ExecStart` accordingly.

#### Start / stop / enable at boot (systemd user service)

```sh
mkdir -p ~/.config/systemd/user
mv ~/taskmgr-remote.user.service ~/.config/systemd/user/taskmgr-remote.service
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
also runs outside systemd (tmux, nohup): `nohup ~/.taskmgr-remote/taskmgr-remote.pyz
--root ~/.taskmgr-remote serve > /dev/null 2>&1 &`; stop with `kill <pid>` (pid in
`locks/daemon.lock`).

A second `serve` on the same root exits with code 2 (it cannot take `locks/daemon.lock`).

## Remote `config.json`

`<remote_root>/config.json`; every key is optional, unknown keys are ignored. Values are
type-checked: a value of the wrong type (e.g. `"30"` for `stop_grace_seconds`, or `true`
for a number) makes the daemon and every `taskmgr-remote` command that reads the config
fail with `bad_config`.

| Key | Default | Meaning |
| --- | --- | --- |
| `max_package_bytes` | 1073741824 | max total extracted size of a received package; an upload larger than this plus 1/16 plus 1 MiB is refused before it is fully received |
| `max_cpu_jobs` | `null` (unlimited) | max concurrent jobs with `gpus: []` and precompiles (running/stopping/unknown) |
| `max_auto_restarts` | 2 | automatic re-runs after reboot/never-started interruptions |
| `stop_grace_seconds` | 30 | SIGTERM → SIGKILL delay in the stop procedure (timeout and leftover stops; a cancel sends SIGKILL at once) |
| `kill_confirm_seconds` | 10 | wait after SIGKILL before declaring `unknown` |
| `control_poll_seconds` | 2 | how often shim / daemon re-read `job.json` for cancel; health-check period |
| `scheduler_poll_seconds` | 1 | scheduling pass interval (assumption: not in the spec) |
| `gpu_busy_exit_code` | 75 | entry exit code meaning "GPU busy, no work done": the shim starts the entry again instead of finishing the job; `null` turns this off (75 is then an ordinary failure) |
| `gpu_retry_interval_seconds` | 1 | wait between a GPU-busy exit and the relaunch |
| `base_env` | `{}` | overrides/extends the entry process base environment |
