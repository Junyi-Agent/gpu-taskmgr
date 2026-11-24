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
  runner.prev-N.json  作业重新排队时（GPU 忙退出后，或预编译完成后等待 GPU）移到一边的运行记录；
                      连续的 GPU 忙记录共用一个文件
  shim.lock         shim 在整个生命周期内持有的 flock
  stdout.log  stderr.log  shim.log   重新启动时追加写入，不清空
  workdir/          package/ 的副本（每个作业复制一次，GPU 忙后的重新启动继续使用），加上 progress.json、result.json、artifacts/
```

一个作业最多运行一次。重试（`task retry job`）或中断后的自动重跑都是一个新作业，有自己的 jobid 和
目录：在 `incoming/retry-<jobid>/` 下构建，再重命名到 `queue/`。它的 `package.tar.gz` 和
`package/` 中的文件是旧作业对应文件的硬链接（无法链接时复制）；这些文件只读、从不修改，所以两个
作业可以共用。删除作业目录时只把目录改为可写，不改共用的文件。

`job.json`：

```json
{"schema_version": 1, "jobid": "3f9a1c0e7b2d4a56", "task_id": "AA-01", "state": "running",
 "submitted_at": "...", "updated_at": "...", "ended_at": "...", "enqueue_seq": 17,
 "content_hash": "<sha256>", "submission_key": "cli-...",
 "gpus": [{"declared": 0, "uuid": "GPU-..."}],
 "cancel_requested": false, "stop_reason": null | "cancel" | "timeout" | "leftover",
 "cancel_request": {"by": "junyi@laptop", "at": "...", "via": "cli" | "remote"},  （取消之后）
 "gpu_request": {"count": 1, "min_free_gb": 40, "from_uuids": [...]},  （启动时挑选 GPU）
 "auto_restarts": 0, "dispatched_at": null | "...",
 "retry_of": null | "<本作业重跑的 jobid>", "retried_as": ["<重跑作业的 jobid>"],
 "pending_reason": "entry exited 75 (GPU busy); retry #3 in 2 s",   （只在等待期间存在）
 "gpu_busy_requeues": 2, "not_before_epoch": 1761725702.5,   （GPU 忙后重新排队之后）
 "error": null}
```

`cancel_request` 由第一次取消请求写入，同时设置 `cancel_requested`：`by` 是发出 `task cancel job` 的
CLI 的 `user@hostname`（`via: "cli"`，通过 `taskmgr-remote cancel job --by` 发送）；取消请求不带 `--by` 时
（例如在服务器上直接运行 `taskmgr-remote cancel job`，或较旧的 CLI），`by` 是服务器上的用户和主机名
（`via: "remote"`）。之后的取消请求不会改变它。

`auto_restarts` 是重跑链上的自动重跑次数（提交或手动重试的作业为 0，自动重跑的作业为前一个作业的
值 + 1）。`dispatched_at` 在派发时写入；没有它的作业从未运行，也没有运行文件。入口进程以
`gpu_busy_exit_code` 退出、shim 等待重新启动它期间（见下文 Shim），以及预编译成功后，设置
`pending_reason`；某次运行真正开始或作业结束时删除。`phase` 只在 `task.json` 有 `precompile` 时存在，
先是 `precompile`，然后是 `gpu`（见 [[docs/task-package.zh-CN]]）。`runner.json` 会复制它。
`gpu_busy_requeues` 是作业因 GPU 忙退出而回到 `queued` 的次数。带 `gpu_request` 的作业这样重新排队时
还会写入 `not_before_epoch`（退出时间加 `gpu_retry_interval_seconds`），`pending_reason` 写明这个时间；
调度器在此之前跳过它（如果剩余等待超过这个间隔，比如时钟被往回调，视为已到时间），派发时删除这个键。

`runner.json`：

```json
{"schema_version": 1, "jobid": "...", "status": "dispatched|starting|running|
  waiting_gpu|<终态>|interrupted", "gpus": [...], "dispatched_at": "...",
 "boot_id": "...", "shim_pid": 1234, "shim_start_time": 98765, "pgid": 1240,
 "leader_pid": 1240, "leader_start_time": 98770, "argv_hash": "<argv JSON 的 sha256>",
 "argv": [...], "started_at": "...", "started_at_epoch": 1761725700.1,
 "exited": true, "return_code": 0, "signal": null, "ended_at": "...",
 "final": true, "taskmgr": {<_taskmgr 块>}, "takeovers": [{"at", "by_pid"}],
 "stop_unconfirmed_reason": null, "interrupted_reason": null,
 "gpu_busy_retries": 2, "last_gpu_busy_at": "..."}
```

身份字段、`started_at*` 和退出字段描述最近一次启动；GPU 忙退出后的重新启动会替换它们。

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
running --以 gpu_busy_exit_code 退出--> running（shim 等待后重新启动；显示 "pending-gpu"）
running --shim 在 GPU 忙退出和重新启动之间死亡--> queued（同一个 jobid）
running（有 gpu_request）--以 gpu_busy_exit_code 退出--> queued（同一个 jobid；过 gpu_retry_interval_seconds 后重新挑选 GPU）
queued --取消--> cancelled
running（precompile 阶段）--退出码 0--> queued（gpu 阶段；jobid、enqueue_seq、工作目录不变）
running（precompile 阶段）--其他退出--> failed(precompile)
running --shim fork/exec 失败--> failed(shim_start_failed)
running --启动前检查失败--> failed(setup)
running --shim 在启动入口命令之前出错--> failed(shim_error)（已请求取消时为 cancelled）
running/stopping --守护进程对该作业的恢复或接管连续失败 3 次--> unknown(recovery_error)
running --发现取消请求 / 超时--> stopping --进程组已全部退出--> cancelled | timed_out
stopping --SIGKILL 后仍有进程存活--> unknown
running/stopping --重启恢复--> 见下文
unknown --resolve（检查得出结论）--> 见下文（从不创建重跑作业）
unknown --resolve --as stopped--> failed(operator_marked_stopped) | cancelled
终态 --retry--> （不变；新建一个 queued 作业，retry_of = 本作业）
```

状态转换在 `locks/state.lock` 下进行：先写 `job.json`，再重命名目录；两者不一致时以
`job.json` 为准，守护进程启动时会把目录移到正确位置。运行状态（`runner.json`）final 后是某个终态
或 `interrupted`；shim 等待重新启动期间是 `waiting_gpu`。

`task get jobs` 把设置了 `pending_reason` 的 running 作业和 `gpu` 阶段的 queued 作业的 STATE 显示为
`pending-gpu`（黄色），把 `precompile` 阶段的 running 作业显示为 `precompiling`（青色）。这些只是显示，
状态机里没有这些状态。running 作业等待期间继续占用它的 GPU，所以之后要用这些 GPU 的作业仍排在它
后面（FIFO）。`precompile` 阶段的作业不占用也不预留 GPU：调度器把它当作 `gpus: []` 的作业，受
`max_cpu_jobs` 限制。到了 `gpu` 阶段，它和其他 queued 作业一样按 `enqueue_seq` 调度。

### 调度

每轮调度（在状态锁下）按 `enqueue_seq` 遍历 queued 作业，并维护一个预留集合：作业所需的 GPU
全部空闲且都不在预留集合中时才启动，否则把它需要的 GPU 加入预留集合；如果作业用到的某块 GPU
被 `unknown` 作业占用，它既不启动也不预留。`gpus: []` 的作业只受 `max_cpu_jobs` 限制。带 `gpu_request` 且还没有 GPU 的作业（task.json 的 `gpus` 为对象，
见 [[docs/task-package.zh-CN]] 按条件选择 GPU）轮到它时，在没有被占用或预留的 GPU 中挑选；挑选依据的显存和进程信息由
本轮调度在拿状态锁之前用 nvidia-smi 读取；选择结果在派发前写入 `job.json` 的 `gpus`。只有这样的作业
到期（没有因 GPU 忙退出而被推迟）时才读取，读到的结果最多复用 5 秒，除非此后派发了带 GPU 的作业，或者
占用 GPU 的 taskmgr 作业集合变了。一次读取的两次 nvidia-smi 调用共用 10 秒超时（超时后杀掉进程，不等它
退出）；失败后（同一条消息只记一次日志）这些作业继续等待，`pending_reason` 为 `cannot choose GPUs: ...`，
30 秒后再读，所以 nvidia-smi 挂住时其他作业每 30 秒最多被耽误 10 秒。没有合格的 GPU 时，
`pending_reason` 说明原因（只在文字变化时写入），作业不预留任何 GPU。派发时先
写 `runner.json`，再写 `job.json`（running）并重命名目录，最后才两次 fork 出 shim（新会话，被 init
收养），并把已加锁的 `shim.lock` 交给它。shim 运行 `python3 -m taskmgr.remote ... shim`，
`PYTHONPATH` 最前面是守护进程的代码根目录；在服务器上它是解析后的压缩包
`<remote_root>/versions/<版本>.pyz`，这个文件从不修改，所以 shim 启动期间发生更新也不会让它的导入
失败（[[docs/install.zh-CN]]）。fork/exec 失败时作业以 `failed`
（`shim_start_failed`）结束。queued 作业如果已经有 `runner.json`（两次写入之间崩溃），记录中没有
执行身份时重新派发；有执行身份时作业变为 `unknown`，继续占用 GPU。

### Shim

1. 把 `package/` 复制到 `workdir/`，构造环境变量，执行启动前检查（venv 和必需路径存在，入口能
   解析且可执行，声明的 UUID 在 nvidia-smi 中可见）。检查失败 → `failed`，`error.kind=setup`，
   `return_code: null`。
2. fork；子进程调用 `setsid()` 后阻塞在管道上。shim 写入身份记录（`boot_id`、shim 的 pid 和
   启动时间、pgid、组长 pid 和启动时间、argv 哈希）并 fsync，然后放行子进程；子进程把所有被忽略
   的信号恢复为默认处理、清空信号屏蔽字，再 exec 入口命令。没有身份记录 ⇒ 入口命令从未执行。shim
   在这一步之前抛出异常时，作业以 `failed` 结束，`error.kind=shim_error`（已请求取消时为
   `cancelled`）；这不算中断，不会重跑。入口命令可能已经启动之后的错误交给恢复流程处理。
3. 轮询 `waitpid`，每隔 `control_poll_seconds` 重新读取 `job.json` 检查 `cancel_requested`，并
   执行 `timeout_seconds`（从本次启动开始计时）。
4. 入口进程退出后：写入 `exited/return_code/signal/ended_at`；停止进程组中残留的进程（警告
   `leftover_processes`）；合并 `result.json`；把 `runner.json` 标记为 final；更新 `job.json`。
5. GPU 忙：如果退出码等于 `gpu_busy_exit_code`（默认 75，`null` 表示关闭），并且不是因取消或超时
   而停止，第 4 步在清理残留进程后就停下，不做收尾。shim 在 `runner.json` 写入
   `status: "waiting_gpu"`、`gpu_busy_retries` 加 1 和 `last_gpu_busy_at`（退出字段保留到下一次
   身份记录），设置 `job.json` 的 `pending_reason`（如果作业因清理残留进程处于
   `stopping`/`leftover`，改回 `running`），然后等待 `gpu_retry_interval_seconds`，期间每隔
   `control_poll_seconds` 重新读取 `job.json`。入口进程从环境变量 `TASKMGR_GPU_BUSY_EXIT_CODE`
   得知这个退出码（约定关闭时不设置）。等待期间收到取消请求时，作业 running → stopping →
   cancelled（此时没有进程在运行）。等待结束后保留 `workdir/` 原样（按 GPU 约定，GPU 忙的那次
   启动没有做任何工作；每次重试都重新复制包只会白白消耗 I/O），向
   `stdout.log` 和 `stderr.log` 追加一行 `--- taskmgr: relaunch #N after GPU busy ---`，再按第 2 步
   重新启动入口命令（新的 pgid、组长、启动时间和身份记录）。等待时间不计入 `timeout_seconds`。
   某次重新启动持续运行满 `gpu_retry_interval_seconds` 后才删除 `pending_reason`（这样反复退出 75
   的作业不会在两种显示之间来回跳），作业结束时也会删除。重新启动时不再执行启动前检查。

### 停止流程

向进程组发送 SIGTERM → 等待 `stop_grace_seconds` → SIGKILL → 等待 `kill_confirm_seconds`。取消时
直接发送 SIGKILL；在超时或残留进程清理的等待期内到达的取消请求会结束等待，停止原因不变。只有
确认进程组中没有存活进程，才释放 GPU 并进入终态；否则作业变为 `unknown`，继续占用 GPU。进程组
成员通过 `/proc` 判断：pgid 等于记录值，且启动时间不早于组长的启动时间；僵尸进程视为已退出。
此外，`/proc/<pid>/environ` 中的 `TASKMGR_JOBID` 必须是本作业的：进程组全部退出后，它的 pgid
可能被另一个作业新建的会话重新使用。读不到标记的进程（environ 不可读，或任务清空了环境变量）仍算
成员，除非与 pgid 相同的那个 pid 现在属于记录的组长以外的进程；这时整个进程组都是新的。
