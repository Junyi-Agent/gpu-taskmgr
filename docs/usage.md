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
task jobs [--host H] [--state running]
task status <jobid> [--host H]
task status <jobid> [--host H] --wait [--poll-interval 5] [--retry-timeout 300] [--timeout S]
task cancel  <jobid> --host H
task retry   <jobid> --host H
task resolve <jobid> --host H [--as stopped --confirm <jobid>]
task logs  <jobid> --host H [--attempt N] [--stream stdout|stderr|shim] [--follow]
task fetch <jobid> --host H --output ./results/ [--attempt N | --all-attempts]
task hosts                             # list configured servers (extra)
```

All commands except `logs` print one JSON object on stdout; errors are
`{"error": {"code", "message", "host"?, "details"?}}` with diagnostics on stderr.

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
`{"host", "jobid", "task_id", "state", "cancel_requested", "attempt": {attempt, status,
dispatched_at, started_at, exited, return_code, signal, takeovers, identity}, "progress":
<progress.json or null>, "submitted_at", "gpus", "error", "complete"}`. For a finished job it
prints the final `result.json`.
