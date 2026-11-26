# JSON 输出（`-o json`）

[English](json-output.md)

除 `logs job` 外，每个 `task` 命令加 `-o json` 时都在 stdout 输出一个 JSON 对象，键按字母排序。本页列出每个命令输出对象的顶层键、退出码和错误码。命令做什么见 [[docs/usage.zh-CN]]。

先看退出码，再看对象。对象里有 `error` 键时也可能带着数据（见[错误](#错误)）。

```sh
task get jobs -o json | jq '.jobs[] | {jobid, state}'
```

键后面的 `?` 表示这个键可能不存在。`|null` 表示值可能是 `null`。

本页的键都是稳定的，标了**内部**的除外。内部键用于排查问题，任何版本都可能改名或删掉，不要依赖它们写逻辑。

## 错误

命令失败时输出：

```json
{"error": {"code": "not_found", "message": "...", "host": "gpu1", "details": {}}}
```

`host` 和 `details` 只在适用时出现。文本模式下，同一个错误以 `error: <message> (<code>)` 写到 stderr。退出码 2。

有三种情况，结果对象和错误一起输出，或者输出结果但没有错误：

| 命令 | 对象 | 退出码 |
|---|---|---|
| `get jobs`、`get user`、`get version`，没有一台服务器应答 | 正常的对象（`jobs` 或 `hosts` 为空），加 `"error": {"code": "all_hosts_failed", "message"}` | 2 |
| `wait job` 到了 `--timeout` | 最后一次 `get job` 的对象，加 `"error": {"code": "wait_timeout", "message", "host"}`；一次状态都没拿到时只有 `{"error": ...}` | 2 |
| `delete host`，你回答了 no | `{"host", "removed": false, "machines"}`，没有 `error` | 2 |

按 Ctrl-C 时输出 `{"error": {"code": "interrupted", ...}}`，退出码 2，stderr 不写 `error:` 行。

## 作业

### `create job`

```
{"host", "jobid", "task_id", "submission_key", "deduplicated": bool,
 "content_hash": str|null, "state": str|null}
```

新作业的 `state` 是 `"queued"`。去重命中时（`deduplicated: true`）返回已有作业的当前状态。

错误：`submission_uncertain`（提交中连接断开，或没有收到有效回复；`details.submission_key` 里是 key，用 `--host H --submission-key K` 重试）、`submission_key_conflict`（key 相同但包不同）、`bad_submission_key`、`invalid_package`、`package_too_large`、`unsupported_feature`、`no_enabled_hosts`。

### `get jobs`

```
{"jobs": [job], "errors": [{"host", "error", "code"}], "skipped": [host],
 "complete": bool}
```

`job` 的键：`host`、`jobid`、`task_id`、`state`、`submitted_at`、`dispatched_at`、`enqueue_seq`（内部）、`cancel_requested`、`cancel_request`、`gpus`、`gpu_request`、`error`、`retry_of`、`retried_as`、`pending_reason`、`phase`。有服务器查询失败时 `complete` 为 `false`，错误在 `errors` 里。`skipped` 列出停用的服务器。

### `get job ID` 和 `wait job ID`

未结束的作业：

```
{"host", "jobid", "task_id", "state", "pending_reason", "phase",
 "cancel_requested", "cancel_request", "run": {...}|null, "progress": ...|null,
 "submitted_at", "gpus", "gpu_request", "error", "retry_of", "retried_as": [jobid],
 "gpu_holders", "gpu_holders_error"?, "complete"}
```

- `run`：`{status, dispatched_at, started_at, exited, return_code, signal, takeovers, gpu_busy_retries, last_gpu_busy_at, identity}`；作业还没运行时为 `null`。`identity` 是内部键。
- `progress`：作业的 `progress.json`；文件不存在时为 `null`；读不了时为 `{"error": "<原因>"}`。

已结束的作业：作业的 `result.json`，加上 `jobid`、`state`、`host`、`complete`、`retry_of`、`retried_as`、`cancel_request`、`gpu_request`。这些顶层键会覆盖 `result.json` 里同名的键。`_taskmgr` 保留。

先看 `state`。作业到了终态才有 `_taskmgr`，还没结束的作业没有这个键。

`wait job` 的退出码：0 成功，1 其他结束状态，3 `unknown`，2 超时或连接错误。

### `cancel job ID`

```
{"host", "jobid", "state", "changed": bool, "cancel_requested": bool,
 "cancel_request"?: {"by", "at", "via"}}
```

### `retry job ID`

```
{"host", "jobid", "retry_of", "state", "changed": true, "enqueue_seq": int}
```

`enqueue_seq` 是内部键。
`jobid` 是新作业的 ID。错误：`resolve_required`（旧作业是 `unknown`）、`not_terminal`。

### `resolve job ID`

```
{"host", "jobid", "state", "changed": bool, "decision"?, "identity"?,
 "alive_pids"?, "reason"?, "hint"?}
```

`decision`、`identity`、`alive_pids`、`reason`、`hint` 是内部键。带 `--as STATE`，或作业不是 `unknown` 时，只有 `host`、`jobid`、`state` 和 `changed`。错误：`not_unknown`。

### `fetch job ID --dest DIR`

```
{"host", "jobid", "state", "partial": bool, "path", "skipped": [{"path", "reason"}]}
```

错误：`no_run`、`exists`。

### `logs job ID`

加 `-o json` 也输出日志原文。错误仍按上面的 JSON 格式输出（例如 `no_run`）。

## 任务包

### `create package TASK_ID`

```
{"path", "task_id"}
```

错误：`bad_task_id`、`exists`。

### `validate package PATH`

合格（退出码 0）：

```
{"valid": true, "task_id", "content_hash", "features": [str],
 "warnings": [{"code", "message", "path"?}]}
```

不合格（退出码 1）：

```
{"valid": false, "errors": [{"code", "message", "path"?}],
 "warnings": [{"code", "message", "path"?}]}
```

### `build package [SRC...]`

合格（退出码 0）：

```
{"valid": true, "path", "archive": bool, "task_id", "content_hash",
 "features": [str], "source_files": int, "warnings": [str]}
```

不合格（退出码 1）：`{"valid": false, "errors": [{"code", "message", "path"?}], "path": null}`。这里的 `warnings` 是字符串；`validate package` 里的是对象。

## 服务器

### `create host NAME`、`update host NAME`

```
{"host", "steps": [{"name", "status", "detail"}], "complete": bool,
 "plan"?: true, "planned"?: [步骤名]}
```

`status`：`ok`、`changed`、`kept`、`skipped`、`planned`、`declined`、`failed`。带 `--plan` 时才有 `plan` 和 `planned`。退出码：0 全部完成，2 有步骤失败；带 `--plan` 时 0 没有要改的，1 有计划中的改动，2 检查失败。

### `get hosts [NAME]`

```
{"machines": 路径, "servers": [{"name", "enabled", "transport", "destination", "remote_root"}]}
```

### `enable host NAME`、`disable host NAME`

```
{"host", "enabled": bool, "changed": bool, "machines"}
```

### `delete host NAME`

```
{"host", "removed": true, "machines", "entry": {...}}
```

`entry` 是被删掉的那条机器配置。错误：`not_interactive`（没有终端，也没加 `--yes`）。

### `ping host [NAME]`

```
{"hosts": [row], "skipped": [host], "complete": bool}
```

`row`：`{"host", "enabled", "reachable": bool, "ms": int}`；服务器没应答时再加 `{"code", "error"}`，应答了再加 `{"protocol_version", "features", "code_version", "code_dir", "user", "hostname"}`。所有服务器都应答时退出码 0，否则 2。

## 其他

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
 "hosts": [{... 同 get user ..., "up_to_date": bool}],
 "errors", "skipped", "complete"}
```

没有 git 时（例如在 `.pyz` 里运行）`dirty` 为 `null`。

### `update cli`

```
{"path", "before", "after", "changed": bool}
```

错误：`not_a_checkout`、`update_failed`。

## 错误码

| 错误码 | 含义 |
|---|---|
| `bad_args` | 参数错误（包括 argparse 报错） |
| `not_found`、`ambiguous`、`incomplete` | 找作业：没找到、在多台服务器上都有、或有服务器没应答 |
| `exists`、`conflict` | 目标文件或目录已存在，或源文件重叠 |
| `bad_task_id`、`bad_task_json`、`copy_failed` | 创建和构建任务包 |
| `invalid_package`、`unsafe_package`、`bad_archive`、`package_too_large` | 任务包检查 |
| `submission_uncertain`、`submission_key_conflict`、`bad_submission_key` | 提交 |
| `no_run`、`resolve_required`、`not_terminal`、`not_unknown` | 作业状态不允许这个命令 |
| `connection_error` | ssh 失败 |
| `remote_error` | 服务器没有返回 JSON |
| `protocol_mismatch` | 服务器协议版本不同；运行 `task update host H` |
| `unsupported_feature` | 服务器代码缺少任务包用到的功能 |
| `bad_machines`、`no_machines`、`unknown_host`、`no_enabled_hosts`、`not_interactive` | 机器列表 |
| `all_hosts_failed`、`wait_timeout` | 出现在结果对象里，见[错误](#错误) |
| `build_failed`、`not_a_checkout`、`update_failed` | `build pyz`、`update cli` |
| `bad_root`、`bad_config`、`corrupt_job`、`nvidia_smi_failed`、`invalid_gpus` | 服务器端状态或 GPU 问题 |
| `interrupted`、`internal_error` | 按了 Ctrl-C，或程序 bug：请上报 |
