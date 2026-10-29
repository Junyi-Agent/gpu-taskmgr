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

task add host gpu1 --ssh tuner@10.0.0.12   # 配置服务器，并把它加入 machines.json
task hosts
```

`task add host`（见下文）会创建 `~/.taskmgr/machines.json` 和其中的条目。也可以参照
`taskmgr/deploy/machines.example.json` 手工编写这个文件。

本地状态保存在 `$TASKMGR_HOME`（默认 `~/.taskmgr`，也可用 `task --home DIR ...` 指定）：

| 文件 | 内容 |
| --- | --- |
| `machines.json` | `{"servers": [...]}`，机器列表（需求文档第 4 节） |
| `rr.json` | 轮询游标 `{"cursor": N, "last": name}` |
| `rr.lock` | flock 锁，保证多个 CLI 并发时 `rr.json` 的读取和更新串行进行 |

任务包就是普通的本地目录（或 `.tar.gz` 文件），放在哪里都可以。本地不保存 jobid 索引，也不
保存运行状态。

### 远程服务器：`task add host`（无需 root）

守护进程以 **systemd 用户服务** 的形式运行在拥有这些作业的账号下，不需要 root 权限。
`task add host` 通过 ssh 配置一台服务器，或更新已配置的服务器：

```sh
task add host NAME --ssh USER@HOST [--port N] [--identity-file PATH]
              [--remote-root DIR] [--ssh-option KEY=VALUE ...] [--disabled | --enabled] [--yes]
task add host gpu1 --ssh tuner@10.0.0.12          # 新服务器
task add host gpu1                                # 已有条目：检查并更新
```

命令分九步执行。每一步都会输出它检查了什么、发现了什么。需要改动时，先说明要做的改动，再询问
`proceed? [y/N]`；回答 `y` 以外的任何内容都会在这一步停止（退出码 2），后面的步骤不再执行。
只读检查从不询问。`--yes` 对所有询问回答 yes。没有 `--yes`、stdin 也不是终端时，命令在第一处
需要改动的地方停止（退出码 2），不做任何改动。

这个命令是幂等的：服务器已是最新状态时，每一步都报告 `ok`，不做改动，也不询问。`git pull`
（或修改代码）之后要更新服务器，再执行一次 `task add host NAME` 即可：只有代码这一步和服务重启
会执行。

| 步骤 | 检查 | 改动（每项都会询问） |
| --- | --- | --- |
| `ssh` | ssh 能连通（BatchMode）、Linux、`python3` >= 3.10、`systemctl --user` 可用、linger、已安装的代码版本、unit 文件、服务状态、`remote_root` 所在文件系统（NFS、CIFS、FUSE 等给出警告） | 无；检查失败则停止 |
| `code` | 比较 `~/taskmgr/.taskmgr-code-version` 和本地包 | 把 `taskmgr/`（不含 `tests/` 和 `__pycache__`）和 `bin/taskmgr-remote` 上传到 `~/taskmgr.new-*`；`data` 步骤之后用它替换 `~/taskmgr`，旧目录保留为 `~/taskmgr.old-<时间>`（更早的 `~/taskmgr.old-*` 被删除） |
| `remote_root` | `remote_root` 和 `config.json` 是否存在 | `mkdir -p`；`config.json` 不存在时写入默认配置；已有的 `config.json` 永远不会被覆盖 |
| `data` | `taskmgr-remote inspect`（只读）：新代码无法处理的作业目录（协议 1 的 `attempts/`、`job.json` 缺少当前字段） | 停止服务，然后把这些目录（以及对应的 `submission-keys/*.json`）移到 `<remote_root>.backup-<UTC 时间>/`；不删除任何东西。可能仍在运行的作业（running、stopping、unknown，或位于 `in-progress/`）永远不会被移动：这一步失败并列出它们 |
| `unit` | 比较 `~/.config/systemd/user/taskmgr-remote.service` 和生成的 unit（显示 diff） | 写入文件，执行 `systemctl --user daemon-reload` |
| `service` | 是否 enabled、是否 active，以及正在运行的守护进程是否早于已安装的代码或 unit | `enable`；`start`；`restart`（正在运行的任务不受影响，`KillMode=process`）；之后等待守护进程拿到 `locks/daemon.lock` |
| `linger` | `loginctl show-user` | `loginctl enable-linger <user>`；被拒绝时这一步失败，并给出管理员需要执行一次的命令，后续步骤继续执行 |
| `verify` | `taskmgr-remote --protocol-version N version` 有应答；服务的主 PID 持有 `locks/daemon.lock` | 无 |
| `machines` | `~/.taskmgr/machines.json` 中的条目 | 新增或更新该条目（显示改动）；其他条目保持原样 |

stdout 输出一个 JSON 对象：`{"host", "steps": [{"name", "status", "detail"}], "complete"}`，
`status` 取值为 `ok`、`changed`、`skipped`、`declined`、`failed`。所有步骤都是 `ok` 或
`changed` 时退出码为 0，否则为 2。

新条目使用绝对路径（`remote_root`，`remote_command = <home>/taskmgr/bin/taskmgr-remote`），
并带有以下 ssh 选项，与 `--ssh-option` 合并（同名选项以后者为准）：`ServerAliveInterval=15`、
`ConnectTimeout=10`、`ControlMaster=auto`、`ControlPath=~/.ssh/cm-%r@%h:%p`、
`ControlPersist=10m`。对已有条目，省略的参数取条目中保存的值；它的 ssh 选项保持不变，
`--ssh-option` 在其上追加。`--disabled` / `--enabled` 设置 `enabled`；不给时，新条目为启用，
已有条目保持原值。

`remote_root` 必须位于本地文件系统，不能放在 NFS 上，因为所有进程间协调都依赖 `flock`。默认值
是 `~/taskmgr-root`；如果 `$HOME` 在 NFS 上，请传入 `--remote-root /scratch/tuner/taskmgr-root`
（本地磁盘上的路径）。`~` 和相对路径在服务器上解析。

说明：

- 生成的 unit 用 ssh 会话找到的 `python3`（写成绝对路径）运行守护进程，所以 `ssh` 步骤检查的
  版本就是实际运行的版本。
- `remote_root` 中的数据永远不会被删除。不兼容的作业目录在你确认后移走；仍在运行的作业留在原处，
  直到结束。
- 不支持对同一台服务器同时执行两个 `add host`。
- 代码版本是所发送文件的内容哈希。版本文件一致时，在 `~/taskmgr` 中手工改动的文件不会被发现。

### `add host` 做了什么，以及手工方式

下面是用户 `tuner` 的手工配置方式。它安装随代码提供的 unit
（`taskmgr/deploy/taskmgr-remote.user.service`，使用 `%h` 路径）；之后执行 `task add host`
会把它替换为生成的 unit（并显示 diff）。

```sh
git archive HEAD | ssh gpu1 'mkdir -p ~/taskmgr && tar -x -C ~/taskmgr'   # 在本机执行
mkdir -p ~/taskmgr-root                                                   # 在服务器上执行
cp ~/taskmgr/taskmgr/deploy/config.example.json ~/taskmgr-root/config.json
```

机器列表中要写绝对路径（不能用 `~`）：
`"remote_root": "/home/tuner/taskmgr-root"`，
`"remote_command": "/home/tuner/taskmgr/bin/taskmgr-remote"`。如果 `remote_root` 不是
`~/taskmgr-root`，相应修改 unit 中的 `--root`。

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
| `gpu_busy_exit_code` | 75 | 表示"GPU 忙、没有做任何工作"的入口退出码：shim 重新启动入口命令，不结束作业；`null` 关闭此功能（75 此时就是普通的失败） |
| `gpu_retry_interval_seconds` | 2 | GPU 忙退出到重新启动之间的等待时间 |
| `base_env` | `{}` | 覆盖或扩展入口进程的基础环境变量 |
