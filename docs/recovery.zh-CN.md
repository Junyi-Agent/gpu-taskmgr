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
3. queued → 继续等待。
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
5. 重跑：attempt 标记为 `interrupted`；若 `cancel_requested` → cancelled；自动重跑次数已达
   `max_auto_restarts` → `failed` / `too_many_interruptions`；否则回到 queued 并保留原
   `enqueue_seq`，下一个 attempt 从 `package/` 重新开始。

## 手动处理 `unknown`

`unknown` 作业不会自动重跑，也不会释放 GPU。

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
5. 如需再次运行，执行 `task retry <jobid> --host H`。
