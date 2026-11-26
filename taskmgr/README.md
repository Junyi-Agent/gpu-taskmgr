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
bin/task build pyz                   # in the checkout: writes dist/

# install (a symlink, so that task create host finds taskmgr-remote.pyz in dist/)
ln -s "$PWD/dist/task.pyz" ~/.local/bin/task
task --help
```

**2. Your machine: set up the server**

```sh
task create host gpu1 --ssh tuner@10.0.0.12
# main steps, in order (all ten: docs/install.md); every change asks [y/N] first (--yes answers
# yes, but never deletes: old code outside remote_root needs --prune-old-code; --plan
# prints what would change and changes nothing):
#  1. check ssh, Linux, python3 >= 3.10, systemctl --user
#  2. upload dist/taskmgr-remote.pyz to ~/.taskmgr-remote/versions/<version>.pyz and
#     point ~/.taskmgr-remote/taskmgr-remote.pyz at it (nothing is built locally; fails
#     when its code version differs from task's)
#  3. create ~/.taskmgr-remote/config.json
#  4. write ~/.config/systemd/user/taskmgr-remote.service, enable and start the service
#  5. loginctl enable-linger
#  6. add gpu1 to the local ~/.taskmgr/machines.json
```

To update the server (after `git pull` or a code change): run `task build pyz`
again, then `task update host gpu1`. `task create host` and `task update host` are idempotent:
on an up-to-date server they change nothing and ask nothing. Every step, the data check and
the manual fallback: [[docs/install]] (section "Remote server").

**3. Use it**

```sh
task get hosts                       # check the machine list
task ping host                       # check that every server answers
task create package AA-01            # template; fill every <REQUIRED...> marker
task validate package AA-01/
task create job AA-01/               # prints JOBID TASK_ID HOST STATE
task wait job <jobid>
task fetch job <jobid> --dest ./results/
# or build the package from project files instead of a template (docs/usage.md):
task create job "$(task build package src/ --task-id AA-02 --entry 'python3 run.py' --gpus 0)"
```

Commands are `task <verb> <resource>`, as in kubectl; `task -h` lists them, job, host and
package also have a plural name (`task get jobs`), and `-o json` prints the full JSON object (keys and error codes: `docs/json-output.md`).
