---
title: 假设与限制
tags: [taskmgr, assumptions, limitations]
lang: zh-CN
up: "[[taskmgr/README.zh-CN]]"
---

# 假设与限制

其他语言：[[docs/assumptions]]

## 假设（需求文档未规定之处）

1. 上传的归档可以把任务包放在根目录，也可以放在唯一的顶层目录中（例如
   `tar czf AA-01.tar.gz AA-01/`）。
2. `task init` 写入一个有效的 `progress.json`（当前时间），并在 `task.json`（`gpus`、
   `environment.venv`、`result.required_outputs`）和 `TASK.md` 的每一节中放入 `<REQUIRED...>`
   标记；`task_id` 取自命令参数。`--output` 指定目录（默认 `./<task_id>`）。
3. 不指定 `--submission-key` 时，CLI 生成 `cli-<32 位十六进制>`。
4. submission key 映射在重命名进 `queue/` 之前写入，两者都在状态锁下进行。如果映射指向的作业
   不存在（两步之间发生崩溃），该映射视为过期并被覆盖。
5. 对 running 作业执行 `task cancel` 只设置 `cancel_requested`；shim（或接管期间的守护进程）在
   `control_poll_seconds` 内把它转为 `stopping`。入口进程退出之后才到达的取消请求不改变其结果。
   入口启动前就被发现的取消请求，按 running → stopping → cancelled 结束，入口命令不会执行。
6. 对已结束的作业，`task status` 输出 `result.json`，并在顶层额外加两个键 `host` 和
   `complete`。没有任何 attempt 就结束的作业（在 queued 时被取消）输出一个合成的
   `{"schema_version", "_taskmgr": {...}}`。
7. `--wait --timeout` 超时后输出当前状态对象，并附带 `error`（`wait_timeout`），退出码 2。
   Ctrl-C 输出 `interrupted` 错误，退出码 2。
8. 协议兼容指 `protocol_version` 整数相等（当前为 1）。ssh 退出码 255 视为连接失败；ssh 以
   `BatchMode=yes` 运行。
9. 入口退出后清理残留进程时，作业转为 `stopping`，`stop_reason: "leftover"`；停止得到确认后按
   真实退出码收尾，未得到确认则作业进入 `unknown`（与取消/超时相同）。
10. `resolve` 时进程组仍有存活成员：如果 unknown 是因为检查无法判断（`/proc` 不可读、记录
    不完整）造成的，作业回到 running/stopping，由守护进程接管跟踪（情况 4.4）。如果 SIGKILL
    已经失败过（设置了 `stop_unconfirmed_reason`），作业保持 unknown，交给运维人员处理。对
    unknown 作业记录的取消请求，在停止得到确认后使作业变为 cancelled。
11. `_taskmgr.paths` 指向 attempt 的最终位置（`done/...`）；运行期间 `TASKMGR_WORKDIR` 等变量
    指向 `in-progress/...`（作业结束时目录会移动）。
12. `logs --follow` 在以下情况结束：attempt 已 final、作业进入终态或 unknown、或被更新的
    attempt 取代。
13. `fetch` 会跳过不安全的条目（任务创建的绝对路径或越界符号链接、特殊文件），打印警告并在
    `skipped` 中列出；下载内容包含 `shim.lock`。
14. `max_cpu_jobs` 统计处于 running、stopping 和 unknown 的仅 CPU 作业。
15. jobid 唯一性针对该服务器上所有作业目录检查。`receive` 在解包和计算哈希后、解析 GPU 和校验
    之前，就对已知的 submission key 作出响应（去重或冲突），因此即使机器上的 GPU 发生了变化，
    ACK 丢失后的重试也能成功。损坏的 `result.json` 保留为 `result.json.corrupt`（该名字被占用时
    用 `.corrupt.N`）。
16. `_taskmgr.host` 是远程机器的主机名。
17. 额外的配置键 `scheduler_poll_seconds`；额外的命令 `task hosts`。

## 限制

- 只管理任务所在的进程组。自行调用 `setsid()`/`setpgid()` 的进程会离开该进程组，taskmgr 不会
  跟踪或停止它。
- 不支持 MIG；不检测也不阻止 taskmgr 之外的进程使用 GPU。
- `remote_root` 必须位于本地文件系统（flock 在 NFS 上不可靠）。
- 不支持断点续跑：自动重跑从原始任务包重新开始。
- 不安装任何软件或环境；venv 和所需路径必须已存在于服务器上。
