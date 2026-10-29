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
docker run --rm --init -v "$PWD":/src -w /src python:3.11-slim \
    python -m unittest discover -s taskmgr/tests
```

Run with `--init` (or any init that reaps orphans) in containers: tests kill shims and the
orphaned task processes must be reaped.

**Test-only overrides** (do not use in production):

| Variable / setting | Effect |
| --- | --- |
| `TASKMGR_NVIDIA_SMI` | path of a fake `nvidia-smi` printing `index, uuid` lines |
| `TASKMGR_BOOT_ID_FILE` | read boot_id from this file (simulated reboot) |
| machine entry `"local": {"fail": "msg"}` | `transport: local` raises a connection error |
| machine entry `"local": {"fail_if_exists": "/path"}` | connection error while the file exists |
| `TASKMGR_TEST_PROTOCOL_VERSION` | remote reports this protocol version |
| `TASKMGR_TEST_SKIP_DAEMON_LOCK=1` | daemon skips `daemon.lock` (multi-daemon test) |
| `TASKMGR_SHIM_EXECUTABLE` | interpreter used to exec the shim (shim start failure) |
| `TASKMGR_TEST_SHIM_CRASH=<point>` | shim exits at `before_identity`, `before_exec`, `after_exit_recorded`, `before_finalize` |
| `TASKMGR_TEST_IGNORE_KILL=1` | stop procedure sends no signals (unconfirmed stop) |
| `TASKMGR_TEST_RECEIVE_PAUSE=<s>` | `receive` sleeps while holding the upload lock |

The systemd behaviour is tested by checking `KillMode=process` in the user unit and by stopping
the daemon with SIGTERM to its whole process group: shim and task keep running and the
real exit code is recorded.

## Optional smoke test against real SSH

```sh
# on the server
git clone <repo> ~/taskmgr && mkdir -p ~/taskmgr-root
nohup ~/taskmgr/bin/taskmgr-remote --root ~/taskmgr-root serve >/dev/null 2>&1 &
# locally, machines.json with transport "ssh" pointing at it
task submit --host gpu1 taskmgr/examples/succeed
task status <jobid> --host gpu1 --wait --poll-interval 2      # exit 0, outputs present
task submit --host gpu1 taskmgr/examples/multi-gpu            # needs >= 2 GPUs
task logs <jobid> --host gpu1 --follow
task fetch <jobid> --host gpu1 --output ./results/
```
