# 文档漂移核查

核查基线：`77f28a548ebd`，2026-11-01。只修改本报告。以下行号均为该基线中的行号；中英文同一问题合并记录。规格按后续明确的用户决策解释，不把旧规格的文字差异直接当成实现错误。

## 范围与验证方法

已通读并对照实现：根 README、`taskmgr/README.md` / `README.zh-CN.md`、`docs/` 顶层全部 16 份中英文 Markdown、`taskmgr/template/TASK.md`、`taskmgr/deploy/` 的两份 JSON 示例和 systemd unit；提取并核对 `taskmgr/` 下 Python 模块及其类、函数 docstring（含模板、示例和测试说明）。另读 `agent/desc-1.md`、`desc-2.md`、`desc-3.md`，检查实现与有效规格的偏差。

实际环境是 Linux、Python 3.14.10。执行了：

- `python3 bin/task --help`、`python3 bin/taskmgr-remote --help`，以及全部本地子命令（包括六个 host 子命令）的 `--help`，均退出 0。
- `python3 -m taskmgr.pyz -o /tmp/dist`，成功生成两个 zipapp；代码版本为 `cc392b87d0f3e23341e2dbcf153e66ce652500fc0c981bd9fdc0fd7825a602e4`。
- 在 `/tmp` 中用示例包、local transport、临时机器列表和合成恢复记录复现下述边界；无真实服务器写操作。涉及分支隔离的验证明确标为 mock。
- 对照 `common.DEFAULT_REMOTE_CONFIG` 验证部署 JSON 默认值完全一致；对照生成 unit 核对 `KillMode=process`、解释器路径处理、启动命令及重启策略。

真实 SSH、polkit、systemd 用户会话、NVIDIA 驱动及 Exclusive_Process 行为未在真实服务器验证；这些部分只核对代码和现有测试，不能据此声称已做部署/GPU 冒烟测试。

## 需要优先处理的行为偏差

### 1. unknown 作业在 resolve 后仍可能自动重跑

- 文档：`docs/recovery.md:64`、`docs/recovery.zh-CN.md:57` 声称 unknown 不会重跑；有效规格 `agent/desc-1.md:634` 要求 unknown 永不自动重跑。`docs/assumptions.md:76` / `docs/assumptions.zh-CN.md:63` 也强调不会重新排队。
- 实际：`taskmgr/monitor.py:339` 对没有身份且未记录 exited 的 runner 调用 `interrupt_run`，`taskmgr/monitor.py:364` 对 boot_id 改变也调用它，没有排除 `resolve=True` 的 unknown。`taskmgr/store.py:427` 的 `_settled` 不包含 unknown，`taskmgr/store.py:562` 至 `:576` 因而创建自动重跑。临时创建 unknown、`runner={status: dispatched, final: false, exited: false}` 后执行 `assess(..., resolve=True)`，实际返回 `decision=interrupted,state=failed`，并出现 `retry_of=<旧 jobid>` 的新 queued 作业。
- 建议：**改代码**。unknown 的人工检查可以确认结束或恢复跟踪，但不得直接走创建自动重跑的分支；补充缺少身份及跨 boot 的 unknown 用例。

### 2. 关闭 GPU 忙约定后，base_env 仍可泄漏该约定的环境变量

- 文档：`docs/task-package.md:109`、`docs/task-package.zh-CN.md:101`、`taskmgr/shim.py:46` 声称 `gpu_busy_exit_code=null` 时不设置 `TASKMGR_GPU_BUSY_EXIT_CODE`。
- 实际：`taskmgr/shim.py:55` 先叠加任意 `base_env`，`:74` 仅在 busy_code 非空时覆盖变量，不会在关闭时删除。直接调用 `build_env`，配置 `base_env={TASKMGR_GPU_BUSY_EXIT_CODE: "88"}`、busy_code=None，返回环境仍有值 `88`。此时 helper 按 88 退出，服务器却按普通失败处理。
- 建议：**改代码**。关闭约定时显式删除该变量；或在配置校验时禁止 base_env 设置 runner 保留变量，并在文档中说明。

### 3. fetch 对特殊文件的省略没有警告或 skipped 记录

- 文档：`docs/assumptions.md:54`、`docs/assumptions.zh-CN.md:46`，以及规格 `agent/desc-2.md:178`，说特殊文件与不安全链接一样会被跳过、警告并列入 `skipped`。
- 实际：`taskmgr/package.py:600` 至 `:603` 在远程生成 tar 时直接跳过 FIFO/设备/socket；`taskmgr/remote.py:456` 只传这个 tar。`taskmgr/taskmgr.py:561` 至 `:563` 只能报告本地解包发现的条目，无法获知已省略的文件。临时目录放入普通文件和 FIFO 后调用 `add_tree_to_tar`，tar 中只有目录和普通文件，没有 FIFO 或省略清单。
- 建议：**改代码**。通过协议传回远程省略清单并合并回执；如产品决定静默忽略，则同步改文档和规格约定。当前不能承诺下载回执完整列出所有省略文件。

### 4. “每处改动先确认 / 最新服务器不做改动”忽略了无提示删除暂存上传

- 文档：`taskmgr/README.md:35`、`:48`，`taskmgr/README.zh-CN.md:34`、`:45`，`docs/install.md:80`、`:86`，`docs/install.zh-CN.md:76`、`:81`，`docs/usage.md:168` / `docs/usage.zh-CN.md:155`，以及 `taskmgr/hostsetup.py:3`、`taskmgr/hostcmds.py:49` 都作此承诺。
- 实际：`taskmgr/hostsetup.py:617` 在确认前调用 `_remove_stale_uploads`，`:661` 至 `:665` 执行 `find ... -mmin +10 -print -delete`；即使已安装版本相同，也先删除再返回 `ok`。mock `sh` 隔离验证确认该顺序及删除脚本，未操作真实服务器。
- 建议：**改代码**，让删除也经过确认，以保持现有交互承诺；若接受自动清理，须在上述总述中列出例外，而不仅在安装步骤表中提到过期文件会删除。

### 5. 任务 helper 没有实现“每次 JSON 写入都 fsync 所在目录”

- 文档：`docs/internals.md:85`、`docs/internals.zh-CN.md:81` 把目录 fsync 描述为所有 JSON 写入的统一行为；规格 `agent/desc-1.md:349` 也要求按需同步目录。
- 实际：管理记录通过 `taskmgr/common.py:127`、`:140` 同步文件及目录；`taskmgr/template/taskmgr_task.py:51`、`taskmgr/examples/succeed/taskmgr_task.py:51` 和 `taskmgr/examples/multi-gpu/taskmgr_task.py:51` 只 fsync 文件并 replace，没有目录 fsync。mock 计数验证一次 `write_progress` 仅有一次 fsync 调用。另外 `taskmgr/taskmgr.py:121` 的 init progress 直接写目标文件。helper 的原子可见性成立，但不能把它等同于目录项的持久性。
- 建议：**改代码**，统一三份 helper 的持久化实现；或者把 internals 的保证明确限定为 taskmgr 管理记录，并说明任务 helper 的较弱保证。

## 命令、默认值与使用说明

### 6. 提交结果不确定时的对外错误码已经是 submission_uncertain

- 文档：`docs/usage.md:122`、`docs/usage.zh-CN.md:114` 将无有效回复的错误码写成 `remote_error`。
- 实际：`taskmgr/transport.py:80` 的底层错误确为 remote_error，但它继承 ConnError；`taskmgr/taskmgr.py:202` 至 `:221` 统一捕获连接失败、无回复和上传中断，向用户抛出 `submission_uncertain`，details 含 submission_key。local transport 注入失败时，真实 CLI 退出 2，stdout 的 `error.code` 为 `submission_uncertain`，stderr 有原主机及重试命令。
- 建议：**改文档**，区分底层失败原因和 submit 的对外错误码，补充 Ctrl-C 也属于提交结果不确定。

### 7. 按 jobid 查找成功时没有列出跳过的禁用服务器

- 文档：`docs/usage.md:178`、`docs/usage.zh-CN.md:162`，`docs/assumptions.md:78` / `docs/assumptions.zh-CN.md:65` 承诺包括 jobid lookup 在内的查询会在 stderr 和 JSON 中列出 skipped。
- 实际：`taskmgr/taskmgr.py:345` 获得 skipped，但 `:356` 至 `:363` 找到唯一作业就返回，不报告 skipped；`:371` 只在完全找不到时使用它。`render_status`（`:295`）也不输出 skipped。临时配置一个启用的 local 服务器和一个禁用服务器，提交后不带 `--host` 查 status，实际退出 0、stderr 为空、JSON 没有 skipped。jobs/whoami/version 的 `query_hosts` 路径确实会报告。
- 建议：**改代码**，让成功的 lookup 也报告跳过列表；若有意保持精简输出，则将文档承诺限定为 jobs/whoami/version，并说明 lookup 只在 not_found 时列出。

### 8. 所有服务器被禁用时的 no_enabled_hosts 缺少说明

- 文档：`docs/usage.md:98`、`:116`，`docs/usage.zh-CN.md:93`、`:108` 只讲成功查询、空列表和所有查询失败；禁用行为段落也未说明全部禁用的分支。
- 实际：`taskmgr/taskmgr.py:241` 至 `:247` 在发出任何查询前抛 `no_enabled_hosts`（退出 2），不会返回正常的 `{jobs: [], skipped: [...]}`。jobs、lookup、whoami、version 都受影响；`--host NAME` 仍能显式查询。
- 建议：**改文档**，补充此已实现行为及恢复方法 `--host` / `task host enable`。同时区别空 `servers: []` 与机器列表文件不存在（`taskmgr/transport.py:127` 的 `no_machines`）。

### 9. scheduler_poll_seconds 并非独立的调度间隔

- 文档：`docs/install.md:262`、`docs/install.zh-CN.md:239`，规格 `agent/desc-2.md:157` 将它描述为调度轮间隔。
- 实际：`taskmgr/daemon.py:366` 使用 `min(scheduler_poll_seconds, control_poll_seconds)` 作为整个循环间隔，每次循环都会调度（`:374`）。例如设 scheduler=10、control=2，实际目标间隔为 2 秒，另加各阶段执行耗时；默认 1 秒本身正确。
- 建议：**改文档**，明确两者取最小值及耗时影响；若希望两个周期独立，则改 daemon 用两个截止时间。

### 10. host add 的多次 SSH 选项应重复旗标

- 文档：`docs/usage.md:28` / `docs/usage.zh-CN.md:28`，`docs/install.md:75` / `docs/install.zh-CN.md:71` 写成 `[--ssh-option KEY=VALUE ...]`，容易理解为一个旗标接多个值。
- 实际：`taskmgr/hostcmds.py:58` 至 `:63` 使用 `action="append"`，每次只接一个值。真实运行 `host add gpu1 --ssh x --ssh-option A=B C=D --plain` 退出 2，报 `unrecognized arguments: C=D`；帮助明确写 repeatable。
- 建议：**改文档**，示例改成 `--ssh-option A=B --ssh-option C=D`，语法写作 `[--ssh-option KEY=VALUE]...`。

### 11. 安装页首个本地安装流程没有构建远程 zipapp

- 文档：`docs/install.md:17` 至 `:23`、`docs/install.zh-CN.md:17` 至 `:23` 安装 CLI 后直接 `task host add`。
- 实际：`taskmgr/hostsetup.py:609` 首先检查预构建包；`taskmgr/hostsetup.py:165` 在文件不存在时失败。源码/uv 安装默认查 `taskmgr/pyz.py:154` 的 `<CODE_ROOT>/dist/taskmgr-remote.pyz`；没有自动构建。页面后面及 taskmgr README 已有正确说明，但按第一个代码块顺序操作仍缺一步。
- 建议：**改文档**，在首个流程中、host add 之前加入 `python3 -m taskmgr.pyz -o dist`，并说明应从 checkout 构建或用 `--pyz` 指定对应版本。

### 12. 安装页给出的远程 version 命令缺少必需的 --root

- 文档：`docs/install.md:59`、`docs/install.zh-CN.md:56` 使用 `taskmgr-remote.pyz version` 描述查询 code_dir；步骤表的 `docs/install.md:100` / `docs/install.zh-CN.md:94` 同样省略 root。
- 实际：`taskmgr/remote.py:571` 将 `--root` 设为 required，version/inspect 也不例外。真实运行 `python3 /tmp/dist/taskmgr-remote.pyz version` 退出 2，报 `the following arguments are required: --root`。
- 建议：**改文档**，完整写作 `python3 taskmgr-remote.pyz --root /absolute/remote_root version`，保留协议参数时也补 root。

### 13. “任何非 y 回答都会停止后续步骤”不适用于 linger

- 文档：`docs/install.md:80` 至 `:83`、`docs/install.zh-CN.md:76` 至 `:78` 作统一停止承诺。
- 实际：`taskmgr/hostsetup.py:367` 将 linger 标为非必需步骤，`:385` 捕获 Declined，`:393` 只有 required 步骤失败才停止。拒绝 linger 后仍执行 verify、machines、old_code，最终退出 2。安装表只说执行 enable-linger 被拒绝时继续，没有清楚说明用户主动回答 no 也继续。
- 建议：**改文档**，明确 linger 的 failed/declined 均不阻止后续步骤；old_code 也是非必需步骤，不过它已位于最后。

### 14. “不需要 root”应保留 lingering 的管理员例外

- 文档：`taskmgr/README.md:15`、`taskmgr/README.zh-CN.md:15`、`taskmgr/hostsetup.py:5` 绝对声称整个过程不需 root；部署示例 `taskmgr/deploy/taskmgr-remote.user.service:1` 的注释也未提例外。
- 实际：`taskmgr/hostsetup.py:1007` 至 `:1020` 明确在策略拒绝时要求管理员执行 `sudo loginctl enable-linger <user>`。`docs/install.md:229` / `docs/install.zh-CN.md:209` 已正确解释此例外。没有真实 polkit 主机验证，不推断目标服务器一定需要管理员。
- 建议：**改文档/docstring/示例注释**，简写“以普通用户部署；linger 被策略拒绝时需管理员开启一次”，链接安装页。

### 15. “错误只有一行 stderr，stdout 为空”的范围过宽

- 文档：`docs/usage.md:62`、`docs/usage.zh-CN.md:61` 无条件描述非 detail 模式的错误输出。
- 实际：`taskmgr/cliparse.py:225` 的参数错误会输出 usage、error 和 hint 三部分；`taskmgr/render.py:73` 的校验失败将 invalid 和逐条错误写到 stdout；`taskmgr/taskmgr.py:778` 对带结果的失败仍先打印结果。真实 `host add ... --ssh-option A=B C=D` 即显示多行 stderr。只有抛出 TaskmgrError 的常规操作错误符合这一行描述。
- 建议：**改文档**，分别说明参数错误、校验失败、带部分结果的失败和常规操作错误；避免脚本据此错误判定 stdout 一定为空。

### 16. Ctrl-C 的 interrupted 结构只在 detail 模式输出

- 文档：`docs/assumptions.md:31`、`docs/assumptions.zh-CN.md:28`，规格 `agent/desc-2.md:173` 说 Ctrl-C 打印 interrupted 错误。
- 实际：`taskmgr/taskmgr.py:762` 至 `:767` 在普通模式只向 stderr 打印 `interrupted: remote jobs are not affected`，不调用通用错误 renderer，也没有 `error.code` 或 stdout；detail 模式才输出 `{error: {code: interrupted, ...}}`。上传中的 Ctrl-C 又被 cmd_submit 转换为 submission_uncertain（见第 6 条）。
- 建议：**改文档**，明确模式和上传例外，退出码 2 的说明不变。

## 路径、持久化与模块说明

### 17. TASK.md 会被读取以检查占位符

- 文档：`docs/task-package.md:15`、`docs/task-package.zh-CN.md:15`、`taskmgr/template/TASK.md:27` 声称 taskmgr 从不读取它。
- 实际：`taskmgr/package.py:24` 把它列为 REQUIRED_FILES；`:256` 至 `:273` 读取其全部内容并检查 `<REQUIRED`。临时示例包的 TASK.md 改成一行占位符，`task validate --detail --plain` 实际退出 1，报 `placeholder`、path=TASK.md。规格 `agent/desc-1.md:113` 的“never interprets”与这个实现并不冲突。
- 建议：**改文档/模板说明**，改为“不解释或执行任务描述，但校验时读取并检查占位符”。

### 18. retry 暂存目录不是 jobid 命名

- 文档：`docs/internals.md:21`、`:42`，`docs/internals.zh-CN.md:21`、`:42`，以及规格 `agent/desc-2.md:260` 写 `incoming/retry-<jobid>/`。
- 实际：`taskmgr/store.py:622` 生成 `retry-<12 位随机十六进制>`；`:710` 才分配新的 16 位 jobid。手动 retry 在 `taskmgr/remote.py:316` 先创建暂存目录。实际调用 staging_dir 得到例如 `retry-473446673228`，它不是新旧 jobid。
- 建议：**改文档**，统一写 `incoming/retry-<random>/`；这不影响队列语义，无需为路径拼写改代码。

### 19. 已清空 dispatched_at 的 GPU 忙作业仍有历史运行文件

- 文档：`docs/internals.md:63`、`docs/internals.zh-CN.md:61` 和 `taskmgr/store.py:262` 的 has_run docstring 将“dispatched_at 为空”解释为“从未运行，也没有运行文件”。
- 实际：`taskmgr/monitor.py:254` 在 GPU 忙恢复重排时把 dispatched_at 设为 None，但 `:250` 明确保留 workdir，`:259` 保留 runner.prev-N.json，日志也保留。`taskmgr/store.py:263` 的 has_run 只是检查当前 dispatched_at。因此该窗口内 fetch 会报 no_run，非 follow 的 logs 也会报 no_run，虽然磁盘上存在历史文件；重新派发后又可读取。
- 建议：**改文档/docstring**，明确它表示“本次派发是否已有记录”，并记录重排窗口中的 logs/fetch 限制；如果希望历史文件一直可取，再改 has_run 和查询协议。

### 20. 文档代码版本规则应说明模板 Markdown 例外

- 文档：`docs/install.md:54`、`docs/install.zh-CN.md:53` 和 `taskmgr/pyz.py:8` 概括称改文档不会改变 code version 或触发重启。
- 实际：同一安装段也说 template 下所有文件计入哈希；`taskmgr/pyz.py:76` 正是 `rel.endswith(".py") or top == "template"`。所以修改作为说明文档的 `taskmgr/template/TASK.md` 会改变版本，重新构建并 host add 后会更新和重启。这与 README、docs/ 和 deploy 示例的处理不同。
- 建议：**改文档/docstring**，把概括改为“运行包之外的文档不影响版本；template 下 TASK.md 等内容属于分发资产，影响版本”。

### 21. 开发文档指向了不存在的 override 导出

- 文档：`docs/development.md:67`、`docs/development.zh-CN.md:64` 指导从 `taskmgr.records` 导入 override。
- 实际：`taskmgr/records.py:8` 及 `:13` 的导入和导出列表没有 override，运行时 `hasattr(taskmgr.records, "override")` 为 False；正确实现位于 `taskmgr/common.py:20` 至 `:28`，调用方如 `taskmgr/cliparse.py:14` 从 common 导入。
- 建议：**改文档**，模块名改为 `taskmgr.common`，保留 TYPE_CHECKING 时导入 typing_extensions、运行时 no-op 的正确说明。

### 22. 模块 docstring 还有旧模块名、旧命令名及遗漏子命令

- 文档：`taskmgr/hostlegacy.py:3`、`:32` 引用 `hostprobe.legacy`；`taskmgr/records.py:194` 写 `taskmgr validate`；`taskmgr/hostcmds.py:1` 只列 add/list/remove/ping。
- 实际：探测函数已在 `taskmgr/hostserver.py:114`，由 `taskmgr/hostsetup.py:269` 读取执行；仓库不存在 hostprobe 模块。CLI 在 `pyproject.toml:11` 注册为 task，validate 注册于 `taskmgr/cliparse.py:290`；host 的 HELP / parser 在 `taskmgr/hostcmds.py:26`、`:101`、`:110` 还实现 enable/disable。实际帮助中也有这两个子命令。
- 建议：**改 docstring**，替换为 hostserver.legacy、task validate，补齐 enable/disable。

### 23. “runner.json 在 workdir 外，所以任务不能写”没有实现权限隔离

- 规格：`agent/desc-1.md:395` 据路径位置推导任务不能写 runner.json；`docs/internals.md:34` / `docs/internals.zh-CN.md:34` 也称其只由 runner 写。
- 实际：`taskmgr/shim.py:244` 至 `:246` 只 chdir、恢复信号并 exec，没有 chroot、降权、ACL 或沙箱；任务与 shim 使用同一用户，job 目录和 runner 文件均可由这个用户写入（`taskmgr/common.py:127`、`taskmgr/remote.py:132`）。因此 `../runner.json` 并无权限屏障，绝对路径也可达。目录分离只是合作约定，不构成执行隔离。
- 建议：**改文档并明确规格落差**，说明只适用于可信任务、禁止任务修改管理文件。若“任务不能写”是强制安全要求，则必须另行**改代码**实现用户/沙箱隔离；不能用目录在 workdir 外作为实现证据。

### 24. --wait --timeout 没有限制单次查询，不能兑现总时限

- 文档：`docs/assumptions.md:31` / `docs/assumptions.zh-CN.md:28` 描述超时退出；规格 `agent/desc-1.md:544` 明确是 total wait time，`agent/desc-2.md:171` 还要求连接重试时适用 deadline。
- 实际：`taskmgr/taskmgr.py:400` 的 locate 和 `:402` 的 Remote.call 未传剩余 deadline，`taskmgr/transport.py:266` 使用独立默认 30 秒查询超时，查询完成后才检查总时限；终态、自动重跑分支还早于 deadline 检查。临时 local 命令先 sleep 0.3 秒再返回 queued，真实运行 `status ... --host s --wait --timeout 0.01 --detail --plain` 耗时约 0.905 秒后才返回 wait_timeout、退出 2。
- 建议：**改代码**，为单次查询及跨服务器定位传入剩余总时长，并定义超时与终态同时发生时的优先级。报告中的 0.905 秒包括进程启动开销，远程命令自身的 0.3 秒延迟已足以超过 0.01 秒预算。

### 25. 恢复页遗漏 resolve 将存活任务交回 daemon 的分支

- 文档：`docs/recovery.md:67`、`docs/recovery.zh-CN.md:60` 说仍有存活进程就保持 unknown，且输出 identity 和 alive_pids。
- 实际：`taskmgr/monitor.py:384` 仅在 stop_unconfirmed_reason 存在时保持 unknown；`:393` 之后否则恢复 running/stopping，返回 handed_to_daemon。隔离 mock boot_id 和 group_members，合成 unknown 活进程记录，实际 assess 返回 handed_to_daemon 及 alive_pids=[123]。缺失 runner 的分支（`:307`）也不能输出 alive_pids。
- 建议：**改文档**，与 `docs/assumptions.md:44` / `docs/assumptions.zh-CN.md:38` 已说明的分支保持一致，将 identity/alive_pids 描述为条件字段。

### 26. 全输出着色和每条命令返回对象的总述遗漏 logs

- 文档：`docs/usage.md:43`、`docs/usage.zh-CN.md:43` 说所有输出带颜色；`taskmgr/textout.py:3` 说每条命令返回 JSON 对象，由 renderer 生成文本。
- 实际：`taskmgr/taskmgr.py:520` 的 cmd_logs 直接让 Remote.stream 写 stdout.buffer，返回 `(None, 0)`；`taskmgr/transport.py:304` 原样转发字节。--plain 只控制 CLI 生成的文本，不会删除任务日志自带的 ANSI 颜色。
- 建议：**改文档/docstring**，明确 logs 原样输出、没有 detail 对象，颜色开关不处理任务日志。

### 27. 绝对入口路径的支持应明确说明

- 文档：`docs/task-package.md:36`、`docs/task-package.zh-CN.md:36` 在同一括号中说明含斜杠入口及“包内可执行文件”，没有单独交代绝对路径。
- 实际：`taskmgr/package.py:285` 仅检查相对路径是否在包内；`taskmgr/shim.py:79` 至 `:88` 明确接受远端绝对路径，并在启动前检查。临时示例入口改为 `/bin/true`，实际 validate 退出 0。该支持符合 `agent/desc-1.md:86`，属于文档说明不充分，不是非法命令。
- 建议：**改文档**，明确相对路径须在包内，绝对路径指远端文件、存在性和可执行性在启动前检查。

### 28. 顶层帮助列出的 --detail 在顶层位置不能解析

- 文档：`taskmgr/cliparse.py:131` 生成顶层 options 清单，将 --detail 与 --plain/--home 并列；`docs/usage.md:39` / `docs/usage.zh-CN.md:39` 未解释位置限制。
- 实际：`taskmgr/cliparse.py:276` 仅在结果子命令的 common parser 注册 detail，顶层没有该选项。真实 `python3 bin/task --detail jobs --plain` 退出 2，报 `unrecognized arguments: --detail`；`task jobs --detail` 可解析。--home 则需放在顶层。
- 建议：**改帮助和文档**，明确位置；也可以改 parser 接受顶层 detail，使顶层帮助与行为一致。

## 规格中已被后续决定覆盖的偏差

- `agent/desc-1.md:508`、`:526` 要求查询禁用主机；实现 `taskmgr/taskmgr.py:235` 只查 enabled，`docs/assumptions.md:78` / `docs/assumptions.zh-CN.md:65` 已明确是后续用户决定。建议保留代码，把该决定归并进规格。
- `agent/desc-1.md:566` 要求除 logs 外默认 JSON；`agent/desc-2.md:162` 只明确 jobs 表格的覆盖，而所有本地结果命令实际均默认文本（`taskmgr/taskmgr.py:778`），detail 才输出 JSON（`:775`）。`docs/usage.md:48` 已说明完整文本体系。建议在规格补记覆盖范围，保留已文档化的现行行为。

## 已核对且未发现漂移的主要部分

- 顶层命令、现行选项、状态名、协议版本 2、16 位 jobid、task_id/submission-key 格式、wait 默认 5/300 秒、本地 home 选择顺序、远程默认根路径与 TASKMGR_REMOTE_ROOT 的本地读取位置，均能对应当前 parser 和实现。`--attempt`、`--all-attempts`、`--remote-root` 不在现行帮助中；它们只在旧规格/后续取代说明中出现，不应恢复。
- `agent/desc-2.md:239` 起 D16 已明确覆盖旧 attempt 模型：新 job 重试、retry_of/retried_as、扁平运行目录、wait 跟随自动重跑均已实现。D17 的退出 75 重启、保留 workdir、延迟清除 pending_reason 已实现；D19 的预构建 zipapp、版本目录、单根布局及环境变量选择也已实现。
- 默认表与部署 config 示例一致；机器列表示例字段和 SSH identity_file 的本地展开与 transport 相符；systemd 示例的 KillMode 和生成器设置一致。示例中的地址、用户名、私钥为用户替换值，不认定为不存在路径。
- 包占位符/模式/大小校验、排除规则、安全解包、内容哈希、GPU UUID 解析、FIFO 预留调度、CPU 并发计数、日志/fetch 目标路径、终态结果合并与损坏备份、shim 双 fork 和锁交接、状态锁及恢复流程均逐段核对；上述条目列出了发现的例外。
- 根 README 的设计文档入口、开发工具配置（uv/ruff/basedpyright/pre-commit）与 `agent/desc-3.md` 要求相符；本报告不把 `/quiz` 的人工工作流约定当作 CLI 命令。

## 本次完整测试结果

将 `taskmgr/`、`bin/`、`pyproject.toml`、`.python-version` 复制到 `/tmp`（未复制 Git 元数据），在副本内执行 `python3 -m unittest discover -s taskmgr/tests`。Python 3.14.10 下运行 166 项，耗时 730.824 秒，164 项通过、2 项失败，退出码 1：

- `test_cli.CliTest.test_job_commands_find_the_host_by_jobid`（`taskmgr/tests/test_cli.py:153`）：期望新重试作业收到取消请求，实际查询时已经 succeeded，输出 nothing to cancel。
- `test_gpu_busy.GpuBusyTest.test_busy_exits_are_relaunched_then_succeed`（`taskmgr/tests/test_gpu_busy.py:85`）：期望 pending_reason 含 GPU busy，实际读到 None。

两处失败的直接表现都涉及测试观察时的作业状态；本次未进一步确认根因，不能声称测试全绿，也未为此修改代码或测试。它们不替代上面各项的代码证据和独立边界复现。测试副本及临时复现数据已清理；两个按要求构建的归档留在 `/tmp/dist`。
