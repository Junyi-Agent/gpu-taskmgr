---
title: CLI 用法
tags: [taskmgr, cli, usage]
lang: zh-CN
up: "[[taskmgr/README.zh-CN]]"
---

# CLI 用法

其他语言：[[docs/usage]]

```sh
task init AA-01                        # 生成 ./AA-01/ 模板；填写所有 <REQUIRED...> 标记
task validate AA-01/                   # 退出码 0 输出 {"valid": true}；1 列出所有错误；2 路径错误
task submit AA-01/                     # 在启用的服务器之间轮询
task submit --host nebius-h200-2 AA-01.tar.gz
task submit --host nebius-h200-2 --submission-key tune-aa01-v3 AA-01/   # 幂等重试
task jobs [--host H] [--state running] [--detail] [--color auto|always|never]
task status <jobid> [--host H]
task status <jobid> [--host H] --wait [--poll-interval 5] [--retry-timeout 300] [--timeout S]
task cancel  <jobid> --host H
task retry   <jobid> --host H          # 创建一个新作业，输出新的 jobid
task resolve <jobid> --host H [--as stopped --confirm <jobid>]
task logs  <jobid> --host H [--stream stdout|stderr|shim] [--follow]
task fetch <jobid> --host H --output ./results/
task hosts                             # 列出已配置的服务器（额外命令）
```

除 `logs` 和 `jobs` 表格外，所有命令都在 stdout 输出一个 JSON 对象；出错时输出
`{"error": {"code", "message", "host"?, "details"?}}`，诊断信息写到 stderr。

`task jobs` 输出表格（按用户决定，偏离需求文档 §16）：

```
JOBID             TASK_ID  SUBMITTED             HOST           STATE
b466fc71d0017505  GT-01    2026-10-29T18:55:33Z  nebius-h200-1  succeeded
```

- running 作业的入口进程因 GPU 被其他进程占用而退出 75、正在等待重新启动时，STATE 显示为
  `pending (gpu busy)`（见 [[docs/task-package.zh-CN]] 的 GPU 约定）。它的状态仍是 `running`；
  `--detail` 和 `task status` 在 `pending_reason` 中给出原因。
- 按提交时间（UTC）排序。stdout 是终端时 STATE 带颜色（queued 和 `pending (gpu busy)` 黄、
  running 青、stopping 品红、unknown 粗体品红、succeeded 绿、failed / invalid_result / timed_out
  红、cancelled 灰）；设置 `NO_COLOR` 或 `TERM=dumb` 时关闭，`--color always|never` 可强制指定。
- 连不上的服务器在 stderr 报告（`warning: H unreachable, ...`）；退出码规则不变（至少一台服务器有
  响应为 0，否则为 2）。表格模式下的错误只写到 stderr。
- `--detail` 改为输出 JSON 对象 `{"complete", "errors", "jobs"}`。

| 退出码 | 含义 |
| --- | --- |
| 0 | 成功；`--wait`：作业 succeeded；`jobs`：至少一台服务器有响应 |
| 1 | `--wait`：作业以 failed / invalid_result / cancelled / timed_out 结束；`validate`：校验不通过 |
| 2 | 参数、连接、查询或操作错误；`jobs` 时所有服务器都失败 |
| 3 | `--wait` 发现作业处于 `unknown` |

提交回执：`{"host", "jobid", "task_id", "submission_key", "deduplicated", "content_hash",
"state"}`。如果提交结果不确定（连接失败或没有收到有效回复），stderr 会给出所选的服务器和
submission key，并打印重试命令 `task submit --host H --submission-key K PATH`；CLI 不会自动
改投其他服务器。

对未结束的作业，`task status` 输出
`{"host", "jobid", "task_id", "state", "pending_reason", "cancel_requested", "run": {status,
dispatched_at, started_at, exited, return_code, signal, takeovers, gpu_busy_retries,
last_gpu_busy_at, identity} 或 null, "progress":
<progress.json 或 null>, "submitted_at", "gpus", "error", "retry_of", "retried_as",
"complete"}`。对已结束的作业，输出最终的 `result.json`，并在顶层加上 `host`、`complete`、
`retry_of` 和 `retried_as`。

## 重试与重跑

一个作业最多运行一次。`task retry <jobid> --host H`（只用于终态作业；`unknown` →
`resolve_required`，queued/running/stopping → `not_terminal`，退出码都是 2）用同一个包创建一个
**新作业**，排到队尾，输出 `{"jobid": <新>, "retry_of": <旧>, "state": "queued", "changed": true,
"enqueue_seq", "host"}`。旧作业的状态、日志和结果保持不变；它的 `retried_as` 列出新作业的
jobid，新作业的 `retry_of` 指回旧作业。用原来的 submission key 重新提交，仍然返回原作业的 jobid。

运行被中断（重启，或入口命令从未启动）时，作业以 `failed` 结束，`error.kind = "interrupted"`，
并按同样方式创建一个自动重跑作业，保留原来的队列位置（见 [[docs/recovery.zh-CN]]）。
`task status --wait` 会跟随这种自动重跑：在 stderr 打印
`job X was interrupted; following automatic re-run Y`，然后继续等待 Y；输出和退出码以最后一个
作业为准。手动重试不会被跟随。

## 日志与下载

- `task logs` 输出作业的日志（`--stream`，默认 stdout）。`--follow` 持续输出，直到运行记录变为
  final 或作业进入终态或 unknown；对 queued 作业会先等它开始运行。从未运行的作业 → 错误
  `no_run`（退出码 2）。
- `task fetch` 把作业的运行文件（`job.json`、`runner.json`、`stdout.log`、`stderr.log`、
  `shim.log`、`workdir/`；不含 `package/`、`package.tar.gz`、`shim.lock`）下载到
  `<output>/<host>/<jobid>/`。目标已存在 → `exists`（退出码 2），不写任何文件。从未运行的作业 →
  `no_run`（退出码 2）。作业未结束时 stderr 给出警告，回执中 `partial: true`。回执：
  `{"host", "jobid", "state", "partial", "path", "skipped"}`。
