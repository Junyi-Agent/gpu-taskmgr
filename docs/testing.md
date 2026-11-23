---
title: Testing
tags: [taskmgr, testing]
lang: en
up: "[[taskmgr/README]]"
---

# Testing

Other language: [[docs/testing.zh-CN]]

```sh
python -m unittest discover -s taskmgr/tests      # Linux, no GPU, no network
# from macOS:
docker run --rm --init -v "$PWD":/src:ro python:3.10-slim \
    sh -c 'cp -a /src /w && cd /w && python -m unittest discover -s taskmgr/tests'
# one module: ... python -m unittest taskmgr.tests.test_query
```

The tests need Linux (`/proc`, process groups, `setsid`). On any other platform every test
class built on `helpers.Env` is skipped with a message pointing here; only the few pure unit
tests run natively. Use the Docker command on macOS.

The checkout is mounted read-only and copied into the container (`cp -a /src /w`): the
tests write nothing into your working tree (no `__pycache__`, no `dist/`), and two runs
from different worktrees do not share files. `python:3.10-slim` matches the oldest Python
the project supports (`.python-version`).

Run with `--init` (or any init that reaps orphans) in containers: tests kill shims and the
orphaned task processes must be reaped.

Cleanup: each test registers its teardown with `addCleanup` (so it runs even when `setUp`
fails), and an `atexit` hook plus a SIGTERM / SIGHUP handler clean up whatever is left if
the run is interrupted. Cleanup stops the test's daemons, kills every process running in
or naming the test's temp directory, gives read-only package copies `u+w` back, and removes
the directory. A `SIGKILL`ed runner cannot clean up; in Docker the container goes away with
it.

The test environment drops every inherited `TASKMGR_*` variable and `NO_COLOR`, then sets
what the tests need, so settings from your shell cannot change the results.

To assert that a job is "still queued" after the scheduler had its chance, use
`Env.wait_scheduler_passes(n)` instead of a fixed sleep: it submits no-op probe jobs and
waits until each has succeeded; the pass that dispatches a probe has considered every job
queued before it.

To read a job while it is in a given state, let its script wait for a gate file
(`while [ ! -f GATE ]; do sleep 0.05; done`) and create the file after the assertions; a
fixed sleep in the script makes the test fail on a slow machine. To change something once a
command has reached a given point (for example, bring a server back after the first failed
query), use `Env.task_when(marker, action, *args)`: it runs `task` and calls `action` when
`marker` appears on its stderr.

Fault injection must also hold for root (the Docker command above runs as root): root
ignores permission bits, so `chmod 0500` does not stop writes. To make writes into a
directory fail, put a regular file where the directory was (writes fail with ENOTDIR or
EEXIST).

**Test-only overrides** (do not use in production):

| Variable / setting | Effect |
| --- | --- |
| `TASKMGR_NVIDIA_SMI` | path of a fake `nvidia-smi` printing `index, uuid` lines |
| `TASKMGR_BOOT_ID_FILE` | read boot_id from this file (simulated reboot) |
| machine entry `"local": {"fail": "msg"}` | `transport: local` raises a connection error |
| machine entry `"local": {"fail_if_exists": "/path"}` | connection error while the file exists |
| `TASKMGR_TEST_PROTOCOL_VERSION` | remote reports this protocol version |
| `TASKMGR_TEST_FEATURES` | remote reports these features (comma-separated; `-` for none) |
| `TASKMGR_TEST_SKIP_DAEMON_LOCK=1` | daemon skips `daemon.lock` (multi-daemon test) |
| `TASKMGR_SHIM_EXECUTABLE` | interpreter used to exec the shim (shim start failure) |
| `TASKMGR_TEST_SHIM_CRASH=<point>` | shim exits at `before_identity`, `before_exec`, `after_exit_recorded`, `before_finalize` (`test_recovery.py` checks the recovery after each) |
| `TASKMGR_TEST_IGNORE_KILL=1` | stop procedure sends no signals (unconfirmed stop) |
| `TASKMGR_TEST_RECEIVE_PAUSE=<s>` | `receive` sleeps while holding the upload lock |
| machine entry `"local": {"home": "/dir"}` | `transport: local` runs every command (and `task create host` scripts) with `HOME=/dir` |
| `TASKMGR_TEST_LOCAL_HOME=/dir` | `task create host` creates a new entry with `transport: local` and `"local": {"home": "/dir"}` |
| `TASKMGR_TEST_INTERACTIVE=1` | `task create host` reads answers from stdin even when it is not a terminal |

The systemd behaviour is tested by checking `KillMode=process` in the user unit and by stopping
the daemon with SIGTERM to its whole process group: shim and task keep running and the
real exit code is recorded.

`task create host` is tested without ssh and without systemd (`taskmgr/tests/test_add_host.py`).
The "server" is a local-transport entry whose HOME is a test directory (the test-only
`"local": {"home"}` above). Fake `systemctl` and `loginctl` scripts, written by the test into a
directory that is put first on PATH, keep their state under `$HOME/.fake-systemd/` and log
every call there: `start` launches the unit's `ExecStart` (the real daemon), `stop` sends
SIGTERM to the main PID only (like `KillMode=process`), `daemon-reload` is required after
the unit file changes, and `enable-linger` fails while `linger-refuse` exists. The fakes
exist only in the tests; nothing in `taskmgr/` refers to them. The same file tests
`task get hosts`, `task delete host` and `task ping host` against plain local-transport
servers; an unreachable server is a machine entry with `"local": {"fail": "message"}`.

These tests install `taskmgr-remote.pyz` as on a real server (`$HOME/.taskmgr-remote/taskmgr-remote.pyz`, a symlink into
`versions/`); the fake `start` runs it. An older installed version is
simulated by installing a copy of the archive whose `.taskmgr-code-version` differs and
switching the link, as `create host` does. The class builds `taskmgr-remote.pyz` once and
every run passes it with `--pyz`; a missing archive, an archive of another code version and
`task.pyz` given as `--pyz` make the `code` step fail before anything changes. Other tests
cover `TASKMGR_REMOTE_ROOT`, the cleanup of the first zipapp layout (`~/taskmgr-remote.pyz`,
`~/.taskmgr-remote/<version>.pyz`) and the warning about data under `~/taskmgr-root`. The code-update test keeps a task running across
two updates; it catches a shim that imports from an archive replaced during its start.

`taskmgr/tests/test_pyz.py` builds `task.pyz` and `taskmgr-remote.pyz` with
`task build pyz` and runs them without `PYTHONPATH` and outside the checkout:
archive contents, exit codes, the code version (same as the checkout), `task create package` (same
package as the checkout), running through a symlink, where `task create host` looks for
`taskmgr-remote.pyz` when run from `task.pyz` (next to it, symlinks resolved), and a job submitted with `task.pyz`
that the daemon and shim of `taskmgr-remote.pyz` run to `succeeded`.

## Optional smoke test against real SSH

```sh
task create host gpu1 --ssh tuner@<server>                    # every step ok or changed
task create host gpu1                                         # second run: every step ok, no prompt
task ping host gpu1                                           # ok, code version of the local tree
ssh gpu1 ls -l .taskmgr-remote/                               # taskmgr-remote.pyz -> versions/<version>.pyz
task create job taskmgr/examples/succeed --host gpu1
task wait job <jobid> --host gpu1 --poll-interval 2           # exit 0, outputs present
task create job taskmgr/examples/multi-gpu --host gpu1        # needs >= 2 GPUs
task logs job <jobid> --host gpu1 --follow
task fetch job <jobid> --host gpu1 --dest ./results/
```
