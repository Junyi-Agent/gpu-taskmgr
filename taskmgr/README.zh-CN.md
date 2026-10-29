# taskmgr

[[taskmgr/README|English]] | 简体中文

用于 GPU 性能调优的任务管理工具。本地 CLI（`task`）负责保存和提交任务包。每台远程 GPU
服务器运行 `taskmgr-remote`，由它维护自己的队列，把作业调度到 GPU 上运行，并在重启后恢复状态。
系统中没有中心调度器。运行要求：Python 3.10+ 标准库；远程端须为 Linux。

需求文档：[[agent/desc-1.md]]。设计决策：[[agent/desc-2.md]]。

## 快速开始

下面 `gpu1` 是你在机器列表中给 GPU 服务器起的名字，`tuner@10.0.0.12` 是它的 ssh 地址，`tuner`
是服务器上的用户名。
整个过程不需要 root。

**1. 本机：安装 CLI**

```sh
uv tool install --editable /path/to/gpu-taskmgr     # 提供 task 命令
```

**2. 本机：配置服务器**

```sh
task add host gpu1 --ssh tuner@10.0.0.12
```

这条命令把代码拷贝到服务器的 `~/taskmgr`，创建 `~/taskmgr-root` 和其中的 `config.json`，安装并
启动 systemd 用户服务，开启 lingering，并把 `gpu1` 加入 `~/.taskmgr/machines.json`。每一步都会
说明发现了什么；每一处改动都先询问 `[y/N]`（`--yes` 回答 yes）。如果 `loginctl enable-linger`
被拒绝，这一步会给出管理员需要执行一次的命令。如果 `$HOME` 在 NFS 上，加上
`--remote-root /scratch/tuner/taskmgr-root`（本地磁盘）。详见 [[docs/install.zh-CN]]。

以后要更新服务器（`git pull` 之后），再执行一次 `task add host gpu1`。它是幂等的：服务器已是最新
状态时，不做改动，也不询问。

CLI 和每台服务器的 `protocol_version` 必须相同，不同时命令以 `protocol_mismatch` 失败（退出码 2）。
协议 2 去掉了 attempt（一个作业只运行一次，重试是一个新作业）。协议 1 写下的作业目录
（`attempts/NNNN/`）不做迁移：`task add host` 在询问后把它们移到 `<remote_root>.backup-<时间>/`，
如果这样的作业可能仍在运行，则拒绝移动。

**3. 使用**

```sh
task hosts                           # 确认机器列表
task init AA-01                      # 生成模板；填写所有 <REQUIRED...> 标记
task validate AA-01/
task submit AA-01/                   # 输出 {"host", "jobid", ...}
task status <jobid> --wait
task fetch <jobid> --host gpu1 --output ./results/
```

## 文档

| 文档 | 内容 |
| --- | --- |
| [[docs/install.zh-CN]] | 本地 CLI、`task add host`、systemd 用户服务、`config.json` |
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
- GPU 被其他进程占用的任务以 75 退出；shim 稍等后重新启动它，`task jobs` 显示
  `pending (gpu busy)`（[[docs/task-package.zh-CN]] 的 GPU 约定）。
- 仅用于测试的覆盖项在 [[docs/testing.zh-CN]] 中列出并标注，该文档也包含可选的真实 SSH 冒烟
  测试。

## 代码结构

```
taskmgr/
  taskmgr.py      本地 CLI 入口（`task`）
  hostsetup.py    `task add host`：通过 ssh 逐步配置和更新服务器
  transport.py    机器列表；ssh / local 传输（`Remote`）
  remote.py       远程入口（`taskmgr-remote`）：子命令、`serve`、`shim`
  daemon.py       调度守护进程：扫描队列、GPU 预留、启动 shim、恢复循环
  shim.py         每次运行一个的监管进程：启动前检查、exec、wait、停止、收尾、
                  GPU 忙退出（75）后重新启动
  monitor.py      停止流程、重启恢复检查、守护进程接管跟踪
  store.py        远程存储根目录、状态锁、job/runner 记录、结果合并
  package.py      校验、确定性打包、内容哈希、安全解包
  gpu.py          查询 nvidia-smi、解析 GPU 声明
  procutil.py     /proc：boot_id、进程启动时间、进程组成员
  common.py       常量、原子 JSON 写入、flock 辅助函数
  template/       任务包模板（`task init`），含 taskmgr_task.py
  examples/       succeed/（EX-01，仅 CPU）和 multi-gpu/（MG-01，gpus [0, 1]）
  deploy/         taskmgr-remote.user.service（systemd 用户 unit）、machines.example.json、
                  config.example.json
  tests/          unittest 测试套件：python -m unittest discover -s taskmgr/tests
bin/task, bin/taskmgr-remote   从源码目录直接运行的包装脚本
docs/            文档（英文 *.md，中文 *.zh-CN.md）
```
