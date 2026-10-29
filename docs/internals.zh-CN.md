---
title: 远程目录结构与状态机
tags: [taskmgr, internals, state-machine, schema]
lang: zh-CN
up: "[[taskmgr/README.zh-CN]]"
---

# 远程目录结构与状态机

其他语言：[[docs/internals]]

## 远程目录结构

```
<remote_root>/
  config.json
  locks/state.lock          串行化所有状态变更（所有进程、所有客户端）
  locks/daemon.lock         守护进程实例锁（文件内容是守护进程 pid）
  locks/counters.json       下一个 enqueue_seq（假设：需求文档未规定位置）
  incoming/up-*/            正在上传的包（持有该目录的 flock）；调度器不会扫描
  incoming/retry-<jobid>/   正在构建的重试或重跑作业（规则同上）
  submission-keys/<key>.json  {"submission_key", "jobid", "content_hash", "created_at"}
  queue/<jobid>/            queued
  in-progress/<jobid>/      running / stopping / unknown
  done/<jobid>/             终态
  daemon/daemon.log         守护进程日志（同时写到 stderr / journald）
```

```
<jobid>/
  job.json          状态记录（见下）
  package.tar.gz    原始上传文件，不可修改
  package/          只读的解压副本；运行从这里开始
  runner.json       运行的权威记录，只由 runner 写入
  shim.lock         shim 在整个生命周期内持有的 flock
  stdout.log  stderr.log  shim.log
  workdir/          package/ 的全新副本，加上 progress.json、result.json、artifacts/
```

一个作业最多运行一次。重试（`task retry`）或中断后的自动重跑都是一个新作业，有自己的 jobid 和
目录：先在 `incoming/retry-<jobid>/` 下复制 `package.tar.gz` 和 `package/`，再重命名到 `queue/`。

`job.json`：

```json
{"schema_version": 1, "jobid": "3f9a1c0e7b2d4a56", "task_id": "AA-01", "state": "running",
 "submitted_at": "...", "updated_at": "...", "ended_at": "...", "enqueue_seq": 17,
 "content_hash": "<sha256>", "submission_key": "cli-...",
 "gpus": [{"declared": 0, "uuid": "GPU-..."}],
 "cancel_requested": false, "stop_reason": null | "cancel" | "timeout" | "leftover",
 "auto_restarts": 0, "dispatched_at": null | "...",
 "retry_of": null | "<本作业重跑的 jobid>", "retried_as": ["<重跑作业的 jobid>"],
 "error": null}
```

`auto_restarts` 是重跑链上的自动重跑次数（提交或手动重试的作业为 0，自动重跑的作业为前一个作业的
值 + 1）。`dispatched_at` 在派发时写入；没有它的作业从未运行，也没有运行文件。

`runner.json`：

```json
{"schema_version": 1, "jobid": "...", "status": "dispatched|starting|running|
  <终态>|interrupted", "gpus": [...], "dispatched_at": "...",
 "boot_id": "...", "shim_pid": 1234, "shim_start_time": 98765, "pgid": 1240,
 "leader_pid": 1240, "leader_start_time": 98770, "argv_hash": "<argv JSON 的 sha256>",
 "argv": [...], "started_at": "...", "started_at_epoch": 1761725700.1,
 "exited": true, "return_code": 0, "signal": null, "ended_at": "...",
 "final": true, "taskmgr": {<_taskmgr 块>}, "takeovers": [{"at", "by_pid"}],
 "stop_unconfirmed_reason": null, "interrupted_reason": null}
```

每次写 JSON 都先写到同目录下的临时文件，fsync 后用 `os.replace` 替换，再对所在目录 fsync。

## 状态机

| 状态 | 目录 | 终态 | 占用 GPU |
| --- | --- | --- | --- |
| queued | queue/ | 否 | 否 |
| running | in-progress/ | 否 | 是 |
| stopping | in-progress/ | 否 | 是 |
| unknown | in-progress/ | 否 | 是 |
| succeeded / failed / invalid_result / cancelled / timed_out | done/ | 是 | 否 |

```
queued --派发--> running --退出--> succeeded | failed | invalid_result
queued --取消--> cancelled
running --shim fork/exec 失败--> failed(shim_start_failed)
running --启动前检查失败--> failed(setup)
running --发现取消请求 / 超时--> stopping --进程组已全部退出--> cancelled | timed_out
stopping --SIGKILL 后仍有进程存活--> unknown
running/stopping --重启恢复--> 见下文
unknown --resolve（检查得出结论）--> 见下文
unknown --resolve --as stopped--> failed(operator_marked_stopped) | cancelled
终态 --retry--> （不变；新建一个 queued 作业，retry_of = 本作业）
```

状态转换在 `locks/state.lock` 下进行：先写 `job.json`，再重命名目录；两者不一致时以
`job.json` 为准，守护进程启动时会把目录移到正确位置。运行状态（`runner.json`）是某个终态或
`interrupted`。

### 调度

每轮调度（在状态锁下）按 `enqueue_seq` 遍历 queued 作业，并维护一个预留集合：作业所需的 GPU
全部空闲且都不在预留集合中时才启动，否则把它需要的 GPU 加入预留集合；如果作业用到的某块 GPU
被 `unknown` 作业占用，它既不启动也不预留。`gpus: []` 的作业只受 `max_cpu_jobs` 限制。派发时先
写 `runner.json`，再写 `job.json`（running）并重命名目录，最后才两次 fork 出 shim（新会话，被 init
收养），并把已加锁的 `shim.lock` 交给它。fork/exec 失败时作业以 `failed`
（`shim_start_failed`）结束。queued 作业如果已经有 `runner.json`（两次写入之间崩溃），记录中没有
执行身份时重新派发；有执行身份时作业变为 `unknown`，继续占用 GPU。

### Shim

1. 把 `package/` 复制到 `workdir/`，构造环境变量，执行启动前检查（venv 和必需路径存在，入口能
   解析且可执行，声明的 UUID 在 nvidia-smi 中可见）。检查失败 → `failed`，`error.kind=setup`，
   `return_code: null`。
2. fork；子进程调用 `setsid()` 后阻塞在管道上。shim 写入身份记录（`boot_id`、shim 的 pid 和
   启动时间、pgid、组长 pid 和启动时间、argv 哈希）并 fsync，然后放行子进程，子进程 exec 入口
   命令。没有身份记录 ⇒ 入口命令从未执行。
3. 轮询 `waitpid`，每隔 `control_poll_seconds` 重新读取 `job.json` 检查 `cancel_requested`，并
   执行 `timeout_seconds`（从入口进程启动时开始计时）。
4. 入口进程退出后：写入 `exited/return_code/signal/ended_at`；停止进程组中残留的进程（警告
   `leftover_processes`）；合并 `result.json`；把 `runner.json` 标记为 final；更新 `job.json`。

### 停止流程

向进程组发送 SIGTERM → 等待 `stop_grace_seconds` → SIGKILL → 等待 `kill_confirm_seconds`。只有
确认进程组中没有存活进程，才释放 GPU 并进入终态；否则作业变为 `unknown`，继续占用 GPU。进程组
成员通过 `/proc` 判断：pgid 等于记录值，且启动时间不早于组长的启动时间；僵尸进程视为已退出。
