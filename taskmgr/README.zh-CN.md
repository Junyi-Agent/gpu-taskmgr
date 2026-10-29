# taskmgr

[[taskmgr/README|English]] | 简体中文

用于 GPU 性能调优的任务管理工具。本地 CLI（`task`）负责保存和提交任务包。每台远程 GPU
服务器运行 `taskmgr-remote`，由它维护自己的队列，把作业调度到 GPU 上运行，并在重启后恢复状态。
系统中没有中心调度器。运行要求：Python 3.10+ 标准库；远程端须为 Linux。

需求文档：[[agent/desc-1.md]]。设计决策：[[agent/desc-2.md]]。

## 快速开始

```sh
# 本地
uv tool install --editable /path/to/gpu-taskmgr
mkdir -p ~/.taskmgr && cp taskmgr/deploy/machines.example.json ~/.taskmgr/machines.json

# 远程（无需 root）：systemd 用户服务，见 docs/install.zh-CN.md
systemctl --user enable --now taskmgr-remote && loginctl enable-linger "$USER"

# 使用
task init AA-01                      # 填写所有 <REQUIRED...> 标记
task validate AA-01/
task submit AA-01/                   # 输出 {"host", "jobid", ...}
task status <jobid> --wait
task fetch <jobid> --host <host> --output ./results/
```

## 文档

| 文档 | 内容 |
| --- | --- |
| [[docs/install.zh-CN]] | 本地 CLI、远程服务器、systemd 用户服务、`config.json` |
| [[docs/usage.zh-CN]] | 命令、JSON 输出、退出码 |
| [[docs/task-package.zh-CN]] | `task.json`、`progress.json`、`result.json`、入口进程环境 |
| [[docs/internals.zh-CN]] | 远程目录结构、`job.json` / `runner.json`、状态机、调度、shim、停止流程 |
| [[docs/recovery.zh-CN]] | 重启恢复、手动处理 `unknown` |
| [[docs/assumptions.zh-CN]] | 需求文档未规定之处的全部假设；限制 |
| [[docs/testing.zh-CN]] | 测试命令、仅用于测试的覆盖项、真实 SSH 冒烟测试 |
| [[docs/development.zh-CN]] | uv 环境、pre-commit（ruff 格式化、basedpyright 检查） |

需求文档要求 README 说明的几点：

- systemd unit 使用 `KillMode=process`，停止守护进程不会停止正在运行的任务。与为每个 shim 使用
  `systemd-run --scope` 相比，这种方式不需要访问 D-Bus（[[docs/install.zh-CN]]）。
- 只管理任务所在的进程组；自行调用 `setsid()` 的进程会脱离管理（[[docs/assumptions.zh-CN]]）。
- 仅用于测试的覆盖项在 [[docs/testing.zh-CN]] 中列出并标注，该文档也包含可选的真实 SSH 冒烟
  测试。

## 代码结构

```
taskmgr/
  taskmgr.py      本地 CLI 入口（`task`）
  remote.py       远程入口（`taskmgr-remote`）：子命令、`serve`、`shim`
  daemon.py       调度守护进程：扫描队列、GPU 预留、启动 shim、恢复循环
  shim.py         每个 attempt 一个的监管进程：启动前检查、exec、wait、停止、收尾
  monitor.py      停止流程、重启恢复检查、守护进程接管跟踪
  store.py        远程存储根目录、状态锁、job/runner 记录、结果合并
  package.py      校验、确定性打包、内容哈希、安全解包
  gpu.py          查询 nvidia-smi、解析 GPU 声明
  procutil.py     /proc：boot_id、进程启动时间、进程组成员
  common.py       常量、原子 JSON 写入、flock 辅助函数
  template/       任务包模板（`task init`），含 taskmgr_task.py
  examples/       succeed/（EX-01，仅 CPU）和 multi-gpu/（MG-01，gpus [0, 1]）
  deploy/         taskmgr-remote.user.service（默认）、taskmgr-remote.service（需 root）、
                  machines.example.json、config.example.json
  tests/          unittest 测试套件：python -m unittest discover -s taskmgr/tests
bin/task, bin/taskmgr-remote   从源码目录直接运行的包装脚本
docs/            文档（英文 *.md，中文 *.zh-CN.md）
```
