---
title: 故障恢复
tags: [taskmgr, recovery, unknown]
lang: zh-CN
up: "[[taskmgr/README.zh-CN]]"
---

# 故障恢复

其他语言：[[docs/recovery]]

守护进程启动时先获取 `daemon.lock`，删除 `incoming/` 中能加锁且超过 1 小时的条目，然后在状态锁
下检查每个作业。对 shim 已死的 running/stopping 作业，同样的检查每隔 `control_poll_seconds` 也会
执行一次（假设：需求文档只要求在启动时检查）；`task resolve` 对 unknown 作业执行同样的检查。

1. `runner.json` 已 final → 恢复对应终态（如果 `result.json._taskmgr` 缺失，从 `runner.json`
   重新生成）；状态为 `interrupted` → 第 5 步。
2. 已记录 `exited` 但未 final → 完成收尾（残留进程检查、合并结果、标记 final），不重跑。
   例外是 GPU 忙：如果退出码是 `gpu_busy_exit_code`，且作业不是因取消或超时而停止，说明入口
   命令没有做任何工作。进程组全部退出后（若还有存活成员，先经过接管），作业以**同一个** jobid
   和 `enqueue_seq` 回到 `queued`：删除 `workdir/`，把 `runner.json` 重命名为
   `runner.prev-N.json`，清空 `dispatched_at`，保留日志。这不算中断：`auto_restarts` 不变，也不受
   `max_auto_restarts` 限制。若已请求取消，作业改为以 `cancelled` 结束。`runner.json` 状态为
   `waiting_gpu` 且 shim 已死（在等待重新启动时死亡，进程组此前已全部退出）时同样处理。
3. queued → 继续等待。queued 作业的 `runner.json` 如果记录的是这种 GPU 忙退出（重新排队过程中
   崩溃），派发时先把该记录移到一边，再正常派发。
4. running/stopping 且未记录 `exited`：
   1. `shim.lock` 被持有 → shim 还活着，不做处理。
   2. 没有身份记录 → 入口命令从未执行 → 第 5 步。
   3. `boot_id` 改变 → 机器重启导致中断 → 第 5 步。
   4. 同一次启动内，进程组仍有存活成员 → **接管**：守护进程轮询该进程组，按停止流程处理取消和
      超时（超时从 `started_at_epoch` 起算），进程组全部退出后作业以 `failed` /
      `exit_code_lost` 结束（若期间执行了停止，则按停止结果标记为 cancelled / timed_out /
      unknown）。
   5. 同一次启动内，没有存活成员 → `failed` / `exit_code_lost`，保留 result.json 中的业务字段；
      如果作业原先处于 stopping（或已请求取消）→ cancelled / timed_out。
   6. 无法判断（`/proc` 不可读、记录损坏或不完整）→ `unknown`。
5. 重跑：`runner.json` 标记为 `interrupted`；若 `cancel_requested` → cancelled；
   `auto_restarts` 已达 `max_auto_restarts` → `failed` / `too_many_interruptions`；否则作业以
   `failed` 结束，`error.kind = "interrupted"`，并用它的包的副本新建一个作业（自动重试）：新作业
   保留原 `enqueue_seq`，`auto_restarts` 加 1，`retry_of` 指向被中断的作业，被中断作业的
   `retried_as` 记下新 jobid。这一步中途崩溃时，会复用已经创建的重跑作业，不会再建第二个。
   `task status --wait` 会跟随这个重跑作业。

## 手动处理 `unknown`

`unknown` 作业不会自动重跑，也不会释放 GPU（GPU 忙退出后也一样：对这样的作业执行 resolve，会以
退出码 75 结束为 `failed`）。

1. `task resolve <jobid> --host H` 重新执行检查。如果进程组已全部退出，按上面的规则结束作业（已
   请求取消的标记为 cancelled）。如果仍有进程存活，或记录无法读取，作业保持 unknown，输出中会给出
   `identity`（`boot_id`、`pgid`、`leader_pid`、`leader_start_time`、`shim_pid` 等）和
   `alive_pids`。
2. 在服务器上排查：`ps -o pid,pgid,lstart,stat,cmd -g <pgid>`、
   `cat /proc/sys/kernel/random/boot_id`、`nvidia-smi`（查看该作业 GPU 上的进程）。
3. 杀掉残留进程：先 `kill -TERM -<pgid>`，再 `kill -KILL -<pgid>`。处于 `D` 状态的进程可能要
   先修复底层设备或驱动问题，或者重启机器。
4. 再执行一次 `task resolve <jobid> --host H`；或者在确认所有进程都已退出后执行
   `task resolve <jobid> --host H --as stopped --confirm <jobid>` → `failed`
   （`operator_marked_stopped`）或 `cancelled`；GPU 被释放，不会自动重跑。
5. 如需再次运行，执行 `task retry <jobid> --host H`（会创建一个新作业）。
