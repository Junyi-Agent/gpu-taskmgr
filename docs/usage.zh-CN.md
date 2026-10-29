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
task jobs [--host H] [--state running]
task status <jobid> [--host H]
task status <jobid> [--host H] --wait [--poll-interval 5] [--retry-timeout 300] [--timeout S]
task cancel  <jobid> --host H
task retry   <jobid> --host H
task resolve <jobid> --host H [--as stopped --confirm <jobid>]
task logs  <jobid> --host H [--attempt N] [--stream stdout|stderr|shim] [--follow]
task fetch <jobid> --host H --output ./results/ [--attempt N | --all-attempts]
task hosts                             # 列出已配置的服务器（额外命令）
```

除 `logs` 外，所有命令都在 stdout 输出一个 JSON 对象；出错时输出
`{"error": {"code", "message", "host"?, "details"?}}`，诊断信息写到 stderr。

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
