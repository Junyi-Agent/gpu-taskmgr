# 一致性审查

审查对象：`review/consistency` 分支，基于提交 `070f3a2`。范围是代码、变量、注释、文档、配置和工具链版本之间的一致性。bug、帮助文本、测试质量和文档漂移由其他 agent 负责，这里不报告。

优先级：**高**＝同一名字或同一错误在用户可见处有不同含义；**中**＝维护时容易改漏，或读者会被误导；**低**＝风格问题，顺手统一即可。

标注"持久化格式"的条目会改动 `job.json`、`result.json`、`config.json` 或 `--detail` 输出。这类改名要同时考虑 `SCHEMA_VERSION`、`PROTOCOL_VERSION` 和已有服务器上的数据，不能当作单纯重命名。

## 建议的统一术语

| 概念 | 统一用词 | 中文 | 不再使用 |
| --- | --- | --- | --- |
| 任务定义（目录或归档） | task、task package | 任务、任务包 | 单独的 package（指 Python 包时） |
| 服务器上的一次提交及其运行 | job | 作业 | 用 task 指作业 |
| 作业运行时的进程 | entry process、job's processes | 入口进程、作业的进程 | 用 task 指进程 |
| 远程机器 | server；CLI 和 JSON 的既有名字 `--host`、`host` 保留，含义固定为机器列表中的名字 | 服务器 | machine（`machine list` 除外） |
| 机器的主机名 | hostname | 主机名 | 用 `host` 表示主机名 |
| 每次运行的监管进程 | shim；文件名 `runner.json` 保留 | shim | runner、supervisor（指进程时） |
| 调度进程 | daemon | 守护进程 | |
| GPU 忙退出后同一作业重新启动 | relaunch | 重新启动 | retry |
| 中断后新建作业 | automatic re-run | 自动重跑 | automatic retry、restart |
| 用户发起新建作业 | retry | 重试 | |
| `.pyz` 内容的哈希 | code version | 代码版本 | 单独的 version |

## 1. 概念命名

### 1.1 `host` 一词有三种含义（高，持久化格式）

- `_taskmgr.host` 是 `socket.gethostname()` 的结果（`taskmgr/store.py:398`）。
- 同一个 `task status` 输出的顶层 `host` 是机器列表中的名字（`taskmgr/taskmgr.py:298`）。
- `ssh.host` 是连接地址（`taskmgr/records.py:231`）。而 `version` 回复已经把主机名单独叫 `hostname`（`taskmgr/remote.py:558`）。

所以一个已结束作业的 `--detail` 输出里，`host` 和 `_taskmgr.host` 的值可以不同。

**建议**：向 `hostname` 这一边统一，把 `_taskmgr.host` 改为 `_taskmgr.hostname`。顶层 `host` 固定表示机器列表名字。`ssh.host` 是配置文件的键，保留。

### 1.2 同一组服务器在 JSON 和错误信息里有两个名字（中，持久化格式）

- `task host list --detail` 的键是 `servers`（`taskmgr/hostcmds.py:149`），`task host ping --detail` 的键是 `hosts`（`taskmgr/hostcmds.py:263`）。
- 错误码 `no_enabled_hosts` 配的文字是 "no enabled server"（`taskmgr/taskmgr.py:86`），`unknown_host` 配的文字是 "host %r is not in the machine list"（`taskmgr/taskmgr.py:79`）。
- 代码里也混用：`HostError` 描述的是 unreachable server（`taskmgr/transport.py:16`）；`query_hosts` 内部调用 `query_targets(servers, ...)`（`taskmgr/taskmgr.py:266`）。

**建议**：错误信息和说明文字一律写 server。错误码已是对外接口，保留。`ping` 的 `hosts` 改名价值不高，可以留到下次协议变更时并入 `servers`。

### 1.3 task 同时指任务和运行中的进程（中）

- `taskmgr/monitor.py:290` 写 "the task ended while its supervisor was gone"，`taskmgr/monitor.py:426` 写 "the task lives on"。
- `taskmgr/daemon.py:376` 写 "running shims and tasks are left alone"，`taskmgr/hostsetup.py:877` 写 "running tasks keep running"。
- `taskmgr/package.py:565` 写 "a running task may still be changing"。
- 同一件事在别处写成 job 或 entry：`docs/install.md:98` 同一格里并列 "running tasks keep running" 和 "a running job's shim"；`taskmgr/shim.py` 全程用 entry。

**建议**：向 job 和 entry 这一边统一。task 只指任务定义，进程写 entry process 或 "the job's processes"。

### 1.4 shim、runner、supervisor 三个词指同一进程（中）

- shim：模块名和绝大多数代码。
- runner："set by the runner"（`taskmgr/template/taskmgr_task.py:4`）、"the runner owns it"（`taskmgr/template/taskmgr_task.py:11`）、"(set by the runner)"（`taskmgr/package.py:170`）、"merged by the runner"（`docs/task-package.md:48`）。
- supervisor：`taskmgr/shim.py:1`、`taskmgr/shim.py:478`、`taskmgr/monitor.py:290`。

`runner` 还是文件名和类型名（`runner.json`、`Runner`），而这个文件也会由 daemon 写入。

**建议**：进程一律叫 shim，文件仍叫 `runner.json`，内容称为 run record。`taskmgr_task.py` 的三份副本要一起改（见 5.1）。对任务作者的文字可以写 taskmgr，不必出现 shim。

### 1.5 retry、re-run、restart、relaunch 互相混用（高，部分为持久化格式）

- GPU 忙退出的同一事件，同一函数里连写两次，用词不同：日志 "relaunch #%d"（`taskmgr/shim.py:324`），`pending_reason` "retry #%d"（`taskmgr/shim.py:325`）。文档示例跟随后者（`docs/internals.md:58`），分隔行用 relaunch（`taskmgr/shim.py:183`）。
- 键名 `gpu_busy_retries`（`taskmgr/records.py:155`）和 `gpu_retry_interval_seconds`（`taskmgr/common.py:64`）里的 retry 也指 relaunch，和 `task retry` 无关。
- 中断后新建作业：`taskmgr/records.py:76` 写 automatic re-runs，`taskmgr/store.py:536` 和 `docs/recovery.md:45` 写 automatic retry，配置键写 `max_auto_restarts`（`taskmgr/common.py:58`），作业字段写 `auto_restarts`（`taskmgr/records.py:67`）。

**建议**：先改非持久化部分：`pending_reason` 改用 relaunch（并同步 `docs/internals.md:58`），文字里的 automatic retry 改为 automatic re-run。`max_auto_restarts`、`gpu_retry_interval_seconds` 是用户配置键，改名需要旧键兼容，建议等 schema 变更时再做，先在 `docs/install.md` 的配置表说明它们的含义。

### 1.6 package 同时指任务包和 Python 包（低）

`taskmgr/package.py:1` 处理任务包，`PACKAGE_DIR` 和 `copy_package_tree` 处理 Python 包（`taskmgr/common.py:32`、`taskmgr/common.py:221`）。

**建议**：Python 侧向已有的 `CODE_ROOT` 靠拢，改为 `CODE_DIR`、`copy_code_tree`。

### 1.7 version 指代不明（中）

- `HostSetup.version` 是 code version（`taskmgr/hostsetup.py:294`），`task version` 的 `local.version` 是 pyproject 版本（`taskmgr/taskmgr.py:639`）。
- hostsetup 的用户文字写 "version %s"（`taskmgr/hostsetup.py:613`），也写 "code version %s"（`taskmgr/hostsetup.py:175`）。
- 文档里 `up_to_date`（JSON 键，`docs/install.md:57`）和 `up to date`（文本输出，`docs/usage.md:198`）描述同一个显示。

**建议**：变量改名为 `code_version`，用户文字写全 code version。`docs/install.md:57` 及中文版改写 `up to date`，因为那里说的是默认文本输出。

### 1.8 root 和 home 的写法（低）

- `remote_root` 的别名有 `--root`、storage root（`taskmgr/hostcmds.py:50`）、`<root>`（`docs/install.md:224`）。
- `HostSetup.home` 是本地状态目录（`taskmgr/hostsetup.py:284`），`p_home()` 是服务器的 `$HOME`（`taskmgr/hostsetup.py:456`）。

**建议**：文字统一为 `remote_root`，`--root` 是参数名保留。`HostSetup.home` 改为 `local_home`，`p_home` 改为 `server_home`。

### 1.9 `jobid` 与 `task_id` 写法不对称，局部变量缩写不统一（低）

- `jobid`、`JOBID_RE`、`TASKMGR_JOBID` 无下划线；`task_id`、`TASK_ID_RE`、`TASKMGR_TASK_ID` 有下划线。这是需求文档定下的外部接口，不建议改。
- 局部变量并存 `jobid`/`jid`（`taskmgr/daemon.py:127`）和 `jobdir`/`jd`（`taskmgr/remote.py:235`），结果键又是 `job_dir`（`taskmgr/store.py:402`）。
- `st` 同时指 Store（`taskmgr/daemon.py:124`）、status 回复（`taskmgr/taskmgr.py:339`）、`os.lstat` 结果（`taskmgr/package.py:222`）和 wait status（`taskmgr/shim.py:386`）。

**建议**：局部变量统一为 `jobid`、`jobdir`；`st` 只保留给 stat 结果，其余写 `store`、`status`。

### 1.10 同一字段名或同一概念有不同形状（低，持久化格式）

- `state`（`job.json`，`taskmgr/records.py:59`）和 `_taskmgr.status`（`taskmgr/records.py:102`）存同一个终态；`runner.json` 的 `status`（`taskmgr/records.py:129`）取值范围不同。`render.py` 只能把 `status` 读出来显示成 state。
- `gpus` 在 `task.json` 是索引或 UUID 列表（`taskmgr/records.py:176`），在 `job.json` 是 `GpuDecl` 列表（`taskmgr/records.py:65`）。
- `error` 有三种形状：`{kind, message}`（`taskmgr/records.py:45`）、`{code, message}`（`taskmgr/common.py:88`）和字符串（`taskmgr/transport.py:20`）。
- `complete` 在查询里表示所有服务器都回答了（`taskmgr/taskmgr.py:269`），在 `host add` 里表示全部步骤成功（`taskmgr/hostsetup.py:398`）。
- `return_code` 是 JSON 键，错误 kind 和配置键写 exit code（`exit_code`、`gpu_busy_exit_code`）。

**建议**：先不改键名。以后调整 schema 时，`_taskmgr.status` 向 `state` 统一，`HostError.error` 改名 `message`，以便和 `code` 配对。

## 2. 错误信息

### 2.1 同一校验在本地和远程给不同错误码（中）

- `--submission-key` 格式错误：本地 `bad_args`（`taskmgr/taskmgr.py:182`），远程 `bad_submission_key`（`taskmgr/remote.py:99`）。
- task_id 格式错误：`task init` 是 `bad_task_id`（`taskmgr/taskmgr.py:109`），校验问题码是 `task_id`（`taskmgr/package.py:102` 附近）。
- GPU 声明错误：包校验是 `gpu` 或 `schema`（`taskmgr/package.py:118`），服务器解析是 `invalid_gpus`（`taskmgr/gpu.py:75`）。

**建议**：`bad_submission_key` 向 `bad_args` 统一。问题码和错误码是两层，可以不同，但在 `docs/task-package.md` 列出问题码的取值。

### 2.2 同一件事的文字不同（低）

| 主题 | 一方 | 另一方 | 建议 |
| --- | --- | --- | --- |
| 协议不匹配 | "client protocol %s is incompatible with remote protocol %s"（`taskmgr/remote.py:628`） | "remote protocol_version %r is incompatible with local %d"（`taskmgr/transport.py:342`） | 用前者的说法 |
| `--confirm` | "must repeat the same jobid"（`taskmgr/taskmgr.py:511`） | "must repeat the jobid %s"（`taskmgr/remote.py:351`） | 用后者，带上 jobid |
| 无运行记录 | "has not run (nothing to show)"（`taskmgr/remote.py:404`） | "has not run (%s); nothing to fetch"（`taskmgr/taskmgr.py:538`） | 共用一条，状态放括号 |
| 目标已存在 | "target %s already exists; nothing was written"（`taskmgr/taskmgr.py:112`） | "target directory already exists: %s"（`taskmgr/taskmgr.py:545`） | 用后者的结构 |
| 包大小超限 | "upload exceeds %d bytes (max_package_bytes %d)"（`taskmgr/remote.py:179`） | "extracted size exceeds max_package_bytes (%d)"（`taskmgr/package.py:511`）、"package files total %d bytes, over max_package_bytes (%d)"（`taskmgr/package.py:308`） | 都写成 "<对象> %d bytes exceeds max_package_bytes (%d)" |
| GPU 字符串错误 | `%r`（`taskmgr/package.py:123`） | `%s`（`taskmgr/gpu.py:66`） | 共用校验（见 3.4） |

### 2.3 消息里抄写正则（中）

`^[A-Z]{2}-[0-9]{2,4}$` 和 `^[A-Za-z0-9._-]{1,128}$` 以字面量写在 `taskmgr/taskmgr.py:109`、`taskmgr/taskmgr.py:182`、`taskmgr/package.py:102`、`taskmgr/remote.py:99`，和 `taskmgr/common.py:37`、`taskmgr/common.py:38` 的实际正则相互独立，而且结尾是 `$`，实际是 `\Z`。

**建议**：在 `common.py` 定义 `TASK_ID_PATTERN` 这类文本常量，编译和消息都用它。

### 2.4 时长的写法有四种（低）

`"%g s"`（`taskmgr/shim.py:324`）、`"%gs"`（`taskmgr/transport.py:277`）、`"%.0fs"`（`taskmgr/taskmgr.py:421`、`taskmgr/hostsetup.py:890`）、`"%ss"`（`taskmgr/taskmgr.py:469`）。

**建议**：统一为无空格的 `%gs`。`pending_reason` 的 `"%g s"` 同时改，并同步 `docs/internals.md:58`。

### 2.5 占位符的称呼（低）

- `<REQUIRED...>`：用户提示和文档（`taskmgr/render.py:66`、`docs/usage.md:14`）。
- `<REQUIRED>`：校验错误（`taskmgr/package.py:266`）。
- "REQUIRED marker (angle-bracket placeholder)"：`taskmgr/template/TASK.md:5`。

**建议**：统一为 "`<REQUIRED...>` marker"，变量名 `PLACEHOLDER` 可保留。

## 3. 模块间重复的 helper

### 3.1 同名不同义（高）

`_s` 在 `hostcmds.py` 里默认值是空字符串（`taskmgr/hostcmds.py:270`），在 `render.py` 里是 `cell` 的别名，默认值是 `-`（`taskmgr/render.py:30`）。同一个名字在两个渲染模块里输出不同。

**建议**：去掉两个别名，直接用 `cell(v, default)`。

### 3.2 服务器查找写了多遍（中）

- `find_server`（`taskmgr/taskmgr.py:75`）和 `_find`（`taskmgr/hostcmds.py:166`）逻辑相同；`_must_have`（`taskmgr/hostcmds.py:177`）再抄一次同样的错误。
- `_is`（`taskmgr/hostcmds.py:173`）已存在，但 `set_flag` 又内联了一次（`taskmgr/hostcmds.py:191`）。

**建议**：`transport.py` 提供 `find_server`，其余调用它；`set_flag` 用 `_is`。

### 3.3 JSON 取值 helper 语义各异（中）

| helper | 位置 | 非目标类型时 |
| --- | --- | --- |
| `as_dict` | `taskmgr/textout.py:149` | 返回 `{}` |
| `_as_dict` | `taskmgr/store.py:293` | 返回 `None` |
| `json_obj` | `taskmgr/transport.py:88` | 抛 `TypeError` |
| `json_str` | `taskmgr/transport.py:95` | 返回调用方给的默认值 |
| `_str` | `taskmgr/hostsetup.py:1095` | 返回 `""` |

**建议**：集中到 `records.py`，用名字区分语义，例如 `dict_or_empty`、`dict_or_none`、`require_dict`、`str_or`。`render.py` 把 `json_str` 也叫 `_str`，改用新名字时一并消除。

### 3.4 校验和默认值重复（中）

- 布尔排除的整数判断：`_is_int`（`taskmgr/package.py:59`）、`taskmgr/store.py:205`、`taskmgr/store.py:247`、`_type_ok`（`taskmgr/store.py:55`）。
- GPU 声明格式：`taskmgr/package.py:118`-`123` 和 `taskmgr/gpu.py:57`-`66`。
- `.get("enabled", True)` 在 `taskmgr.py`、`hostcmds.py` 共 6 处（如 `taskmgr/taskmgr.py:84`、`taskmgr/hostcmds.py:152`）。
- `transport` 默认值写法也不同：`.get("transport", "ssh") == "local"`（`taskmgr/transport.py:194`）和 `.get("transport") != "local"`（`taskmgr/transport.py:200`）。
- `machines.json` 解析：`load_machines`（`taskmgr/transport.py:120`、`:130`）和 `edit_machines`（`taskmgr/transport.py:381`、`:387`）。
- 并发度 `min(16, len(servers))`：`taskmgr/transport.py:356`、`taskmgr/hostcmds.py:260`。

**建议**：增加 `is_int`、`server_enabled(s)`、`is_local(s)`、一个解析 `servers` 数组的内部函数和 `MAX_PARALLEL_HOSTS` 常量。GPU 格式校验放到 `gpu.py`，`package.py` 调用，错误码和格式由一处决定。

### 3.5 状态和目录名字面量（中）

- `("running", "stopping")` 出现 10 次（`taskmgr/daemon.py:133`、`:337`，`taskmgr/monitor.py:112`、`:184`、`:243`、`:502`，`taskmgr/shim.py:138`、`:292`、`:336`、`:471`）。`GPU_HOLDING_STATES`（`taskmgr/common.py:42`）多一个 `unknown`，读者容易把两者混淆。
- 目录名分散在 `JOB_DIRS`（`taskmgr/common.py:53`）、`ACTIVE_JOB_DIRS`（`taskmgr/store.py:42`）和正则（`taskmgr/hostsetup.py:824`）。
- 日志流 `("stdout", "stderr", "shim")`：`taskmgr/cliparse.py:341`、`taskmgr/remote.py:589`，另有 `cmd_logs` 的映射。

**建议**：`common.py` 增加 `SUPERVISED_STATES`、`LOG_STREAMS`；`ACTIVE_JOB_DIRS` 移到 `common.py`；正则由 `JOB_DIRS` 生成。

### 3.6 文件名和默认值常量（中）

- `.taskmgr-code-version`：`pyz.VERSION_FILE`（`taskmgr/pyz.py:29`）存在，但 `taskmgr/remote.py:552` 写字面量。
- `.taskmgr-commit`：`pyz.COMMIT_FILE`（`taskmgr/pyz.py:30`）存在，但 `taskmgr/taskmgr.py:635` 写字面量。
- `hostlegacy.LEGACY_MARKER`（`taskmgr/hostlegacy.py:24`）同值。它描述旧布局，可以独立。
- ssh 默认值：`taskmgr/hostsetup.py:51`、`:52` 写 `ServerAliveInterval=15`、`ConnectTimeout=10`；`taskmgr/transport.py:26`、`:30` 有常量 `CONNECT_TIMEOUT`、`SERVER_ALIVE_INTERVAL`。
- `schema_version` 写成字面量 1（`taskmgr/daemon.py:231`、`taskmgr/taskmgr.py:124`），其余地方用 `SCHEMA_VERSION`。
- code version 的截断长度 `[:12]` 分散各处，`[:16]`（`taskmgr/hostsetup.py:466`）另有含义，没有命名常量；`hostserver.py` 的 `{16}` 也靠人工保持一致。

**建议**：一律引用常量。`remote.py` 引入 `pyz` 需要确认没有循环导入和启动开销，也可以把这两个文件名下移到 `common.py`。截断长度定义为 `SHORT_VERSION_LEN = 12` 和 `ARCHIVE_VERSION_LEN = 16`。`hostserver.py` 必须独立运行，它的 `{16}` 可改由 probe 参数传入。

### 3.7 输出 helper 重复（低）

- `Ui.say`（`taskmgr/prompt.py:28`）和 `textout.say`（`taskmgr/textout.py:64`）实现相同，直接委托即可。
- `taskmgr/taskmgr.py:210` 用 `note("hint", CYAN, ...)` 手写了 `hint()` 的格式。
- `.tmp-` 前缀：`taskmgr/package.py:33` 有 `EXCLUDED_PREFIXES`，`taskmgr/package.py:595` 仍写字面量。
- 排除列表三份：`package.py` 的 `EXCLUDED_*`、`taskmgr/pyz.py:32`、`taskmgr/taskmgr.py:113`。用途不同，保留，但 `taskmgr.py` 的那份可以命名。

### 3.8 config 读取方式不统一（低）

`Store.config` 已经合并默认值并做了类型检查，多数地方用 `cfg["key"]`（如 `taskmgr/shim.py:317`），`taskmgr/store.py:246` 和 `taskmgr/store.py:545` 用 `.get` 并重复写默认值 75 和 2，`taskmgr/remote.py:105` 和 `:109` 用无默认值的 `.get`。

**建议**：统一为 `cfg["key"]`，删除重复默认值。

### 3.9 `traceback` 导入位置（低）

`taskmgr/daemon.py:8` 在模块顶层导入，`taskmgr/shim.py:488`、`taskmgr/remote.py:647`、`taskmgr/taskmgr.py:769` 在函数内导入。

**建议**：都移到模块顶层。

## 4. 日志和输出格式

### 4.1 `host add` 失败时 stderr 打印 `done:`（中）

失败时 stderr 打印红色的 `done: NOT complete, see the steps`（`taskmgr/hostsetup.py:400`），紧接着 stdout 打印 `incomplete: host X is NOT set up ...`（`taskmgr/hostcmds.py:314`）。而 `textout.message` 的文档说每个单行输出都是 `label: text`，label 表示结果。

**建议**：stderr 也用 `incomplete:`，并通过 `textout.message` 输出。

### 4.2 警告前缀（低）

`host add` 的警告写成大写 `WARNING:`（`taskmgr/hostsetup.py:528`、`:563`、`:879`），其余警告是 `warning:`（`taskmgr/textout.py:87`）。

**建议**：向小写 `warning:` 统一。

### 4.3 远程 JSON 错误有三条路径（中）

- 常规路径 `emit` 带 `remote_protocol_version()`（`taskmgr/remote.py:84`）。
- 流式命令的错误路径写 `PROTOCOL_VERSION`（`taskmgr/remote.py:640`、`:654`），绕过了 `TASKMGR_TEST_PROTOCOL_VERSION`。
- `serve` 重复启动时手写一段 JSON，没有 `protocol_version`（`taskmgr/daemon.py:354`），而 `taskmgr/remote.py:5` 说 `serve` 不输出 JSON。

**建议**：抽出 `error_payload()`，三条路径共用；`serve` 的情况同步修改文件头 docstring。

### 4.4 回复里的 `ok` 只有部分命令有（低）

`receive` 和 `version` 带 `"ok": True`（`taskmgr/remote.py:153`、`:199`、`:554`），其他 JSON 子命令没有。客户端只在 `_strip` 里把它去掉（`taskmgr/taskmgr.py:251`），需求文档也没有定义它。

**建议**：删除 `ok`。这是协议回复的变动，但客户端不读它，风险很小。

### 4.5 时间戳（低）

| 用途 | 格式 | 位置 |
| --- | --- | --- |
| 记录 | `2026-11-01T00:00:00Z` | `taskmgr/common.py:96` |
| 备份目录 | UTC `20261101T000000Z` | `taskmgr/hostsetup.py:133` |
| 上传目录 | **本地时间** `20261101000000` | `taskmgr/remote.py:102` |

**建议**：上传目录改用 UTC 紧凑格式，把 `utc_stamp` 移到 `common.py`。

### 4.6 JSON 写入格式（低）

`atomic_write_json` 排序键；`taskmgr init` 的 `progress.json`（`taskmgr/taskmgr.py:122`）不排序，也不走原子写；`edit_machines`（`taskmgr/transport.py:389`）和 `config_text`（`taskmgr/hostsetup.py:216`）也不排序。

**建议**：`cmd_init` 改用 `atomic_write_json`。后两处保持键序是有意的，加注释说明。

### 4.7 `pyz` 构建失败的输出（低）

`build failed: %s`（`taskmgr/pyz.py:196`）既不是 `error:` 格式，也不是 JSON。

**建议**：用 `textout.message("error", ...)`。

### 4.8 GPU busy 的写法（低）

用户文字有 `GPU busy`、`gpu busy`（`taskmgr/textout.py:27`）和 `GPU-busy`（文档）。

**建议**：显示状态 `pending (gpu busy)` 已是固定输出，保留；其余文字用 `GPU busy`，作定语时写 `GPU-busy`。

## 5. 模板、`taskmgr_task.py` 与配置键

### 5.1 三份 `taskmgr_task.py` 当前相同（说明）

`taskmgr/template/`、`taskmgr/examples/succeed/`、`taskmgr/examples/multi-gpu/` 的 `taskmgr_task.py` 内容一致（md5 相同）。本报告涉及它的修改要同时改三份。

### 5.2 退出码合法范围不一致（中）

服务器接受任意整数 `gpu_busy_exit_code`（`taskmgr/store.py:247`），并原样导出为 `TASKMGR_GPU_BUSY_EXIT_CODE`（`taskmgr/shim.py:75`）；`taskmgr_task.py` 只接受 1 到 255，否则退回 75（`taskmgr/template/taskmgr_task.py:160`）。配置为 300 时，两边对"GPU 忙"的退出码理解不同。

**建议**：向 1 到 255 这一边统一，在 `Store.config` 校验并以 `bad_config` 报错。

### 5.3 固定写 75 与可配置退出码（低）

`gpu_busy()` 用配置的退出码，但下列文字写死 75：`taskmgr/template/taskmgr_task.py:237`、`taskmgr/template/run.py:8`、`taskmgr/examples/multi-gpu/run.py:11`、`taskmgr/shim.py:181`、`taskmgr/template/TASK.md:17`。

**建议**：代码注释写 "the GPU-busy exit code"。`TASK.md` 面向任务作者，写 "75（服务器默认值）"。

### 5.4 示例包和模板细节不同（低）

- 初始 `progress.json`：`task init` 有 `"message": "not started"`（`taskmgr/taskmgr.py:124` 起），示例包没有（`taskmgr/examples/succeed/progress.json:1`）。
- `taskmgr/examples/succeed/run.py` 结束时不写 `write_progress("done", ...)`，`taskmgr/template/run.py:13` 和 `taskmgr/examples/multi-gpu/run.py:32` 都写。

**建议**：都向模板看齐。

### 5.5 `python` 与 `python3`（中）

模板和示例的 `entry.argv` 是 `python3`（`taskmgr/template/task.json:4`、`taskmgr/examples/succeed/task.json:4`），文档示例是 `python`（`docs/task-package.md:26`、`:92`，`docs/task-package.zh-CN.md:26`、`:86`）。入口进程的默认 `PATH` 里不一定有 `python`。

**建议**：文档向 `python3` 统一。

### 5.6 配置示例中的名字（低）

`taskmgr/deploy/machines.example.json` 和 `docs/usage.md` 用 `nebius-h200-*`，`taskmgr/README.md`、`docs/install.md`、`docs/testing.md` 用 `gpu1`，`taskmgr/deploy/taskmgr-remote.user.service:5` 用 `SERVER`。

`docs/install.md:197`-`202` 的手工步骤包含 `config.json`，`taskmgr/deploy/taskmgr-remote.user.service:4`-`13` 的手工步骤没有。

**建议**：示例统一用 `gpu1`。unit 文件的手工步骤删去，改为指向 `docs/install.md`，避免两份步骤继续分叉。

## 6. `TASKMGR_*` 环境变量

### 6.1 仅测试用的变量没有 `TASKMGR_TEST_` 前缀（中）

`TASKMGR_NVIDIA_SMI`（`taskmgr/gpu.py:13`）、`TASKMGR_BOOT_ID_FILE`（`taskmgr/procutil.py:13`）、`TASKMGR_SHIM_EXECUTABLE`（`taskmgr/daemon.py:270`）的注释都写着 test-only，但和其他 `TASKMGR_TEST_*` 命名不同，`docs/testing.md` 的同一张表里也混排。

**建议**：改为 `TASKMGR_TEST_NVIDIA_SMI`、`TASKMGR_TEST_BOOT_ID_FILE`、`TASKMGR_TEST_SHIM_EXECUTABLE`，同步 `taskmgr/tests/helpers.py`、`taskmgr/tests/test_exec.py` 和两份 testing 文档。`desc-1` 用的是旧名，需要在 `desc-2` 的偏离表里记一笔。

## 7. docstring、注释与代码

| 位置 | 写的是 | 实际 | 建议 |
| --- | --- | --- | --- |
| `taskmgr/hostlegacy.py:3`、`:32` | `hostprobe.legacy` | 没有 `hostprobe` 模块，函数是 `hostserver.legacy` | 改为 `hostserver.legacy` |
| `taskmgr/hostsetup.py:88` | "What PROBE reports" | 没有 `PROBE`，是 `hostserver.probe` | 改为 `hostserver.probe` |
| `taskmgr/hostsetup.py:75` | `taskmgr.Args` | `Args` 在 `cliparse.py` | 改为 `cliparse.Args` |
| `taskmgr/hostcmds.py:1` | `task host add\|list\|remove\|ping` | 还有 `enable`、`disable` | 补全 |
| `taskmgr/hostcmds.py:3` | "Interface used by taskmgr.py" | `add_parsers` 由 `cliparse.py` 调用，`render` 由 `render.py` 调用 | 写明实际调用方 |
| `taskmgr/records.py:194` | `taskmgr validate` | 命令是 `task validate` | 改正 |
| `taskmgr/remote.py:550`、`:551` | 版本文件由 `task host add` 写入 | 只有 `pyz.stage` 写；`task host add` 只上传 | 改为 "written by the .pyz build" |
| `taskmgr/remote.py:5` | `serve` 不输出 JSON | 重复启动时输出（见 4.3） | 随 4.3 修改 |
| `taskmgr/monitor.py:43` | `identity` 在 `record_incomplete` 时没有 | `:347` 的 `record_incomplete` 分支带 `identity`，只有 runner 不可读的分支不带 | 改为 "except an unreadable runner.json" |
| `taskmgr/hostserver.py:5` | "annotations only as strings" | `str`、`int`、`bool` 未加引号（如 `:46`、`:71`、`:110`）。在任意 Python 3 上都合法，规则写得比实际严 | 改为 "annotations that need newer syntax only as strings" |
| `taskmgr/monitor.py:416` | "as before" | 引用已不存在的历史写法 | 删去 |
| `taskmgr/taskmgr.py:3`-`5` | 本地状态只列 `machines.json`、`rr.json` | 还有 `machines.lock`、`rr.lock` | 补全 |

## 8. 工具链

### 8.1 版本：一致

下列项目已核对，没有不一致：

- Python：`.python-version`、`requires-python`、ruff `py310`、basedpyright `3.10`、`hostsetup.MIN_PYTHON`、文档都是 3.10。
- `uv_build>=0.10.6,<0.11.0` 和 `uv-pre-commit` 的 `rev: 0.10.6` 一致。
- `uv.lock` 的 ruff 0.17.3、basedpyright 1.40.2 等于 `pyproject.toml` 的下限；项目版本 0.1.0 一致。
- `.pre-commit-config.yaml` 的命令和 `docs/development.md` 的表格一致。

### 8.2 pre-commit 锁定了两个版本（低）

`uv.lock:131` 是 4.6.2（Python < 3.11），`uv.lock:150` 是 4.7.0（Python >= 3.11）。`docs/development.md` 说 `uv.lock` 锁定开发工具，但没有说明版本随 Python 变化。`.python-version` 固定 3.10 时实际只用 4.6.2。

**建议**：不必改动锁文件，文档加一句说明即可。

### 8.3 "whole project" 的范围不一致（中）

`docs/development.md:39` 说 basedpyright 检查 whole project，但 `pyproject.toml:52` 只包含 `taskmgr`。ruff 则通过 `pyproject.toml:31` 覆盖 `bin/*`。`bin/` 下的脚本无扩展名，basedpyright 不会检查它们，所以加进 `include` 也不解决问题。

**建议**：文档改为 "the whole `taskmgr/` package"，中文版同步。

### 8.4 `noqa` 写法（低）

- `SIM115` 已在 `pyproject.toml:43` 全局忽略，`taskmgr/package.py:607` 的 `# noqa: SIM115` 多余。
- 理由的分隔符有 ` -- `（同一行）和 ` - `（如 `taskmgr/daemon.py:383`），也有不写理由的 `# noqa: BLE001`（如 `taskmgr/daemon.py:67`）。

**建议**：删除多余的 `noqa`，统一为 `# noqa: CODE - reason`。

### 8.5 安装说明的 "without pip"（低）

`docs/install.md:18` 写 "or, without pip"，中文版 `docs/install.zh-CN.md:18` 相同，但上一行用的是 `uv tool install`。

**建议**：改为 "or, without uv"。

## 9. 英文文档与 `*.zh-CN.md`

8 对文档和 README 的章节结构、代码块数量、表格行数都能对应。下列是内容或用词的差异。

### 9.1 内容不对应（中）

| 英文 | 中文 | 问题 | 建议 |
| --- | --- | --- | --- |
| `docs/install.md:162` 标题列出 `list`、`enable`、`disable`、`remove`、`ping` | `docs/install.zh-CN.md:148` 只有 `list`、`remove`、`ping` | 中文标题少两项 | 向英文统一 |
| `docs/assumptions.md:100` 有 "(an installed archive is never modified...)" | `docs/assumptions.zh-CN.md:84` 没有 | 中文缺这句 | 补上 |
| `docs/assumptions.md:28` 写 "two added top-level keys"，随后列了四个 | 中文 `docs/assumptions.zh-CN.md:25` 没写数量，列了四个 | 英文自相矛盾 | 删去 "two" |
| `docs/task-package.md:37` | `docs/task-package.zh-CN.md:36`-`37` 出现 "必须必须" | 中文重复 | 删一个 |

### 9.2 中文用词不统一（低）

| 概念 | 并存写法 | 位置 | 建议 |
| --- | --- | --- | --- |
| job | 作业、任务 | `docs/testing.zh-CN.md:38`、`:39` 把 job 译成任务，其余文档都是作业 | 作业 |
| daemon | 守护进程、daemon | `docs/install.zh-CN.md:55`、`:56`，`docs/testing.zh-CN.md:31` | 守护进程 |
| code version | code version、代码版本 | `docs/install.zh-CN.md:52`、`:61`、`:88`，`docs/testing.zh-CN.md:74` | 代码版本 |
| answer | 应答、响应 | `docs/install.zh-CN.md:23`，`docs/usage.zh-CN.md:33`，`taskmgr/README.zh-CN.md:52` | 响应 |
| checkout | checkout、本地仓库、代码目录 | `docs/usage.zh-CN.md:173`，`docs/testing.zh-CN.md:23` | checkout |
| 引号 | `"..."`、`“...”` | `docs/testing.zh-CN.md:38`、`:62` | `“...”` |

### 9.3 文档内链接和抬头（低）

- 链接目标的写法：`[[agent/desc-1.md]]` 带扩展名（`taskmgr/README.md:10`），`[[docs/install]]` 不带（`taskmgr/README.md:19`）。
- 语言切换：README 用 `English | 简体中文`，`docs/` 用 `Other language:`。
- `docs/*.md` 有 frontmatter，`taskmgr/README*.md` 没有。
- 根目录 `README.md` 只有中文。

**建议**：链接一律不带扩展名。语言切换用 `English | 简体中文` 一种写法。根 README 面向 agent 和本项目作者，保持现状。

### 9.4 文档占位符（低）

- `--identity-file` 的值：`FILE`（`taskmgr/hostcmds.py:57`）、`PATH`（`docs/install.md:74`）、`F`（`docs/usage.md:27`）。
- `--host` 的值：`H`（`docs/usage.md:20`）、`NAME`（`docs/usage.md:182`）；`--host` 没有 metavar（`taskmgr/cliparse.py:294`），`--help` 显示 `HOST`。
- 秒数：`--timeout S`（`docs/usage.md:21`），`--help` 显示 `SECONDS`（`taskmgr/cliparse.py:319`）。
- 服务器上的诊断命令：`ps -o pid,pgid,lstart,cmd`（`taskmgr/remote.py:393`）、`ps -o pid,pgid,lstart,stat,cmd`（`docs/recovery.md:72`）。

**建议**：文档向 `--help` 的写法统一：`FILE`、`HOST`、`SECONDS`。`ps` 命令保留 `stat`，因为恢复步骤要看 `D` 状态。

## 10. 引用格式（低）

代码注释用 `Section 12.2` 这类写法（共 27 处，集中在 `shim.py`、`package.py`、`monitor.py`、`store.py`、`remote.py`），也用 `desc-2 D18`（如 `taskmgr/hostsetup.py:1`）。文档还用 `Section 4 of the spec`（`docs/install.md:33`）、`spec §16`（`docs/usage.md`）、`desc-1 §14`（`docs/assumptions.md`）。`Section N` 没有写是哪份文档，而 `desc-1`、`desc-2` 都有编号内容。

**建议**：统一为 `desc-1 §12.2` 和 `desc-2 D18`。
