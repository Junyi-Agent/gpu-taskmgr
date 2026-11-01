# CLI 结构与帮助文本审阅

审阅基线：`070f3a2`。目标是缩短帮助、统一用词，并保留操作所需的限制和默认值。本报告不修改实现；以下英文建议均可作为对应帮助字符串直接替换，结构调整另行标明。

## 范围与结论

实际以 `python3 bin/task … --help`、`python3 bin/taskmgr-remote … --help` 递归遍历解析器注册的全部命令：task 顶层、14 个一级命令、6 个 host 子命令，共 21 页；remote 顶层及 12 个子命令，共 13 页。34 次均退出 0。对照 `taskmgr/taskmgr.py`、`taskmgr/cliparse.py`、`taskmgr/hostcmds.py`、`taskmgr/remote.py`，并以无网络、无部署操作的参数错误检查核实表现。

现有树基本合理，不建议增加 job 分组或迁移现有命令。没有命令别名；`-h/--help`、`-y/--yes` 是正常的选项别名。`host add --enabled/--disabled` 用于配置时一并设置状态，`host enable/disable` 用于单独切换，保留两种入口即可。`whoami`、`version`、`host ping` 虽共享远端 version 查询，但分别回答身份、版本一致性、连通性，不能仅因实现重用而合并。远端是协议和服务入口，与本地同名命令不算多余别名，尤其 receive/fetch 有二进制流语义。

优先处理顶层选项误导、查询范围表述、互斥参数 usage；其次删减 host 长描述与通用页脚。远端帮助当前过少，应补最小语义，而非继续删短。

## 逐项建议

### 1. 顶层只列 host 入口

位置：`taskmgr/cliparse.py:126`、`taskmgr/taskmgr.py:706`。

当前：顶层将 `host add/list/remove/enable/disable/ping` 六行全部展开，`task host --help` 再列一遍；host 的注册文本为 `manage the machine list`。

建议：顶层 hosts 区改为一行 `host  manage servers`，对应注册文本替换为 `manage servers`；删除顶层展开逻辑，完整子命令留在 `task host --help`。目前顶层 36 行，并非失控，但这能直接减少 5 行且减少两处同步阅读。

### 2. 顶层选项必须说明放置位置

位置：`taskmgr/cliparse.py:131`、`taskmgr/cliparse.py:133`、`taskmgr/cliparse.py:137`、`taskmgr/cliparse.py:264`、`taskmgr/cliparse.py:276`。

当前：`--detail  print the full JSON object instead of text (all but logs)`；`--home DIR  local state directory (default $TASKMGR_HOME or ~/.taskmgr)`；usage 为 `task <command> [args]`。

问题：实测 `task --detail jobs` 退出 2，报 `unrecognized arguments: --detail`；detail 实际属于叶子命令，home 只属于顶层。

建议替换顶层文案：

- usage：`task [--home DIR] [--plain] <command> [args]`
- detail：`JSON output; place after the leaf command (except logs)`
- home：`local state directory; before command (default: $TASKMGR_HOME or ~/.taskmgr)`

同时让顶层错误 usage（`taskmgr/cliparse.py:247`）使用相同文字。此建议仅澄清当前语法，无需增加全局 detail 支持。

### 3. 缩短公共选项与重复页脚

位置：`taskmgr/cliparse.py:66`、`taskmgr/cliparse.py:67`、`taskmgr/cliparse.py:140`、`taskmgr/cliparse.py:209`。

当前 → 建议：

- `print the full JSON object instead of text` → `output JSON`
- `no colors (also NO_COLOR=1)` → `disable color (or set NO_COLOR=1)`
- `` `task <command> -h` shows a command's arguments and options. `` → `` See `task <command> --help`. ``
- `` `%s <subcommand> -h` shows a subcommand's arguments and options. `` → `` See `%s <subcommand> --help`. ``
- 叶子页脚 `` `task -h` lists all commands. ``（`taskmgr/cliparse.py:211`）→ 空字符串，叶子页不再显示返回顶层的通用提示。

注册时共用 help/description（`taskmgr/cliparse.py:283`）本身没有导致同一页重复输出，不建议为消除源码字符串重用而拆开。也没有设置 argparse epilog；目前重复来自自绘 footer。

### 4. 顶层命令摘要少说参数已表达的内容

位置及当前 → 建议：

| file:line | 当前文本 | 可直接替换文本 |
| --- | --- | --- |
| `taskmgr/taskmgr.py:688` | `check a task package directory or tar.gz` | `validate a task package` |
| `taskmgr/taskmgr.py:692` | `submit a task package to a server` | `submit a task package` |
| `taskmgr/taskmgr.py:693` | `list the jobs on every server` | `list jobs on enabled servers` |
| `taskmgr/taskmgr.py:695` | `show one job, or wait for it to end` | `show or wait for a job` |
| `taskmgr/taskmgr.py:697` | `print a job's stdout, stderr or shim log` | `print job logs` |
| `taskmgr/taskmgr.py:701` | `run a finished job again as a new job` | `resubmit a finished job` |
| `taskmgr/taskmgr.py:710` | `the user and hostname each server runs as` | `show remote user and hostname` |
| `taskmgr/taskmgr.py:717` | `update this local CLI (git pull --ff-only)` | `update the local CLI with git pull --ff-only` |
| `taskmgr/taskmgr.py:724` | `local version and the code version on each server` | `show local and remote code versions` |

init、fetch、cancel、resolve 的现有摘要足够短，保留。validate 的目录/归档限制已在 path 参数说明，logs 的流种类已在 stream choices。

### 5. 查询范围统一为 enabled servers

位置：`taskmgr/cliparse.py:285`、`taskmgr/cliparse.py:286`、`taskmgr/cliparse.py:294`、`taskmgr/cliparse.py:302`、`taskmgr/cliparse.py:371`、`taskmgr/cliparse.py:374`；行为见 `taskmgr/taskmgr.py:236`、`taskmgr/taskmgr.py:345`。

当前 → 建议：

- `the job's id (16 hex digits)` → `job ID (16 hex digits)`
- `server name (default: look the jobid up on every server)` → `server name (default: search enabled servers)`
- `server name (default: round-robin over enabled servers)` → `server name (default: round-robin among enabled servers)`
- 三处 `only this server (default: every server)` → `server name (default: all enabled servers)`

当前 every server 容易误导禁用主机的用户。ping 默认确实遍历所有配置主机，不能一并替换为 enabled。

### 6. 等待参数要说明退出码，而非泛称 tells how

位置：`taskmgr/cliparse.py:313`、`taskmgr/cliparse.py:320`、`taskmgr/cliparse.py:327`、`taskmgr/cliparse.py:334`；退出行为见 `taskmgr/taskmgr.py:456`、`taskmgr/taskmgr.py:464`、`taskmgr/taskmgr.py:472`。

当前 → 建议：

- `wait until the job ends; the exit code tells how` → `wait; exit 0: succeeded, 1: other terminal state, 2: error, 3: unknown`
- `--wait: seconds between polls (default 5)` → `--wait poll interval (default: 5 seconds)`
- `--wait: give up after this long without a connection (default 300)` → `--wait connection failure limit (default: 300 seconds)`
- `--wait: give up after this long (default: no limit)` → `--wait total time limit (default: unlimited)`

保留 --retry-timeout、--timeout 的现名，二者范围不同，无需为字面精简引入破坏性改名。

### 7. host 摘要统一动词并删口语修饰

位置：`taskmgr/hostcmds.py:26` 至 `taskmgr/hostcmds.py:31`。

| 当前文本 | 可直接替换文本 |
| --- | --- |
| `set up or update a server and add it to the machine list` | `configure or update a server` |
| `list the configured servers` | `list configured servers` |
| `remove a server from the machine list` | `remove a local server entry` |
| `use a server again for submits and queries` | `enable automatic submits and queries` |
| `leave a server out of submits and queries for now` | `disable automatic submits and queries` |
| `check that servers answer` | `check server connectivity` |

### 8. host add 的部署细节压成操作契约

位置：`taskmgr/hostcmds.py:47`。

当前：`Check the server step by step and change only what differs from the wanted state (code, remote_root, systemd user service, linger, machine list). Every change is confirmed with [y/N]; a second run changes nothing. The server's storage root is $TASKMGR_REMOTE_ROOT (default: the existing entry's, else ~/.taskmgr-remote on the server); it holds config.json, the job data and the code.`

建议整段替换：`Configure or update a server, confirming each change. Remote root: $TASKMGR_REMOTE_ROOT, otherwise the existing root or ~/.taskmgr-remote.`

配置文件目录清单、systemd/linger 枚举和幂等实现细节移出帮助；环境变量和默认目录保留。

位置：`taskmgr/hostcmds.py:62`、`taskmgr/hostcmds.py:82`、`taskmgr/hostcmds.py:86`、`taskmgr/hostcmds.py:96`。

当前 → 建议：

- `extra `ssh -o` option (repeatable); replaces an option with the same key` → `SSH option (repeatable; replaces the same key)`
- `taskmgr-remote.pyz to upload, built by `python3 -m taskmgr.pyz` (default: dist/taskmgr-remote.pyz in the checkout, or next to task.pyz); its code version must equal this task's` → `remote zipapp; must match local code (default: dist/taskmgr-remote.pyz or beside task.pyz)`
- `answer yes to every prompt` 和 `do not ask for confirmation` → 统一为 `skip confirmation prompts`。

### 9. enable/disable 描述保留例外，删查询枚举

位置：`taskmgr/hostcmds.py:101`。

当前：`Mark a server enabled in the machine list: round-robin submits and the queries over every server (jobs, the jobid lookup, whoami, version) use it again.`

建议：`Include this server in automatic submits and queries.`

位置：`taskmgr/hostcmds.py:109`。

当前：`Mark a server disabled in the machine list, e.g. while it is down: round-robin submits skip it and the queries over every server (jobs, the jobid lookup, whoami, version) do not wait for it. `--host NAME` still reaches it and `task host ping` still pings it. Nothing changes on the server; its jobs keep running.`

建议：`Skip this server in automatic submits and queries. Explicit --host and host ping still reach it; running jobs continue.`

位置：`taskmgr/hostcmds.py:70`、`taskmgr/hostcmds.py:77`。

当前 `keep the server out of round-robin submits` / `put the server back into round-robin submits` → 分别替换为 `disable automatic submits and queries` / `enable automatic submits and queries`，与独立子命令保持同一语义。

### 10. remove/ping 描述删实现过程

位置：`taskmgr/hostcmds.py:92`。

当前：`Remove a server from the local machine list. The server itself (code, service, remote_root and its jobs) is not touched; `task host add` adds it again.`

建议：`Remove the local server entry; remote code, service and jobs remain.`

位置：`taskmgr/hostcmds.py:120`。

当前：`Run `taskmgr-remote version` on each server (in parallel) and report whether it answers, the round-trip time and the installed code version. Exit 0 when every pinged server answered, 2 otherwise.`

建议：`Show connectivity, latency and code version for all configured servers, including disabled ones. Exit 0 if all answer, otherwise 2.`

### 11. usage 应表达互斥，而非改变选项名称

位置：`taskmgr/cliparse.py:188`、`taskmgr/cliparse.py:198`；互斥定义 `taskmgr/hostcmds.py:64`。

当前 usage 片段：`[--disabled] [--enabled]`，看起来两者可以并用；实测同时传入时正确报互斥错误。

建议 usage 片段替换为：`[--enabled | --disabled]`。这是 formatter 的结构修正，需要读取 mutually exclusive group，不能仅替换 help 字符串。保留现有 --enabled/--disabled 名字，避免与独立 enable/disable 子命令的动词形式混淆。

### 12. resolve 用文字区分自动检查与人工确认

位置：`taskmgr/taskmgr.py:704`、`taskmgr/cliparse.py:366`、`taskmgr/cliparse.py:368`。

当前摘要 `resolve a job in the unknown state` → 建议 `check an unknown job or confirm it stopped`。

当前 `declare that the job's processes are gone (needs --confirm)` → 建议 `confirm processes are gone (requires --confirm JOBID)`。

当前 `repeat the jobid to confirm --as` → 建议 `repeat job ID to confirm --as stopped`。

保留双重确认及唯一的 stopped 取值，不为缩短文字删除安全步骤。

### 13. 远端补用途，避免只给命令集合

位置：`taskmgr/remote.py:570`、`taskmgr/remote.py:573` 至 `taskmgr/remote.py:594`。

当前：顶层 description 为空，仅 inspect 有 `read-only: job counts and incompatible job directories`；其余命令没有 help，所有子命令 description 为空，inspect 子页也因此没有用途说明。顶层命令集合在 usage 和 positional arguments 中重复一遍。

建议顶层 description：`Remote protocol and service commands; normally invoked by task.`；subparsers metavar 使用 `<command>`，展示简短命令表。各命令以下列字符串同时作为 help、description（inspect 替换现有字符串，其余补入）：

| 命令 | 可直接使用的文本 |
| --- | --- |
| version | `show code version and runtime identity` |
| inspect | `inspect job counts and incompatible directories (read-only)` |
| receive | `receive a task package from stdin and enqueue it` |
| jobs | `list jobs` |
| status | `show job status` |
| cancel | `cancel a job` |
| retry | `resubmit a finished job` |
| resolve | `check an unknown job or confirm it stopped` |
| logs | `stream a job log to stdout` |
| fetch | `write job results as tar.gz to stdout` |
| serve | `run the scheduler service` |
| shim | `run a job shim (internal)` |

本地 fetch 下载到目录，远端 fetch 写 tar.gz，不能使用同一句模糊的 download 描述。serve/shim 保留原位置和调用兼容性，以文字标明内部用途即可，无需新增 internal 分组。

### 14. 远端选项补最短说明并统一占位符

位置：`taskmgr/remote.py:571` 至 `taskmgr/remote.py:596`。

| file:line | 当前文本或展示 | 可直接使用的替换文字 |
| --- | --- | --- |
| `taskmgr/remote.py:571` | `remote storage root (from the machine list)` | `remote storage directory; place before command` |
| `taskmgr/remote.py:572` | `--protocol-version PROTOCOL_VERSION`，help 空 | metavar `N`；help `require protocol version N` |
| `taskmgr/remote.py:577` | `--submission-key SUBMISSION_KEY`，help 空 | metavar `KEY`；help `idempotency key (default: generated)` |
| `taskmgr/remote.py:579` | `--state STATE`，help 空 | `filter by job state` |
| `taskmgr/remote.py:582` | `jobid`，help 空 | `job ID (16 hex digits)` |
| `taskmgr/remote.py:584` | `jobid`，help 空 | `job ID (16 hex digits)` |
| `taskmgr/remote.py:585` | `--as AS_`，help 空 | choices `stopped`；help `confirm processes are gone (requires --confirm JOBID)` |
| `taskmgr/remote.py:586` | `--confirm CONFIRM`，help 空 | metavar `JOBID`；help `repeat job ID to confirm --as stopped` |
| `taskmgr/remote.py:588` | `jobid`，help 空 | `job ID (16 hex digits)` |
| `taskmgr/remote.py:589` | `--stream {stdout,stderr,shim}`，help 空 | `log stream (default: stdout)` |
| `taskmgr/remote.py:590` | `--follow`，help 空 | `follow until the job ends` |
| `taskmgr/remote.py:592` | `jobid`，help 空 | `job ID (16 hex digits)` |
| `taskmgr/remote.py:595` | `jobid`，help 空 | `job ID (16 hex digits)` |
| `taskmgr/remote.py:596` | `--lock-fd LOCK_FD`，help 空 | metavar `FD`；help `inherited job lock file descriptor (internal)` |

本地 stream 的 `which log (default stdout)`（`taskmgr/cliparse.py:343`）同步改为 `log stream (default: stdout)`。远端 --state 建议复用本地 ALL_STATES choices，并用 `metavar="STATE"`，help 改为与本地相同的 `only jobs in this state: %s` 模板，避免无效状态静默返回空列表（`taskmgr/remote.py:221`）。--as 的 choices 应与现有运行时 stopped 校验保持一致。保留 --root 和 --home：二者对应不同机器上的目录，不应为了统一名称而混为一谈。

### 15. 错误消息给出可读规则与下一步

位置：`taskmgr/taskmgr.py:109`。

当前：`task_id must match ^[A-Z]{2}-[0-9]{2,4}$`；实测 `task init bad` 仅显示正则和 bad_task_id。

建议：`invalid task ID: use two uppercase letters, a dash and 2-4 digits (e.g. AA-01)`。

位置：`taskmgr/taskmgr.py:182`、`taskmgr/remote.py:99`。

当前：`--submission-key must match ^[A-Za-z0-9._-]{1,128}$` / `submission key must match ^[A-Za-z0-9._-]{1,128}$`。

建议统一：`--submission-key: use 1-128 ASCII letters, digits, dots, underscores or dashes`。

位置：`taskmgr/taskmgr.py:352`、`taskmgr/taskmgr.py:374`。

当前：`job %s exists on several servers %s; use --host` / `job %s not found on any enabled server; disabled, not searched: %s (use --host)`。

建议：`job %s exists on multiple servers %s; select one with --host NAME` / `job %s not found on enabled servers; disabled: %s; search one with --host NAME`。

位置：`taskmgr/taskmgr.py:462`。

当前：`job %s is in the unknown state; see `task resolve``。

建议：`job %s has unknown state; see task resolve --help`。当前裸 resolve 缺少 jobid，会再触发一次参数错误。

### 16. 保留解析错误的上下文，避免 JSON 只转述 stderr

位置：`taskmgr/taskmgr.py:744`、`taskmgr/cliparse.py:225`。

当前 detail 模式参数错误 message 恒为 `invalid arguments (see stderr)`；实际 stderr 可以是 `the following arguments are required: jobid` 或 `unrecognized arguments: --detail`。

建议 JSON message 直接复用具体解析错误文字，例如 `missing required argument: jobid`；未知 detail 放置位置可使用 `place --detail after the leaf command (e.g. task jobs --detail)`。这需要传递解析错误消息，不能只把固定字符串换成另一句固定字符串。保留 bad_args 和退出码 2。

普通 task 解析错误目前已有 usage、具体原因和精准的命令级 -h 提示，整体清楚，无需再加一段说明。远端缺少 --root 时也明确报出 required --root；将第 14 项的位置要求补入子页可减少试错，不必重写其 JSON/二进制流错误协议。

## 建议后的命令树

仅收起 task 顶层 host 展示；以下保留实际可调用路径，不新增别名或迁移命令。task 的 --home 在命令前，--detail 在叶子命令后（logs 除外），--plain 支持各解析层级；remote 的 --root、--protocol-version 在命令前。

```text
task [--home DIR] [--plain] <command> [args]
├── init <task_id> [--output DIR]
├── validate <path>
├── submit <path> [--host HOST] [--submission-key KEY]
├── jobs [--host HOST] [--state STATE]
├── status <jobid> [--host HOST] [--wait]
│          [--poll-interval SECONDS] [--retry-timeout SECONDS] [--timeout SECONDS]
├── logs <jobid> [--host HOST] [--stream stdout|stderr|shim] [--follow]
├── fetch <jobid> --output DIR [--host HOST]
├── cancel <jobid> [--host HOST]
├── retry <jobid> [--host HOST]
├── resolve <jobid> [--host HOST] [--as stopped] [--confirm JOBID]
├── host
│   ├── add <name> [--ssh USER@HOST] [--port PORT] [--identity-file FILE]
│   │              [--ssh-option KEY=VALUE] [--enabled | --disabled] [--pyz FILE] [-y]
│   ├── list
│   ├── remove <name> [-y]
│   ├── enable <name>
│   ├── disable <name>
│   └── ping [<name>]
├── whoami [--host HOST]
├── update
└── version [--host HOST]

taskmgr-remote --root ROOT [--protocol-version N] <command> [args]
├── version
├── inspect
├── receive [--submission-key KEY]                 # stdin: package
├── jobs [--state STATE]
├── status <jobid>
├── cancel <jobid>
├── retry <jobid>
├── resolve <jobid> [--as stopped] [--confirm JOBID]
├── logs <jobid> [--stream stdout|stderr|shim] [--follow]
├── fetch <jobid>                                 # stdout: tar.gz
├── serve                                        # scheduler service
└── shim <jobid> --lock-fd FD                      # internal
```
