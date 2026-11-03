---
title: CLI 用法
tags: [taskmgr, cli, usage]
lang: zh-CN
up: "[[taskmgr/README.zh-CN]]"
---

# CLI 用法

其他语言：[[docs/usage]]

```sh
task -h                                # 按分组列出命令（tasks / hosts / others）
task init AA-01                        # 生成 ./AA-01/ 模板；填写所有 <REQUIRED...> 标记
task validate AA-01/                   # 退出码 0 校验通过；1 列出所有错误；2 路径错误
task submit AA-01/                     # 在启用的服务器之间轮询
task submit --host nebius-h200-2 AA-01.tar.gz
task submit --host nebius-h200-2 --submission-key tune-aa01-v3 AA-01/   # 幂等重试
task jobs [--host H] [--state STATE] [-n N]   # STATE：queued、running、stopping、unknown 或某个终态；N 默认 10
task status <jobid> [--host H]
task status <jobid> [--host H] --wait [--poll-interval 5] [--retry-timeout 300] [--timeout S]
task logs    <jobid> [--host H] [--stream stdout|stderr|shim] [--follow]
task fetch   <jobid> [--host H] --output ./results/
task cancel  <jobid> [--host H]
task retry   <jobid> [--host H]        # 创建一个新作业，输出新的 jobid
task resolve <jobid> [--host H] [--as stopped --confirm <jobid>]
task host add NAME [--ssh USER@HOST] [--port N] [--identity-file F]
                   [--ssh-option KEY=VALUE ...] [--disabled | --enabled] [--pyz FILE]
                   [--yes] [--plan] [--prune-old-code]
task host list                         # 已配置的服务器
task host remove NAME [--yes]          # 从机器列表删除一台服务器
task host enable NAME                  # 重新使用一台服务器
task host disable NAME                 # 暂时不向这台服务器提交、也不查询它
task host ping [NAME]                  # 检查服务器是否有响应
task whoami                            # 每台服务器上 taskmgr 以哪个用户、哪台主机运行
task update                            # 更新本地 CLI（git pull --ff-only）
task version                           # 本地版本和每台服务器上的代码版本
```

除 `logs` 外，所有命令都支持 `--detail`；所有命令都支持 `--plain`。

## 输出

所有输出都带颜色：stdout 上的文本和 `--detail` JSON、stderr 上的错误和进度、`-h` 帮助和参数错误，
不论是否在终端里。`--plain`（放在命令行任意位置）或环境变量 `NO_COLOR` 关闭颜色。读取输出的脚本
使用 `--detail --plain`。

所有命令的输出只有以下几种形式：

| 形式 | 用于 | 颜色 |
| --- | --- | --- |
| 表格 | 列表（`jobs`、`retry`、`submit`、`host list`、`host ping`、`whoami`、`version`） | 表头粗体；STATE 和状态列按取值着色 |
| `key: value` 行 | 单个对象（`status`、`resolve`、`version` 的本地部分） | key 粗体；状态按取值着色 |
| `label: message` | 一行结果和提示 | label 绿色（完成）、黄色（提示、警告）、红色（失败）、青色（`hint`） |
| JSON（`--detail`） | 完整对象 | key 蓝色，字符串绿色，数字青色，`true`/`false`/`null` 品红 |

```
created: AA-01 in /work/AA-01
hint: fill every <REQUIRED...> marker, then run `task validate /work/AA-01`
```

不带 `--detail` 时，错误只在 stderr 输出一行 `error:`，行尾是错误码，stdout 为空：

```
error: path does not exist: AA-01 (not_found)
```

带 `--detail` 时，错误以 `{"error": {"code", "message", "host"?, "details"?}}` 输出到 stdout，
stderr 另有一行 `error:`。

`task -h` 和每个 `task <command> -h` 的格式相同：一行 `usage:`、一句说明、`commands` /
`arguments` / `options` 几节（粗体），名称在同一青色列中对齐，`-h, --help` 在最后，最后一行
指出下一步该看哪个帮助。

只给 `<jobid>` 就能找到作业：不带 `--host` 时，`status`、`logs`、`fetch`、`cancel`、`retry` 和
`resolve` 会向每台已启用的服务器查询这个 jobid（不查已禁用的服务器；找不到时错误信息会列出它们）。多台服务器上都有这个 jobid → `ambiguous`（退出码 2，
需加 `--host`）；没找到且有服务器连不上 → `incomplete`（退出码 2）。带 `--host` 时不做查询。

`task jobs` 输出表格（按用户决定，偏离需求文档 §16）：

```
JOBID             TASK_ID  SUBMITTED             HOST           STATE
b466fc71d0017505  GT-01    2026-10-29T18:55:33Z  nebius-h200-1  succeeded
```

- running 作业的入口进程因 GPU 被其他进程占用而退出 75、正在等待重新启动时，STATE 显示为
  `pending-gpu`（见 [[docs/task-package.zh-CN]] 的 GPU 约定）。它的状态仍是 `running`；
  `--detail` 和 `task status` 在 `pending_reason` 中给出原因。
- 带 `precompile` 的作业在不占 GPU 编译时显示 `precompiling`（状态是 `running`），等待 GPU 时显示
  `pending-gpu`（状态是 `queued`）。`--detail` 和 `task status` 给出 `phase`（见
  [[docs/task-package.zh-CN]] 的“预编译”）。
- `task jobs` 列出所查询的全部服务器合计最近提交的 10 个作业；`-n N`（正整数）修改数量。
  `--host` 和 `--state` 先过滤，`-n` 统计过滤后剩下的作业。`--detail` 返回同样的作业。
- 按提交时间（UTC）排序，最新的在最后。所有显示 STATE 的命令中，同一状态颜色相同：queued 和 `pending-gpu` 黄、
  running 和 `precompiling` 青、stopping 品红、unknown 粗体品红、succeeded 绿、failed / invalid_result / timed_out
  红、cancelled 灰。
- `--state` 只接受作业状态（`precompiling` 和 `pending-gpu` 只是显示，请用 `--state running` 或
  `--state queued`）；
  其他值是用法错误（退出码 2）。
- 连不上的服务器在 stderr 报告（`warning: H unreachable, ...`），被跳过的已禁用服务器也一样；
  加 `--detail` 时同样输出。至少一台服务器有响应、或机器列表为空时退出码为 0，否则为 2。
- 宕机的服务器不会卡住命令：ssh 连接 10 秒后放弃（`ConnectTimeout=10`；服务器在机器列表中自己的
  `-o ConnectTimeout=N` 优先）；向所有服务器发出的查询（`jobs`、按 jobid 查找、`whoami`、`version`）
  对每台服务器最多等 30 秒（用 `TASKMGR_QUERY_TIMEOUT=<秒数>` 修改）。对单台服务器的调用
  （`status`、`cancel`、`retry`、`resolve`、`status --wait` 的每次轮询）也受这个上限约束；上传
  （`submit`）、`logs` 和 `fetch` 不限时。
- `--detail` 改为输出 JSON 对象 `{"complete", "errors", "skipped", "jobs"}`。`errors` 的每一项有
  `host`、`error` 和 `code`：连不上服务器或没有按时响应时为 `connection_error`；连上了但远程命令
  没有返回 JSON（远程命令崩溃）时为 `remote_error`。

| 退出码 | 含义 |
| --- | --- |
| 0 | 成功；`--wait`：作业 succeeded；`jobs`：至少一台服务器有响应，或没有配置任何服务器 |
| 1 | `--wait`：作业以 failed / invalid_result / cancelled / timed_out 结束；`validate`：校验不通过 |
| 2 | 参数、连接、查询或操作错误；`jobs` 时所有服务器都失败 |
| 3 | `--wait` 发现作业处于 `unknown` |

以下 JSON 对象是 `--detail` 的输出。

提交回执：`{"host", "jobid", "task_id", "submission_key", "deduplicated", "content_hash",
"state"}`。如果提交结果不确定（连接失败，或没有收到有效回复：错误码 `remote_error`），stderr 会给出所选的服务器和
submission key，并打印重试命令 `task submit --host H --submission-key K PATH`；CLI 不会自动
改投其他服务器。任务包用到的功能（如 `precompile`）服务器的代码不支持时，`task submit` 在上传前以
`unsupported_feature` 拒绝（退出码 2），并提示运行 `task host add H`（见
[[docs/task-package.zh-CN]]，服务器功能）。

对未结束的作业，`task status` 输出
`{"host", "jobid", "task_id", "state", "pending_reason", "phase", "cancel_requested", "run": {status,
dispatched_at, started_at, exited, return_code, signal, takeovers, gpu_busy_retries,
last_gpu_busy_at, identity} 或 null, "progress":
<progress.json 或 null>, "submitted_at", "gpus", "error", "retry_of", "retried_as",
"cancel_request", "gpu_holders", "complete"}`。对已结束的作业，输出最终的 `result.json`，并在顶层加上
`host`、`complete`、`retry_of`、`retried_as` 和 `cancel_request`。

- `cancel_request`：`null`，或者有人请求取消该作业后为 `{"by", "at", "via"}`：`by` 是运行
  `task cancel` 的 CLI 的 `user@hostname`（`via: "cli"`）；在服务器上取消或由较旧的 CLI 取消时，是服务器
  上的用户和主机名（`via: "remote"`）；`at` 是第一次请求的 UTC 时间。文本输出显示为
  `cancelled_by: junyi@laptop at 2026-11-03T09:12:44Z`。
- `gpu_holders`：对等待 GPU 的作业（带 GPU 的 queued 作业、`pending-gpu`），列出此刻在它的 GPU 上持有
  CUDA context 的进程，来自服务器上的 `nvidia-smi --query-compute-apps` 和 `/proc`：`[{"gpu": <索引>,
  "uuid", "pid", "user", "command"（截断到 200 个字符）, "used_memory_mib", "taskmgr_jobid"?}]`
  （进程属于某个 taskmgr 作业时有 `taskmgr_jobid`）。没有进程占用时为 `[]`；不等待 GPU 的作业或没有
  `gpu_holders` 功能的服务器为 `null`。nvidia-smi 失败时 `gpu_holders` 为 `null`，`gpu_holders_error`
  说明原因。服务器的 `/proc` 看不到的 pid（其他 pid namespace 中的进程）`user` 和 `command` 为 `null`。
  文本输出每个占用者一行：

  ```
  state:          pending-gpu
  pending_reason: entry exited 75 (GPU busy); retry #12 in 1 s
  gpu_holder:     GPU 0: pid 81234 user root 40.0 GiB: python /root/xbr/mkc/dl/tools/gpu_exec.py ...
  ```

## 重试与重跑

一个作业最多运行一次。`task retry <jobid>`（只用于终态作业；`unknown` →
`resolve_required`，queued/running/stopping → `not_terminal`，退出码都是 2）用同一个包创建一个
**新作业**，排到队尾，输出表格 `JOBID RETRY_OF HOST STATE`；JSON 为 `{"jobid": <新>, "retry_of": <旧>, "state": "queued", "changed": true,
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
  `{"host", "jobid", "state", "partial", "path", "skipped"}`；`skipped` 列出每个没有写入的条目
  `{"path", "reason"}`（特殊文件、不安全的符号链接），每条也在 stderr 给出警告。

## 服务器

`task host add NAME --ssh USER@HOST` 通过 ssh 配置一台 GPU 服务器，并把它加入机器列表；
`task host add NAME` 检查一台已有的服务器并更新它（代码、`remote_root`、systemd 用户 unit 和
服务、linger、机器列表条目，以及删除 zipapp 之前的版本安装的代码目录）。每一处改动都会先显示，再用 `[y/N]` 询问；`--yes` 回答 yes，但删除旧版本的代码除外：它需要
`--prune-old-code`（或在终端里输入 `y`），否则报告 `kept`。`--plan` 输出这次运行会做的每处改动，不做任何改动
（退出码 0 没有要改的，1 有计划中的改动，2 某项检查失败）。
服务器已是最新状态时，再执行一次不做改动，也不询问。进度和询问写到 stderr；stdout 输出各步骤的
摘要（`--detail`：`{"host", "steps": [{"name", "status", "detail"}], "complete"}`），所有步骤都是
`ok`、`changed` 或 `kept` 时退出码为 0，否则为 2。各步骤见 [[docs/install.zh-CN]]。

- `task host list` 列出机器列表。
- `task host remove NAME` 从机器列表删除一台服务器，不改动服务器本身。
- `task host disable NAME` 把一台服务器标为禁用，例如它宕机时：轮询提交跳过它，向所有服务器发出的
  查询（`jobs`、按 jobid 查找、`whoami`、`version`）也不再问它，因此不用等它超时。输出会列出被跳过的
  服务器（stderr 上 `skipped: disabled, not queried: NAME`；JSON 中的 `"skipped"`）。不带 NAME 的
  `task host ping` 也跳过它。`--host NAME` 和 `task host ping NAME` 仍然可以访问它，服务器本身不做任何改动。`task host enable NAME`
  重新启用。两者都只修改 `machines.json` 中的 `enabled`。
- `task host ping [NAME]` 调用每台已启用的服务器（或指定的一台，无论是否禁用），显示是否有响应、
  往返耗时和已安装的代码版本；有服务器没响应时退出码为 2。禁用的服务器列为
  `skipped: disabled, not pinged: NAME`（JSON 中的 `"skipped"`）。

## whoami、update、version

- `task whoami [--host H]` 询问每台服务器，taskmgr 在上面以哪个用户、哪个主机名运行。
- `task update` 在 CLI 所在的本地仓库执行 `git pull --ff-only`。服务器上的代码不变，直到
  `task host add NAME` 安装新版本。从 `task.pyz` 运行时报 `not_a_checkout`（退出码 2），需要
  重新构建压缩包（`python3 -m taskmgr.pyz`，见 [[docs/install.zh-CN]]）。
- `task version [--host H]` 输出本地版本（pyproject 版本、git commit、代码版本、协议版本；从
  `task.pyz` 运行时，版本和 commit 是构建压缩包时的值，`dirty` 为 null）和每台
  服务器上已安装的代码版本，并标出 `up to date` 或 `differs`。代码版本就是 `task host add` 安装的
  哈希，`differs` 表示 `task host add NAME` 会上传新代码。
