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

The CLI is one file, `task.pyz`, and needs only `python3` >= 3.10 ([[docs/install]],
single-file zipapps).

```sh
# build (skip if dist/task.pyz and dist/taskmgr-remote.pyz exist)
python3 -m taskmgr.pyz -o dist

# install (a symlink, so that task host add finds taskmgr-remote.pyz in dist/)
ln -s "$PWD/dist/task.pyz" ~/.local/bin/task
task --help
```

**2. Your machine: set up the server**

```sh
task host add gpu1 --ssh tuner@10.0.0.12
# main steps, in order (all ten: docs/install.md); every change asks [y/N] first (--yes answers yes):
#  1. check ssh, Linux, python3 >= 3.10, systemctl --user
#  2. upload dist/taskmgr-remote.pyz to ~/.taskmgr-remote/versions/<version>.pyz and
#     point ~/.taskmgr-remote/taskmgr-remote.pyz at it (nothing is built locally; fails
#     when its code version differs from task's)
#  3. create ~/.taskmgr-remote/config.json
#  4. write ~/.config/systemd/user/taskmgr-remote.service, enable and start the service
#  5. loginctl enable-linger
#  6. add gpu1 to the local ~/.taskmgr/machines.json
```

To update the server (after `git pull` or a code change): run `python3 -m taskmgr.pyz -o dist`
again, then `task host add gpu1`. `task host add` is idempotent: on an up-to-date server it
changes nothing and asks nothing. Every step, the data check and the manual fallback:
[[docs/install]] ("Remote server: `task host add`").

**3. Use it**

```sh
task host list                       # check the machine list
task host ping                       # check that every server answers
task init AA-01                      # template; fill every <REQUIRED...> marker
task validate AA-01/
task submit AA-01/                   # prints JOBID TASK_ID HOST STATE
task status <jobid> --wait
task fetch <jobid> --output ./results/
```
