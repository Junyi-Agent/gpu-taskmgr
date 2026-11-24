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
2. `task create package` 写入一个有效的 `progress.json`（当前时间），并在 `task.json`（`gpus`、
   `environment.venv`、`result.required_outputs`）和 `TASK.md` 的每一节中放入 `<REQUIRED...>`
   标记；`task_id` 取自命令参数。`--dest` 指定目录（默认 `./<task_id>`）。
3. 不指定 `--submission-key` 时，CLI 生成 `cli-<32 位十六进制>`。
4. submission key 映射在重命名进 `queue/` 之前写入，两者都在状态锁下进行。如果映射指向的作业
   不存在（两步之间发生崩溃），该映射视为过期并被覆盖。
5. 对 running 作业执行 `task cancel job` 只设置 `cancel_requested`；shim（或接管期间的守护进程）在
   `control_poll_seconds` 内把它转为 `stopping` 并直接发送 SIGKILL，没有宽限期，入口进程没有机会做清理。入口进程退出之后才到达的取消请求不改变其结果。
   入口启动前就被发现的取消请求，按 running → stopping → cancelled 结束，入口命令不会执行。
6. 对已结束的作业，`task get job` 输出 `result.json`，并在顶层额外加上 `host`、`complete`、
   `retry_of` 和 `retried_as`。没有运行就结束的作业（在 queued 时被取消）输出一个合成的
   `{"schema_version", "_taskmgr": {...}}`。
7. `task wait job --timeout S` 总共只等 S 秒：每次状态查询（以及在所有服务器上查找 jobid）最多只用剩余
   的时间，所以不应答的服务器不会延长等待。超时后输出最后一次得到的状态对象（没有任何查询成功时为空），
   并附带 `error`（`wait_timeout`），退出码 2。如果截止时刻之后返回的查询结果是终态（或
   `unknown`），以该状态为准，退出码也按该状态。Ctrl-C 输出 `interrupted` 错误，退出码 2。
8. 协议兼容指 `protocol_version` 整数相等（当前为 3，`taskmgr-remote` 的命令和 `task` 一样是
   `<动词> <资源>`，见 [[docs/adr/0004-kubectl-style-cli]]；2 是 `jobs`、`status` 这类单词子命令；1 是
   基于 attempt 的旧协议）。协议不同的服务器应答 `protocol_mismatch`；协议 2 的服务器不认识新命令、
   报用法错误，客户端同样报 `protocol_mismatch`。错误信息给出 `task update host NAME`。不改变协议 3
   的新增功能通过应答中的 `features` 键声明（[[docs/task-package.zh-CN]]，服务器功能）；客户端在依赖某个功能之前先检查它。对
   `taskmgr-remote` 命令，ssh 退出码 255 视为连接失败。`task create host` 运行的 shell 脚本自己也
   可能以 255 退出，所以只有 stderr 看起来像 ssh 的错误（`ssh:`、`connection refused`、
   `permission denied (` 等）时，255 才算连接失败。ssh 以 `BatchMode=yes` 运行；机器条目没有设置时，
   还会加上 `ConnectTimeout=10`、`ServerAliveInterval=15` 和 `ServerAliveCountMax=3`（服务器不再
   响应约 45 秒后断开连接）。
9. 入口退出后清理残留进程时，作业转为 `stopping`，`stop_reason: "leftover"`；停止得到确认后按
   真实退出码收尾，未得到确认则作业进入 `unknown`（与取消/超时相同）。
10. `task resolve job` 时进程组仍有存活成员：如果 unknown 是因为检查无法判断（`/proc` 不可读、记录
    不完整）造成的，作业回到 running/stopping，由守护进程接管跟踪（情况 4.4）。如果 SIGKILL
    已经失败过（设置了 `stop_unconfirmed_reason`），作业保持 unknown，交给运维人员处理。对
    unknown 作业记录的取消请求，在停止得到确认后使作业变为 cancelled。
11. `_taskmgr.paths` 指向作业的最终位置（`done/...`）；运行期间 `TASKMGR_WORKDIR` 等变量
    指向 `in-progress/...`（作业结束时目录会移动）。
12. `task logs job --follow` 在以下情况结束：运行记录已 final、作业进入终态或 unknown；对 queued 作业
    先等待它被派发。
13. `task fetch job` 会跳过不安全的条目（任务创建的绝对路径或越界符号链接、特殊文件），打印警告并在
    `skipped` 中列出。特殊文件（FIFO、socket、设备）由服务器在打包时省略，并写进数据流的最后一个
    成员 `taskmgr-fetch.json`；客户端把这份清单合并进 `skipped`，不保留这个文件。下载 `job.json`、`runner.json`、三个日志和 `workdir/`，不含 `package/`、
    `package.tar.gz` 和 `shim.lock`。
14. `max_cpu_jobs` 统计处于 running、stopping 和 unknown 的仅 CPU 作业。
15. jobid 唯一性针对该服务器上所有作业目录检查。`create job` 在解包和计算哈希后、解析 GPU 和校验
    之前，就对已知的 submission key 作出响应（去重或冲突），因此即使机器上的 GPU 发生了变化，
    ACK 丢失后的重试也能成功。损坏的或不是普通文件的 `result.json` 保留为 `result.json.corrupt`
    （该名字被占用时用 `.corrupt.N`）。
16. `_taskmgr.host` 是远程机器的主机名。
17. 额外的配置键 `scheduler_poll_seconds`；额外的命令 `task get hosts`、`task update|delete|enable|disable|ping host`、`task get user`、`task get version`、`task update cli`。
18. 重试或自动重跑的作业有自己的 `submitted_at`（创建时间），所以 `task get jobs` 把它列为单独一行，
    排在被重跑的作业之后。它沿用旧作业已解析的 GPU UUID（或沿用 `gpu_request` 并重新挑选），不重新校验。queued 作业如果已经有带执行身份的
    `runner.json`（不应出现），它变为 `unknown`，`error.kind = "record_incomplete"`，不派发。
19. GPU 忙（desc-2 D17）：某次重新启动持续运行满 `gpu_retry_interval_seconds` 后才删除
    `pending_reason`，而不是一启动就删除，这样反复退出 75 的作业不会在 `running` 和
    `pending-gpu` 之间来回跳。退出 75 后清理残留进程留下的 `leftover_processes` 警告会保留到
    最终结果。重新启动时不再执行启动前检查。shim 在退出 75 与重新启动之间死亡时，`runner.json`
    保留为 `runner.prev-N.json`。`unknown` 作业即使是退出 75 也不会重新排队。`CUDA_VISIBLE_DEVICES`
    未设置时（在 taskmgr 之外运行），`claim_gpu()` 和为空时一样什么也不做。
20. 禁用的服务器（按用户决定，偏离 desc-1 §14）：不带 `--host` 时，`task get jobs` 和按 jobid 查找
    （以及 `get user`、`get version`）只询问已启用的服务器，所以宕机并用 `task disable host` 禁用的服务器
    不会再让命令等它超时。输出会列出被跳过的服务器；`--host` 仍然可以访问禁用的服务器。desc-1 原本
    也查询禁用的服务器，因为上面可能还有作业。
21. 命令行（按用户决定，偏离 desc-1）：采用 kubectl 风格的 `task <动词> <资源> [参数]`
    （`task create job PKG`、`task get jobs`、`task create host NAME`），job、host、package 也可以用复数名，
    输出格式用 `-o text|json`。使用 desc-1 的命令名是用法错误，错误信息给出新写法；
    [[docs/usage.zh-CN]]（命令变更）列出了对应关系。

## 限制

- 只管理任务所在的进程组。自行调用 `setsid()`/`setpgid()` 的进程会离开该进程组，taskmgr 不会
  跟踪或停止它。
- 不支持 MIG；taskmgr 本身不检测也不阻止 taskmgr 之外的进程使用 GPU。遵守 GPU 约定（退出 75，
  见 [[docs/task-package.zh-CN]]）的任务会等待这样的 GPU，而不是失败；不遵守的任务照旧失败。
- 因其他原因退出 75 的任务会被一直重新启动，直到被取消。
- `remote_root` 必须位于本地文件系统（flock 在 NFS 上不可靠）。
- 不支持断点续跑：自动重跑从原始任务包重新开始。
- 不安装任何软件或环境；venv 和所需路径必须已存在于服务器上。`task create host` 只安装 taskmgr
  本身（代码、unit、`remote_root`）。
- 对同一服务器用户的两次 `task create host` 会串行化：从 `ssh` 步骤结束到整次运行结束，服务器上有
  一个进程持有 `$XDG_RUNTIME_DIR/taskmgr-hostsetup-<uid>.lock`（没有 `XDG_RUNTIME_DIR` 时在
  `/tmp/` 下），锁被占用时第二次运行失败。`task create host` 以
  `<remote_root>/taskmgr-remote.pyz` 内记录的代码版本为准。
