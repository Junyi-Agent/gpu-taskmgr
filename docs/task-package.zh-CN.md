---
title: 任务包
tags: [taskmgr, task-package, schema]
lang: zh-CN
up: "[[taskmgr/README.zh-CN]]"
---

# 任务包

其他语言：[[docs/task-package]]

| 文件 | 是否必需 | 说明 |
| --- | --- | --- |
| `task.json` | 是 | 执行配置（见下） |
| `TASK.md` | 是 | 供人或 AI agent 阅读；taskmgr 从不读取它 |
| `progress.json` | 是 | 初始进度 |
| 入口脚本、代码、输入文件 | 按需 | |
| `precompile.py`、`cutecache.py` | 模板自带 | 在 CPU 上编译 CuTe DSL kernel、在 GPU 上加载（见“预编译”）；任务不需要编译时删除 |
| `taskmgr_task.py` | 模板自带 | 仅依赖标准库的辅助模块：`write_progress`、`update_progress`、`write_result`、`update_outputs`（flock + 原子替换）；`claim_gpu`、`gpu_busy`（见下文 GPU 约定） |

`task.json`（允许出现未知的额外键，会被忽略）：

```json
{
  "schema_version": 1,
  "task_id": "AA-01",
  "entry": {"argv": ["python", "run.py", "--config", "cfg.yaml"]},
  "gpus": [0, 1],
  "environment": {"venv": "/opt/venvs/tune", "required_paths": ["/usr/local/cuda-12.4"]},
  "env": {"NCCL_DEBUG": "WARN"},
  "timeout_seconds": 7200,
  "result": {"required_outputs": ["best_config", "speedup"]}
}
```

- `task_id` 须匹配 `^[A-Z]{2}-[0-9]{2,4}$`。
- `entry.argv[0]`：包含 `/` 时视为路径（相对路径指包内文件，相对作业的工作目录解析，必须
  必须是包内的可执行文件）；否则视为命令名，在远程入口进程的 PATH 上查找。执行时不经过 shell；需要 shell 语法时
  使用 `["bash", "-c", "..."]`。
- `precompile`（可选）：`{"argv": [...], "timeout_seconds": N}`，见“预编译”。`argv` 的规则与
  `entry.argv` 相同；`timeout_seconds` 可省略。
- `gpus`：nvidia-smi 索引（PCI 总线顺序）或完整的 `GPU-...` UUID；`[]` 表示不用 GPU。MIG
  UUID 和重复项（包括索引和 UUID 指向同一块 GPU 的情况）会被拒绝。
- `env` 的变量名须匹配 `^[A-Za-z_][A-Za-z0-9_]*$`，值须为字符串。`env` 不得设置
  `CUDA_VISIBLE_DEVICES`、`CUDA_DEVICE_ORDER`、`VIRTUAL_ENV`、`TASKMGR_*`。

`progress.json`：`{"schema_version": 1, "stage": str, "updated_at": "UTC ISO 8601",
"percent"?: 0..100, "message"?: str}`。只由任务写入。

`result.json`（运行期间由任务写入，进程组全部退出后由 runner 合并）：

```json
{"schema_version": 1, "outputs": {...}, "<其他业务字段>": "...",
 "_taskmgr": {"jobid", "task_id", "retry_of", "host", "gpus": [{"declared", "uuid"}],
              "status", "return_code", "signal", "started_at", "ended_at",
              "validation_errors": [], "error": {"kind", "message"} | null,
              "warnings": ["task_wrote_taskmgr" | "leftover_processes"],
              "paths": {"job_dir", "workdir", "stdout", "stderr", "shim_log",
                        "progress", "result", "artifacts"},
              "final": true}}
```

结果有效的条件：是一个 JSON 对象，含有 `outputs` 对象，且 `outputs` 包含 `required_outputs`
中列出的每个键。文件不存在时生成 `{"schema_version", "_taskmgr"}`；文件损坏（包括嵌套过深、无法
解析的 JSON），或 `result.json` 不是普通文件（目录、FIFO、符号链接：不跟随链接）时，原文件保留为
`result.json.corrupt`，并在 `validation_errors` 中报告。`progress.json` 不是普通文件时，
`task status` 中显示为 `{"error": ...}`。任务自己写的 `_taskmgr` 会被丢弃，并记录
警告 `task_wrote_taskmgr`。

`error.kind` 的取值：`setup`、`precompile`（预编译命令失败）、`shim_start_failed`、`shim_error`（shim 在启动入口命令之前出错；
作业以 `failed` 结束，不会重跑）、`exit_code`、`signal`、`exit_code_lost`、
`interrupted`（运行被中断；除 `task resolve` 结束的 `unknown` 作业外，已创建自动重跑作业）、
`too_many_interruptions`、`operator_marked_stopped`、`stop_unconfirmed`（作业处于 unknown）、
`record_incomplete` / `undeterminable`（恢复后处于 unknown）、`recovery_error`（处于 unknown：
守护进程对该作业的恢复或跟踪反复出错，见 [[docs/recovery.zh-CN]]）。

## 打包与校验

`task validate` 和 `task submit` 在本地检查任务包；上传后服务器会再检查一次。

- 不打进归档、也不计入内容哈希的路径：任一路径分量名为 `__pycache__`、`.git` 或 `.DS_Store`，
  以 `.pyc` 结尾，或以 `.tmp-` 开头（未完成的原子写入）；另外，包根目录下的 `result.json`、
  `result.json.lock`、`progress.json.lock`（用 `taskmgr_task.py` 本地试跑留下的文件）和以
  `result.json.corrupt` 开头的名字。`result.jsonl` 这类其他名字照常打包。服务器收到归档后，
  先删除这些条目再计算内容哈希，所以归档里残留的 `result.json` 不会被当成本次运行的结果。
  `task validate` 把每个被排除的路径列为警告（代码 `excluded`；被排除的目录只列目录本身）。
- 大小：服务器解包时，普通文件总大小超过它的 `max_package_bytes` 就拒收（错误码
  `package_too_large`）；上传数据超过上限加 1/16 再加 1 MiB 时，直接停止接收。`task submit`
  不在本地检查大小。包超过服务器默认值（1073741824 字节）时，`task validate` 给出警告（代码
  `package_too_large`）；各服务器的上限可以更大或更小。
- 符号链接必须是相对路径，并且（包括经过其他链接后）解析到包内；特殊文件（FIFO、设备、socket）
  会被拒绝。
- 相对路径的 `entry.argv[0]` 必须指向包内一个不被排除在打包之外、带可执行位的普通文件（错误码
  `entry`；用 `chmod +x` 修复，或通过解释器运行，例如 `["python", "run.py"]`）。
- 解包归档时（服务器上，以及对 `.tar.gz` 运行 `task validate` 时）拒绝：绝对路径、`..`、重复条目
  （不区分大小写比较）、硬链接、设备文件、FIFO，以及指向或解析到包外的符号链接。不会经由前面条目
  创建的符号链接写文件，也不会覆盖已存在的文件。`task validate` 列出所有不安全的条目；服务器遇到
  第一个就停止（`unsafe_package`）。

## 预编译

入口运行期间作业占着 GPU，独占模式下别的进程用不了这些 GPU。编译要放在这之前。在 `task.json`
中设置 `precompile`：

```json
"precompile": {"argv": ["python3", "precompile.py", "kernels.py"], "timeout_seconds": 1800}
```

| 显示的 STATE | 作业状态 | 发生的事 | GPU |
| --- | --- | --- | --- |
| `queued` | queued | 等待 CPU 名额（`max_cpu_jobs`） | 无 |
| `precompiling` | running | 运行预编译命令 | 无，`CUDA_VISIBLE_DEVICES=""` |
| `pending-gpu` | queued | 按原来的排队位置等待 GPU | 无 |
| `running` | running | 运行入口 | 有 |

- 两条命令在同一个工作目录中运行，入口能直接用预编译的产物。
- 退出码 0 时进入下一阶段。其他退出码（包括 75）使作业失败，`error.kind = "precompile"`，入口不会
  运行。预编译时不设置 `TASKMGR_GPU_BUSY_EXIT_CODE`。
- `precompile.timeout_seconds` 限制预编译，`timeout_seconds` 只限制入口。`pending-gpu` 的等待时间
  两者都不计。
- 每个阶段都能 `task cancel`。`task logs` 显示两条命令的输出。
- 重试和自动重跑都是工作目录为空的新作业，会重新预编译。

`task init` 会带上 `precompile.py` 和 `cutecache.py`。`precompile.py` 在 CPU 上运行文件中的
`_*compile_all()` 步骤，把 kernel 存到工作目录的 `kernel-cache/`（可用 `CUTECACHE_DIR` 覆盖）。入口用
`cutecache.load_precompiled("kernels.py")` 加载；没有预编译的 kernel 会让运行失败，不会在 GPU 上
编译。默认值对应 H200，可用 `PRECOMPILE_ARCH`、`PRECOMPILE_QUACK_ARCH`、`PRECOMPILE_N_SM` 和
`PRECOMPILE_CAPABILITY` 修改。

## 入口进程的环境

1. `PATH=/usr/local/bin:/usr/bin:/bin`、`HOME`、`USER`（守护进程所属用户）、`LANG=C.UTF-8`，
   然后叠加 `config.json` 中的 `base_env`。
2. 声明了 `environment.venv` 时：`VIRTUAL_ENV=<venv>`，`PATH=<venv>/bin:$PATH`。
3. `task.json` 中的 `env`。
4. runner 设置：`CUDA_DEVICE_ORDER=PCI_BUS_ID`、`CUDA_VISIBLE_DEVICES=<按声明顺序的 UUID>`
   （`[]` 时为空字符串）、`TASKMGR_JOBID`、`TASKMGR_TASK_ID`、
   `TASKMGR_WORKDIR`、`TASKMGR_PROGRESS_FILE`、`TASKMGR_RESULT_FILE`，以及
   `TASKMGR_GPU_BUSY_EXIT_CODE`（服务器的 `gpu_busy_exit_code`；为 `null` 时删除这个变量，
   `base_env` 设置了也删除）。

stdin 接到 `/dev/null`；stdout/stderr 分别写入作业目录下的 `stdout.log` / `stderr.log`。

一个作业只运行一次。`retry_of` 是本作业重跑的 jobid（`task retry` 或中断后的自动重跑），直接提交的
作业为 `null`。

## GPU 约定：退出码 75

GPU 服务器运行在 Exclusive_Process 计算模式下，taskmgr 之外的进程也可能占着某块 GPU，这时无法在
这块 GPU 上创建 CUDA context。taskmgr 不为此查询 nvidia-smi，而是由任务自己报告：

- 入口进程以 **75**（`EX_TEMPFAIL`，即 `taskmgr_task.GPU_BUSY_EXIT_CODE`）退出，表示"拿不到
  GPU，没有做任何工作，稍后再启动我"。shim 等待 `gpu_retry_interval_seconds`（默认 1 秒）后，在
  同一个 workdir 中重新启动入口命令（日志追加写入，并加一行分隔）。作业保持 `running`，继续占用
  它的 GPU，`task jobs` 显示为 `pending-gpu`。等待时间不计入 `timeout_seconds`，每次启动
  都从自己的启动时刻计时。等待期间可以 `task cancel`。
- 在产生任何副作用（workdir 之外的文件、结果）之前退出 75。workdir 会保留：重新启动时能看到上一次
  启动留下的文件（例如编译好的二进制），所以退出 75 之前写下的东西必须可以直接复用。
- 不要因为其他原因退出 75：这样的任务会被一直重新启动，直到被取消。服务器可以关闭这个约定
  （`gpu_busy_exit_code: null`），此时 75 就是普通的失败。
- `taskmgr_task.claim_gpu()`（模板的 `run.py` 第一步就调用它）：`CUDA_VISIBLE_DEVICES` 为空或未
  设置时什么也不做；否则用 ctypes 加载 `libcuda.so.1`，调用 `cuInit`，并对每个可见设备调用
  `cuDeviceGet` 和 `cuDevicePrimaryCtxRetain`。primary context 在进程整个生命周期内保持 retain，
  torch 等框架会复用它。任一调用返回 `CUDA_ERROR_DEVICE_UNAVAILABLE`（46）时调用
  `gpu_busy("GPU <i> (<uuid>) is held by another process ...")`；其他 CUDA 错误以及找不到
  `libcuda.so.1` 时抛出 `RuntimeError`（任务声明了 GPU，这属于环境问题）。应在耗时的 import 或编译
  之前调用。它的 `load` 参数用于在测试中替换库加载函数。
- `taskmgr_task.gpu_busy(message="")` 把消息写到 stderr，并以 `taskmgr_task.gpu_busy_exit_code()`
  退出：取 `TASKMGR_GPU_BUSY_EXIT_CODE` 的值；未设置或不是 1 到 255 的整数时用 75。你自己的 CUDA
  代码遇到 "device busy/unavailable" 时调用它。
- 如果 CUDA 工作在子进程中进行，不要在父进程里调用 `claim_gpu()`：在 exclusive-process 模式下，
  父进程的 context 会挡住子进程。让子进程退出 75，父进程再用 `gpu_busy()` 转发。
