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
task retry   <jobid> --host H
task resolve <jobid> --host H [--as stopped --confirm <jobid>]
task logs  <jobid> --host H [--attempt N] [--stream stdout|stderr|shim] [--follow]
task fetch <jobid> --host H --output ./results/ [--attempt N | --all-attempts]
task hosts                             # 列出已配置的服务器（额外命令）
```

除 `logs` 和 `jobs` 表格外，所有命令都在 stdout 输出一个 JSON 对象；出错时输出
`{"error": {"code", "message", "host"?, "details"?}}`，诊断信息写到 stderr。

`task jobs` 输出表格（按用户决定，偏离需求文档 §16）：

```
JOBID             TASK_ID  SUBMITTED             HOST           STATE
b466fc71d0017505  GT-01    2026-10-29T18:55:33Z  nebius-h200-1  succeeded
```

- 按提交时间（UTC）排序。stdout 是终端时 STATE 带颜色（queued 黄、running 青、stopping 品红、
  unknown 粗体品红、succeeded 绿、failed / invalid_result / timed_out 红、cancelled 灰）；设置
  `NO_COLOR` 或 `TERM=dumb` 时关闭，`--color always|never` 可强制指定。
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
`{"host", "jobid", "task_id", "state", "cancel_requested", "attempt": {attempt, status,
dispatched_at, started_at, exited, return_code, signal, takeovers, identity}, "progress":
<progress.json 或 null>, "submitted_at", "gpus", "error", "complete"}`。对已结束的作业，输出最终
的 `result.json`。
