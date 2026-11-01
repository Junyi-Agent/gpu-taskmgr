# 审阅汇总

汇总五份审阅报告，去重后按严重度和修复优先级排序。各条保留原报告的证据、`file:line`、验证情况和建议；原条目编号写在“来源”中，便于回查。

| 简称 | 报告 | 分支 | 写报告时的 HEAD |
| --- | --- | --- | --- |
| BUG | 缺陷审阅 | `review/bugs` | `070f3a2` |
| CLI | CLI 结构与帮助文本 | `review/cli` | `960d0ba` |
| CON | 一致性审查 | `review/consistency` | `070f3a2` |
| TST | 测试审查 | `review/tests` | `070f3a2` |
| DOC | 文档漂移核查 | `review/docs-drift` | `77f28a5` |

**基线说明**：`960d0ba` 和 `77f28a5` 相对 `070f3a2` 只多出各自的报告文件（`git diff --stat` 已确认），所以五份报告针对的是同一份代码，行号可以直接对照。汇总时抽查了 P0 和部分 P1 条目的行号（`store.py:323-333`、`store.py:424`、`remote.py:360-368`、`monitor.py:360-365`、`shim.py:55`/`:74`、`hostsetup.py:617`/`:661-665`、`taskmgr.py:182`/`:400-402`、`package.py:37-49`/`:238`），都和描述一致。汇总没有重新运行任何复现。

**验证标注**（沿用各报告的说法）：

- **已验证**：报告作者实际运行过复现（临时 unittest、直接调用模块函数、local transport 或真实 CLI）。
- **部分验证**：只复现了其中一部分。
- **推断**：只读代码得出，没有运行。
- **mock**：在隔离或打桩的环境里验证，没有接触真实服务器。

**优先级定义**：

- **P0**：会卡死服务器、GPU 永久不释放，或违反核心恢复约定。优先修。
- **P1**：结果错误、静默丢数据、承诺的行为没有兑现，或测试给出虚假的安全感。
- **P2**：边界条件下出错、文档写错导致误操作、维护时容易改漏。
- **P3**：用词、风格、帮助文本精简。

---

## 1. 总览

| ID | 标题 | 优先级 | 类别 | 来源 | 验证 |
| --- | --- | --- | --- | --- | --- |
| P0-1 | `result.json`/`progress.json` 是 FIFO 时 shim 和 daemon 持锁永久阻塞 | P0 | 缺陷 | BUG H1 | 已验证 |
| P0-2 | `result.json` 是目录或嵌套过深时 finalize 永久失败，`--as stopped` 反而触发重跑 | P0 | 缺陷 | BUG H2 | 已验证 |
| P0-3 | unknown 作业经 `resolve` 或重启后被自动重跑 | P0 | 缺陷 | BUG M1 + DOC 1 | 已验证 |
| P1-1 | 归档内残留的 `result.json` 被当成本次结果 | P1 | 缺陷 | BUG M2 | 已验证 |
| P1-2 | 包根目录下以 `result.json` 开头的文件被静默丢弃 | P1 | 缺陷 | BUG M3 | 已验证 |
| P1-3 | `submit` 在本地按 1 GiB 拦截，服务器的 `max_package_bytes` 不起作用 | P1 | 缺陷 | BUG M4 | 已验证 |
| P1-4 | 关闭 GPU 忙约定后，`base_env` 仍会注入 `TASKMGR_GPU_BUSY_EXIT_CODE` | P1 | 缺陷 | DOC 2 | 已验证 |
| P1-5 | `gpu_busy_exit_code` 合法范围在服务器和 helper 之间不一致 | P1 | 缺陷 | CON 5.2 | 推断 |
| P1-6 | `--wait --timeout` 管不住单次查询，总时限兑现不了 | P1 | 缺陷 | DOC 24 | 已验证 |
| P1-7 | `host add` 在确认之前就删除暂存上传 | P1 | 缺陷/承诺 | DOC 4 | mock |
| P1-8 | fetch 省略特殊文件时既不警告，也不记入 `skipped` | P1 | 缺陷/承诺 | DOC 3 | 已验证 |
| P1-9 | 已观察到的不稳定测试（3 次全量运行都有失败） | P1 | 测试 | TST T1/T2 + DOC 测试结果 | 已验证 |
| P1-10 | 改坏被测代码后测试仍然通过（变异未被发现） | P1 | 测试 | TST B1-B5 | 已验证 |
| P2-1 | GPU busy 重排后 `has_run` 判为“未运行”，日志和 workdir 取不回 | P2 | 缺陷 | BUG L1 + DOC 19 | 已验证 |
| P2-2 | 按 jobid 查找成功时不报告跳过的禁用服务器 | P2 | 缺陷/承诺 | DOC 7 | 已验证 |
| P2-3 | 任务 helper 写 JSON 时没有 fsync 所在目录 | P2 | 缺陷/承诺 | DOC 5 | mock |
| P2-4 | 只有大小写不同的文件名被判为重复条目 | P2 | 缺陷 | BUG L2 | 已验证 |
| P2-5 | 残留的 `.link-new` 让 `host add` 一直失败 | P2 | 缺陷 | BUG L3 | 已验证 |
| P2-6 | `retry` 与作业结束并发时，可能克隆出缺少 `package/` 的作业 | P2 | 缺陷 | BUG L4 | 部分验证 |
| P2-7 | 接管期间取消，结果被记为 `exit_code_lost` | P2 | 缺陷 | BUG L5 | 推断 |
| P2-8 | `status --wait` 可能跟随手动 retry 的作业 | P2 | 缺陷 | BUG L6 | 推断 |
| P2-9 | step data 停掉服务后失败，服务保持停止且没有提示 | P2 | 缺陷 | BUG L7 | 推断 |
| P2-10 | 模板 helper 在 Python ≤3.9 的任务 venv 中导入失败 | P2 | 缺陷 | BUG L8 | 推断 |
| P2-11 | 顶层帮助列出的 `--detail` 放在顶层无法解析 | P2 | CLI/文档 | CLI 2 + DOC 28 | 已验证 |
| P2-12 | 文档漂移：会导致误操作的部分 | P2 | 文档 | DOC 6、8-16、21 | 多数已验证 |
| P2-13 | 文档漂移：设计保证写得过强 | P2 | 文档/规格 | DOC 17-20、23、25-27 | 已验证/推断 |
| P2-14 | 测试中的时序依赖、环境依赖和弱断言 | P2 | 测试 | TST B6-B8、T3-T6、E1-E4、W1-W12 | 部分已验证 |
| P2-15 | 高风险路径缺少测试 | P2 | 测试 | TST §5 | 覆盖率数据 |
| P2-16 | 一致性问题：同名不同义、改漏风险高的部分 | P2 | 一致性 | CON 1.1、1.5、2.3、3.1、4.3 等 | 读代码 |
| P3-1 | CLI 帮助精简与补全 | P3 | CLI | CLI 1、3-19 | 已验证（`--help` 遍历） |
| P3-2 | 其余一致性与风格问题 | P3 | 一致性 | CON 其余条目 | 读代码 |
| P3-3 | 测试重复、白盒依赖和效率 | P3 | 测试 | TST D1-D8、I1-I4、4.7 | 读代码 |

---

## 2. P0：先修

### P0-1. `result.json`/`progress.json` 是 FIFO 时，shim 和 daemon 持锁永久阻塞

- 来源：BUG H1。已验证（Linux 容器，Python 3.10，daemon、shim 和 local transport 都是真实进程）。
- 位置：
  - `taskmgr/store.py:323`：`read_task_result` 直接 `open(p, "rb")`，没有检查文件类型。
  - `taskmgr/shim.py:458`：shim 在 `finalize_run` 中走到上面这行。
  - `taskmgr/daemon.py:333`、`taskmgr/monitor.py:381`：health check 在**持有 `state.lock`** 时经 `assess` → `finish_stopped` → `finalize_run` 走到同一行。
  - `taskmgr/common.py:150`：`try_read_json` 同样如此，`job_status` 读 `progress.json`（`taskmgr/remote.py:275`）会受影响。
- 触发：任务执行 `mkfifo "$TASKMGR_RESULT_FILE"`（或在 workdir 留下名为 `result.json` 的 FIFO）后退出。
- 后果（复现结果）：
  1. shim 阻塞在 `open()`，作业一直是 `running`，`runner.final=false`，GPU 不释放。
  2. `task cancel` 写入了 `cancel_requested=true`，5 秒后作业仍是 `running`。原因是 shim 卡住不再轮询，shim.lock 也还被持有，daemon 判定 shim 仍存活，不接管。`timeout_seconds` 也失效。
  3. kill 掉 shim 后，daemon 的 health check 持 `state.lock` 阻塞在同一个 FIFO 上。之后 `task submit` 40 秒不返回；`cancel`、`retry`、`resolve` 和调度全部停住。**整台服务器停摆**。
  4. `progress.json` 是 FIFO 时，`task status` 在远端阻塞，客户端 30 秒后报 connection_error（推断）。
- 建议：
  - 先 `os.lstat`，不是普通文件时改名保留（与 corrupt 文件的处理相同），按“result.json 无效”记录校验错误。
  - 打开时加 `O_NONBLOCK | O_NOFOLLOW`；`try_read_json` 用同样的方式打开。
  - 任务产物的读取移出 `state.lock`。
- 测试：目前没有覆盖。修复时补“FIFO 形式的 result.json / progress.json”用例。

### P0-2. `result.json` 是目录或嵌套过深时 finalize 永久失败，`--as stopped` 还会触发重跑

- 来源：BUG H2。已验证。
- 位置：
  - `taskmgr/store.py:329-333`：只捕获 `ValueError`/`UnicodeDecodeError`，没有捕获 `RecursionError`。
  - `taskmgr/store.py:424`：`write_result` 用 `atomic_write_json` 往目录上 `os.replace`，抛出 `IsADirectoryError`。
  - `taskmgr/store.py:741-745`：`end_without_result` 走同一路径。
  - `taskmgr/remote.py:360-368`：`resolve --as stopped` 先 `mark_run_interrupted`，后 `end_without_result`。
- 触发（任一）：任务执行 `mkdir "$TASKMGR_RESULT_FILE"`；或往 result.json 写 `'[' * 200000`。
- 后果（复现结果）：
  1. `finalize_run` 每次都抛异常，daemon 连续失败 3 次后作业变成 `unknown`（`recovery_error`），GPU 不释放。
  2. `task resolve --as stopped --confirm <jobid>` 返回 `internal_error: IsADirectoryError`。此时 `runner.json` 已经被改成 `final/interrupted`，作业仍是 `unknown`。
  3. 再运行普通的 `task resolve`：`assess` 看到 `final/interrupted`，调用 `interrupt_run`，**先克隆出一个 queued 的重跑作业**，然后再次抛异常。原作业继续占着 GPU，重跑作业会被调度。这违反“`--as stopped` 不会重跑”的约定。
  4. 只能到服务器上手工删除 `workdir/result.json`。
- 建议：
  - `read_task_result` 捕获 `RecursionError`。
  - 写入前，目标不是普通文件就先改名移开。
  - `--as stopped` 只在 `end_without_result` 成功后才提交 runner 的修改（或者先写 job，再写 runner）。
  - `assess` 遇到 `interrupted_reason == "operator_marked_stopped"` 时直接终结，不重跑。
- 测试：TST W1 和 §5 第 7 项指出，remote 侧 `resolve --as stopped` 的校验（`remote.py:349`、`:351`）和 `internal_error` 兜底（`remote.py:644-656`）都没有测试。修复时一起补上。

### P0-3. unknown 作业经 `resolve` 或机器重启后被自动重跑

- 来源：BUG M1（原定“中”）与 DOC 1 合并。两份报告各自独立复现，汇总时提升为 P0：它直接违反 `docs/recovery.md:64` 的“`unknown` jobs never re-run”，也违反规格 `agent/desc-1.md:634` 的验收项，而且会让一个已超时的任务带着完整的时间预算再跑一遍。
- 位置：
  - `taskmgr/monitor.py:339`：没有身份、也没有记录 exited 的 runner 会调用 `interrupt_run`（DOC）。
  - `taskmgr/monitor.py:360-365`：boot_id 变化时调用 `interrupt_run`，没有排除 `resolve=True` 的 unknown（BUG、DOC）。
  - `taskmgr/monitor.py:311-316`。
  - `taskmgr/store.py:427`：`_settled` 不包含 unknown。
  - `taskmgr/store.py:547-577`：`interrupt_run` 只看 `cancel_requested`，不看 `stop_reason`，并在 `:562-576` 创建自动重跑。
- 复现：
  - BUG：作业因 `timeout_seconds` 停止，SIGKILL 后仍有 D 状态进程，作业进入 `unknown`（`stop_reason=timeout`）。按 `docs/recovery.md` 的提示重启机器，再运行 `task resolve`，返回 `decision=interrupted`，原作业变成 `failed`（"machine rebooted"），并克隆出一个 `queued` 的新作业。对处于 `stopping` 的作业，daemon 重启恢复时也走同一分支（推断）。
  - DOC：构造 unknown、`runner={status: dispatched, final: false, exited: false}`，调用 `assess(..., resolve=True)`，返回 `decision=interrupted, state=failed`，并出现 `retry_of=<旧 jobid>` 的 queued 作业。
- 建议（两份报告一致）：
  - `interrupt_run` 在 `stop_reason == "timeout"` 时终结为 `timed_out`。
  - `assess(resolve=True)` 不调用 `clone_job`：按 cancel/stop_reason 结束；没有停止原因时结束为 `failed(interrupted)`，由操作者显式 `task retry`。
  - 补“缺少身份”和“跨 boot”两种 unknown 的用例（TST §5 第 3、6 项也指出这些分支没有测试）。

---

## 3. P1

### 3.1 缺陷

#### P1-1. 归档内残留的 `result.json` 被当成本次结果

- 来源：BUG M2。已验证。
- 位置：`taskmgr/package.py:37-49`（`is_excluded` 把 `result.json` 排除在校验和哈希之外）、`taskmgr/package.py:402-545`（`safe_extract` 不应用排除规则）、`taskmgr/remote.py:109-129`、`taskmgr/shim.py:123`。
- 复现：同一个包（入口为 `true`，`required_outputs=["best"]`），以 tar.gz 提交得到 `succeeded`、`outputs={"best": "stale"}`；以目录提交得到 `invalid_result`。`content_hash` 不包含这个文件，所以去重也区分不了两种包。
- 建议：`cmd_receive` 解压后删除所有命中 `is_excluded` 的条目，再计算哈希和生成 `package/`。`task validate` 对归档里被排除的条目给出警告。

#### P1-2. 包根目录下以 `result.json` 开头的文件被静默丢弃

- 来源：BUG M3。已验证。
- 位置：`taskmgr/package.py:34`、`:44`（前缀匹配 `EXCLUDED_ROOT_PREFIXES`）。
- 复现：包根目录有 `result.jsonl`。`task validate` 返回 `valid=true`，但打包时这个文件不在包里。远端 `cat result.jsonl` 报 `No such file or directory`，作业 `failed`，整个过程没有任何提示。任意层级下以 `.json.lock` 结尾的文件同理。
- 建议：根目录只精确排除 `result.json`、`result.json.lock` 和 `result.json.corrupt*`；校验输出里列出被排除的路径。修复时与 P1-1 一起改 `is_excluded`。

#### P1-3. `task submit` 在本地按 1 GiB 拦截，服务器的 `max_package_bytes` 不起作用

- 来源：BUG M4。已验证。
- 位置：`taskmgr/taskmgr.py:184` → `:142`/`:158` → `taskmgr/package.py:238-239`、`:304`。
- 复现：服务器 `config.json` 设置 `"max_package_bytes": null`，提交超过 1 GiB 的包，返回 `invalid_package` / `package_too_large (1073741824)`，退出码 2，没有绕过的办法。
- 建议：`cmd_submit` 调用 `validate_dir(..., max_bytes=None)`，由服务器判定大小；`task validate` 把超限降为警告。
- 相关：CON 2.2 指出包大小超限的三处报错文字不同（`remote.py:179`、`package.py:511`、`package.py:308`），修复时一并统一。

#### P1-4. 关闭 GPU 忙约定后，`base_env` 仍会注入 `TASKMGR_GPU_BUSY_EXIT_CODE`

- 来源：DOC 2。已验证（直接调用 `build_env`）。
- 文档承诺：`docs/task-package.md:109`、`docs/task-package.zh-CN.md:101`、`taskmgr/shim.py:46` 说 `gpu_busy_exit_code=null` 时不设置这个变量。
- 实际：`taskmgr/shim.py:55` 先叠加任意 `base_env`，`:74` 只在 busy_code 非空时覆盖，关闭时不删除。配置 `base_env={TASKMGR_GPU_BUSY_EXIT_CODE: "88"}`、busy_code=None 时，环境里仍是 `88`：helper 以 88 退出，服务器却按普通失败处理。
- 建议：关闭约定时显式删除该变量；或者在配置校验时禁止 `base_env` 设置 `TASKMGR_*` 保留变量，并写进文档。

#### P1-5. `gpu_busy_exit_code` 的合法范围在服务器和 helper 之间不一致

- 来源：CON 5.2。推断（读代码）。
- 位置：服务器接受任意整数（`taskmgr/store.py:247`），原样导出（`taskmgr/shim.py:75`）；`taskmgr_task.py` 只接受 1-255，否则退回 75（`taskmgr/template/taskmgr_task.py:160`）。
- 后果：配置为 300 时，helper 以 75 退出，服务器等的是 300，GPU 忙约定失效。
- 建议：统一为 1-255，在 `Store.config` 中校验，不合法时报 `bad_config`。与 P1-4 同属 GPU 忙配置校验，可以一起改。

#### P1-6. `--wait --timeout` 管不住单次查询，总时限兑现不了

- 来源：DOC 24。已验证。
- 规格：`agent/desc-1.md:544` 要求它是 total wait time；`agent/desc-2.md:171` 要求连接重试时也受 deadline 约束。
- 实际：`taskmgr/taskmgr.py:400` 的 `locate` 和 `:402` 的 `Remote.call` 不传剩余时长，`taskmgr/transport.py:266` 用独立的 30 秒默认超时，查询完成后才检查总时限；终态和自动重跑分支也在 deadline 检查之前。本地命令先 sleep 0.3 秒再返回 queued，`--timeout 0.01` 实际约 0.905 秒才返回 `wait_timeout`（含进程启动开销）。
- 建议：把剩余总时长传给单次查询和跨服务器定位；明确超时与终态同时发生时以哪个为准。

#### P1-7. `host add` 在确认之前就删除暂存上传

- 来源：DOC 4。mock 验证（打桩 `sh`，没有接触真实服务器）。
- 承诺：`taskmgr/README.md:35`、`:48`，`docs/install.md:80`、`:86`，`docs/usage.md:168`，`taskmgr/hostsetup.py:3`，`taskmgr/hostcmds.py:49`（及对应中文版）都说“每处改动先确认，已是最新的服务器不做改动”。
- 实际：`taskmgr/hostsetup.py:617` 在确认之前调用 `_remove_stale_uploads`，`:661-665` 执行 `find ... -mmin +10 -print -delete`。即使已安装的版本相同，也是先删除再返回 `ok`。
- 建议：删除也走确认；如果接受自动清理，就在上述总述中写明这个例外。

#### P1-8. fetch 省略特殊文件时既不警告，也不记入 `skipped`

- 来源：DOC 3。已验证（调用 `add_tree_to_tar`）。
- 承诺：`docs/assumptions.md:54`、`agent/desc-2.md:178` 说特殊文件与不安全链接一样会被跳过、警告，并列入 `skipped`。
- 实际：`taskmgr/package.py:600-603` 在远端生成 tar 时直接跳过 FIFO/设备/socket，`taskmgr/remote.py:456` 只传这个 tar，`taskmgr/taskmgr.py:561-563` 只能报告本地解包时发现的条目。
- 建议：通过协议传回远端的省略清单并合并进回执；如果决定静默忽略，就同步修改文档和规格。
- 相关：BUG L2 推断 `skip_unsafe` 分支遇到大小写冲突时会丢文件且只给警告（见 P2-4）。

### 3.2 测试可信度

#### P1-9. 已观察到的不稳定测试

- 来源：TST T1、T2；DOC 的全量测试结果。已验证。
- 三次全量运行的结果：

  | 运行 | 环境 | 结果 |
  | --- | --- | --- |
  | TST 第 1 次 | Python 3.10.22，loadavg 14-28 | 166 项，1 失败，641 s |
  | TST 第 2 次（coverage） | 同上 | 166 项，1 失败 1 错误，956 s |
  | DOC | Python 3.14.10 | 166 项，2 失败，731 s |

- **T1** `taskmgr/tests/test_cli.py:148-153` `test_job_commands_find_the_host_by_jobid`：**3 次运行都失败**。retry 出来的新作业脚本瞬间完成，`cancel` 时作业已经 `succeeded`，输出 `nothing to cancel`。建议 retry 之前先 `stop_daemon("s2")`，或者用 gate 脚本保证 cancel 时作业还在 queued/running。
- **T2** `taskmgr/tests/test_gpu_busy.py:74-89` `test_busy_exits_are_relaunched_then_succeed`：3 次中 2 次失败（TST 报 `KeyError: 'state'`，DOC 报 `pending_reason` 为 None）。作业在测试读状态前就结束了。建议用 `busy_script(gate=...)` 让作业停在等待状态，读完再放行。
- **T3** `taskmgr/tests/test_wait.py:21`、`taskmgr/tests/test_query.py:241`：`threading.Timer(1.5, unlink(flag))` 假设 CLI 1.5 秒内完成首次查询；Timer 没有 join，测试失败后还可能在 sandbox 清理之后抛 `FileNotFoundError`。建议改用 Popen 读 stderr 等到出现 "retrying" 再删 flag，并与 D1 合并（见 P3-3）。

#### P1-10. 改坏被测代码后测试仍然通过

- 来源：TST B1-B5，各有一个单点变异作为证据（变异在临时拷贝中进行）。
- **B1** `taskmgr/tests/test_exec.py:186-207` `test_progress_written_by_task_only`：变异 M9（`shim.py:456` 结束前覆盖 `progress.json`）未被发现。建议作业结束后读 `done/<jobid>/workdir/progress.json`，断言内容与任务写入的完全一致。
- **B2** `taskmgr/tests/test_exec.py:285-295` `test_timeout_with_term_and_kill`：变异 M10（`monitor.py:87` 第一步直接发 SIGKILL）未被发现。建议 polite 作业 trap TERM 写标记文件，stubborn 作业断言 `_taskmgr.signal == 9`，并检查 shim.log 里 TERM 在 KILL 之前。
- **B3** `taskmgr/tests/test_submit.py:166-204` `test_protocol_version_mismatch`：变异 M11（`transport.py:339` 客户端不检查版本）未被发现。实际是 remote 在拒绝客户端，客户端的 `check_protocol`（`transport.py:337`）从未被单独验证；第 171-180 行与第 167-168 行重复。建议用返回 `{"protocol_version": 1}` 的假 remote（参考 `test_query.py:469`）断言 `protocol_mismatch`，删除重复段，第 169 行补断言 error.code。
- **B4** `taskmgr/tests/test_package.py:72-77` `test_hash_sees_exec_bit_and_content`：变异 M4（`package.py:383` 计算哈希时不读内容）下，两个 hash 测试都通过，只有 `test_submit.py:91-94` 间接发现。建议补三个断言：改一个字节、文件改名、symlink 目标变化，哈希都应变化。
- **B5** `taskmgr/tests/test_sched.py:428-442`：变异 M1（`store.py:114` 去掉 `flock`）下，测试是因为 60 秒等待超时才失败，断言本身检测不到双重派发。建议每个作业在 workdir 之外按 jobid 记录执行次数，断言恰好执行一次。

---

## 4. P2

### 4.1 缺陷

#### P2-1. GPU busy 重排后 `has_run` 判为“未运行”，日志和 workdir 取不回

- 来源：BUG L1（已验证）与 DOC 19 合并。
- 位置：`taskmgr/monitor.py:254` 清空 `dispatched_at`，但 `:250` 保留 workdir、`:259` 保留 `runner.prev-N.json`；`taskmgr/store.py:261-263` 的 `has_run` 只看 `dispatched_at`；`taskmgr/remote.py:296-299`、`:409-411`、`:458-459`。文档 `docs/internals.md:63`（及中文版 `:61`）和 `store.py:262` 的 docstring 把“dispatched_at 为空”解释为“从未运行”。
- 复现（BUG）：`stdout.log` 里有 `first-launch`，但 `task logs` 报 `has not run (no_run)`，fetch 同样拒绝。如果作业在队列中被取消，会以“未运行”结束，已有日志和 workdir 都取不回来。
- 建议：BUG 主张改代码（`has_run` 同时检查 `runner.prev-*.json`，或持久化 `ever_ran`）；DOC 给出备选方案，即至少修改文档和 docstring 并写明这个限制。汇总倾向改代码，因为“取消后取不回日志”会丢诊断信息。

#### P2-2. 按 jobid 查找成功时不报告跳过的禁用服务器

- 来源：DOC 7。已验证（一台启用、一台禁用的 local 服务器）。
- 承诺：`docs/usage.md:178`、`docs/assumptions.md:78`（及中文版）说包括 jobid 查找在内的查询都会在 stderr 和 JSON 中列出 skipped。
- 实际：`taskmgr/taskmgr.py:345` 拿到了 skipped，但 `:356-363` 找到唯一作业就返回；`:371` 只在完全找不到时才用它；`render_status`（`:295`）也不输出。jobs/whoami/version 走 `query_hosts`，确实会报告。
- 建议：成功的查找也报告 skipped；或者把文档承诺限定为 jobs/whoami/version。

#### P2-3. 任务 helper 写 JSON 时没有 fsync 所在目录

- 来源：DOC 5。mock 验证（计数 fsync 调用）。
- 位置：管理记录经 `taskmgr/common.py:127`、`:140` 同步文件和目录；三份 `taskmgr_task.py:51`（template、examples/succeed、examples/multi-gpu）只 fsync 文件后 replace；`taskmgr/taskmgr.py:121` 的 init progress 直接写目标文件。文档 `docs/internals.md:85` 和规格 `agent/desc-1.md:349` 写的是统一行为。
- 建议：统一三份 helper 的持久化实现，或者把 internals 的保证限定为 taskmgr 管理的记录。
- 相关：CON 5.1 提醒三份 `taskmgr_task.py` 目前完全相同，必须一起改；CON 4.6 建议 `cmd_init` 改用 `atomic_write_json`。

#### P2-4. 只有大小写不同的文件名被判为重复条目

- 来源：BUG L2。已验证。
- 位置：`taskmgr/package.py:416`、`:444-447`。
- 后果：同时有 `Data.csv` 和 `data.csv` 时，`safe_extract` 报 `duplicate entry`：本地校验通过，服务器却拒收。fetch 走 `skip_unsafe` 时会丢掉其中一个，只给出警告（推断）。
- 建议：`seen` 用原始名称；大小写冲突交给后面的 `lstat`/`O_EXCL` 检测。

#### P2-5. 残留的 `taskmgr-remote.pyz.link-new` 让 `host add` 一直失败

- 来源：BUG L3。已验证。
- 位置：`taskmgr/hostsetup.py:686-687`。
- 后果：上一次在 `ln -s` 和 `mv -f` 之间中断后，每次运行 `ln -s` 都报 `File exists`，`set -e` 让脚本退出，step data 失败。
- 建议：先 `rm -f "$path.link-new"`，或者用 `ln -sfn`。改动很小，可以与 P1-7 一起改。

#### P2-6. `task retry` 与作业结束并发时，可能克隆出缺少 `package/` 的作业

- 来源：BUG L4。部分验证：已验证源目录只有 `package.tar.gz` 时 `stage_package` 正常返回；“遍历中途目录被移走”属于推断。
- 位置：`taskmgr/remote.py:316-322`、`taskmgr/store.py:656-682`。
- 建议：`link_tree` 给 `os.walk` 传 `onerror` 并抛出；或者在锁内确认作业已是终态、目录未变，再使用锁外 staging 的结果。

#### P2-7. 接管期间取消，进程组恰好退出时结果被记为 `exit_code_lost`

- 来源：BUG L5。推断。
- 位置：`taskmgr/monitor.py:494-497`；同一场景下 `assess`（`:333-338`）给出 `cancelled`，两条路径不一致。
- 建议：`tick` 在 finish 前重读 job，用 `requested_stop` 决定结束原因。

#### P2-8. `status --wait` 可能跟随手动 retry 的作业

- 来源：BUG L6。推断。
- 位置：`taskmgr/taskmgr.py:321-332` 取 `retried_as[-1]`，违反 `docs/recovery.md` 的“`--wait` follows the re-run”。
- 建议：单独记录自动重跑的 jobid，或者选 `auto_restarts > 0` 的那个。

#### P2-9. step data 停掉服务后失败，服务保持停止且没有提示

- 来源：BUG L7。推断。
- 位置：`taskmgr/hostsetup.py:801-838`。
- 建议：记录“本次运行停止了服务”，在 `run()` 的 `finally` 中重新启动，或者在 detail 中写明。

#### P2-10. 模板 helper 在 Python ≤3.9 的任务 venv 中导入失败

- 来源：BUG L8。推断。
- 位置：`taskmgr/template/taskmgr_task.py:90`（`percent: float | None`）、`:236`。
- 建议：在文件开头加 `from __future__ import annotations`（三份副本一起改，见 CON 5.1）。TST 也指出 `taskmgr_task.py` 覆盖率只有 49%，示例的 `run.py` 从未被执行（TST §5 第 13 项）。

### 4.2 CLI 与文档：会导致误操作的部分

#### P2-11. 顶层帮助列出的 `--detail` 放在顶层无法解析

- 来源：CLI 2 与 DOC 28 合并。已验证：`task --detail jobs` 退出 2，报 `unrecognized arguments: --detail`。
- 位置：`taskmgr/cliparse.py:131`（顶层 options 列出 detail）、`:133`、`:137`、`:264`、`:276`（detail 实际只注册在叶子命令上）；`docs/usage.md:39`。
- 建议（CLI 给出了可直接替换的文本）：
  - usage：`task [--home DIR] [--plain] <command> [args]`
  - detail：`JSON output; place after the leaf command (except logs)`
  - home：`local state directory; before command (default: $TASKMGR_HOME or ~/.taskmgr)`
  - 顶层错误 usage（`cliparse.py:247`）用同样的文字。
  - DOC 提供的另一种做法：让顶层 parser 也接受 `--detail`。
- 相关：CLI 16，detail 模式的参数错误 message 固定为 `invalid arguments (see stderr)`（`taskmgr/taskmgr.py:744`、`cliparse.py:225`），建议把具体的解析错误传进 JSON。

#### P2-12. 文档漂移：会导致误操作的部分（多数已验证，只需改文档）

| DOC # | 文档位置 | 问题 | 实际 / 证据 | 建议 |
| --- | --- | --- | --- | --- |
| 11 | `docs/install.md:17-23`（中文 `:17-23`） | 首个安装流程没有构建远端 zipapp | `hostsetup.py:609`、`:165` 找不到预构建包时直接失败 | 在 host add 前加 `python3 -m taskmgr.pyz -o dist` |
| 12 | `docs/install.md:59`、`:100`（中文 `:56`、`:94`） | 远端 `version` 命令缺少必需的 `--root` | `remote.py:571` required；真实运行退出 2 | 写成 `... --root /absolute/remote_root version` |
| 10 | `docs/usage.md:28`、`docs/install.md:75`（及中文） | `[--ssh-option KEY=VALUE ...]` 看起来能接多个值 | `hostcmds.py:58-63` 是 `append`；`A=B C=D` 报 unrecognized | 写作 `[--ssh-option KEY=VALUE]...` |
| 6 | `docs/usage.md:122`（中文 `:114`） | 结果不确定时写的是 `remote_error` | `taskmgr.py:202-221` 对外统一抛 `submission_uncertain`，真实 CLI 已验证 | 区分底层原因与对外错误码；补充 Ctrl-C 的情况 |
| 8 | `docs/usage.md:98`、`:116`（中文 `:93`、`:108`） | 没有说明全部服务器被禁用时的行为 | `taskmgr.py:241-247` 在查询前抛 `no_enabled_hosts` | 补充，并给出 `--host` / `host enable` 两种恢复方式 |
| 13 | `docs/install.md:80-83`（中文 `:76-78`） | “任何非 y 回答都停止后续步骤” | `hostsetup.py:367`、`:385`、`:393`：linger 是非必需步骤，拒绝后继续 | 写明 linger 拒绝或失败都不阻止后续步骤 |
| 15 | `docs/usage.md:62`（中文 `:61`） | “错误只有一行 stderr，stdout 为空” | `cliparse.py:225`、`render.py:73`、`taskmgr.py:778` 都有例外 | 按错误类别分别说明 |
| 16 | `docs/assumptions.md:31`、`agent/desc-2.md:173` | Ctrl-C 打印 interrupted 错误 | `taskmgr.py:762-767` 只有 detail 模式输出 `error.code`；上传中的 Ctrl-C 变成 `submission_uncertain` | 写明模式区别和上传例外 |
| 9 | `docs/install.md:262`、`agent/desc-2.md:157` | `scheduler_poll_seconds` 被描述为独立的调度间隔 | `daemon.py:366` 取它和 control 间隔的最小值 | 改文档，或让 daemon 使用两个独立的截止时间 |
| 21 | `docs/development.md:67`（中文 `:64`） | 让人从 `taskmgr.records` 导入 override | 实际在 `common.py:20-28` | 模块名改为 `taskmgr.common` |
| 14 | `taskmgr/README.md:15`、`hostsetup.py:5`、`deploy/taskmgr-remote.user.service:1` | “不需要 root” | `hostsetup.py:1007-1020`：linger 被策略拒绝时需要管理员 | 加上管理员例外（未在真实 polkit 主机上验证） |

#### P2-13. 文档漂移：设计保证写得过强

| DOC # | 文档位置 | 问题 | 实际 | 建议 |
| --- | --- | --- | --- | --- |
| 23 | `agent/desc-1.md:395`、`docs/internals.md:34` | “runner.json 在 workdir 外，所以任务不能写” | `shim.py:244-246` 没有降权或沙箱，任务与 shim 是同一用户，`../runner.json` 可写 | 文档写明只适用于可信任务；如果这是安全要求，就要另行实现隔离。**需要产品决定** |
| 17 | `docs/task-package.md:15`、`template/TASK.md:27` | “taskmgr 从不读取 TASK.md” | `package.py:24`、`:256-273` 读取并检查占位符（validate 已验证） | 改为“不解释，但校验时检查占位符” |
| 18 | `docs/internals.md:21`、`:42`，`agent/desc-2.md:260` | 写的是 `incoming/retry-<jobid>/` | `store.py:622` 生成 12 位随机串 | 写成 `retry-<random>` |
| 20 | `docs/install.md:54`、`pyz.py:8` | “改文档不影响 code version” | `pyz.py:76` 把 template 下的所有文件计入哈希 | 写明 template 下的 TASK.md 属于例外 |
| 25 | `docs/recovery.md:67`（中文 `:60`） | 有存活进程就一直保持 unknown | `monitor.py:384`、`:393` 还可能 `handed_to_daemon`（mock 验证） | 写明分支，identity/alive_pids 是条件字段 |
| 26 | `docs/usage.md:43`、`textout.py:3` | 所有输出带颜色，每条命令都返回对象 | `taskmgr.py:520`、`transport.py:304`：logs 原样转发字节 | 写明 logs 例外 |
| 27 | `docs/task-package.md:36` | 没有单独交代绝对路径入口 | `package.py:285`、`shim.py:79-88` 支持绝对路径 | 补充说明 |
| 22 | `hostlegacy.py:3`、`:32`，`records.py:194`，`hostcmds.py:1` | 旧模块名、旧命令名、漏列子命令 | 见 CON §7 | 与 CON §7 一起改（见 P2-16） |

DOC 还列出两处规格已被后续用户决定覆盖的偏差（只查询已启用的服务器；本地命令默认输出文本），建议把这两项决定写回规格，代码不动。

### 4.3 测试

#### P2-14. 时序依赖、环境依赖和弱断言

- 来源：TST。
- **固定 sleep 之后做负断言**（建议统一换成 `Env.wait_scheduler_passes(n)` 或 gate 文件，`docs/testing.md` 已经这样要求）：
  - B6 `test_gpu_busy.py:117-129`：重试间隔 60 秒，`sleep 1.5` 后断言“没有再次启动”，这个断言恒为真。
  - B7 `test_recovery.py:70-79`：`sleep 2` 的任务在负载高时会在 daemon 重启期间结束，测试并没有覆盖“运行中重启 daemon”。
  - B8 `test_submit.py:232-235`：`sleep(0.5)` 不能保证清理已经执行；`TASKMGR_TEST_RECEIVE_PAUSE=4` 在负载高时会误报失败。
  - T4 `test_sched.py:419`、T5 `test_recovery.py:148-155`。
  - T6：`test_exec.py:268`、`:312`，`test_gpu_busy.py:139`，`test_recovery.py:65`、`:196`、`:313`、`:374`、`:417`、`:429`、`:444`，`test_query.py:337`。
- **依赖环境**：
  - E1 `test_exec.py:12-18`、`:32-35`、`:72`：python3 只装在 pyenv/conda 路径时，任务 PATH 里找不到它。
  - E2 `test_gpu_busy.py:314-317`：在装有 NVIDIA 驱动的机器上（也就是生产环境）会失败。
  - E3 `test_add_host.py:1043-1055`：没有清理 `TASKMGR_*` 和 `NO_COLOR` 环境变量。
  - E4：纯单元测试挂在 `Env` 上，macOS 上被整体跳过（`test_gpu_busy.py:265`，`test_query.py:46`、`:429`、`:480`、`:490`，`test_package.py:398`，`test_recovery.py:43`、`:485`）。
- **弱断言**（W1-W12，详见 TST §4.4）：优先处理 W1（remote 侧 `resolve` 校验从未执行，与 P0-2 相关）、W6（只断言退出码）、W10（fake systemctl 证明不了 KillMode 生效）和 W12（`helpers.py:189` 路径前缀匹配可能误杀并行测试的进程）。

#### P2-15. 高风险路径缺少测试

来源：TST §5，按风险排序。其中第 3、6、7 项与 P0 修复直接相关。

1. daemon 启动时修复“状态与目录不一致”（`daemon.py:131-132`）。
2. shim 崩溃点 `before_exec`、`before_finalize`（`shim.py:277`、`:456`）及对应的恢复路径（`daemon.py:147`、`store.py:457-458`）。
3. /proc 或 boot_id 不可读时进入 unknown（`monitor.py:352-355`、`:369-372`、`:525-527`，`procutil.py:67-70`）。
4. 接管期间停止未确认（`monitor.py:516-523`）。
5. 自动重跑的幂等性（`store.py:572-573`）。
6. identity 记录不完整（`monitor.py:343-345`）。
7. remote 侧参数校验和 `internal_error` 兜底（`remote.py:349`、`:351`、`:644-656`）。
8. `task update`（`taskmgr.py:659-676`）。
9. fetch 失败时的清理（`taskmgr.py:563-574`）。
10. transport 的异常路径（`transport.py:278-279`、`:315-331`、`:380-387`）。
11. daemon 的 fork/exec 失败处理（`daemon.py:288-291`、`:309-313`）。
12. 包校验的部分规则（`package.py:118-127`、`:194-208`、`:487-504`）。
13. `template/taskmgr_task.py` 覆盖率 49%，示例包从未真正提交运行过。

覆盖率总计 85%（语句覆盖）。`hostserver.py` 显示 0%，`remote.py`、`daemon.py`、`hostsetup.py` 的数字是下限，原因是这些代码在 pyz 内或以 `python3 -c` 方式执行，coverage 统计不到，见 TST §2 的限制说明。

### 4.4 一致性：改漏风险高的部分

来源：CON。只读代码，无需运行验证。标注“持久化格式”的条目改名会影响 `SCHEMA_VERSION`/`PROTOCOL_VERSION` 和已有数据，不能当作单纯重命名。

#### P2-16

- **CON 3.1** `_s` 同名不同义：`hostcmds.py:270` 的默认值是 `""`，`render.py:30` 的默认值是 `-`。建议都直接用 `cell(v, default)`。
- **CON 2.3 + CLI 15**：消息里抄写了正则字面量（`taskmgr.py:109`、`:182`，`package.py:102`，`remote.py:99`），与 `common.py:37-38` 的实际正则相互独立，而且写的是 `$`，实际是 `\Z`。CON 建议定义 `TASK_ID_PATTERN` 等常量；CLI 建议把消息改成人能读懂的规则，例如 `invalid task ID: use two uppercase letters, a dash and 2-4 digits (e.g. AA-01)`。两者可以一起做：常量用于编译正则，消息用可读的描述。
- **CON 2.1**：`--submission-key` 格式错误时本地报 `bad_args`、远端报 `bad_submission_key`，建议统一为 `bad_args`。
- **CON 4.3**：远端 JSON 错误有三条路径（`remote.py:84`、`:640`、`:654`，`daemon.py:354`），其中两条绕过了 `TASKMGR_TEST_PROTOCOL_VERSION`，一条没有 `protocol_version`。建议抽出 `error_payload()`。这与 TST §5 第 7 项一起改。
- **CON 1.1**（持久化格式）：`host` 有三种含义（`store.py:398` 的主机名、`taskmgr.py:298` 的机器列表名、`records.py:231` 的连接地址）。建议 `_taskmgr.host` 改名为 `_taskmgr.hostname`。
- **CON 1.5**（部分为持久化格式）：retry、re-run、restart、relaunch 混用（`shim.py:324-325`、`records.py:76`、`store.py:536`、`docs/recovery.md:45`）。先改非持久化的文字；`max_auto_restarts`、`gpu_retry_interval_seconds` 等配置键等到 schema 变更时再改，并兼容旧键。
- **CON 3.2-3.6**：服务器查找、JSON 取值 helper、校验与默认值、状态与目录名字面量、文件名常量都有多份。例如 `("running", "stopping")` 出现 10 次，容易与 `GPU_HOLDING_STATES` 混淆；`remote.py:552`、`taskmgr.py:635` 写字面量而不用 `pyz.VERSION_FILE`/`COMMIT_FILE`。
- **CON 6.1**：仅测试用的环境变量没有 `TASKMGR_TEST_` 前缀（`gpu.py:13`、`procutil.py:13`、`daemon.py:270`）。
- **CON §7 + DOC 22**：docstring 与代码不符，共 12 处（`hostlegacy.py:3`、`:32`，`hostsetup.py:75`、`:88`，`hostcmds.py:1`、`:3`，`records.py:194`，`remote.py:5`、`:550-551`，`monitor.py:43`、`:416`，`hostserver.py:5`，`taskmgr.py:3-5`）。
- **CON 5.5**：文档里的入口是 `python`（`docs/task-package.md:26`、`:92` 及中文版），模板是 `python3`。入口进程的默认 PATH 里不一定有 `python`，照抄文档可能运行失败。
- **CON 9.1**：中英文内容不一致（`docs/install.zh-CN.md:148` 少两个子命令、`docs/assumptions.zh-CN.md:84` 缺一句、`docs/assumptions.md:28` 自相矛盾、`docs/task-package.zh-CN.md:36-37` 中出现“必须必须”）。
- **CON 8.3**：`docs/development.md:39` 写 basedpyright 检查 whole project，实际只检查 `taskmgr/`。

---

## 5. P3

### P3-1. CLI 帮助精简与补全

来源：CLI。已用 `--help` 递归遍历全部 34 页（task 21 页，remote 13 页），都退出 0；参数错误检查都在任何部署或网络操作之前失败。CLI 报告的结论是现有命令树基本合理，**不建议**增加分组、别名或迁移命令。可直接替换的文本见原报告各节，要点如下：

- **范围措辞**（CLI 5，优先）：把 `every server` 改成 `enabled servers`（`cliparse.py:285`、`:286`、`:294`、`:302`、`:371`、`:374`），因为对禁用了服务器的用户有误导。`ping` 确实会遍历所有服务器，不能一并替换。
- **互斥 usage**（CLI 11）：`[--disabled] [--enabled]` 改为 `[--enabled | --disabled]`（`cliparse.py:188`、`:198`）。需要修改 formatter，不只是换字符串。
- **等待参数**（CLI 6）：`the exit code tells how` 改为写明退出码 `0/1/2/3`（`cliparse.py:313` 等）。
- **follow**（CLI 18）：`keep printing until the job ends` 改为 `follow log output`，因为 unknown 状态下停止跟随不代表作业已经结束（`remote.py:435`）。
- **resolve**（CLI 12）：摘要改为 `check an unknown job or confirm it stopped`，保留双重确认。
- **错误消息**（CLI 15）：多台服务器上都有该作业、找不到作业、unknown 提示里的裸 `task resolve` 会再触发一次参数错误（`taskmgr.py:352`、`:374`、`:462`）。
- **远端帮助**（CLI 13、14、19）：目前几乎没有，需要补用途说明、metavar 和 help；`--state` 复用本地的 `ALL_STATES` choices，避免无效状态静默返回空列表（`remote.py:221`）；子页加 epilog 说明 `--root` 必须放在命令之前。
- **精简**：顶层只列 `host` 一行（CLI 1）、缩短页脚（CLI 3）、命令摘要（CLI 4）、host 摘要与描述（CLI 7-10）、submit 的幂等说明（CLI 17）。

### P3-2. 其余一致性与风格

来源：CON。都是读代码得出，可以顺手改。

- 术语表见 CON“建议的统一术语”，涉及 task/job、server/host、shim/runner/supervisor、code version 等（CON 1.2-1.4、1.6-1.10）。
- 错误文字（2.2）、时长写法（2.4）、占位符称呼（2.5）。
- 输出 helper 重复（3.7）、config 读取方式（3.8）、`traceback` 导入位置（3.9）。
- 日志格式：`host add` 失败时 stderr 打印 `done:`（4.1）、`WARNING:` 大小写（4.2）、只有部分命令返回 `ok`（4.4）、上传目录用本地时间（4.5，建议改 UTC）、JSON 写入格式（4.6）、pyz 构建失败的输出（4.7）、GPU busy 写法（4.8）。
- 模板与示例：写死的 75（5.3）、示例与模板不一致（5.4）、配置示例中的名字（5.6）。
- 工具链：版本一致（8.1，无需改）；pre-commit 锁了两个版本（8.2，文档加一句说明）；`noqa` 写法（8.4）；install 里的“without pip”应为“without uv”（8.5）。
- 中文用词（9.2）、链接与抬头（9.3）、占位符（9.4）、引用格式统一为 `desc-1 §12.2`（§10）。

### P3-3. 测试重复、白盒依赖和效率

来源：TST。

- 重复（D1-D8）：D1 `test_wait.py` 整个文件并入 `test_query.py`（与 T3 一起修）；其余见 TST §4.6。
- 白盒依赖（I1-I4）：I1 覆盖私有方法 `_scan_job_dir` 并读取 `_lock_depth`，重构时容易误报；I3 删除对 `_retained` 的断言。
- 效率：`test_pyz.py:295-314` 每个用例都重新构建两个 pyz，共 7 次，可以改为 setUpClass。全量测试 640-960 秒，减少重复构建和固定 sleep 后可以缩短。

---

## 6. 跨报告去重对照

| 汇总条目 | BUG | CLI | CON | TST | DOC |
| --- | --- | --- | --- | --- | --- |
| P0-2 / remote resolve 校验 | H2 | | | W1、§5-7 | |
| P0-3 unknown 自动重跑 | M1 | | | §5-3、§5-6 | 1 |
| P1-3 包大小 | M4 | | 2.2 | | |
| P1-4 / P1-5 GPU 忙配置 | | | 5.2 | | 2 |
| P1-9 不稳定测试 | | | | T1、T2 | 测试结果 |
| P2-1 `has_run` | L1 | | | | 19 |
| P2-3 helper fsync | | | 4.6、5.1 | | 5 |
| P2-10 helper 兼容性 | L8 | | 5.1 | §5-13 | |
| P2-11 `--detail` 位置 | | 2、16 | | | 28 |
| P2-16 docstring | | | §7 | | 22 |
| P2-16 正则消息 | | 15 | 2.3 | | |
| P2-16 远端错误路径 | | | 4.3 | §5-7 | |
| P2-12 `--ssh-option` | | 8 | 9.4 | | 10 |
| P3-1 enabled servers | | 5 | 1.2 | | 7（行为侧） |

各报告之间没有发现结论冲突。唯一的分歧在 P2-1：BUG 建议改代码，DOC 认为至少改文档。汇总倾向改代码。

---

## 7. 建议的修复顺序

1. **P0-1、P0-2**：任务产物的读写先 `lstat`、非阻塞打开、移出 `state.lock`，并修正 `--as stopped` 的写入顺序。同时补 FIFO/目录/深嵌套 result.json 的用例，以及 remote 侧 resolve 的用例（TST W1、§5-7）。
2. **P0-3**：`assess(resolve=True)` 和 `interrupt_run` 不再克隆作业，`timeout` 结束为 `timed_out`。补 TST §5 第 3、5、6 项的用例。
3. **P1-9、P1-10**：先修 T1/T2/T3，避免 CI 随机变红；再重写 B1-B5，否则上面这些修复也得不到可靠的回归保护。
4. **P1-1 至 P1-8**：包排除规则（P1-1、P1-2 一起改 `is_excluded`）→ 包大小交给服务器判定 → GPU 忙配置校验（P1-4、P1-5 一起改）→ wait 总时限 → host add 的确认与 `.link-new`（P1-7、P2-5）→ fetch 省略清单（涉及协议，需要决定是否 bump `PROTOCOL_VERSION`）。
5. **P2-11、P2-12**：成本低、能直接减少误操作的帮助与文档修正，可以和第 4 步并行。
6. **P2-13 中的 DOC 23**：需要先由产品决定“任务不能写 runner.json”是合作约定还是安全要求，再决定改文档还是实现隔离。
7. **P2 其余缺陷、测试与一致性条目**：依次处理固定 sleep 迁移（P2-14）、覆盖缺口（P2-15）、高风险一致性条目（P2-16）。
8. **P3**：CLI 文案、风格统一、测试去重。涉及持久化格式的改名留到下次 schema/协议变更时一并处理。
