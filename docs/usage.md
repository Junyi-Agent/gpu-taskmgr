---
title: CLI usage
tags: [taskmgr, cli, usage]
lang: en
up: "[[taskmgr/README]]"
---

# CLI usage

Other language: [[docs/usage.zh-CN]]

```sh
task -h                                # the commands by group (tasks / hosts / others)
task init AA-01                        # ./AA-01/ template; fill every <REQUIRED...> marker
task validate AA-01/                   # exit 0 valid, 1 with all errors, 2 bad path
task submit AA-01/                     # round-robin over enabled servers
task submit --host nebius-h200-2 AA-01.tar.gz
task submit --host nebius-h200-2 --submission-key tune-aa01-v3 AA-01/   # idempotent retry
task jobs [--host H] [--state STATE] [-n N]   # STATE: queued, running, stopping, unknown or a terminal state; N: default 10
task status <jobid> [--host H]
task status <jobid> [--host H] --wait [--poll-interval 5] [--retry-timeout 300] [--timeout S]
task logs    <jobid> [--host H] [--stream stdout|stderr|shim] [--follow]
task fetch   <jobid> [--host H] --output ./results/
task cancel  <jobid> [--host H]
task retry   <jobid> [--host H]        # creates a new job; prints its jobid
task resolve <jobid> [--host H] [--as stopped --confirm <jobid>]
task host add NAME [--ssh USER@HOST] [--port N] [--identity-file F]
                   [--ssh-option KEY=VALUE ...] [--disabled | --enabled] [--pyz FILE] [--yes]
task host list                         # configured servers
task host remove NAME [--yes]          # drop a server from the machine list
task host enable NAME                  # use a server again
task host disable NAME                 # leave a server out of submits and queries for now
task host ping [NAME]                  # check that servers answer
task whoami                            # user and hostname each server runs as
task update                            # update this local CLI (git pull --ff-only)
task version                           # local version and the code version on each server
```

Every command except `logs` takes `--detail`; every command takes `--plain`.

## Output

All output is colored: text and `--detail` JSON on stdout, errors and progress on stderr,
`-h` help and usage errors, on a terminal or not. `--plain` (anywhere on the command line)
or the environment variable `NO_COLOR` turns colors off. Scripts that read the output use
`--detail --plain`.

Every command prints in the same shapes:

| Shape | Used for | Colors |
| --- | --- | --- |
| table | lists (`jobs`, `retry`, `submit`, `host list`, `host ping`, `whoami`, `version`) | bold header; STATE and status columns by value |
| `key: value` lines | one object (`status`, `resolve`, the local part of `version`) | bold keys; state by value |
| `label: message` | one-line results and notes | label green (done), yellow (note, warning), red (failed), cyan (`hint`) |
| JSON (`--detail`) | the full object | keys blue, strings green, numbers cyan, `true`/`false`/`null` magenta |

```
created: AA-01 in /work/AA-01
hint: fill every <REQUIRED...> marker, then run `task validate /work/AA-01`
```

Errors without `--detail` are one `error:` line on stderr, with the error code at the end,
and nothing on stdout:

```
error: path does not exist: AA-01 (not_found)
```

With `--detail` an error is `{"error": {"code", "message", "host"?, "details"?}}` on stdout,
plus the `error:` line on stderr.

`task -h` and every `task <command> -h` have one layout: a `usage:` line, a one-sentence
description, the sections `commands` / `arguments` / `options` (bold) with names in one
cyan column, `-h, --help` last, and a line naming the next help to read.

`<jobid>` is enough to find a job: without `--host`, `status`, `logs`, `fetch`, `cancel`,
`retry` and `resolve` ask every enabled server for the jobid (disabled servers are not
searched; a not-found error names them). A jobid found on several servers →
`ambiguous` (exit 2, pass `--host`); not found while some server is unreachable →
`incomplete` (exit 2). `--host` skips the lookup.

`task jobs` prints a table (a departure from spec §16, by user decision):

```
JOBID             TASK_ID  SUBMITTED             HOST           STATE
b466fc71d0017505  GT-01    2026-10-29T18:55:33Z  nebius-h200-1  succeeded
```

- A running job whose entry exited 75 because its GPU was held by another process, and
  which waits to be started again, shows STATE `pending-gpu` (see
  [[docs/task-package]], GPU contract). Its state is still `running`; `--detail` and
  `task status` show the reason in `pending_reason`.
- A job with `precompile` shows `precompiling` while it compiles without GPUs (state
  `running`), then `pending-gpu` while it waits for them (state `queued`). `--detail` and
  `task status` show `phase` (see [[docs/task-package]], Precompile).
- `task jobs` lists the 10 most recently submitted jobs over all queried servers together;
  `-n N` (a positive integer) changes the count. `--host` and `--state` filter first, so
  `-n` counts the jobs that remain. `--detail` returns the same jobs.
- Rows are sorted by submission time (UTC), newest last. STATE has the same color in every command that
  shows it: queued and `pending-gpu` yellow, running and `precompiling` cyan, stopping magenta, unknown bold
  magenta, succeeded green, failed / invalid_result / timed_out red, cancelled gray.
- `--state` takes a job state only (`precompiling` and `pending-gpu` are displays: use
  `--state running` or `--state queued`); any other value is a usage error (exit 2).
- Unreachable servers are reported on stderr (`warning: H unreachable, ...`), and so are
  the disabled servers that were skipped; with `--detail` too. The exit code is 0 if at
  least one server answered or the machine list is empty, 2 otherwise.
- A server that is down does not hold the command up: ssh gives up connecting after 10 s
  (`ConnectTimeout=10`; an `-o ConnectTimeout=N` in the server's machine entry wins), and a
  query to every server (`jobs`, the jobid lookup, `whoami`, `version`) waits at most 30 s
  for each server's answer (`TASKMGR_QUERY_TIMEOUT=<seconds>` changes it). The same limit
  applies to every call to a single server (`status`, `cancel`, `retry`, `resolve`, each
  poll of `status --wait`); uploads (`submit`), `logs` and `fetch` have no limit.
- `--detail` prints the JSON object `{"complete", "errors", "skipped", "jobs"}` instead.
  Each entry of `errors` has `host`, `error` and `code`: `connection_error` when the server
  could not be reached or did not answer in time, `remote_error` when it was reached but
  the remote command gave no JSON reply (it crashed).

| Exit | Meaning |
| --- | --- |
| 0 | success; `--wait`: job succeeded; `jobs`: at least one server answered, or no server is configured |
| 1 | `--wait`: job ended failed / invalid_result / cancelled / timed_out; `validate`: invalid |
| 2 | parameter, connection, query or operation error; `jobs` with every server failing |
| 3 | `--wait` found the job in `unknown` |

The JSON objects below are what `--detail` prints.

Submit receipt: `{"host", "jobid", "task_id", "submission_key", "deduplicated",
"content_hash", "state"}`. If the outcome is uncertain (connection failure, or no valid
reply: error code `remote_error`), stderr names the chosen host and the submission key and prints the retry command
`task submit --host H --submission-key K PATH`; the CLI never resubmits to another server.
A package that uses a feature (such as `precompile`) the server's code lacks is refused
before the upload with `unsupported_feature` (exit 2) and the hint `task host add H` (see
[[docs/task-package]], Server features).

`task status` for an unfinished job prints
`{"host", "jobid", "task_id", "state", "pending_reason", "phase", "cancel_requested", "run": {status,
dispatched_at, started_at, exited, return_code, signal, takeovers, gpu_busy_retries,
last_gpu_busy_at, identity} or null, "progress":
<progress.json or null>, "submitted_at", "gpus", "error", "retry_of", "retried_as",
"cancel_request", "gpu_holders", "complete"}`. For a finished job it prints the final
`result.json` with `host`, `complete`, `retry_of`, `retried_as` and `cancel_request` added at
the top level.

- `cancel_request`: `null`, or `{"by", "at", "via"}` once someone asked to cancel the job:
  `by` is the `user@hostname` of the CLI that ran `task cancel` (`via: "cli"`), or the server's
  user and hostname when the cancel was made on the server or by an older CLI
  (`via: "remote"`); `at` is the UTC time of the first request. The text output shows it as
  `cancelled_by: junyi@laptop at 2026-11-03T09:12:44Z`.
- `gpu_holders`: for a job that waits for GPUs (queued with GPUs, `pending-gpu`), the
  processes that hold a CUDA context on its GPUs right now, from `nvidia-smi
  --query-compute-apps` and `/proc` on the server: `[{"gpu": <index>, "uuid", "pid",
  "user", "command" (cut to 200 characters), "used_memory_mib", "taskmgr_jobid"?}]`
  (`taskmgr_jobid` when the process belongs to a taskmgr job). `[]` when no process holds
  them, `null` for a job that waits for no GPU or a server without the `gpu_holders`
  feature. If nvidia-smi fails, `gpu_holders` is `null` and `gpu_holders_error` says why.
  `user` and `command` are `null` for a pid the server's `/proc` does not show (a process in
  another pid namespace). The text output has one line per holder:

  ```
  state:          pending-gpu
  pending_reason: entry exited 75 (GPU busy); retry #12 in 1 s
  gpu_holder:     GPU 0: pid 81234 user root 40.0 GiB: python /root/xbr/mkc/dl/tools/gpu_exec.py ...
  ```

## Retry and re-runs

A job runs at most once. `task retry <jobid>` (terminal jobs only; `unknown` →
`resolve_required`, queued/running/stopping → `not_terminal`, both exit 2) creates a **new
job** from the same package at the back of the queue. It prints a table
`JOBID RETRY_OF HOST STATE`; the JSON is
`{"jobid": <new>, "retry_of": <old>, "state": "queued", "changed": true, "enqueue_seq",
"host"}`. The old job keeps its state, logs and result; its `retried_as` lists the new
jobids, and the new job's `retry_of` points back. A resubmission with the original
submission key still returns the original jobid.

When a run is interrupted (reboot, or the entry never started) the job ends `failed` with
`error.kind = "interrupted"` and an automatic re-run is created the same way, keeping the
original queue position (see [[docs/recovery]]). `task status --wait` follows such
automatic re-runs: it prints `job X was interrupted; following automatic re-run Y` on stderr
and keeps waiting on Y; output and exit code are those of the last job. Manual retries are
not followed.

## Logs and fetch

- `task logs` prints the job's log (`--stream`, default stdout). `--follow` keeps printing
  until the run is final or the job is terminal or unknown; on a queued job it waits until
  the job starts. A job that never ran → error `no_run` (exit 2).
- `task fetch` downloads the job's run files (`job.json`, `runner.json`, `stdout.log`,
  `stderr.log`, `shim.log`, `workdir/`; not `package/`, `package.tar.gz`, `shim.lock`) to
  `<output>/<host>/<jobid>/`. An existing target → `exists` (exit 2), nothing written. A job
  that never ran → `no_run` (exit 2). For an unfinished job stderr warns and the receipt has
  `partial: true`. Receipt: `{"host", "jobid", "state", "partial", "path", "skipped"}`;
  `skipped` lists `{"path", "reason"}` for every entry not written (special files, unsafe
  symlinks), each also a warning on stderr.

## Servers

`task host add NAME --ssh USER@HOST` sets up a GPU server over ssh and adds it to the
machine list; `task host add NAME` checks an existing server and updates it (code,
`remote_root`, systemd user unit and service, linger, machine entry, removal of the code
tree that versions before the zipapp installed). Each change is
shown and asked with `[y/N]`; `--yes` answers yes. A second run on an up-to-date server
changes nothing and asks nothing. Progress and prompts go to stderr; stdout gets a summary
of the steps (`--detail`: `{"host", "steps": [{"name", "status", "detail"}], "complete"}`),
exit 0 if every step is `ok` or `changed`, otherwise 2. The steps are described in
[[docs/install]].

- `task host list` lists the machine list.
- `task host remove NAME` removes a server from the machine list; the server itself is not
  touched.
- `task host disable NAME` marks a server disabled, e.g. while it is down: round-robin
  submits skip it, and the queries over every server (`jobs`, the jobid lookup, `whoami`,
  `version`) do not ask it, so they do not wait for its timeout. Output names the skipped
  servers (`skipped: disabled, not queried: NAME` on stderr; `"skipped"` in the JSON).
  `task host ping` without NAME skips it too. `--host NAME` and `task host ping NAME` still
  reach it, and nothing changes on the server. `task host enable NAME` turns it back on. Both only change `enabled` in
  `machines.json`.
- `task host ping [NAME]` calls every enabled server (or the named one, disabled or not) and
  shows whether it answers, the round-trip time and the installed code version; exit 2 if
  one did not answer. Disabled servers are listed as `skipped: disabled, not pinged: NAME`
  (`"skipped"` in the JSON).

## whoami, update, version

- `task whoami [--host H]` asks each server which user and hostname taskmgr runs as there.
- `task update` runs `git pull --ff-only` in the local checkout the CLI runs from. Servers
  keep their code until `task host add NAME` installs the new version. Run from `task.pyz`
  it fails with `not_a_checkout` (exit 2): build a new archive instead
  (`python3 -m taskmgr.pyz`, see [[docs/install]]).
- `task version [--host H]` prints the local version (pyproject version, git commit, code
  version, protocol version; run from `task.pyz`, the version and commit are the ones the
  archive was built from, and `dirty` is null) and the code version installed on each server, marked
  `up to date` or `differs`. The code version is the hash `task host add` installs, so
  `differs` means `task host add NAME` would upload new code.
