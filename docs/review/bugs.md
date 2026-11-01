# taskmgr 缺陷审阅

基线：`070f3a2`。范围：taskmgr/*.py、bin/、taskmgr/template/、taskmgr/deploy/，设计意图对照 agent/desc-*.md 和 docs/。

标注：

- **已验证**：在 Linux 容器（Python 3.10）里用 `taskmgr/tests/helpers.Env` 写的临时 unittest 或直接调用模块函数复现。daemon、shim 和 local transport 都是真实进程，GPU 和 boot_id 用 `TASKMGR_NVIDIA_SMI` / `TASKMGR_BOOT_ID_FILE` 模拟。复现脚本没有放进仓库。
- **推断**：只读代码得出，没有运行。依赖 ssh、systemd 或特定时序的条目属于这一类。

同一严重度内按影响排序。

## 高

### H1. `result.json` 是 FIFO 时 shim 永久阻塞；处理时整台服务器的状态锁被占住（已验证）

- 位置：
  - `taskmgr/store.py:323`：`read_task_result` 直接 `open(p, "rb")`，没有检查文件类型。
  - `taskmgr/shim.py:458`：shim 在 `finalize_run` 中走到上面这行。
  - `taskmgr/daemon.py:333`、`taskmgr/monitor.py:381`：daemon 的 health check 在持有 `state.lock` 时调用 `assess` → `finish_stopped` → `finalize_run`，同样走到 `store.py:323`。
  - `taskmgr/common.py:150`：`try_read_json` 也一样，`job_status` 读 `progress.json`（`taskmgr/remote.py:275`）会受影响。
- 触发条件：任务执行 `mkfifo "$TASKMGR_RESULT_FILE"`（或在 workdir 里留下名为 `result.json` 的 FIFO）后退出。
- 实际后果（复现结果）：
  1. 任务退出后，shim 阻塞在 `open()` 上。job 一直是 `running`，`runner.final=false`，GPU 不释放。
  2. `task cancel` 写入了 `cancel_requested=true`，但 5 秒后 job 仍是 `running`：shim 卡住，不再轮询；shim.lock 还被持有，daemon 判定 `shim_alive`，不接管。`timeout_seconds` 也不起作用。
  3. 运维 kill 掉 shim 后，daemon 的 health check 在持有 `state.lock` 时同样阻塞在这个 FIFO 上。之后 `task submit --host s1` 40 秒没有返回（上传没有超时，`receive` 一直等状态锁）。`cancel`、`retry`、`resolve` 和调度也全部停住，直到有人往 FIFO 写数据或删除它。
  4. `progress.json` 是 FIFO 时，`task status` 在远端阻塞，客户端 30 秒后报 connection_error（推断）。
- 修复建议：
  - 先 `os.lstat`。不是普通文件时，把它改名保留（与 corrupt 的处理相同），并按“result.json 无效”记录校验错误。
  - 打开时加 `O_NONBLOCK | O_NOFOLLOW`。
  - `try_read_json` 用同样的方式打开。
  - 任务产物的读取不要放在 `state.lock` 内执行。

### H2. `result.json` 是目录或嵌套过深的 JSON 时 finalize 永久失败，`--as stopped` 也无法释放 GPU（已验证）

- 位置：
  - `taskmgr/store.py:329-333`：只捕获 `ValueError`/`UnicodeDecodeError`，没有捕获 `RecursionError`。
  - `taskmgr/store.py:424`：`write_result` 的 `os.replace` 写到目录上会抛 `IsADirectoryError`。
  - `taskmgr/store.py:741-745`：`end_without_result` 走同一路径。
  - `taskmgr/remote.py:360-368`：`resolve --as stopped` 先调用 `mark_run_interrupted`，再调用 `end_without_result`。
- 触发条件（任一）：任务执行 `mkdir "$TASKMGR_RESULT_FILE"`；或任务向 result.json 写入 `'[' * 200000`。
- 实际后果（复现结果）：
  1. 每次 `finalize_run` 都抛异常。daemon 连续失败 3 次后，job 变成 `unknown`（`recovery_error`），GPU 不释放。
  2. `task resolve --as stopped --confirm <jobid>` 返回 `internal_error: IsADirectoryError`。job 仍是 `unknown`，但 `runner.json` 已经被改成 `final/interrupted`（`interrupted_reason=operator_marked_stopped`）。
  3. 接着运行普通的 `task resolve`：`assess` 看到 `runner` 是 `final/interrupted`，于是调用 `interrupt_run`，先克隆出一个 `queued` 的重跑作业，然后在 `end_without_result` 处再次抛异常。原作业仍是 `unknown`，继续占着 GPU；重跑作业会被调度执行，违反“`--as stopped` 不会重跑”的约定。
  4. 只能在服务器上手工删除 `workdir/result.json`。
- 修复建议：
  - `read_task_result` 捕获 `RecursionError`。
  - 目标不是普通文件时，先把它改名移开，再写入。
  - `cmd_resolve --as stopped` 只在 `end_without_result` 成功后才提交 runner 的修改（或者先写 job，再写 runner）。
  - `assess` 遇到 `interrupted_reason == "operator_marked_stopped"` 时直接终结，不重跑。

## 中

### M1. 超时后处于 unknown 的作业，重启机器再 `task resolve` 会被自动重跑（已验证）

- 位置：`taskmgr/monitor.py:360-365`（boot_id 变化 → `interrupt_run`）、`taskmgr/monitor.py:311-316`、`taskmgr/store.py:547-577`（`interrupt_run` 只看 `cancel_requested`，不看 `stop_reason`）。
- 触发条件：
  1. 作业因 `timeout_seconds` 进入停止流程。
  2. SIGKILL 后仍有进程存活（常见于 D 状态的 GPU 进程），作业变成 `unknown`，`stop_reason=timeout`。
  3. 运维按 docs/recovery.md 的提示重启机器来清掉 D 状态进程，然后运行 `task resolve`。
- 实际后果（复现结果）：`resolve` 返回 `decision=interrupted`。原作业变成 `failed`（`interrupted`，"machine rebooted"），并克隆出一个 `queued` 的新作业，`retried_as` 指向它。一个已经超时的任务会以完整的时间预算重新执行。这违反 docs/recovery.md 的“`unknown` jobs never re-run”，以及 desc-1 验收项“The unknown state never re-runs”。对 `stopping` 状态（超时停止中）的作业，daemon 重启恢复时也走同一分支（推断）。
- 修复建议：
  - `interrupt_run` 在 `stop_reason == "timeout"` 时终结为 `timed_out`。
  - `assess(resolve=True)` 不调用 `clone_job`：按 cancel/stop_reason 结束；没有停止原因时结束为 `failed(interrupted)`，由操作者显式运行 `task retry`。

### M2. 归档内的 `result.json` 原样进入 workdir，任务没写结果也会判为成功（已验证）

- 位置：`taskmgr/package.py:37-49`（`is_excluded` 把 `result.json` 排除在校验和哈希之外）、`taskmgr/package.py:402-545`（`safe_extract` 不应用排除规则）、`taskmgr/remote.py:109-129`、`taskmgr/shim.py:123`。
- 触发条件：提交 tar.gz，其中带有以前某次运行留下的 `result.json`；任务退出码为 0，但没有重写 result.json。
- 实际后果（复现结果）：用同一个包（入口为 `true`，`required_outputs=["best"]`）提交两次：
  - 以归档提交，得到 `succeeded`，`outputs={"best": "stale"}`。
  - 以目录提交，得到 `invalid_result`。

  旧结果被当成本次结果。`content_hash` 不包含该文件，所以去重也无法区分这两种包。
- 修复建议：`cmd_receive` 解压后，删除所有命中 `is_excluded` 的条目，再计算哈希和生成 `package/`。`task validate` 对归档里被排除的条目给出警告。

### M3. 包根目录下以 `result.json` 开头的文件被静默丢弃（已验证）

- 位置：`taskmgr/package.py:34`、`taskmgr/package.py:44`。
- 触发条件：包根目录有 `result.jsonl`、`result.json.bak`、`result.json.d/` 之类的文件。任意层级下以 `.json.lock` 结尾的文件同理。
- 实际后果（复现结果）：`task validate` 返回 `valid=true`，但打包时没有这个文件。远端任务执行 `cat result.jsonl` 失败（`No such file or directory`），作业 `failed`。整个过程没有任何提示。
- 修复建议：根目录只精确排除 `result.json`、`result.json.lock` 和 `result.json.corrupt*`。校验输出中列出被排除的路径。

### M4. `task submit` 在本地按默认 1 GiB 拦截，服务器的 `max_package_bytes` 不起作用（已验证）

- 位置：`taskmgr/taskmgr.py:184` → `taskmgr/taskmgr.py:142`/`158` → `taskmgr/package.py:238-239`、`taskmgr/package.py:304`。
- 触发条件：服务器 `config.json` 设置 `"max_package_bytes": null` 或更大的值，包的大小超过 1 GiB。
- 实际后果（复现结果）：`task submit --host s1` 返回 `invalid_package` / `package_too_large (1073741824)`，退出码 2。没有绕过选项。
- 修复建议：`cmd_submit` 调用 `validate_dir(..., max_bytes=None)`，由服务器判定大小。`task validate` 把超限降为警告。

## 低

### L1. 因 GPU busy 被放回队列的作业，日志和 workdir 无法查看（已验证）

- 位置：`taskmgr/monitor.py:254`（清空 `dispatched_at`）、`taskmgr/store.py:261-263`（`has_run` 只看 `dispatched_at`）、`taskmgr/remote.py:409-411`、`taskmgr/remote.py:458-459`、`taskmgr/remote.py:296-299`。
- 触发条件：shim 在等待 GPU 重试期间死掉，daemon 用 `requeue_gpu_busy` 把作业放回 `queued`；之后作业还在排队，或在队列中被取消。
- 实际后果（复现结果）：`stdout.log` 里有 `first-launch`，但 `task logs` 报 `has not run (no_run)`。`fetch` 同样拒绝。如果在队列中取消，作业会以“未运行”结束，已有的日志和 workdir 都取不回来。
- 修复建议：`has_run` 同时检查 `runner.prev-*.json`，或者单独持久化一个 `ever_ran` 字段。

### L2. 只有大小写不同的文件名被判为重复条目（已验证）

- 位置：`taskmgr/package.py:416`、`taskmgr/package.py:444-447`。
- 触发条件：包或任务产物中同时有 `Data.csv` 和 `data.csv`。
- 实际后果（复现结果）：`safe_extract` 报 `unsafe archive entry 'data.csv': duplicate entry`。本地目录校验能通过，但服务器拒收。`task fetch` 走 `skip_unsafe` 分支时，会丢掉其中一个文件，只给出警告（推断）。
- 修复建议：`seen` 用原始名称。大小写冲突交给后面的 `lstat`/`O_EXCL` 检测；在不区分大小写的文件系统上，这一步已经能拒绝冲突。

### L3. 残留的 `taskmgr-remote.pyz.link-new` 让 `task host add` 一直失败（已验证）

- 位置：`taskmgr/hostsetup.py:686-687`。
- 触发条件：上一次 `host add` 在 `ln -s` 和 `mv -f` 之间中断（例如 ssh 断开），留下了 `.link-new`。
- 实际后果（复现结果）：之后每次运行，`ln -s` 都报 `File exists`，`set -e` 使脚本退出，step data 失败，已上传的代码被丢弃。只能手工删除这个文件。
- 修复建议：先 `rm -f "$path.link-new"`，再 `ln -s`；或者用 `ln -sfn` 写入临时名。

### L4. `task retry` 与作业结束并发时，可能克隆出缺少 `package/` 的作业（部分验证）

- 位置：`taskmgr/remote.py:316-322`、`taskmgr/store.py:656-682`。
- 触发条件：`retry` 在锁外执行 `stage_package` 期间，作业目录从 `in-progress/` 移到 `done/`（作业恰好结束）。
- 实际后果：`os.walk` 遍历不存在的目录时不报错。已验证：源目录只有 `package.tar.gz`、没有 `package/` 时，`stage_package` 正常返回，staging 目录里只有 `package.tar.gz`。只有链接 `package.tar.gz` 时抛出的 `OSError` 会让代码回退到在锁内重新 staging；目录在遍历途中移走则不会。随后 `clone_job` 入队一个没有 `package/`（或只有部分文件）的作业。shim 的 `prepare_workdir` 失败，作业以 `shim_error` 结束；如果只缺部分文件，会以错误的内容运行（推断）。
- 修复建议：`link_tree` 给 `os.walk` 传 `onerror` 并抛出异常；或者在锁内确认 job 已是终态、目录未变后，再使用锁外 staging 的结果，否则在锁内重新 staging。

### L5. 接管期间请求取消，进程组恰好在下次轮询前退出时，结果记为 `exit_code_lost`（推断）

- 位置：`taskmgr/monitor.py:494-497`。
- 触发条件：daemon 接管中，`cancel_requested` 已写入，而进程组在下一次 `control_poll` 之前全部退出。
- 实际后果：`finish_stopped(None, ...)` 把作业结束为 `failed`（`exit_code_lost`）。同样场景下 `assess`（`monitor.py:333-338`）给出的是 `cancelled`，两条路径结果不一致。
- 修复建议：`tick` 在 finish 前重读 job，用 `requested_stop` 确定原因。

### L6. `task status --wait` 可能跟随手动 retry 的作业（推断）

- 位置：`taskmgr/taskmgr.py:321-332`。
- 触发条件：作业因中断结束（`error.kind=interrupted`，`retried_as=[自动重跑]`），用户之后又对它运行了 `task retry`。
- 实际后果：`retried_as[-1]` 是手动重试产生的作业，`--wait` 跟随它，而不是自动重跑的作业。这违反 docs/recovery.md 中“`--wait` follows the re-run”的约定。
- 修复建议：在 `job.json` 中单独记录自动重跑的 jobid，或者选 `retried_as` 中 `auto_restarts > 0` 的那个作业。

### L7. step data 停掉服务后被拒绝或失败，服务保持停止，输出不提示（推断）

- 位置：`taskmgr/hostsetup.py:801-838`。
- 触发条件：`_check_data` 已执行 `systemctl --user stop`，之后用户拒绝移动目录，或者 `mv` 失败。
- 实际后果：step data 是必需步骤，后续步骤全部跳过，daemon 保持停止，服务器停止调度。只有“仍有活动作业”这个分支的报错会说明服务已停。
- 修复建议：记录“本次运行停止了服务”，在 `run()` 的 `finally` 中重新启动服务，或者在 detail 中写明。

### L8. 模板 helper 在 Python 3.9 及更早版本的任务 venv 中导入失败（推断）

- 位置：`taskmgr/template/taskmgr_task.py:90`（`percent: float | None`）、`taskmgr/template/taskmgr_task.py:236`。
- 触发条件：`environment.venv` 指向 Python 3.9 或更早版本。服务端要求 3.10，但任务 venv 是独立的环境。
- 实际后果：`X | None` 注解在函数定义时求值，抛出 `TypeError`。`run.py` 执行 `import taskmgr_task` 时就失败，作业 `failed`。
- 修复建议：文件开头加 `from __future__ import annotations`。
