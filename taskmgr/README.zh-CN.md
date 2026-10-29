# taskmgr

[[taskmgr/README|English]] | 简体中文

用于 GPU 性能调优的任务管理工具。本地 CLI（`task`）负责保存和提交任务包。每台远程 GPU
服务器运行 `taskmgr-remote`，由它维护自己的队列，把作业调度到 GPU 上运行，并在重启后恢复状态。
系统中没有中心调度器。运行要求：Python 3.10+ 标准库；远程端须为 Linux。

需求文档：[[agent/desc-1.md]]。设计决策：[[agent/desc-2.md]]。

## 快速开始

下面用 `gpu1` 代表 GPU 服务器的 ssh 地址（例如 `tuner@10.0.0.12`），`tuner` 代表服务器上的用户名。
整个过程不需要 root。

**1. 本机：安装 CLI**

```sh
uv tool install --editable /path/to/gpu-taskmgr     # 提供 task 命令
```

**2. 本机 → 服务器：把代码拷贝到服务器的 `~/taskmgr`**

在本机的仓库根目录执行。`git archive` 只打包已提交的文件（不含 `.venv`、`.git`），通过 ssh 解压到
服务器的 `~/taskmgr`。服务器上不需要 git 凭据。以后更新代码时重新执行这一条即可。

```sh
git archive HEAD | ssh gpu1 'mkdir -p ~/taskmgr && tar -x -C ~/taskmgr'
```

**3. 服务器：安装并启动 systemd 用户服务**

先 `ssh gpu1` 登录服务器，然后执行：

```sh
mkdir -p ~/taskmgr-root ~/.config/systemd/user
cp ~/taskmgr/taskmgr/deploy/config.example.json ~/taskmgr-root/config.json        # 远程配置
cp ~/taskmgr/taskmgr/deploy/taskmgr-remote.user.service \
   ~/.config/systemd/user/taskmgr-remote.service                                 # 用户级 unit
systemctl --user daemon-reload                     # 让 systemd 读到新 unit
systemctl --user enable --now taskmgr-remote       # 立即启动，并随用户管理器自启
loginctl enable-linger "$USER"                     # 开机启动、注销后不退出（被拒绝时需管理员执行一次）
systemctl --user status taskmgr-remote             # 确认 active (running)
```

unit 里写死了路径 `%h/taskmgr/bin/taskmgr-remote` 和 `--root %h/taskmgr-root`，所以第 2 步必须拷到
`~/taskmgr`。`$HOME` 在 NFS 上时要改用本地磁盘，见 [[docs/install.zh-CN]]。

**4. 本机：登记这台服务器**

```sh
mkdir -p ~/.taskmgr
cp /path/to/gpu-taskmgr/taskmgr/deploy/machines.example.json ~/.taskmgr/machines.json
```

编辑 `~/.taskmgr/machines.json`，每台服务器一项。路径要写绝对路径（不能用 `~`）：

```json
{"servers": [{"name": "gpu1", "enabled": true, "transport": "ssh",
              "ssh": {"host": "10.0.0.12", "user": "tuner"},
              "remote_root": "/home/tuner/taskmgr-root",
              "remote_command": "/home/tuner/taskmgr/bin/taskmgr-remote"}]}
```

**5. 使用**

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
  deploy/         taskmgr-remote.user.service（systemd 用户 unit）、machines.example.json、
                  config.example.json
  tests/          unittest 测试套件：python -m unittest discover -s taskmgr/tests
bin/task, bin/taskmgr-remote   从源码目录直接运行的包装脚本
docs/            文档（英文 *.md，中文 *.zh-CN.md）
```
