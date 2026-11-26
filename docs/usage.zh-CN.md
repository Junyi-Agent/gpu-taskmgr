---
title: CLI 用法
tags: [taskmgr, cli, usage]
lang: zh-CN
up: "[[taskmgr/README.zh-CN]]"
---

# CLI 用法

其他语言：[[docs/usage]]

命令由一个动词和一个资源组成，和 kubectl 一样：`task <动词> <资源> [参数]`。资源 `job`、`package`、
`host` 也有复数名（`jobs`、`packages`、`hosts`），两种写法都可以。`task -h` 列出所有
命令，`task <动词> -h` 列出一个动词的资源，`task <动词> <资源> -h` 给出参数和选项。

| 动词 | `job` | `package` | `host` |
| --- | --- | --- | --- |
| `create` | 把任务包提交为新作业 | 生成需要手工填写的模板 | 配置一台服务器（或把它更新到最新），并加入机器列表 |
| `get` | 列出所有服务器上的作业，或显示一个作业 | | 列出机器列表，或其中一项 |
| `update` | | | 把服务器的 taskmgr-remote 更新到本 CLI 的代码版本 |
| `delete` | | | 从机器列表删除一台服务器 |
| `build` | | 用项目文件和参数构建 | |
| `validate` | | 检查目录或归档 | |
| `wait` | 等待作业结束，退出码说明结果 | | |
| `logs` | 输出 stdout、stderr 或 shim 日志 | | |
| `fetch` | 下载作业的结果 | | |
| `cancel` | 取消作业 | | |
| `retry` | 把已结束的作业作为新作业再运行一次 | | |
| `resolve` | 处理 `unknown` 状态的作业 | | |
| `enable` / `disable` | | | 重新使用一台服务器 / 暂时不向它提交、也不查询它 |
| `ping` | | | 检查服务器是否有响应 |

其他命令：`task build pyz`（zipapp，见 [[docs/install.zh-CN]]）、`task get user`、`task get version`
和 `task update cli`（更新本地 CLI）。

```sh
task -h                                # 按分组列出命令（jobs / packages / hosts / others）
task create package AA-01 [--dest DIR] # 生成 ./AA-01/ 模板；填写所有 <REQUIRED...> 标记
task build package SRC... --task-id AA-01 --entry 'python3 run.py' [选项] [--dest DIR|FILE.tar.gz]
                                       # 用项目文件构建任务包；输出其路径
task validate package AA-01/           # 退出码 0 校验通过；1 列出所有错误；2 路径错误
task create job AA-01/                 # 在启用的服务器之间轮询
task create job AA-01.tar.gz --host gpu2
task create job AA-01/ --host gpu2 --submission-key tune-aa01-v3   # 幂等重试
task get jobs [--host H] [--state STATE] [-n N]   # STATE：queued、running、stopping、unknown 或某个终态；N 默认 10
task get job <jobid> [--host H]
task wait job <jobid> [--host H] [--poll-interval 5] [--retry-timeout 300] [--timeout S]
task logs job <jobid> [--host H] [--stream stdout|stderr|shim] [--follow]
task fetch job <jobid> [--host H] --dest ./results/
task cancel job <jobid> [--host H]
task retry job <jobid> [--host H]      # 创建一个新作业，输出新的 jobid
task resolve job <jobid> [--host H] [--as stopped --confirm <jobid>]
task create host NAME [--ssh USER@HOST] [--port N] [--identity-file F]
                      [--ssh-option KEY=VALUE ...] [--disabled | --enabled] [--pyz FILE]
                      [--yes] [--plan] [--prune-old-code]
task update host NAME [--pyz FILE] [--yes] [--plan] [--prune-old-code]
                                       # 更新机器列表中一台服务器的代码
task get hosts [NAME]                  # 已配置的服务器
task delete host NAME [--yes]          # 从机器列表删除一台服务器
task enable host NAME                  # 重新使用一台服务器
task disable host NAME                 # 暂时不向这台服务器提交、也不查询它
task ping host [NAME]                  # 检查服务器是否有响应
task build pyz [--dest DIR]            # task.pyz 和 taskmgr-remote.pyz（默认写到 checkout 里的 dist/）
task get user                          # 每台服务器上 taskmgr 以哪个用户、哪台主机运行
task get version                       # 本地版本和每台服务器上的代码版本
task update cli                        # 更新本地 CLI（git pull --ff-only）
```

所有命令都接受 `-o text|json`，放在命令之后（`task get jobs -o json`；默认 `text`）；`--plain` 可以
放在命令行任意位置。`task logs job` 无论哪种格式都输出原始日志文本；带 `-o json` 时错误输出为 JSON
对象。`--home DIR` 指定本地状态目录（默认 `$TASKMGR_HOME` 或 `~/.taskmgr`）。要写入的路径用
`--dest` 给出：`-o` 后面跟路径是用法错误，错误信息会说明这一点。

## 命令变更

以前的命令行是一组单独的动词；旧写法不再作为别名保留。使用旧命令名是用法错误（退出码 2），错误
信息给出新写法，例如 ``error: `task submit` is now `task create job PKG` ``。脚本需要这样修改：

| 旧写法 | 新写法 |
| --- | --- |
| `task submit PKG` | `task create job PKG` |
| `task jobs` | `task get jobs` |
| `task status ID` | `task get job ID` |
| `task status ID --wait` | `task wait job ID` |
| `task logs ID`、`task fetch ID`、`task cancel ID`、`task retry ID`、`task resolve ID` | `task logs job ID`、`task fetch job ID`、`task cancel job ID`、`task retry job ID`、`task resolve job ID` |
| `task fetch ID --output DIR` | `task fetch job ID --dest DIR` |
| `task init TASK_ID [-o DIR]` | `task create package TASK_ID [--dest DIR]` |
| `task pack SRC... [-o PATH]` | `task build package SRC... [--dest PATH]` |
| `task validate PATH` | `task validate package PATH` |
| `task host add NAME` | `task create host NAME` |
| `task host update NAME` | `task update host NAME` |
| `task host list` | `task get hosts` |
| `task host remove NAME` | `task delete host NAME` |
| `task host enable\|disable\|ping NAME` | `task enable\|disable\|ping host NAME` |
| `python3 -m taskmgr.pyz -o dist` | `task build pyz`（换目录用 `--dest DIR`） |
| `--detail` | `-o json`（默认是 `-o text`） |
| `-o PATH` / `--output PATH` | `--dest PATH` |
| `task whoami` | `task get user` |
| `task version` | `task get version` |
| `task update` | `task update cli` |

JSON 对象与以前 `--detail` 输出的相同。

## 输出

stdout 和 stderr 都是终端时，所有输出都带颜色：stdout 上的文本和 `-o json` JSON、stderr 上的错误和
进度、`-h` 帮助和参数错误。输出到管道或重定向到文件时不带颜色，脚本可以直接读 `-o json`。
`FORCE_COLOR=1` 强制开颜色；`--plain`（放在命令行任意位置）或环境变量 `NO_COLOR` 在任何情况下都关闭
颜色。

所有命令的输出只有以下几种形式：

| 形式 | 用于 | 颜色 |
| --- | --- | --- |
| 表格 | 列表（`get jobs`、`retry job`、`create job`、`get hosts`、`ping host`、`get user`、`get version`） | 表头粗体；STATE 和状态列按取值着色 |
| `key: value` 行 | 单个对象（`get job`、`wait job`、`resolve job`、`get version` 的本地部分） | key 粗体；状态按取值着色 |
| `label: message` | 一行结果和提示 | label 绿色（完成）、黄色（提示、警告）、红色（失败）、青色（`hint`） |
| JSON（`-o json`） | 完整对象 | key 蓝色，字符串绿色，数字青色，`true`/`false`/`null` 品红 |

```
created: AA-01 in /work/AA-01
hint: fill every <REQUIRED...> marker, then run `task validate package /work/AA-01`
```

`-o text` 时，错误只在 stderr 输出一行 `error:`，行尾是错误码，stdout 为空：

```
error: path does not exist: AA-01 (not_found)
```

带 `-o json` 时，错误以 `{"error": {"code", "message", "host"?, "details"?}}` 输出到 stdout，
stderr 另有一行 `error:`。

`task -h`、`task <动词> -h` 和 `task <动词> <资源> -h` 的格式相同：一行 `usage:`、一句说明、几个
粗体小节，名称在同一青色列中对齐（`task -h`：各命令分组和 `options`；动词：`resources` 和
`options`；命令：`arguments` 和 `options`），`-h, --help` 在最后，最后一行指出下一步该看哪个帮助。

只给 `<jobid>` 就能找到作业：不带 `--host` 时，`get job`、`wait job`、`logs job`、`fetch job`、
`cancel job`、`retry job` 和 `resolve job` 会向每台已启用的服务器查询这个 jobid（不查已禁用的服务器；
找不到时错误信息会列出它们）。多台服务器上都有这个 jobid → `ambiguous`（退出码 2，需加 `--host`）；
没找到且有服务器连不上 → `incomplete`（退出码 2）。带 `--host` 时不做查询。

`task get jobs` 输出表格（按用户决定，偏离需求文档 §16）：

```
JOBID             TASK_ID  SUBMITTED             HOST           STATE
b466fc71d0017505  GT-01    2026-10-29T18:55:33Z  gpu1           succeeded
```

- running 作业的入口进程因 GPU 被其他进程占用而退出 75、正在等待重新启动时，STATE 显示为
  `pending-gpu`（见 [[docs/task-package.zh-CN]] 的 GPU 约定）。它的状态仍是 `running`；
  `-o json` 和 `task get job` 在 `pending_reason` 中给出原因。
- 带 `precompile` 的作业在不占 GPU 编译时显示 `precompiling`（状态是 `running`），等待 GPU 时显示
  `pending-gpu`（状态是 `queued`）。`-o json` 和 `task get job` 给出 `phase`（见
  [[docs/task-package.zh-CN]] 的“预编译”）。
- `gpus` 为对象（启动时挑选 GPU）的作业，在没有满足请求的空闲 GPU 时显示 `pending-gpu`（状态是
  `queued`）；`pending_reason` 说明有几块合格，`task get job` 显示 `gpu_request`，选定之后显示 `gpus`
  （见 [[docs/task-package.zh-CN]] 的“按条件选择 GPU”）。
- `task get jobs` 列出所查询的全部服务器合计最近提交的 10 个作业；`-n N`（正整数）修改数量。
  `--host` 和 `--state` 先过滤，`-n` 统计过滤后剩下的作业。`-o json` 返回同样的作业。每台服务器
  只发回它最新的 N 个作业（服务器功能 `jobs_limit`）。`--state` 和 `-n` 只用于列表：给了 jobid 时使用它们是错误（`bad_args`）。
- 按提交时间（UTC）排序，最新的在最后。所有显示 STATE 的命令中，同一状态颜色相同：queued 和 `pending-gpu` 黄、
  running 和 `precompiling` 青、stopping 品红、unknown 粗体品红、succeeded 绿、failed / invalid_result / timed_out
  红、cancelled 灰。
- `--state` 只接受作业状态（`precompiling` 和 `pending-gpu` 只是显示，请用 `--state running` 或
  `--state queued`）；其他值是用法错误（退出码 2）。
- 连不上的服务器在 stderr 报告（`warning: H unreachable, ...`；协议版本不同的服务器：
  `warning: H needs an update, ...`），被跳过的已禁用服务器也一样；
  加 `-o json` 时同样输出。至少一台服务器有响应、或机器列表为空时退出码为 0，否则为 2。
- 宕机的服务器不会卡住命令：ssh 连接 10 秒后放弃（`ConnectTimeout=10`；服务器在机器列表中自己的
  `-o ConnectTimeout=N` 优先）；向所有服务器发出的查询（`get jobs`、按 jobid 查找、`get user`、`get version`）
  对每台服务器最多等 30 秒（用 `TASKMGR_QUERY_TIMEOUT=<秒数>` 修改）。对单台服务器的调用
  （`get job`、`cancel job`、`retry job`、`resolve job`、`wait job` 的每次轮询）也受这个上限约束；
  上传（`create job`）、`logs job` 和 `fetch job` 不限时。
- `-o json` 改为输出 JSON 对象 `{"complete", "errors", "skipped", "jobs"}`。`errors` 的每一项有
  `host`、`error` 和 `code`：连不上服务器或没有按时响应时为 `connection_error`；连上了但远程命令
  没有返回 JSON（远程命令崩溃）时为 `remote_error`。

| 退出码 | 含义 |
| --- | --- |
| 0 | 成功；`wait job`：作业 succeeded；`get jobs`：至少一台服务器有响应，或没有配置任何服务器 |
| 1 | `wait job`：作业以 failed / invalid_result / cancelled / timed_out 结束；`validate package`、`build package`：校验不通过 |
| 2 | 参数、连接、查询或操作错误；`get jobs` 时所有服务器都失败；`wait job`：`--timeout` 到期或连不上 |
| 3 | `wait job` 发现作业处于 `unknown` |

以下 JSON 对象是 `-o json` 的输出。

提交回执（`task create job`）：`{"host", "jobid", "task_id", "submission_key", "deduplicated",
"content_hash", "state"}`。如果提交结果不确定（连接失败，或没有收到有效回复：错误码
`remote_error`），stderr 会给出所选的服务器和 submission key，并打印重试命令
`task create job PATH --host H --submission-key K`；CLI 不会自动改投其他服务器。任务包用到的功能
服务器的代码不支持时，`task create job` 在上传前以 `unsupported_feature` 拒绝
（退出码 2），错误信息以 ``run `task update host H` to update it`` 结尾（见
[[docs/task-package.zh-CN]]，服务器功能）。

对未结束的作业，`task get job` 输出
`{"host", "jobid", "task_id", "state", "pending_reason", "phase", "cancel_requested", "run": {status,
dispatched_at, started_at, exited, return_code, signal, takeovers, gpu_busy_retries,
last_gpu_busy_at, identity} 或 null, "progress":
<progress.json 或 null>, "submitted_at", "gpus", "error", "retry_of", "retried_as",
"gpu_request", "cancel_request", "gpu_holders", "complete"}`。对已结束的作业，输出最终的 `result.json`，并在顶层加上
`host`、`complete`、`retry_of`、`retried_as`、`gpu_request` 和 `cancel_request`。

- `cancel_request`：`null`，或者有人请求取消该作业后为 `{"by", "at", "via"}`：`by` 是运行
  `task cancel job` 的 CLI 的 `user@hostname`（`via: "cli"`）；在服务器上取消或由较旧的 CLI 取消时，是服务器
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

## 打包：`task build package`

`task create package` 生成一个需要手工填写的模板。`task build package` 则用项目里的文件和命令行参数
构建任务包，校验它，并在 stdout 上只输出它的路径，因此可以和 `task create job` 组合使用：

```sh
pkg=$(task build package kernels/ tools/gpu_exec.py src/moe_kernel.py=kernels.py \
        --task-id MO-01 --entry 'python3 run.py --spec spec/moe.yaml' \
        --precompile 'python3 precompile.py kernels.py' --precompile-timeout 1800 \
        --with-cute-helpers --gpus '{"count": 1, "min_free_gb": 40}' \
        --venv /opt/venvs/tune --env CUTE_DSL_ARCH=sm_90a \
        --output-key best_config --output-key speedup \
        --task-md notes/MO-01.md --dest /tmp/MO-01.tar.gz) && task create job "$pkg"
```

源（`SRC`，任意多个）：

| 形式 | 放到哪里 |
| --- | --- |
| 目录 `d/` | 它的内容放到包的根目录（`d/x.py` 变成 `x.py`） |
| 文件 `f.py` | 包的根目录，名为 `f.py` |
| `SRC=DEST` | 包内的 `DEST`（文件或目录；`DEST` 必须在包内） |

符号链接会被跟随：包里放的是内容，因此不依赖项目目录。`--exclude GLOB`（可重复）在复制目录时略过匹配
GLOB 的名字。两个源写同一个文件会报错（`conflict`）。打包本来就会略过的名字（`__pycache__`、`.git`、
`*.pyc` 等，见 [[docs/task-package.zh-CN]]）不会被打包。

然后：

- `task.json`：源里有 `task.json` 时以它为基础；否则由参数生成，此时必须给 `--task-id` 和 `--entry`。
  参数设置或扩展它：`--task-id`、`--entry CMD` 和 `--precompile CMD`（按 shell 的规则拆分，但不经过
  shell 执行；`--precompile-timeout SECONDS`）、`--gpus`（`none`、`0,1`、`GPU-...`，或 JSON，如 `'[0, 1]'`
  或 `'{"count": 1, "min_free_gb": 40}'`；默认 none）、`--venv PATH`、`--require-path PATH`、
  `--env NAME=VALUE`（合并进 `env`）、`--timeout SECONDS`、`--output-key KEY`（追加到
  `result.required_outputs`）。可重复的参数可以给多次。
- `TASK.md`：`--task-md FILE`，否则用源里的 `TASK.md`，否则生成一个简短的占位文件，写明源和入口命令。
- `progress.json`：源里的，否则生成一个 `not_started` 的（`updated_at` 固定，相同输入得到相同的任务包和内容哈希）。
- `taskmgr_task.py` 总是从当前运行的 taskmgr 的模板复制（源里有不同的副本时会被替换，并给出警告）。
  `--with-cute-helpers` 还会复制 `precompile.py` 和 `cutecache.py`（见 [[docs/task-package.zh-CN]] 的“预编译”）。
- 结果按 `task validate package` 的规则校验。无效时：退出码 1，错误写到 stderr，不写任何文件，stdout 为空。
- `--dest PATH`：目录，或以 `.tar.gz` / `.tgz` 结尾的文件（与 `task create job` 打包目录的方式相同，
  逐字节可复现）。默认 `./<task_id>`。PATH 已存在时报错（`exists`，退出码 2）。
  `--force` 只替换以前生成的包（带 `task.json` 的目录、`.tar.gz` / `.tgz` 文件，或符号链接，只删链接本身）；
  目标是当前目录或它的上级、某个 SRC、或包含某个 SRC 的目录时拒绝（`bad_args`，什么都不删）。

退出码 0 有效（stdout：路径），1 无效，2 参数或源有误。`-o json` 输出
`{"valid", "path", "archive", "task_id", "content_hash", "features", "source_files", "warnings"}`
（无效时：`{"valid": false, "errors", "path": null}`）。`features` 是任务包需要的服务器功能（见
[[docs/task-package.zh-CN]] 的“服务器功能”）。

## 重试与重跑

一个作业最多运行一次。`task retry job <jobid>`（只用于终态作业；`unknown` →
`resolve_required`，queued/running/stopping → `not_terminal`，退出码都是 2）用同一个包创建一个
**新作业**，排到队尾，输出表格 `JOBID RETRY_OF HOST STATE`；JSON 为 `{"jobid": <新>, "retry_of": <旧>, "state": "queued", "changed": true,
"enqueue_seq", "host"}`。旧作业的状态、日志和结果保持不变；它的 `retried_as` 列出新作业的
jobid，新作业的 `retry_of` 指回旧作业。用原来的 submission key 重新提交，仍然返回原作业的 jobid。

运行被中断（重启，或入口命令从未启动）时，作业以 `failed` 结束，`error.kind = "interrupted"`，
并按同样方式创建一个自动重跑作业，保留原来的队列位置（见 [[docs/recovery.zh-CN]]）。
`task wait job` 会跟随这种自动重跑：在 stderr 打印
`job X was interrupted; following automatic re-run Y`，然后继续等待 Y；输出和退出码以最后一个
作业为准。手动重试不会被跟随。

## 日志与下载

- `task logs job` 输出作业的日志（`--stream`，默认 stdout）。`--follow` 持续输出，直到运行记录变为
  final 或作业进入终态或 unknown；对 queued 作业会先等它开始运行。从未运行的作业 → 错误
  `no_run`（退出码 2）。
- `task fetch job` 把作业的运行文件（`job.json`、`runner.json`、`stdout.log`、`stderr.log`、
  `shim.log`、`workdir/`；不含 `package/`、`package.tar.gz`、`shim.lock`）下载到
  `<dest>/<host>/<jobid>/`（必须给 `--dest DIR`）。目标已存在 → `exists`（退出码 2），不写任何文件。从未运行的作业 →
  `no_run`（退出码 2）。作业未结束时 stderr 给出警告，回执中 `partial: true`。回执：
  `{"host", "jobid", "state", "partial", "path", "skipped"}`；`skipped` 列出每个没有写入的条目
  `{"path", "reason"}`（特殊文件、不安全的符号链接），每条也在 stderr 给出警告。

## 服务器

`task create host NAME --ssh USER@HOST` 通过 ssh 配置一台 GPU 服务器，并把它加入机器列表；
`task create host NAME` 检查一台已有的服务器并更新它（代码、`remote_root`、systemd 用户 unit 和
服务、linger、机器列表条目，以及删除 zipapp 之前的版本安装的代码目录）。每一处改动都会先显示，再用 `[y/N]` 询问；`--yes` 回答 yes，但删除旧版本的代码除外：它需要
`--prune-old-code`（或在终端里输入 `y`），否则报告 `kept`。`--plan` 输出这次运行会做的每处改动，不做任何改动
（退出码 0 没有要改的，1 有计划中的改动，2 某项检查失败）。
服务器已是最新状态时，再执行一次不做改动，也不询问。进度和询问写到 stderr；stdout 输出各步骤的
摘要（`-o json`：`{"host", "steps": [{"name", "status", "detail"}], "complete"}`），所有步骤都是
`ok`、`changed` 或 `kept` 时退出码为 0，否则为 2。各步骤见 [[docs/install.zh-CN]]。

`task update host NAME` 对机器列表中已有的服务器执行同样的步骤，条目保持不变（不接受 ssh 选项；要改
用 `task create host`）：上传与本 CLI 代码版本相同的 `taskmgr-remote.pyz`（先用 `task build pyz`
构建），检查作业目录，用新代码重启服务（正在运行的作业继续运行），再检查它是否有响应。它和
`task create host` 一样接受 `--pyz FILE`、`--yes`、`--plan` 和 `--prune-old-code`，已禁用的服务器
也能更新，名字不在列表里时以 `unknown_host` 失败。在 `task update cli` 或修改代码之后、
`task get version` 显示 `differs` 时，或 `task create job` 报 `unsupported_feature` 时使用它。

- `task get hosts [NAME]` 列出机器列表（或其中一项）：名字、是否启用、ssh 目标、`remote_root`。
- `task delete host NAME` 从机器列表删除一台服务器，不改动服务器本身。
- `task disable host NAME` 把一台服务器标为禁用，例如它宕机时：轮询提交跳过它，向所有服务器发出的
  查询（`get jobs`、按 jobid 查找、`get user`、`get version`）也不再问它，因此不用等它超时。输出会列出被跳过的
  服务器（stderr 上 `skipped: disabled, not queried: NAME`；JSON 中的 `"skipped"`）。不带 NAME 的
  `task ping host` 也跳过它。`--host NAME` 和 `task ping host NAME` 仍然可以访问它，服务器本身不做
  任何改动。`task enable host NAME` 重新启用。两者都只修改 `machines.json` 中的 `enabled`。
- `task ping host [NAME]` 调用每台已启用的服务器（或指定的一台，无论是否禁用），显示是否有响应、
  往返耗时和已安装的代码版本；有服务器没响应时退出码为 2。禁用的服务器列为
  `skipped: disabled, not pinged: NAME`（JSON 中的 `"skipped"`）。

## get user、get version、update cli

- `task get user [--host H]` 询问每台服务器，taskmgr 在上面以哪个用户、哪个主机名运行。
- `task update cli` 在 CLI 所在的本地仓库执行 `git pull --ff-only`。服务器上的代码不变，直到
  `task update host NAME` 安装新版本；拉取到新提交时，输出最后一行是
  ``hint: servers keep their code until `task update host NAME` (see `task get version`)``。
  从 `task.pyz` 运行时报 `not_a_checkout`（退出码 2），需要在 checkout 里重新构建压缩包
  （`task build pyz`，见 [[docs/install.zh-CN]]）。
- `task get version [--host H]` 输出本地版本（pyproject 版本、git commit、代码版本、协议版本；从
  `task.pyz` 运行时，版本和 commit 是构建压缩包时的值，`dirty` 为 null）和每台
  服务器上已安装的代码版本，并标出 `up to date` 或 `differs`。代码版本就是 `task create host` 和
  `task update host` 安装的哈希，`differs` 表示 `task update host NAME` 会上传新代码，此时输出最后
  一行是 ``hint: `task update host NAME` installs the local version on a server``。
