# JSON output (`-o json`)

[中文](json-output.zh-CN.md)

Every `task` command except `logs job` prints one JSON object on stdout when you pass `-o json`. Keys are sorted. This page lists the top-level keys of each object, the exit codes, and the error codes. [[docs/usage]] explains what the commands do.

Read the exit code first, then the object. An object can have an `error` key and still carry data (see [Errors](#errors)).

```sh
task get jobs -o json | jq '.jobs[] | {jobid, state}'
```

`?` after a key means the key can be missing. `|null` means the value can be `null`.

## Errors

A failed command prints:

```json
{"error": {"code": "not_found", "message": "...", "host": "gpu1", "details": {}}}
```

`host` and `details` are present only when they apply. In text mode the same error goes to stderr as `error: <message> (<code>)`. Exit code 2.

Three cases print a result object together with an error, or a result with no error:

| Command | Object | Exit |
|---|---|---|
| `get jobs`, `get user`, `get version`, no server answered | the usual object (`jobs` or `hosts` empty) plus `"error": {"code": "all_hosts_failed", "message"}` | 2 |
| `wait job` hit `--timeout` | the last `get job` object plus `"error": {"code": "wait_timeout", "message", "host"}`; only `{"error": ...}` if no status ever arrived | 2 |
| `delete host`, you answered no | `{"host", "removed": false, "machines"}`, no `error` | 2 |

Ctrl-C prints `{"error": {"code": "interrupted", ...}}`, exit 2. It writes no `error:` line on stderr.

## Jobs

### `create job`

```
{"host", "jobid", "task_id", "submission_key", "deduplicated": bool,
 "content_hash": str|null, "state": str|null}
```

A new job has `state` `"queued"`. A deduplicated submit (`deduplicated: true`) returns the existing job's current state.

Errors: `submission_uncertain` (connection lost during submit, or no valid reply; `details.submission_key` holds the key: retry with `--host H --submission-key K`), `submission_key_conflict` (same key, different package), `bad_submission_key`, `invalid_package`, `package_too_large`, `unsupported_feature`, `no_enabled_hosts`.

### `get jobs`

```
{"jobs": [job], "errors": [{"host", "error", "code"}], "skipped": [host],
 "complete": bool}
```

`job` keys: `host`, `jobid`, `task_id`, `state`, `submitted_at`, `dispatched_at`, `enqueue_seq`, `cancel_requested`, `cancel_request`, `gpus`, `gpu_request`, `error`, `retry_of`, `retried_as`, `pending_reason`, `phase`. `complete` is `false` when a server failed; its error is in `errors`. `skipped` lists disabled servers.

### `get job ID` and `wait job ID`

Unfinished job:

```
{"host", "jobid", "task_id", "state", "pending_reason", "phase",
 "cancel_requested", "cancel_request", "run": {...}|null, "progress": ...|null,
 "submitted_at", "gpus", "gpu_request", "error", "retry_of", "retried_as": [jobid],
 "gpu_holders", "gpu_holders_error"?, "complete"}
```

- `run`: `{status, dispatched_at, started_at, exited, return_code, signal, takeovers, gpu_busy_retries, last_gpu_busy_at, identity}`; `null` before the job ran.
- `progress`: the job's `progress.json`; `null` if missing; `{"error": "<reason>"}` if unreadable.

Finished job: the job's `result.json` plus `host`, `complete`, `retry_of`, `retried_as`, `cancel_request`, `gpu_request`.

`wait job` exit codes: 0 succeeded, 1 other final state, 3 `unknown`, 2 timeout or connection error.

### `cancel job ID`

```
{"host", "jobid", "state", "changed": bool, "cancel_requested": bool,
 "cancel_request"?: {"by", "at", "via"}}
```

### `retry job ID`

```
{"host", "jobid", "retry_of", "state", "changed": true, "enqueue_seq": int}
```

`jobid` is the new job. Errors: `resolve_required` (the old job is `unknown`), `not_terminal`.

### `resolve job ID`

```
{"host", "jobid", "state", "changed": bool, "decision"?, "identity"?,
 "alive_pids"?, "reason"?, "hint"?}
```

With `--as STATE`, or when the job is not `unknown`, only `host`, `jobid`, `state` and `changed` are present. Error: `not_unknown`.

### `fetch job ID --dest DIR`

```
{"host", "jobid", "state", "partial": bool, "path", "skipped": [{"path", "reason"}]}
```

Errors: `no_run`, `exists`.

### `logs job ID`

Raw log text, also with `-o json`. Errors are JSON as above (for example `no_run`).

## Packages

### `create package TASK_ID`

```
{"path", "task_id"}
```

Errors: `bad_task_id`, `exists`.

### `validate package PATH`

Valid (exit 0):

```
{"valid": true, "task_id", "content_hash", "features": [str],
 "warnings": [{"code", "message", "path"?}]}
```

Invalid (exit 1):

```
{"valid": false, "errors": [{"code", "message", "path"?}],
 "warnings": [{"code", "message", "path"?}]}
```

### `build package [SRC...]`

Valid (exit 0):

```
{"valid": true, "path", "archive": bool, "task_id", "content_hash",
 "features": [str], "source_files": int, "warnings": [str]}
```

Invalid (exit 1): `{"valid": false, "errors": [{"code", "message", "path"?}], "path": null}`. Here `warnings` are strings; in `validate package` they are objects.

## Hosts

### `create host NAME`, `update host NAME`

```
{"host", "steps": [{"name", "status", "detail"}], "complete": bool,
 "plan"?: true, "planned"?: [step name]}
```

`status`: `ok`, `changed`, `kept`, `skipped`, `planned`, `declined`, `failed`. `plan` and `planned` appear with `--plan`. Exit: 0 complete, 2 a step failed; with `--plan` 0 nothing to change, 1 changes planned, 2 a check failed.

### `get hosts [NAME]`

```
{"machines": path, "servers": [{"name", "enabled", "transport", "destination", "remote_root"}]}
```

### `enable host NAME`, `disable host NAME`

```
{"host", "enabled": bool, "changed": bool, "machines"}
```

### `delete host NAME`

```
{"host", "removed": true, "machines", "entry": {...}}
```

`entry` is the removed machine entry. Errors: `not_interactive` (no terminal and no `--yes`).

### `ping host [NAME]`

```
{"hosts": [row], "skipped": [host], "complete": bool}
```

`row`: `{"host", "enabled", "reachable": bool, "ms": int}` plus `{"code", "error"}` when the server did not answer, or `{"protocol_version", "features", "code_version", "code_dir", "user", "hostname"}` when it did. Exit 0 if every server answered, else 2.

## Others

### `build pyz`

```
{"dest", "code_version", "commit": str|null, "files": int,
 "archives": [{"path", "bytes": int}]}
```

### `get user`

```
{"hosts": [{"host", "code_version", "code_dir", "user", "hostname", "features"}],
 "errors", "skipped", "complete"}
```

### `get version`

```
{"local": {"version", "commit", "dirty": bool|null, "code_version",
           "protocol_version": int, "path"},
 "hosts": [{... same as get user ..., "up_to_date": bool}],
 "errors", "skipped", "complete"}
```

`dirty` is `null` when git is not available (for example in a `.pyz`).

### `update cli`

```
{"path", "before", "after", "changed": bool}
```

Errors: `not_a_checkout`, `update_failed`.

## Error codes

| Code | Meaning |
|---|---|
| `bad_args` | bad arguments (also argparse errors) |
| `not_found`, `ambiguous`, `incomplete` | job lookup: none, several hosts, or some hosts not answering |
| `exists`, `conflict` | target file or directory already exists, or sources overlap |
| `bad_task_id`, `bad_task_json`, `copy_failed` | package creation and building |
| `invalid_package`, `unsafe_package`, `bad_archive`, `package_too_large` | package checks |
| `submission_uncertain`, `submission_key_conflict`, `bad_submission_key` | submit |
| `no_run`, `resolve_required`, `not_terminal`, `not_unknown` | job state does not allow the command |
| `connection_error` | ssh failed |
| `remote_error` | the server gave no JSON reply |
| `protocol_mismatch` | the server speaks another protocol; run `task update host H` |
| `unsupported_feature` | the server code lacks a feature the package uses |
| `bad_machines`, `no_machines`, `unknown_host`, `no_enabled_hosts`, `not_interactive` | machine list |
| `all_hosts_failed`, `wait_timeout` | inside a result object, see [Errors](#errors) |
| `build_failed`, `not_a_checkout`, `update_failed` | `build pyz`, `update cli` |
| `bad_root`, `bad_config`, `corrupt_job`, `nvidia_smi_failed`, `invalid_gpus` | server-side state or GPU problems |
| `interrupted`, `internal_error` | Ctrl-C, or a bug: report it |
