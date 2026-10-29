---
title: CLI usage
tags: [taskmgr, cli, usage]
lang: en
up: "[[taskmgr/README]]"
---

# CLI usage

Other language: [[docs/usage.zh-CN]]

```sh
task init AA-01                        # ./AA-01/ template; fill every <REQUIRED...> marker
task validate AA-01/                   # exit 0 {"valid": true}, 1 with all errors, 2 bad path
task submit AA-01/                     # round-robin over enabled servers
task submit --host nebius-h200-2 AA-01.tar.gz
task submit --host nebius-h200-2 --submission-key tune-aa01-v3 AA-01/   # idempotent retry
task jobs [--host H] [--state running] [--detail] [--color auto|always|never]
task status <jobid> [--host H]
task status <jobid> [--host H] --wait [--poll-interval 5] [--retry-timeout 300] [--timeout S]
task cancel  <jobid> --host H
task retry   <jobid> --host H          # creates a new job; prints its jobid
task resolve <jobid> --host H [--as stopped --confirm <jobid>]
task logs  <jobid> --host H [--stream stdout|stderr|shim] [--follow]
task fetch <jobid> --host H --output ./results/
task hosts                             # list configured servers (extra)
```

All commands except `logs` and the `jobs` table print one JSON object on stdout; errors are
`{"error": {"code", "message", "host"?, "details"?}}` with diagnostics on stderr.

`task jobs` prints a table (a departure from spec §16, by user decision):

```
JOBID             TASK_ID  SUBMITTED             HOST           STATE
b466fc71d0017505  GT-01    2026-10-29T18:55:33Z  nebius-h200-1  succeeded
```

- Rows are sorted by submission time (UTC). STATE is colored (queued yellow, running cyan,
  stopping magenta, unknown bold magenta, succeeded green, failed / invalid_result /
  timed_out red, cancelled gray) when stdout is a terminal; `NO_COLOR` or `TERM=dumb` turns it
  off, `--color always|never` overrides.
- Unreachable servers are reported on stderr (`warning: H unreachable, ...`); the exit code
  rules are unchanged (0 if at least one server answered, 2 otherwise). Errors in table mode
  go to stderr only.
- `--detail` prints the JSON object `{"complete", "errors", "jobs"}` instead.

| Exit | Meaning |
| --- | --- |
| 0 | success; `--wait`: job succeeded; `jobs`: at least one server answered |
| 1 | `--wait`: job ended failed / invalid_result / cancelled / timed_out; `validate`: invalid |
| 2 | parameter, connection, query or operation error; `jobs` with every server failing |
| 3 | `--wait` found the job in `unknown` |

Submit receipt: `{"host", "jobid", "task_id", "submission_key", "deduplicated",
"content_hash", "state"}`. If the outcome is uncertain (connection failure or no valid
reply), stderr names the chosen host and the submission key and prints the retry command
`task submit --host H --submission-key K PATH`; the CLI never resubmits to another server.

`task status` for an unfinished job prints
`{"host", "jobid", "task_id", "state", "cancel_requested", "run": {status, dispatched_at,
started_at, exited, return_code, signal, takeovers, identity} or null, "progress":
<progress.json or null>, "submitted_at", "gpus", "error", "retry_of", "retried_as",
"complete"}`. For a finished job it prints the final `result.json` with `host`, `complete`,
`retry_of` and `retried_as` added at the top level.

## Retry and re-runs

A job runs at most once. `task retry <jobid> --host H` (terminal jobs only; `unknown` →
`resolve_required`, queued/running/stopping → `not_terminal`, both exit 2) creates a **new
job** from the same package at the back of the queue and prints
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
  `partial: true`. Receipt: `{"host", "jobid", "state", "partial", "path", "skipped"}`.
