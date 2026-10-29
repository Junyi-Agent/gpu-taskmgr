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
  可执行）；否则视为命令名，在远程入口进程的 PATH 上查找。执行时不经过 shell；需要 shell 语法时
  使用 `["bash", "-c", "..."]`。
- `gpus`：nvidia-smi 索引（PCI 总线顺序）或完整的 `GPU-...` UUID；`[]` 表示不用 GPU。MIG
  UUID 和重复项（包括索引和 UUID 指向同一块 GPU 的情况）会被拒绝。
- `env` 不得设置 `CUDA_VISIBLE_DEVICES`、`CUDA_DEVICE_ORDER`、`VIRTUAL_ENV`、`TASKMGR_*`。

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
中列出的每个键。文件不存在时生成 `{"schema_version", "_taskmgr"}`；文件损坏时原文件保留为
`result.json.corrupt`，并在 `validation_errors` 中报告；任务自己写的 `_taskmgr` 会被丢弃，并记录
警告 `task_wrote_taskmgr`。

`error.kind` 的取值：`setup`、`shim_start_failed`、`exit_code`、`signal`、`exit_code_lost`、
`interrupted`（运行被中断，已创建自动重跑作业）、
`too_many_interruptions`、`operator_marked_stopped`、`stop_unconfirmed`（作业处于 unknown）、
`record_incomplete` / `undeterminable`（恢复后处于 unknown）。

## 入口进程的环境

1. `PATH=/usr/local/bin:/usr/bin:/bin`、`HOME`、`USER`（守护进程所属用户）、`LANG=C.UTF-8`，
   然后叠加 `config.json` 中的 `base_env`。
2. 声明了 `environment.venv` 时：`VIRTUAL_ENV=<venv>`，`PATH=<venv>/bin:$PATH`。
3. `task.json` 中的 `env`。
4. runner 设置：`CUDA_DEVICE_ORDER=PCI_BUS_ID`、`CUDA_VISIBLE_DEVICES=<按声明顺序的 UUID>`
   （`[]` 时为空字符串）、`TASKMGR_JOBID`、`TASKMGR_TASK_ID`、
   `TASKMGR_WORKDIR`、`TASKMGR_PROGRESS_FILE`、`TASKMGR_RESULT_FILE`。

stdin 接到 `/dev/null`；stdout/stderr 分别写入作业目录下的 `stdout.log` / `stderr.log`。

一个作业只运行一次。`retry_of` 是本作业重跑的 jobid（`task retry` 或中断后的自动重跑），直接提交的
作业为 `null`。

## GPU 约定：退出码 75

GPU 服务器运行在 Exclusive_Process 计算模式下，taskmgr 之外的进程也可能占着某块 GPU，这时无法在
这块 GPU 上创建 CUDA context。taskmgr 不为此查询 nvidia-smi，而是由任务自己报告：

- 入口进程以 **75**（`EX_TEMPFAIL`，即 `taskmgr_task.GPU_BUSY_EXIT_CODE`）退出，表示"拿不到
  GPU，没有做任何工作，稍后再启动我"。shim 等待 `gpu_retry_interval_seconds`（默认 2 秒）后，在
  一份全新的包副本中重新启动入口命令（日志追加写入，并加一行分隔）。作业保持 `running`，继续占用
  它的 GPU，`task jobs` 显示为 `pending (gpu busy)`。等待时间不计入 `timeout_seconds`，每次启动
  都从自己的启动时刻计时。等待期间可以 `task cancel`。
- 在产生任何副作用（workdir 之外的文件、结果）之前退出 75；GPU 忙的那次启动的 workdir 会被丢弃。
- 不要因为其他原因退出 75：这样的任务会被一直重新启动，直到被取消。服务器可以关闭这个约定
  （`gpu_busy_exit_code: null`），此时 75 就是普通的失败。
- `taskmgr_task.claim_gpu()`（模板的 `run.py` 第一步就调用它）：`CUDA_VISIBLE_DEVICES` 为空或未
  设置时什么也不做；否则用 ctypes 加载 `libcuda.so.1`，调用 `cuInit`，并对每个可见设备调用
  `cuDeviceGet` 和 `cuDevicePrimaryCtxRetain`。primary context 在进程整个生命周期内保持 retain，
  torch 等框架会复用它。任一调用返回 `CUDA_ERROR_DEVICE_UNAVAILABLE`（46）时调用
  `gpu_busy("GPU <i> (<uuid>) is held by another process ...")`；其他 CUDA 错误以及找不到
  `libcuda.so.1` 时抛出 `RuntimeError`（任务声明了 GPU，这属于环境问题）。应在耗时的 import 或编译
  之前调用。它的 `load` 参数用于在测试中替换库加载函数。
- `taskmgr_task.gpu_busy(message="")` 把消息写到 stderr 并以 75 退出。你自己的 CUDA 代码遇到
  "device busy/unavailable" 时调用它。
- 如果 CUDA 工作在子进程中进行，不要在父进程里调用 `claim_gpu()`：在 exclusive-process 模式下，
  父进程的 context 会挡住子进程。让子进程退出 75，父进程再用 `gpu_busy()` 转发。
