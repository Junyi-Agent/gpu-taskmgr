# 文档漂移核查

核查基线：`070f3a2c91df`，2026-11-01。本文只报告可定位的差异；行号均指本次核查前的文件。中英文同源问题合并列出。

## 范围与验证方式

已对照 README.md、taskmgr/README.md、taskmgr/README.zh-CN.md、docs/ 下全部 16 份直接子级 Markdown、taskmgr/template/TASK.md、taskmgr/deploy/ 三份示例，以及 taskmgr 模块（含模板、示例辅助模块）的模块 docstring。另将 agent/desc-1.md、desc-2.md、desc-3.md 作为规格核查实现，不把旧规格的术语或历史命令逐字不同当作缺陷。

实际运行：

- `python3 bin/task --help`、`python3 bin/taskmgr-remote --help`，以及全部 20 个本地命令/命令组的 `--help --plain`，均退出 0。
- `python3 -m taskmgr.pyz -o /tmp/dist` 成功；两个生成归档的 `--help` 均退出 0。代码版本为 `cc392b87d0f3e23341e2dbcf153e66ce652500fc0c981bd9fdc0fd7825a602e4`。
- 临时目录复现：全部主机禁用、参数位置、绝对入口路径、unknown resolve、fetch 特殊文件、禁用 GPU 忙约定时的环境变量、等待超时。所有复现数据均放在 `/tmp`，未写入项目样例或配置。
- `python3 -m unittest taskmgr.tests.test_query taskmgr.tests.test_recovery taskmgr.tests.test_pyz`：58 项通过，耗时 157.182 秒。
- `config.example.json` 解析结果与 `common.DEFAULT_REMOTE_CONFIG` 完全相等；机器示例字段与 transport 的读取方式匹配；示例 unit 的入口路径、`--root`、`serve`、`KillMode=process` 与实现匹配。

未连接真实 SSH/GPU/systemd 用户实例；有关真实驱动、polkit、系统退出登录策略的陈述仅核查代码路径和部署配置，不声称经过真实服务器验证。

## 建议修代码的偏差

### 1. unknown 的 resolve 仍可能自动创建重跑作业

- 文档：`docs/recovery.md:64`、`docs/recovery.zh-CN.md:57`、`docs/assumptions.md:76`、`docs/assumptions.zh-CN.md:63` 说 unknown 不重跑/不重新排队；规格 `agent/desc-1.md:634` 也要求 unknown 不重跑，`agent/desc-2.md:311` 再次明确。
- 实际：`taskmgr/remote.py:374` 调用 `assess(..., resolve=True)`；`taskmgr/monitor.py:339` 在无执行身份时调用 `interrupt_run`，`taskmgr/monitor.py:360` 在 boot_id 改变时也如此；`taskmgr/store.py:562` 后的分支未排除 unknown，会 clone 新作业。
- 复现：构造 state=unknown、runner 无 pgid/boot_id、未取消、auto_restarts=0 的作业，运行 `python3 bin/taskmgr-remote --root <临时根目录> resolve 0000000000000001`，退出 0，输出 `decision: interrupted, state: failed, changed: true`，且 queue/ 中出现新 jobid。
- 建议：**改代码**，明确 unknown resolve 在无身份、跨 boot 和 final/interrupted 等分支的策略，禁止隐式创建重跑；保留显式 `task retry`。

### 2. fetch 静默丢弃特殊文件，无法列入 skipped

- 文档：`docs/assumptions.md:54`、`docs/assumptions.zh-CN.md:46` 以及规格 `agent/desc-2.md:178` 说特殊文件与不安全符号链接一样会警告并列入 `skipped`。
- 实际：`taskmgr/package.py:600` 的 `add_tree_to_tar` 在远程打包阶段直接跳过非普通文件/目录/符号链接；`taskmgr/remote.py:462` 只传 tar；本地 `taskmgr/taskmgr.py:561` 的 skipped 只来自实际收到的 tar，无法知道被远端丢弃的 FIFO/socket。
- 复现：workdir 内放一个 FIFO 和一个指向 `/etc/passwd` 的符号链接。打包后的成员只有 workdir 和符号链接；安全解包的 skipped 只有符号链接，FIFO 无警告、无记录。
- 建议：**改代码**，通过明确的传输元数据返回远端 skipped，合并到回执；不要把“已完整列出未下载项”的承诺删掉来掩盖丢失。

### 3. --wait --timeout 不是总等待时限

- 文档：`docs/assumptions.md:31`、`docs/assumptions.zh-CN.md:28` 说超时即输出状态并退出；规格 `agent/desc-1.md:544` 定义 total wait time，`agent/desc-2.md:171` 要求连接重试期间也适用 deadline。
- 实际：`taskmgr/taskmgr.py:400` 的初次 locate 和 `taskmgr/taskmgr.py:402` 的后续 Remote.call 未传剩余时长；`taskmgr/transport.py:266` 使用独立的默认 30 秒查询时限，返回后才检查 wait deadline。终态和自动重跑分支还先于 deadline 检查。
- 复现：local 传输命令先睡 0.3 秒再返回 queued；`status ... --host s --wait --timeout 0.01 --detail --plain` 实测约 0.657 秒后才退出 2、返回 wait_timeout。
- 建议：**改代码**，每次查询和发现主机都受剩余 deadline 限制；补充查询期间 deadline 到期的输出规则。

### 4. GPU 忙约定关闭后，保留变量仍可从 base_env 泄入任务

- 文档：`docs/task-package.md:109`、`docs/task-package.zh-CN.md:101`、`docs/internals.md:163`、`docs/internals.zh-CN.md:151` 和 `taskmgr/shim.py:44` 的 docstring 都说 gpu_busy_exit_code=null 时不设置 `TASKMGR_GPU_BUSY_EXIT_CODE`。
- 实际：`taskmgr/shim.py:55` 先复制 base_env，`taskmgr/shim.py:74` 只在 busy_code 非空时覆盖，没有在为空时删除该键；config 的 base_env 允许任意字符串键值（`taskmgr/store.py:89`）。
- 复现：base_env 为 `{"TASKMGR_GPU_BUSY_EXIT_CODE":"42"}`，调用 build_env(..., busy_code=None)，结果中该变量仍为 `42`。
- 建议：**改代码**，关闭约定时主动删除此变量，保证 runner 保留字段的环境契约。

### 5. 按 jobid 定位成功时不报告跳过的禁用主机

- 文档：`docs/usage.md:178`、`docs/usage.zh-CN.md:162`、`docs/assumptions.md:78`、`docs/assumptions.zh-CN.md:65` 说跨主机查询会在 stderr/JSON 列出 skipped。
- 实际：`taskmgr/taskmgr.py:345` 的 locate 收到 skipped，但唯一命中分支 `taskmgr/taskmgr.py:356` 只返回主机、状态和完整性，不报告/返回 skipped。`render_status`（`taskmgr/taskmgr.py:294`）也没有此键。仅 jobs/whoami/version 调用 report_partial；定位失败时才可能在 not_found 中附 skipped。
- 建议：**改代码**，让 status/logs/fetch/cancel/retry/resolve 的隐式定位也报告 skipped；或若产品决定只给聚合查询报告，则同步收窄两种语言的文档。

## 建议修文档的差异与遗漏

### 6. 安装示例缺少 host add 必需的预构建步骤

- 文档：`docs/install.md:17`、`docs/install.zh-CN.md:17` 的第一段安装示例，安装 CLI 后立即执行 host add。
- 实际：`taskmgr/hostsetup.py:157` 的 prebuilt_code 要求归档已存在且版本匹配；`taskmgr/pyz.py:154` 从 checkout 默认找 dist/taskmgr-remote.pyz。只安装 editable CLI 或链接 bin/task 不生成该归档。实际对不存在路径调用 prebuilt_code 会报 `not found; build it with python3 -m taskmgr.pyz -o dist ...`。
- 建议：**改文档**，在这段独立可执行示例里先构建，或给 host add 加上已构建归档的 `--pyz`；不能只在后面的 zipapp 小节补充前提。

### 7. “没有一步需要 root”遗漏 linger 的管理员例外

- 文档：`taskmgr/README.md:15`、`taskmgr/README.zh-CN.md:15`、`taskmgr/hostsetup.py:5` 的模块 docstring 无条件承诺无需 root。
- 实际：`taskmgr/hostsetup.py:1008` 生成管理员运行 `sudo loginctl enable-linger <user>` 的修复提示，拒绝时 step_linger 失败；`docs/install.md:230` 已正确说明 polkit 可能要求管理员处理一次。
- 建议：**改文档/docstring**，快速开始也说明 linger 可能需要管理员一次性配置。代码不应尝试自行提权。

### 8. host add 的统一确认/停止规则与实际例外不符

- 文档：`docs/install.md:80`、`docs/install.zh-CN.md:76` 说非 y 就停止后续步骤、只读步骤不询问、--yes 回答所有问题；`taskmgr/hostsetup.py:3` 说每一处变更均先确认。
- 实际：`taskmgr/hostsetup.py:367` 将 linger 设为非必需，`taskmgr/hostsetup.py:392` 仅必需步骤失败/拒绝才停止；`taskmgr/hostsetup.py:536` 的 ssh 步骤可能询问根目录切换且明确拒绝 --yes；`taskmgr/hostsetup.py:663` 自动删除过期上传文件而不询问。
- 建议：**改文档/docstring**，总规则明确这三种例外。安装页后面的表格/根目录段虽分别提及例外，开头的绝对表述仍相互冲突。

### 9. 全部主机禁用时的错误尚未记录

- 文档：`docs/usage.md:76`、`docs/usage.zh-CN.md:74` 说明找不到作业时列出 disabled；`docs/usage.md:107`、`docs/usage.zh-CN.md:100` 将 jobs 输出描述成带 jobs/errors/skipped 的聚合对象。
- 实际：`taskmgr/taskmgr.py:235` 在存在条目但无 enabled 主机时直接抛 `no_enabled_hosts`，不返回聚合对象，定位也不会走 not_found。这是已实现但未写明的边界行为。
- 复现：机器列表仅有 enabled=false 的 off；jobs 和 status <jobid> 都退出 2，JSON 仅含 `error.code=no_enabled_hosts`，提示 --host 或 host enable。
- 建议：**改文档**，补充全禁用与空数组、machines.json 不存在三种情形的差异；显式 --host 仍可访问禁用主机。

### 10. 提交不确定时的对外错误码描述不准确

- 文档：`docs/usage.md:122`、`docs/usage.zh-CN.md:113` 将无有效远程回复时的错误码写为 remote_error。
- 实际：`taskmgr/transport.py:80` 的 NoReplyError 底层确实是 remote_error，但它继承 ConnError；`taskmgr/taskmgr.py:202` 捕获后，`taskmgr/taskmgr.py:218` 统一对外抛 `submission_uncertain`，附 host 和 submission_key。
- 建议：**改文档**，区分 transport 原因与 submit 最终对外错误码，方便脚本据此做安全重试。

### 11. recovery 手动操作小节遗漏“交回 daemon”的 resolve 分支

- 文档：`docs/recovery.md:67`、`docs/recovery.zh-CN.md:60` 说仍有存活进程就保持 unknown，并输出 identity 和 alive_pids。
- 实际：`taskmgr/monitor.py:384` 只有 stop_unconfirmed_reason 存在时才保持 unknown；否则 `taskmgr/monitor.py:393` 恢复 running/stopping，返回 handed_to_daemon。记录无法读取时，`taskmgr/monitor.py:307` 也不提供 alive_pids。
- 建议：**改文档**，引用 `docs/assumptions.md:44` / `docs/assumptions.zh-CN.md:38` 已准确说明的两个分支；identity/alive_pids 应写成条件字段。

### 12. TASK.md “从不读取”混淆了校验与执行

- 文档：`docs/task-package.md:15`、`docs/task-package.zh-CN.md:15`、`taskmgr/template/TASK.md:27` 说 taskmgr 从不读取 TASK.md。
- 实际：`taskmgr/package.py:256` 遍历 REQUIRED_FILES，读取并解码 TASK.md，随后扫描 `<REQUIRED`；打包和内容哈希也读取其字节（`taskmgr/package.py:362`、`taskmgr/package.py:381`）。
- 建议：**改文档/模板说明**为“不解释或执行 TASK.md 的任务指令；仍会读取它以校验占位符、打包和计算哈希”。

### 13. incoming/retry 的后缀不是新作业 jobid

- 文档：`docs/internals.md:21`、`docs/internals.md:42`、`docs/internals.zh-CN.md:21`、`docs/internals.zh-CN.md:42` 写 incoming/retry-<jobid>/。
- 实际：`taskmgr/store.py:622` 使用独立 `secrets.token_hex(6)`，`taskmgr/store.py:707` 进入 staging 后才在 `taskmgr/store.py:710` 分配新 jobid；手动重试也先在 `taskmgr/remote.py:316` 建 staging。
- 建议：**改文档**为 incoming/retry-<随机后缀>/，避免操作人员根据目录名关联错误 jobid。

### 14. dispatched_at 为空不保证没有运行文件

- 文档：`docs/internals.md:63`、`docs/internals.zh-CN.md:60` 说没有 dispatched_at 的作业从未运行且无运行文件。
- 实际：`taskmgr/monitor.py:254` GPU 忙后重新排队清空 dispatched_at，同时保留 workdir、日志和 runner.prev-N.json；`taskmgr/store.py:261` 的 has_run 只是 API 对当前派发的判断。
- 建议：**改文档**，注明这是当前派发标记，GPU 忙恢复重新排队是例外；也说明此时日志/下载接口仍可能按 no_run 拒绝，磁盘上已有文件不等于接口可读取。

### 15. “每次 JSON 写入都 fsync 目录”范围过宽

- 文档：`docs/internals.md:85`、`docs/internals.zh-CN.md:81` 无条件描述所有 JSON 写入。
- 实际：远程状态写入 `taskmgr/common.py:127` 符合；模板 helper 的 `taskmgr/template/taskmgr_task.py:51` 在 replace 后没有目录 fsync（两个 examples 的 helper 同样如此）；`taskmgr/taskmgr.py:121` 的 init progress 用 json.dump 直接写目标文件。
- 建议：**改文档**，将 durable IO 承诺限定为远程管理状态记录；分别描述任务辅助写入的原子可见性和初始化行为。若要承诺 helper 断电持久性，则需另改 helper 及两个副本。

### 16. @override 的导入路径已漂移

- 文档：`docs/development.md:67`、`docs/development.zh-CN.md:64` 说从 taskmgr.records 导入。
- 实际：定义在 `taskmgr/common.py:20`，`taskmgr/cliparse.py:14` 从 common 导入；records 无 override 定义/再导出。
- 建议：**改文档**为 taskmgr.common。

### 17. 模块 docstring 遗留旧模块名和命令清单

- 文档：`taskmgr/hostlegacy.py:3`、`taskmgr/hostlegacy.py:32` 引用 hostprobe.legacy；`taskmgr/hostcmds.py:1` 的命令清单只有 add/list/remove/ping。
- 实际：当前没有 hostprobe.py，探测函数在 `taskmgr/hostserver.py:114`；hostcmds 的 HELP（`taskmgr/hostcmds.py:25`）和 parser/run（`taskmgr/hostcmds.py:98`、`taskmgr/hostcmds.py:134`）还包含 enable/disable。实际两者 --help 均退出 0。
- 建议：**改 docstring**，更正 hostserver.legacy 并补齐两个已实现命令。

### 18. 全输出着色/每条命令返回对象的承诺遗漏 logs

- 文档：`docs/usage.md:43`、`docs/usage.zh-CN.md:43` 说所有输出带颜色；`taskmgr/textout.py:3` 还说每条命令返回 JSON 对象再由 renderer 转成文本。
- 实际：`taskmgr/taskmgr.py:527` 的 cmd_logs 将远端原始字节直接写 stdout.buffer，返回 `(None, 0)`；其 Command 的 renderer 是 None（`taskmgr/taskmgr.py:697`）。--plain 不会删除任务日志自带的 ANSI 颜色。
- 建议：**改文档/docstring**，说明 logs 原样传输、无 --detail，颜色开关只控制 CLI 自己生成的文本。

### 19. 绝对入口路径支持没有明确写出

- 文档：`docs/task-package.md:36`、`docs/task-package.zh-CN.md:36` 将含斜杠的 argv[0] 与“包内可执行文件”放在同一括号中，只详细解释相对路径，容易理解为绝对路径也必须在包内。
- 实际：`taskmgr/package.py:285` 只对相对路径做包内文件检查；`taskmgr/shim.py:79` 明确接受远端绝对路径，在启动前检查其存在且可执行。把成功示例的入口换成 `/bin/true`，本地 validate 实测退出 0。
- 建议：**改文档**，明确相对路径须在包内，绝对路径指远端文件、只在启动前检查；这也符合规格 `agent/desc-1.md:86` 的路径规则。

### 20. 顶层 --detail 帮助与解析位置不一致

- 文档：`taskmgr/cliparse.py:131` 生成的顶层帮助将 --detail 与 --plain/--home 并列为 options；`docs/usage.md:39`、`docs/usage.zh-CN.md:39` 只说命令支持 --detail，未提醒位置要求。
- 实际：`taskmgr/cliparse.py:276` 只把 --detail 注册到结果子命令，顶层只有 --home/--plain。`python3 bin/task --detail jobs --plain` 实测退出 2、unrecognized arguments；`task jobs --detail` 可解析。
- 建议：**改文档/帮助**，写明 --detail 在最终子命令之后、--home 在顶层；也可另改 parser 接受顶层 --detail，使顶层帮助承诺成立。

## 规格偏差的处理边界

desc-2 的 D16/D17/D19 已明确覆盖 desc-1 的 attempts、原 jobid 重试、GPU 忙退出以及旧部署布局，不将这些视为待修缺陷。desc-3 的 uv/pre-commit/ruff/basedpyright 需求在 pyproject.toml 与 .pre-commit-config.yaml 中均已实现。

另有两项实现与原规格的偏差，现有用户文档已明示，但 desc-2 未完整归并后续决定：

- `agent/desc-1.md:508`、`agent/desc-1.md:526` 要求查询禁用主机；实际 `taskmgr/taskmgr.py:235` 只查 enabled 主机。`docs/assumptions.md:78` / `docs/assumptions.zh-CN.md:65` 已注明是用户决定。建议保留代码，将该决定补充到规格，避免再次实现旧规则。
- `agent/desc-1.md:566` 要求除 logs 外默认 JSON；`agent/desc-2.md:162` 的覆盖只明确 jobs 表格，但当前所有本地结果命令都默认文本（`taskmgr/taskmgr.py:778`），须 --detail 才输出 JSON（`taskmgr/taskmgr.py:775`）。`docs/usage.md:48` 已说明完整文本输出体系。建议确认并补记规格覆盖范围，保持现有已文档化 CLI，不以旧 JSON 默认值直接回改代码。

其余未发现差异的项目包括：现有命令/主要选项名称、协议版本 2、task_id/jobid 格式、submission key 限制、主要配置默认值、打包排除规则、CUDA 环境变量顺序、重试新建 jobid、GPU 忙重启工作目录复用、状态目录映射、调度预留集合及 unknown GPU 处理。真实服务器侧效果仍受前述验证边界限制。
