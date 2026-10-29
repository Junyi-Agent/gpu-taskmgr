---
title: 安装与部署
tags: [taskmgr, install, deploy, systemd]
lang: zh-CN
up: "[[taskmgr/README.zh-CN]]"
---

# 安装与部署

其他语言：[[docs/install]]

## 安装

### 本地 CLI

```sh
uv tool install --editable /path/to/gpu-taskmgr   # 安装 `task` 和 `taskmgr-remote`
# 不用 pip 的话：
ln -s /path/to/gpu-taskmgr/bin/task ~/.local/bin/task

mkdir -p ~/.taskmgr
cp taskmgr/deploy/machines.example.json ~/.taskmgr/machines.json   # 然后按需修改
task hosts
```

本地状态保存在 `$TASKMGR_HOME`（默认 `~/.taskmgr`，也可用 `task --home DIR ...` 指定）：

| 文件 | 内容 |
| --- | --- |
| `machines.json` | `{"servers": [...]}`，机器列表（需求文档第 4 节） |
| `rr.json` | 轮询游标 `{"cursor": N, "last": name}` |
| `rr.lock` | flock 锁，保证多个 CLI 并发时 `rr.json` 的读取和更新串行进行 |

任务包就是普通的本地目录（或 `.tar.gz` 文件），放在哪里都可以。本地不保存 jobid 索引，也不
保存运行状态。

### 远程服务器（无需 root）

守护进程以 **systemd 用户服务** 的形式运行在拥有这些作业的账号下，不需要 root 权限。以用户
`tuner` 为例：

```sh
git clone <repo> ~/taskmgr                 # 提供 ~/taskmgr/bin/taskmgr-remote
mkdir -p ~/taskmgr-root
cp ~/taskmgr/taskmgr/deploy/config.example.json ~/taskmgr-root/config.json
```

`remote_root` 必须位于本地文件系统，不能放在 NFS 上，因为所有进程间协调都依赖 `flock`。如果
`$HOME` 在 NFS 上，请改用本地磁盘（例如 `/scratch/tuner/taskmgr-root`），并相应修改 unit 中的
`--root`。机器列表中要写绝对路径（不能用 `~`）：
`"remote_root": "/home/tuner/taskmgr-root"`，
`"remote_command": "/home/tuner/taskmgr/bin/taskmgr-remote"`。

#### 启动 / 停止 / 开机自启（systemd 用户服务）

```sh
mkdir -p ~/.config/systemd/user
cp ~/taskmgr/taskmgr/deploy/taskmgr-remote.user.service \
   ~/.config/systemd/user/taskmgr-remote.service
systemctl --user daemon-reload
systemctl --user enable --now taskmgr-remote     # 立即启动，并随用户管理器启动
loginctl enable-linger "$USER"                   # 开机启动，注销后继续运行
loginctl show-user "$USER" -p Linger             # 必须输出 Linger=yes
systemctl --user stop taskmgr-remote             # 正在运行的任务不受影响
systemctl --user restart taskmgr-remote          # 启动时执行恢复
journalctl --user -u taskmgr-remote              # 守护进程日志（另有 <root>/daemon/daemon.log）
```

**必须开启 lingering。** 不开启时，用户管理器只在你登录时启动，并在最后一个会话注销时停止。
这样守护进程不会开机启动，而且用户管理器停止时会停掉该 unit 的整个 cgroup，其中包括 shim 和
任务。普通用户能否为自己开启 lingering 取决于主机的 polkit 策略；如果 `loginctl enable-linger`
被拒绝，需要管理员执行一次 `loginctl enable-linger tuner`。这是唯一可能需要 root 的步骤。另外
请确认 logind 不会在注销时杀掉用户进程（`KillUserProcesses=no`，大多数发行版的默认值）。

unit 中设置了 **`KillMode=process`**。shim 经过两次 fork 进入各自的会话，但仍然在该 unit 的
cgroup 里，所以如果用默认的 `KillMode=control-group`，执行 `systemctl --user stop` 时会杀掉所有
shim 和任务。设置 `KillMode=process` 后，systemd 只向守护进程的主 PID 发信号；守护进程收到
SIGTERM 后直接退出，不碰 shim，下次启动时执行重启恢复。我们没有选择为每个 shim 使用
`systemd-run --user --scope`，原因是守护进程不需要访问 D-Bus，而且它保持为普通进程，在
systemd 之外（tmux、nohup）也能运行：`nohup ~/taskmgr/bin/taskmgr-remote --root ~/taskmgr-root
serve > /dev/null 2>&1 &`；停止时执行 `kill <pid>`（pid 记录在 `locks/daemon.lock` 中）。

在同一个 root 上再启动一个 `serve` 会以退出码 2 结束（它拿不到 `locks/daemon.lock`）。

如果主机上有 root 权限，可以使用等价的系统级 unit `deploy/taskmgr-remote.service`（放到
`/etc/systemd/system/`，`User=tuner`，执行 `sudo systemctl enable --now taskmgr-remote`）。

## 远程 `config.json`

位于 `<remote_root>/config.json`，所有键都是可选的。

| 键 | 默认值 | 含义 |
| --- | --- | --- |
| `max_package_bytes` | 1073741824 | 接收的任务包解压后的最大总大小 |
| `max_cpu_jobs` | `null`（不限制） | `gpus: []` 的作业最大并发数（统计 running/stopping/unknown） |
| `max_auto_restarts` | 2 | 因重启或入口命令从未启动而中断后，最多自动重跑的次数 |
| `stop_grace_seconds` | 30 | 停止流程中从 SIGTERM 到 SIGKILL 的等待时间 |
| `kill_confirm_seconds` | 10 | SIGKILL 后等待多久仍未确认停止就判定为 `unknown` |
| `control_poll_seconds` | 2 | shim / 守护进程重新读取 `job.json` 检查取消请求的间隔；也是健康检查周期 |
| `scheduler_poll_seconds` | 1 | 调度轮的间隔（假设：需求文档未规定） |
| `base_env` | `{}` | 覆盖或扩展入口进程的基础环境变量 |
