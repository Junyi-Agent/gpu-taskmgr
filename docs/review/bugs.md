# taskmgr 缺陷审阅

审阅基线：`070f3a2`。已通读 taskmgr/*.py、bin/、template/、deploy/。

标注含义：**已验证**指用脚本直接调用模块函数复现（未启动 daemon，未写成仓库内 unittest）；**推断**指只读代码得出，未运行。
本轮未取得现有测试套件的基线结果。容器内没有 GPU、ssh 目标和 systemd，涉及它们的条目均为推断。
严重度内的编号即排序。

## 高

### H1. 任务可让 finalize 永久失败，作业卡住并一直占用 GPU（已验证）

- 位置：`taskmgr/store.py:322-333`、`taskmgr/store.py:424`、`taskmgr/store.py:741-745`、`taskmgr/common.py:147-166`
- 触发条件（任一）：
  - `workdir/result.json` 是目录：`write_result` 的 `os.replace` 抛 `IsADirectoryError`。
  - `result.json` 嵌套极深（实测 `'[' * 200000`）：`json.loads` 抛 `RecursionError`，而 `read_task_result` 只捕获 `ValueError`/`UnicodeDecodeError`。
  - `result.json` 是 FIFO：`open(p, "rb")` 无写端时阻塞（推断）。
- 实际后果：
  - 前两种情况下 `finalize_run` 抛异常，shim 退出且作业未终结。daemon 连续失败 3 次后标记 `unknown(recovery_error)`，GPU 不释放。
  - `task resolve` 走同一路径，同样失败。
  - `--as stopped` 经 `end_without_result` 同样抛异常（目录情况已验证），事务未完成，job 保持 unknown，但此前 `mark_run_interrupted` 已写入 runner.json。没有 CLI 手段释放 GPU，只能手工删 `workdir/result.json`。
  - FIFO 情况下，若发生在 `end_without_result` 中，会在持有 state lock 时阻塞，整台服务器的调度和提交都会停住（推断）。
- 修复建议：
  - `read_task_result` 先 `lstat`，非普通文件时改名保留，并按 corrupt 处理；打开时加 `O_NONBLOCK`。
  - 捕获 `RecursionError`。
  - `write_result` 写入前处理目标不是普通文件的情况。
  - `try_read_json` 同样捕获 `RecursionError`，并避免打开 FIFO（`job_status` 读 `progress.json` 也有同样的阻塞风险）。

## 中

### M1. `task resolve` 让 unknown 作业自动重跑（已验证）

- 位置：`taskmgr/monitor.py:339-342`、`taskmgr/monitor.py:360-365`、`taskmgr/store.py:533-578`
- 触发条件：作业因 SIGKILL 后仍未确认停止而处于 unknown，且 `stop_reason` 可能是 timeout；机器重启后运行 `task resolve`。无 identity 的 unknown 作业走 `interrupt_run` 同一分支（推断）。
- 实际后果：`assess(resolve=True)` 调用 `interrupt_run`，自动克隆出新的 queued 作业。复现结果为 `decision=interrupted`，原作业 `failed`，新克隆 `queued`。这违反 docs/recovery.md 的“unknown jobs never re-run”和 desc-1 §18.2。已超时的作业也会被重新执行。
- 修复建议：`resolve=True` 时不克隆，直接终结：有 cancel 或 stop_reason 时用对应终态，否则 `failed(interrupted)`，由操作者显式 `task retry`。

### M2. `task submit` 用默认 1 GiB 限制拦截，忽略服务器配置（已验证）

- 位置：`taskmgr/taskmgr.py:142`、`taskmgr/taskmgr.py:158`、`taskmgr/taskmgr.py:184`、`taskmgr/package.py:238-239`、`taskmgr/package.py:304`
- 触发条件：包大于 1 GiB，服务器 `max_package_bytes` 设得更大或为 `null`。
- 实际后果：本地校验返回 `package_too_large`，无法提交，也没有绕过选项，服务器配置不起作用。
- 修复建议：`cmd_submit` 调用校验时传 `max_bytes=None`，由服务器判定；`task validate` 的超限降为警告。

### M3. 归档内的 `result.json` 进入 workdir，缺结果的任务可能误判成功（部分验证）

- 位置：`taskmgr/package.py:37-49`、`taskmgr/package.py:215-235`、`taskmgr/remote.py:109-133`、`taskmgr/shim.py:123`
- 触发条件：提交的 tar.gz 含历史运行留下的 `result.json`，任务退出 0 且没有重写它。
- 实际后果：已验证 `result.json` 被 `is_excluded` 判为排除，因此校验不检查它。`safe_extract` 不应用排除规则，文件会留在 `package/` 并复制到 workdir（推断）。finalize 读到旧 `outputs`，作业变成 `succeeded`（推断）。目录提交因打包时排除而不受影响，同一内容两种提交方式行为不同。
- 修复建议：服务器在 `cmd_receive` 解压后删除 `is_excluded` 命中的条目，再计算哈希和生成 `package/`。

### M4. 包内 `result.json*` 前缀文件被静默丢弃（已验证）

- 位置：`taskmgr/package.py:34`、`taskmgr/package.py:44`
- 触发条件：包根目录有 `result.jsonl`、`result.json.d/` 等；任意层级的 `*.json.lock` 同理。
- 实际后果：`is_excluded` 按前缀匹配，这些文件不打包也不计入哈希，且无任何提示。任务在远端缺少输入文件。
- 修复建议：根目录只精确排除 `result.json`、`result.json.lock`、`result.json.corrupt*`，并在校验时列出被排除的文件。

## 低

### L1. 仅大小写不同的文件被判为重复（已验证）

- 位置：`taskmgr/package.py:416`、`taskmgr/package.py:444-447`
- 触发条件：包或任务产物同时含 `Data.csv` 和 `data.csv`。
- 实际后果：本地目录校验通过，但服务器解压报 `unsafe_package: duplicate entry`，提交失败。`task fetch` 走 `skip_unsafe`（推断）时其中一个文件被跳过，只有警告。Linux 上这两个文件是合法的不同文件。
- 修复建议：`seen` 用原始名称；只在大小写不敏感的本地文件系统上由 `lstat` 检测冲突。

### L2. `--wait` 可能跟随手动重试（推断）

- 位置：`taskmgr/taskmgr.py:321-332`
- 触发条件：作业 `error.kind=interrupted`，之后用户又手动 `task retry`。
- 实际后果：`retried_as[-1]` 是手动重试的作业，违反 D16“manual retries are not followed”。
- 修复建议：在 `job.json` 单独记录 `auto_rerun`，或取 `retried_as` 中 `auto_restarts > 0` 的作业。

### L3. `--as stopped` 中途崩溃后再次 resolve 会重跑（推断）

- 位置：`taskmgr/remote.py:360-368`、`taskmgr/monitor.py:311-316`
- 触发条件：`mark_run_interrupted` 之后、job 保存之前进程崩溃，随后运行 `task resolve`。H1 的异常路径也会留下同样的记录。
- 实际后果：runner 已是 final/interrupted，`assess` 调用 `interrupt_run`，作业被克隆重跑，而“--as stopped”承诺不重跑。
- 修复建议：在 `interrupted_reason == "operator_marked_stopped"` 时直接终结；可与 M1 一起修。

### L4. `task retry` 超时后重试会产生重复作业（推断）

- 位置：`taskmgr/transport.py:265-266`、`taskmgr/remote.py:314-343`
- 触发条件：`retry` 超过 30 秒未返回，客户端报连接错误，但远端已经或仍会完成。
- 实际后果：用户再次 retry 时生成第二个新作业，`retry` 没有幂等键。
- 修复建议：`retry` 接受客户端生成的幂等键，并持久化到 `job.json`。

### L5. 请求取消后任务恰好在接管期间退出，结果记为 `exit_code_lost`（推断）

- 位置：`taskmgr/monitor.py:494-497`
- 触发条件：daemon 接管中，`cancel_requested` 已写入，进程组在下一次 poll 前退出。
- 实际后果：`finish_stopped(None, ...)` 给出 `failed`，而 `assess` 同样场景给出 `cancelled`。
- 修复建议：tick 在 finish 前重读 job，用 `requested_stop` 确定原因。

### L6. 重新排队的作业无法查看日志（推断）

- 位置：`taskmgr/monitor.py:254`、`taskmgr/remote.py:409-411`、`taskmgr/remote.py:458-459`
- 触发条件：GPU busy 后被 `requeue_gpu_busy` 放回队列，之后在队列中被取消或等待。
- 实际后果：`dispatched_at` 已清空，`has_run` 为假，`logs`/`fetch` 返回 `no_run`，已有日志和 workdir 不可取回。
- 修复建议：`has_run` 同时检查 `runner.prev-*` 或单独记录 `ever_ran`。

### L7. `.link-new` 残留使 `task host add` 持续失败（推断）

- 位置：`taskmgr/hostsetup.py:686-687`
- 触发条件：`ln -s` 与 `mv` 之间中断，留下 `taskmgr-remote.pyz.link-new`。
- 实际后果：下次运行时 `ln -s` 报文件已存在，`set -e` 终止，违反幂等，需手工删除。
- 修复建议：`ln -s` 前先 `rm -f`，或用 `ln -sfn` 写入临时名。

### L8. data 步骤停止服务后，拒绝或失败时不提示服务已停（推断）

- 位置：`taskmgr/hostsetup.py:805-838`
- 触发条件：`_check_data` 已 `systemctl stop`，之后用户拒绝移动或 `mv` 失败。
- 实际后果：后续步骤全部跳过，daemon 保持停止，调度中断，输出里没有说明。
- 修复建议：记录“本次停止了服务”，结束时重启，或在 detail 中明确提示。

### L9. 模板 helper 在 Python 3.9 及更早版本的 venv 中无法导入（推断）

- 位置：`taskmgr/template/taskmgr_task.py:90`、`taskmgr/template/taskmgr_task.py:236`
- 触发条件：任务 venv 使用 Python 3.9 或更早版本。服务端要求 3.10，但任务 venv 是独立环境。
- 实际后果：`float | None` 在定义函数时求值，抛 `TypeError`，模板任务启动即失败。
- 修复建议：文件开头加 `from __future__ import annotations`，并处理 `cast`/别名中的运行时表达式。

## 未完成的范围

- `hostsetup.py` 的 ssh/systemd 路径只做了读代码审阅；ControlPersist 与 `capture_output` 的交互未核实。
- 竞态类问题只做了代码推演，未做压力测试。
