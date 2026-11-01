# 测试审查：taskmgr/tests/

审查对象：`taskmgr/tests/helpers.py` 与全部 `test_*.py`（11 个文件，166 个用例），基于提交 `070f3a2`。
方法：逐个读测试；跑完整测试套件和带子进程覆盖率的测试套件；对源码做 11 个单点变异（每个在仓库的临时拷贝里改，跑相关用例，看测试能不能发现）。工作树没有改动，本次只提交这一个文件。

## 1. 运行结果

环境：Linux 7.2（OrbStack 容器），Python 3.10.22，8 核。运行期间机器上还有其它会话在跑测试，loadavg 在 14～28 之间，负载较高。运行方式与 docs/testing.md 一致：先把仓库拷到临时目录再跑。

| 运行 | 命令 | 用例 | 通过 | 失败 | 错误 | 跳过 | 耗时 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `python3.10 -m unittest discover -s taskmgr/tests` | 166 | 165 | 1 | 0 | 0 | 641 s |
| 2 | `coverage run -m unittest discover -s taskmgr/tests` | 166 | 164 | 1 | 1 | 0 | 956 s |

两次失败都是时序竞争，不是功能回归（详见 2.3 节的 T1、T2）：

- 两次都失败：`test_cli.py:153` `test_job_commands_find_the_host_by_jobid`
  `AssertionError: 'cancel requested: job … on s2' not found in 'nothing to cancel: job … on s2 is succeeded'`
- 只在第 2 次出错：`test_gpu_busy.py:87` `test_busy_exits_are_relaunched_then_succeed`
  `KeyError: 'state'`：`status` 返回的是已经结束的作业的 result.json，作业在测试读状态之前就已经成功了。

## 2. 覆盖率

工具：coverage 7.16.2（装在临时 venv 里）。配置是 `parallel = true`、`patch = subprocess, _exit, execv`、`sigterm = true`、`source = taskmgr`，因此 CLI、`taskmgr-remote`、daemon 以及 daemon 用 `execve` 启动的 shim 都计入了覆盖率。以下数据已经 combine，并排除了 `*/tests/*`。只统计语句覆盖，没开分支覆盖。

| 模块 | 语句 | 未覆盖 | 覆盖率 |
| --- | ---: | ---: | ---: |
| cliparse.py | 237 | 2 | 99% |
| common.py | 160 | 32 | 80% |
| daemon.py | 268 | 22 | 92% |
| gpu.py | 54 | 9 | 83% |
| hostcmds.py | 163 | 12 | 93% |
| hostlegacy.py | 42 | 2 | 95% |
| hostserver.py | 191 | 191 | 0%（见限制①） |
| hostsetup.py | 665 | 58 | 91% |
| monitor.py | 292 | 41 | 86% |
| package.py | 411 | 54 | 87% |
| procutil.py | 65 | 10 | 85% |
| prompt.py | 30 | 0 | 100% |
| pyz.py | 127 | 13 | 90% |
| records.py | 151 | 0 | 100% |
| remote.py | 379 | 59 | 84% |
| render.py | 130 | 27 | 79% |
| shim.py | 318 | 31 | 90% |
| store.py | 433 | 29 | 93% |
| taskmgr.py | 414 | 40 | 90% |
| template/taskmgr_task.py | 144 | 74 | 49% |
| textout.py | 109 | 12 | 89% |
| transport.py | 230 | 35 | 85% |
| `__main__.py` | 3 | 3 | 0% |
| **合计** | **5016** | **756** | **85%** |

数据的限制：

- ① `hostserver.py` 不是被 import 的，而是由 `hostsetup` 读出源码文本、以 `python3 -c TEXT` 执行，coverage 无法把它算回源文件，所以是 0%。它实际被 test_add_host 执行过。
- ② test_add_host（fake systemd 启动的 daemon）和 test_pyz 里的代码是从 `.pyz` 里运行的，文件路径在 zip 内部，不会映射回 `taskmgr/*.py`。所以 `remote.py`、`daemon.py`、`hostsetup.py` 的数字是下限。例如 `remote.py:478-492`（`job_dir_problems`）显示未覆盖，但 `test_incompatible_job_dirs_are_moved_to_a_backup` 实际经由 pyz 执行过它。
- ③ `template/taskmgr_task.py` 的 49% 来自 test_gpu_busy 的进程内调用；示例和模板里的 `run.py` 没有被执行。

## 3. 变异验证（每个变异在临时拷贝中进行，验证后拷贝即删除）

| # | 变异（文件:行） | 运行的用例 | 结果 |
| --- | --- | --- | --- |
| M1 | `store.py:114` 去掉 `fcntl.flock`（state.lock 失效） | sched 双派发、submit 并发 | 发现：两个用例都失败。双派发测试是因为**超时**失败，而不是被断言抓到（见 B5） |
| M2 | `daemon.py:199` 去掉 `reserved.update(uuids)` | `test_multi_gpu_job_not_starved` | 发现 |
| M3 | `daemon.py:191-192` 去掉 unknown 不参与预留的判断 | `test_unknown_job_does_not_lock_other_gpus` | 发现 |
| M4 | `package.py:383` 计算 hash 时不读文件内容 | 两个 hash 测试 + `test_submission_key_rules` | 只有 `test_submission_key_rules` 间接发现；**两个 hash 测试都通过**（B4） |
| M5 | `daemon.py:110` 不检查上传锁，清理正在上传的目录 | `test_daemon_restart_keeps_in_progress_upload` | 发现 |
| M6 | `monitor.py:100` 去掉 SIGKILL 升级 | `test_timeout_with_term_and_kill` | 发现 |
| M7 | `store.py:165` 去掉 queue/ 的二次扫描 | `test_a_job_moving_back_to_queue_is_not_missed` | 发现 |
| M8 | `procutil.py:88` 不算无标签的进程 | 两个 leftover 测试 | 只有 untagged 那个发现（说明两者不完全重复，见 D3） |
| M9 | `shim.py:456` 结束前由 shim 覆盖 `workdir/progress.json` | `test_progress_written_by_task_only` | **未发现**（B1） |
| M10 | `monitor.py:87` 停止流程第一步直接发 SIGKILL，不先发 SIGTERM | `test_timeout_with_term_and_kill` | **未发现**（B2） |
| M11 | `transport.py:339` 客户端不再检查 protocol_version | `test_protocol_version_mismatch` | **未发现**（B3） |

## 4. 问题清单

处理建议分为四类：删除 / 合并 / 重写 / 新增。

### 4.1 无效或基本无效的测试（改坏被测代码仍能通过）

**B1. `taskmgr/tests/test_exec.py:186-207` `test_progress_written_by_task_only`**
- 问题：测试名声称"progress 只由任务写"，但它只检查运行期间能读到任务写的 `stage: half`，没有检查 taskmgr 自己从不改写 progress.json。
- 证据：变异 M9 在 finalize 前让 shim 把 progress.json 写成 `{}`，测试仍然通过。
- 建议：**重写**。作业结束后读 `done/<jobid>/workdir/progress.json`（或用 `fetch`），断言内容与任务写入的完全一致；改名或补全断言，使其与测试名相符。

**B2. `taskmgr/tests/test_exec.py:285-295` `test_timeout_with_term_and_kill`**
- 问题：两个作业都只断言最终状态 `timed_out` 和进程已死，没有区分"先 SIGTERM、宽限期后 SIGKILL"这一顺序。
- 证据：变异 M10 让停止流程第一步就发 SIGKILL，测试仍然通过。
- 建议：**重写**。polite 作业 `trap 'echo term > $MARK; exit 0' TERM`，断言 MARK 存在（说明收到了 TERM）；stubborn 作业断言 `_taskmgr.signal == 9`，并检查 shim.log 里的 TERM→KILL 顺序。

**B3. `taskmgr/tests/test_submit.py:166-204` `test_protocol_version_mismatch`**
- 问题：`TASKMGR_TEST_PROTOCOL_VERSION` 是传给本地 transport 启动的 remote 进程的，于是由 remote 自己拒绝客户端，客户端一侧的 `check_protocol`（`transport.py:337`）从未被独立验证过。另外第 169-170 行只断言 `rc == 2`；第 171-180 行与第 167-168 行是同一件事（直接调用 remote 并传 `--protocol-version 99`），属于重复。
- 证据：变异 M11 把客户端检查改成 `if False`，测试仍然通过。
- 建议：**重写 + 合并**。新增一个用例：把 `remote_command` 换成一个返回 `{"protocol_version": 1, ...}` 的假脚本（写法参考 `test_query.py:469`），断言客户端报 `protocol_mismatch`；删除第 171-180 行；第 169 行补断言 error.code。

**B4. `taskmgr/tests/test_package.py:72-77` `test_hash_sees_exec_bit_and_content`**
- 问题：测试名包含 "and content"，但只改了 exec 位。`test_pack_is_deterministic_and_hash_matches_archive`（44-70 行）也只比较"同一目录两次计算"的结果，对内容不敏感。
- 证据：变异 M4 让 hash 完全不看文件内容，这两个测试都通过；只有 `test_submit.py:91-94`（同 key 不同内容应报冲突）间接发现。
- 建议：**重写**。补充三个断言：改一个字节 hash 变化、文件改名 hash 变化、symlink 目标变化 hash 变化。

**B5. `taskmgr/tests/test_sched.py:428-442`（`test_second_daemon_refuses_and_lockless_daemons_never_double_dispatch` 后半）**
- 问题：断言是 `retried_as == []`、`dispatched_at` 非空、`runner.jobid == j`、作业集合不变。同一作业被派发两次时，这些都可能成立，即这些断言本身检测不到"双重派发"。
- 证据：变异 M1 下，测试是因为某个作业卡住、`wait_state` 等 60 s **超时**才失败的，不是被断言抓到。超时这种表现依赖于双派发后具体怎么坏掉。
- 建议：**重写断言**。每个作业的脚本向 `runs-<task>` 文件追加一行，或在 `$TASKMGR_WORKDIR` 外按 jobid 记录次数，最后断言每个作业恰好执行了一次。

**B6. `taskmgr/tests/test_gpu_busy.py:117-129` `test_cancel_while_waiting`**
- 问题：`gpu_retry_interval_seconds=60`，`sleep 1.5` 后断言 `count() == 1`（"never launched again"）。即使取消没有终止等待，60 s 内也不会重启，这条断言等于空过。
- 建议：**重写**。重启间隔设为 1 s，取消后等待超过 2 个间隔（或用 `wait_scheduler_passes`），再断言启动次数仍为 1。

**B7. `taskmgr/tests/test_recovery.py:70-79` `test_daemon_restart_while_running`**
- 问题：任务是 `sleep 2; exit 0`。在高负载下，stop + start daemon 很容易超过 2 s，任务在 daemon 停止期间就结束了（shim 自己 finalize），断言照样成立，"运行中重启 daemon"这一场景并没有被真正覆盖。
- 建议：**重写**。改用 gate 文件：先断言 daemon 重启后作业仍是 `running`、shim_pid 不变，再放行。

**B8. `taskmgr/tests/test_submit.py:232-235`（`test_daemon_restart_keeps_in_progress_upload` 后半）**
- 问题：`clean_incoming` 只在 daemon 启动时跑一次（`daemon.py:363`）。第二次重启后 `sleep(0.5)` 就断言目录还在：清理是否已经执行完，0.5 s 并不能保证。另外，前半段依赖 `TASKMGR_TEST_RECEIVE_PAUSE=4` 覆盖"daemon 启动 + 清理"的全过程，负载高时会误报失败。
- 建议：**重写**。改为等待 daemon.log 里出现 "starting" 之后的下一条日志，或删除第二次重启（前半段已经覆盖同一断言，M5 已证明前半段有效）；PAUSE 用 gate 文件控制，不用固定秒数。

### 4.2 依赖固定 sleep 或时序（已观察到不稳定的排在前面）

**T1.（本次两次运行都失败）`taskmgr/tests/test_cli.py:148-153`**
- 问题：`retry` 之后，新作业在已经运行 daemon 的 s2 上入队，脚本是瞬间完成的 OK_RESULT，随后 `cancel` 期望 "cancel requested"。作业经常在 cancel 之前就已经 `succeeded`。
- 证据：见第 1 节失败信息。
- 建议：**重写**。retry 之前先 `stop_daemon("s2")`，或者包里用 gate 脚本，保证 cancel 时作业还在 queued 或 running。

**T2.（覆盖率运行中出错）`taskmgr/tests/test_gpu_busy.py:74-89`**
- 问题：在 `busy=2`、重试间隔 1 s 的条件下，期望连续 4 次 CLI 调用期间作业一直处于 running/pending。慢机器上作业已经结束，`status` 返回的是 result.json，没有 `state` 字段。
- 建议：**重写**。用 `busy_script(gate=...)` 让作业停留在 waiting，读完状态后再放行。

**T3. `taskmgr/tests/test_wait.py:21`、`taskmgr/tests/test_query.py:241`：`threading.Timer(1.5, unlink(flag))`**
- 问题：断言 stderr 含 "retrying"，前提是 CLI 在 1.5 s 内完成第一次失败的查询。CLI 冷启动慢于 1.5 s 时 flag 已经删掉，不会出现 retrying，测试误报失败。Timer 线程也没有 join：测试提前失败时，线程会在 sandbox 被清理之后再 unlink，并抛出 FileNotFoundError。
- 建议：**重写**。改用 Popen 启动 `status --wait`，读 stderr 等到出现 "retrying" 再删 flag；或者用计数调用的 wrapper（`test_query.py:279-284` 的写法）等第一次调用发生。两处同时 **合并**，见 D1。

**T4. `taskmgr/tests/test_sched.py:419` `time.sleep(0.5)` 后启动第二个 daemon**
- 问题：如果第一个 daemon 0.5 s 内还没拿到 daemon.lock，第二个会成为正式 daemon 并一直运行，`subprocess.run(timeout=20)` 抛出 TimeoutExpired。
- 建议：**重写**。改为 `wait_for` daemon.log 出现 "starting"，或等 `locks/daemon.lock` 里写入 pid。

**T5. `taskmgr/tests/test_recovery.py:148-155` `test_takeover_group_with_exited_leader_is_not_stopped`**
- 问题：组成员是 `sleep 4`，测试需要在 4 s 内完成 kill_shim、等 leader 退出、`sleep(1)`、断言 running。负载高时成员先退出，`assertTrue(pid_alive(member))` 会失败。
- 建议：**重写**。成员改用等待 gate 的循环，断言完成后再放行。

**T6. 固定 sleep 之后断言"仍然是 X"（负断言）**
- 位置：
  - `test_exec.py:268`、`test_exec.py:312`
  - `test_gpu_busy.py:139`
  - `test_recovery.py:65`、`:196`、`:313`、`:374`、`:417`、`:429`、`:444`
  - `test_query.py:337`
- 问题：daemon 是否已经在这段时间内跑完至少一次调度或健康检查，并没有保证。负载高时断言等于空过（假阴性），机器越慢越容易漏掉问题。docs/testing.md 已经要求用 `Env.wait_scheduler_passes(n)` 代替固定 sleep，但这些位置没有迁移。
- 建议：**重写**，统一换成 `wait_scheduler_passes`。GPU 被占时传入空闲 GPU，比较作业集合时剔除返回的 probe。`test_query.py:337`（`logs --follow` 仍在等待）可以改为先确认 follow 进程已经发出第一次查询。

**T7. 合理但可以去掉的 sleep**
- 位置：`test_package.py:51` `sleep(1.1)`、`test_query.py:130` `sleep(1.1)`、`test_add_host.py:473` `sleep(0.6)`
- 说明：前两处是为了制造不同的秒级时间戳。`test_package.py` 已经直接断言 gzip mtime 为 0，这 1.1 s 是多余的。`test_query.py` 可以改为直接改写 job.json 里的 `submitted_at`。
- 建议：**重写**（低优先级）。

### 4.3 依赖环境

**E1. `taskmgr/tests/test_exec.py:12-18, 32-35, 72`**
- 问题：任务脚本里调用的是 `python3`，但任务的 PATH 被固定为 `venv/bin:/usr/local/bin:/usr/bin:/bin`（第 72 行也断言了这一点）。如果 python3 只装在 pyenv、Homebrew 或 conda 的路径里，测试会失败。
- 建议：**重写**。用 `base_env` 或 `environment.venv` 把 `os.path.dirname(sys.executable)` 放进 PATH，或者用 sh 和 printf 写 JSON，不依赖 python3。

**E2. `taskmgr/tests/test_gpu_busy.py:314-317`**
- 问题：断言 `load_libcuda()` 抛出 "libcuda.so.1" 错误。在装有 NVIDIA 驱动的机器（也就是这个项目的生产环境）上运行测试会失败。
- 建议：**重写**。改为 `skipIf(ctypes.util.find_library("cuda"))`，或者在 mock 掉 `ctypes.CDLL` 的前提下断言错误信息。

**E3. `taskmgr/tests/test_add_host.py:1043-1055` `test_inspect_is_read_only`**
- 问题：直接继承调用者的 `os.environ`，没有像 Env 那样剔除 `TASKMGR_*` 和 `NO_COLOR`，与 docs/testing.md 中"shell 设置不会影响结果"的说法不一致。另外第 1055 行在 `TemporaryDirectory` 内部又 rmtree 了一次，是多余的。
- 建议：**重写**。传入清理过的 env，删除多余的 rmtree。

**E4. 纯单元测试挂在 `Env` 上**
- 位置：
  - `test_gpu_busy.py:265`（`TaskHelperTest`）
  - `test_query.py:46`、`:429`、`:480`、`:490`
  - `test_package.py:398`
  - `test_recovery.py:43`、`:485`
- 问题：这些测试不需要 sandbox，却继承了 `Env`。结果是在 macOS 上被整体跳过，在 Linux 上又白白建 sandbox。
- 建议：**重写**，改为继承 `unittest.TestCase`；只依赖 /proc 的（`test_recovery.py:485`）单独加 `skipUnless(linux)`。

### 4.4 断言过弱 / 写法问题

| # | 位置 | 问题 | 建议 |
| --- | --- | --- | --- |
| W1 | `test_recovery.py:405-410` | `--as stopped` 缺 `--confirm`、`--confirm` 写错，都只断言 rc=2。覆盖率显示 remote 侧对应的检查（`remote.py:349,351`）从未执行：rc=2 来自客户端的 `taskmgr.py:508-511`。remote 侧的校验没有测试 | 重写：断言 error.code=`bad_args`；新增直接调 `self.remote("resolve", u, "--as", "stopped", "--confirm", "x")` 的用例 |
| W2 | `test_submit.py:26-28` | `assertEqual(set(r) >= {...}, True)`，失败时信息无用 | 重写为 `assertLessEqual({...}, set(r))` |
| W3 | `test_submit.py:142-146` | `assertFalse(listdir(root) != ["config.json"] and listdir(queue))` 难以读懂，且 queue 不存在时会抛 FileNotFoundError 而不是失败 | 重写为"s2/s3 没有 queue 目录，或 queue 为空" |
| W4 | `test_query.py:394-395` | `assertIn(j, job.json 文本)`，jobid 出现在任何字段都会通过 | 重写为断言 `retry_of == j` |
| W5 | `test_package.py:112-122` | 期望值里的 `content_hash` 直接取自 `out` 本身，属于同义反复 | 重写：断言等于 `package.content_hash(target)` |
| W6 | `test_package.py:158`、`:162`、`:173`、`:237`；`test_submit.py:169-170` | 只断言退出码，不断言错误码，可能因为别的原因失败而被误判为通过 | 重写：补断言 error code |
| W7 | `test_exec.py:93-94` | `assertIn("HOME")`、`assertIn("USER")` 只检查存在 | 重写：断言等于 `pwd` 中的值 |
| W8 | `test_pyz.py:427-428` | `wait_state(... "succeeded")` 之后又断言 state 等于 succeeded，重复 | 删除第 428 行 |
| W9 | `test_pyz.py:373-390` | 名为"host add looks next to task.pyz"，实际只调用 `pyz.default_remote_pyz()` | 重写：改名，或真的从 task.pyz 运行 `host add`（不加 `--pyz`）走到 code 步骤 |
| W10 | `test_add_host.py`（fake systemctl `stop()`，第 63-71 行） | fake 的 stop 无论 unit 写的是什么 KillMode 都只杀主进程，所以 `test_code_update_restarts_service_and_keeps_running_task` 不能证明 KillMode 设置有效。KillMode 实际只由 `test_add_host.py:404` 和 `test_recovery.py:46` 的字符串检查保证 | 重写：fake systemctl 解析 `KillMode=`，非 `process` 时按 control-group 杀整组 |
| W11 | `helpers.py:434-435` | `wait_for` 超时信息总是打印 s1 的 daemon log，等待 s2 的作业时日志不对 | 重写：`wait_for` 接收 `name` 参数 |
| W12 | `helpers.py:189` | `cwd.startswith(root)` 没有加路径分隔符，`/tmp/taskmgr-test-abc` 会匹配 `/tmp/taskmgr-test-abcd`，可能误杀并行测试的进程（概率低） | 重写为 `cwd == root or cwd.startswith(root + "/")` |

### 4.5 测实现细节

- **I1. `test_query.py:529-548` `test_lookup_confirms_a_miss_under_the_state_lock`**：子类覆盖私有方法 `_scan_job_dir`，并读取私有属性 `_lock_depth` 来计数。重构 Store 时会误报失败。
  - 建议：**重写**。用真实竞态模拟：另一线程在两次扫描之间 rename 目录（可通过 monkeypatch `os.listdir` 制造一次未命中）；或者保留，但注明它是白盒测试。
- **I2. `test_query.py:515-527`**：依赖 `iter_jobs` 是生成器，且先列 queue/。M7 证明它确实有效，但会随实现变化而失效。
  - 建议：保留，加注释说明白盒依赖。
- **I3. `test_gpu_busy.py:298`**：断言模块私有变量 `_retained`。
  - 建议：**重写**为断言 `FakeCuda.retained`（第 297 行已有），删除第 298 行。
- **I4. `test_submit.py:266-275`**：在测试进程里直接调用 `store.stage_package`、`store.remove_tree`。这是对"共享只读文件"这一不变量的白盒测试，可以接受，但它与第 253-265 行的端到端部分是两件事。
  - 建议：拆成单独用例（**重写**）。

### 4.6 重复测试

| # | 位置 | 说明 | 建议 |
| --- | --- | --- | --- |
| D1 | `test_wait.py:12-37` 与 `test_query.py:232-272` | 同一场景（等待时连接失败重试，超过 `--retry-timeout` 后报错），区别只在是否带 `--host`；T3 的时序问题两处都有 | **合并**进 `test_query.py`，用 subTest 覆盖带与不带 `--host`；删除 test_wait.py |
| D2 | `test_gpu_busy.py:227-235` 与 `test_gpu_busy.py:319-325` | 都在测 `gpu_busy()` 打印信息并以 75 退出（一个用子进程，一个在进程内） | **合并**：保留进程内版本（它还覆盖了环境变量），删除子进程版本 |
| D3 | `test_exec.py:319-327` 与 `test_exec.py:329-339` | 只差子进程有没有 jobid 标签；M8 证明 untagged 那个有独立价值 | **合并**为一个用例，用 subTest 覆盖两种脚本 |
| D4 | `test_query.py:46-60`、`test_query.py:429-446`、`test_add_host.py:1014-1041` | 都在测 `Remote.ssh_argv` 的默认选项和覆盖逻辑 | **合并**为一个纯单元测试类 |
| D5 | `test_cli.py:26-41` 与 `test_cli.py:43-71` | 第二个已经从 `-h` 输出中解析出全部命令名并逐个检查 | **合并**：把 group 标题和"旧命令已删除"两条断言并入第二个，删除第一个 |
| D6 | `test_package.py:79-96` 与 `test_cli.py:99-123` | init / validate 的退出码、占位符错误、路径不存在，两处都测了 | **合并**：test_package 保留 JSON 语义，test_cli 只保留文本格式 |
| D7 | `test_recovery.py:43-48` 与 `test_add_host.py:994-1001` | 都检查 deploy 模板里的 unit（后者已经要求生成的 unit 与模板逐行一致） | **删除** `test_recovery.py:43-48`，把其中 "User=" 那条断言移到 test_add_host |
| D8 | `test_exec.py:394-405` 与 `test_cli.py:148-150` | 不带 `--host` 的 retry 表格输出 | **合并**：test_cli 的 retry 段只保留"按 jobid 找到 host"，表格格式由 test_exec 负责 |

### 4.7 效率（非缺陷，顺带）

- `test_pyz.py:295-314`：每个用例的 setUp 都重新构建两个 pyz（共 7 次）。可参照 `AddHostTest.setUpClass` 只构建一次。建议：**重写**。

## 5. 覆盖缺口（高风险路径没有测试）

按风险从高到低排列。行号是覆盖率报告中未执行的行；"新增"即建议补的测试。

1. **daemon 启动时修复"状态与目录不一致"**（`daemon.py:131-132`）。job.json 已经写了新状态，但还没 rename 时崩溃，是第 8 节设计里明确要处理的情况，完全没有测试。
   - 新增：手工制造 `state=succeeded` 但目录仍在 in-progress/ 的作业，启动 daemon，断言目录移到 done/。
2. **shim 崩溃点 `before_exec`、`before_finalize`**（`shim.py:277`、`:456`）。docs/testing.md 列出了这两个钩子，但没有任何测试使用。相关恢复路径都没有执行过：`daemon.py:147`（`repair_terminal` 补写 result）、`store.py:457-458`（runner 已是 final 但 result.json 不完整时重写）。
   - 新增：两个用例，分别用这两个崩溃点，断言作业最终结果正确、result.json 完整、没有重跑。
3. **/proc 或 boot_id 不可读时进入 unknown**（`monitor.py:352-355`、`:369-372`、`:525-527`，以及 `procutil.py:67-70`）。这类"无法判定"的分支决定了作业是被标成 unknown 还是被误判为结束（可能导致 GPU 被重复分配），没有测试。`test_resolve_hands_decidable_unknown_back_to_takeover` 只是手工构造了 unknown 状态，并没有走到这些分支。
   - 新增：把 `TASKMGR_BOOT_ID_FILE` 指向不可读的路径，断言作业变为 unknown，error.kind=`undeterminable`。
4. **takeover 期间停止未确认**（`monitor.py:516-523`）。shim 死后由 daemon 接管停止，SIGKILL 无效时应进入 unknown，没有测试；现有 `TASKMGR_TEST_IGNORE_KILL` 用例只覆盖了 shim 在场的情况。
   - 新增：先 kill_shim，再用 IGNORE_KILL 启动 daemon 并 cancel，断言变为 unknown。
5. **自动重跑的幂等性**（`store.py:572-573`）。clone 出新作业后、写 `retried_as` 前崩溃，再次恢复时应找到已有的重跑作业而不是再 clone 一个，没有测试。
   - 新增：手工构造"已有 `retry_of=j` 的作业，但 j 的 `retried_as` 为空"，断言恢复后仍只有一个重跑作业。
6. **identity 记录不完整但任务可能已启动**（`monitor.py:343-345`）。这一分支进入 unknown，没有测试。
   - 新增：runner 带 pgid、不带 leader_start_time 等，断言变为 unknown，error.kind=`record_incomplete`。
7. **remote 侧的参数校验与内部错误兜底**（`remote.py:349`、`:351`、`:644-656`）。`resolve --as stopped` 的确认只在客户端被测过（W1）。remote 遇到未预期异常时应返回 `internal_error` JSON，这一协议约定也没有测试。
   - 新增：直接调用 remote 的 resolve；用损坏的 job.json 触发异常，断言输出 JSON 错误。
8. **`task update`**（`taskmgr.py:659-676`）。会对用户的 checkout 执行 `git pull --ff-only`，只测了"不是 checkout"这一种拒绝。
   - 新增：在临时 git 仓库里测试 fast-forward 成功、非 fast-forward 失败（`update_failed`）、没有变化（`changed=false`）。
9. **fetch 失败时的清理**（`taskmgr.py:563-574`）。中途失败或目标已存在时，应删除 staging 和新建的空 `DIR/<host>`，没有测试；不安全条目被跳过时的警告也没有测试。
   - 新增：在 fetch 过程中让 remote 返回截断的 tar，断言输出目录里没有残留。
10. **transport 的异常路径**（`transport.py:278-279`、`:315-331`、`:380-387`）。transport 命令无法执行（OSError），以及 `stream`（logs/fetch）收到 remote 错误 JSON 时走协议检查，都没有测试。`edit_machines` 的并发和失败路径也没有测试。真实 ssh 从未执行过，只测了 argv（这一点可以接受，有 docs/testing.md 里的手工冒烟流程补充）。
11. **daemon 的 fork/exec 失败处理**（`daemon.py:288-291`、`:309-313`）。fork 失败没有测试；exec 失败由 `TASKMGR_SHIM_EXECUTABLE` 覆盖了，但子进程里 except 分支的代码没有被计入覆盖率，是否真的执行过存疑。
12. **package 校验的部分规则**（`package.py:118-127`、`:194-208`、`:487-504` 等）。GPU 字符串不是 `GPU-` 前缀、各类 schema 类型错误、tar 里 `./` 前缀的名字等，没有测试。
    - 新增：在 `test_invalid_packages_rejected` 的表里补充这些用例。
13. **`template/taskmgr_task.py` 只有 49%**。任务作者直接复制这个文件，`claim_gpu` 以外的辅助函数（写 result 和 progress 的 helper）没有测试；示例的 `run.py` 也从未被执行过（`test_examples_are_valid` 只做了 validate）。
    - 新增：提交 `examples/succeed`，断言作业 succeeded。

## 6. 建议的处理顺序

1. 先修两个已经观察到的不稳定测试（T1、T2）和 Timer 竞争（T3/D1），否则 CI 会随机变红。
2. 修复变异验证中漏掉的问题（B1-B4），以及断言检测不到双派发的问题（B5）：这些测试现在给出的是虚假的安全感。
3. 把固定 sleep 加负断言（T6、B6-B8）统一迁移到 `wait_scheduler_passes` 或 gate 文件。
4. 按第 5 节第 1-6 项补崩溃恢复类测试，这些是第 12、13 节设计的核心路径。
5. 合并重复测试（D1-D8），把纯单元测试移出 `Env`（E4），改善 macOS 上的本地反馈。
