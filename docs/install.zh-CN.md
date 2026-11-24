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

task create host gpu1 --ssh tuner@10.0.0.12      # 配置服务器，并把它加入 machines.json
task get hosts
task ping host                                   # 确认服务器有应答
```

`task create host`（见下文）会创建 `~/.taskmgr/machines.json` 和其中的条目。也可以参照
`taskmgr/deploy/machines.example.json` 手工编写这个文件。

本地状态保存在 `$TASKMGR_HOME`（默认 `~/.taskmgr`，也可用 `task --home DIR ...` 指定）：

| 文件 | 内容 |
| --- | --- |
| `machines.json` | `{"servers": [...]}`，机器列表（需求文档第 4 节） |
| `rr.json` | 轮询游标 `{"cursor": N, "last": name}` |
| `rr.lock` | flock 锁，保证多个 CLI 并发时 `rr.json` 的读取和更新串行进行 |

任务包就是普通的本地目录（或 `.tar.gz` 文件），放在哪里都可以。本地不保存 jobid 索引，也不
保存运行状态。

### 单文件 zipapp（`.pyz`）

`task build pyz` 生成两个文件，只要有 `python3` >= 3.10 就能运行，不需要别的东西。从 checkout
运行时写到 checkout 里的 `dist/`；从 `.pyz` 运行时必须给 `--dest DIR`。已有的文件会被替换。

```sh
bin/task build pyz                                # 生成 dist/task.pyz 和 dist/taskmgr-remote.pyz
ln -s "$PWD/dist/task.pyz" ~/.local/bin/task      # 本地 CLI 只有一个文件
task get version                                  # commit 和 code_version 从压缩包里读取
```

每个压缩包里是整个包（`taskmgr/`，不含 `tests/` 和 `__pycache__`），顶层另有 `pyproject.toml`、`.taskmgr-commit` 和 `.taskmgr-code-version`。
code version 是 `taskmgr/` 中影响运行的文件的哈希：所有 `.py` 文件（`deploy/`、`examples/` 和
`tests/` 中的除外）以及 `template/` 下的全部文件。文档、README 和 deploy 下的示例不影响它，修改它们
不会让服务器重启。checkout 和由它构建的每个压缩包得到的值相同，所以
从 `task.pyz` 运行的 `task create host` 安装的代码和从 checkout 安装的一样，`task get version` 显示服务器为 `up_to_date`。`taskmgr-remote.pyz` 可以运行 daemon 和 shim
（daemon 把压缩包放进 shim 的 `PYTHONPATH`）；`taskmgr-remote.pyz version` 的 `code_dir`
是压缩包路径。在 `.pyz` 里运行 `task update cli` 会报 `not_a_checkout`，需要在 checkout 里用 `task build pyz` 重新构建。
`task create host` 不构建任何东西，只上传由这个命令构建好的 `taskmgr-remote.pyz`（见下文）。从
checkout 运行时默认取 checkout 里的 `dist/taskmgr-remote.pyz`；从 `task.pyz` 运行时取与 `task.pyz` 同一目录下的
`taskmgr-remote.pyz`（先解析符号链接，所以上面 `ln -s` 的安装方式会找到 `dist/`）；`--pyz FILE`
指定别的文件。压缩包的 code version 必须和当前运行的 `task` 相同，否则 `code` 步骤失败，提示用
`task build pyz` 重新构建。

### 远程服务器：`task create host`、`task update host`（无需 root）

守护进程以 **systemd 用户服务** 的形式运行在拥有这些作业的账号下，不需要 root 权限。
`task create host` 通过 ssh 配置一台服务器，或更新已配置的服务器：

```sh
task create host NAME --ssh USER@HOST [--port N] [--identity-file PATH]
                      [--ssh-option KEY=VALUE ...] [--disabled | --enabled] [--pyz FILE]
                      [--yes] [--plan] [--prune-old-code]
task create host gpu1 --ssh tuner@10.0.0.12       # 新服务器
task create host gpu1                             # 已有条目：检查并更新
task create host gpu1 --plan                      # 只输出会改什么，不做任何改动
task create host gpu1 --yes                       # 不询问直接更新；旧代码保留
task create host gpu1 --yes --prune-old-code      # ……并删除旧版本的代码
```

命令分十步执行。每一步都会输出它检查了什么、发现了什么。需要改动时，先说明要做的改动，再询问
`proceed? [y/N]`；回答 `y` 以外的任何内容都会在这一步停止（退出码 2），后面的步骤不再执行。
只读检查从不询问。`--yes` 对所有询问回答 yes，删除除外（见下）。没有 `--yes`、stdin 也不是终端时，
命令在第一处需要改动的地方停止（退出码 2），不做任何改动。

**`--yes` 不包括删除。** `old_code` 步骤会删除 `remote_root` 之外的目录和文件（旧版本的代码）。
`--yes` 不回答这一项：只有加 `--prune-old-code`，或在终端里输入 `y`（即使带 `--yes` 也会询问）时才执行。
两者都没有时，这一步报告 `kept`，什么也不删，并给出该参数；这次运行仍算完成（退出码 0）。其他改动都只是
新增或替换 `task create host` 安装的东西（`versions/` 里的压缩包、unit、缺失时的 `config.json`、机器条目），
或把作业目录移入备份目录而不删除。

**`--plan`**（也可写 `--dry-run`）执行所有只读检查，把这次运行会做的每处改动报告为状态 `planned`，
detail 是改动内容；不询问，也不做任何改动（不上传、不调用 systemctl、不写机器列表，连 setup 锁也不拿）。
依赖某个计划中改动的步骤报告 `skipped`（`data` 需要新代码就位，`verify` 需要配置完成）。退出码 0：
没有要改的；1：有计划中的改动；2：某项检查失败。JSON 带 `"plan": true` 和 `"planned": [<步骤名>]`。

这个命令是幂等的：服务器已是最新状态时，每一步都报告 `ok`，不做改动，也不询问。`git pull`
（或修改代码）之后要更新服务器，先用 `task build pyz` 重新构建，再执行一次
`task update host NAME`（或再执行一次 `task create host NAME`）：只有代码这一步和服务重启会执行。

`task update host NAME [--pyz FILE] [--yes] [--plan] [--prune-old-code]` 对机器列表里已有的
服务器执行同样的步骤，并保持它的条目不变：它不接受 ssh 选项（要改用 `task create host`），名字不在
列表里时报 `unknown_host`，已禁用的服务器也能更新（更新后仍是禁用）。`task get version` 或
`task ping host` 显示某台服务器的代码版本不同，或命令报 `unsupported_feature` 或 `protocol_mismatch` 时用它。

协议 3 之前安装的服务器（`taskmgr-remote` 用 `jobs`、`serve` 这类单词子命令）在更新前对这个 CLI 的
每条命令都应答 `protocol_mismatch`：每台服务器执行一次 `task update host NAME`。更新也会重写 unit，
它的 `ExecStart` 现在以 `run daemon` 结尾；重写之前，新代码仍接受旧 unit 的 `serve`。

| 步骤 | 检查 | 改动（每项都会询问） |
| --- | --- | --- |
| `ssh` | ssh 能连通（BatchMode）、Linux、`python3` >= 3.10、`systemctl --user` 可用、linger、已安装的代码版本、unit 文件、服务状态、`remote_root` 所在文件系统（NFS、CIFS、FUSE 等给出警告） | 无；检查失败则停止 |
| `code` | 构建好的 `taskmgr-remote.pyz`（`--pyz`，默认位置见上文）存在，且 code version 与当前运行的 `task` 相同（否则这一步失败：用 `task build pyz` 重新构建）；再比较 `<remote_root>/taskmgr-remote.pyz` 内的代码版本和它 | 上传这个文件到 `<remote_root>/versions/<版本>.pyz.new-*`；`data` 步骤之后改名为 `<remote_root>/versions/<版本>.pyz`，并把符号链接 `<remote_root>/taskmgr-remote.pyz` 切换过去（一次 rename）。链接原来指向的压缩包保留。超过 10 分钟的上传文件 `*.new-*`（中断的运行留下的）作为这项改动的一部分，在确认后删除；服务器已是最新时只在 detail 中列出 |
| `remote_root` | `remote_root` 和 `config.json` 是否存在 | `mkdir -p`，权限 0700（用户自己拥有的已有 `remote_root` 去掉组和其他用户的权限）；`config.json` 不存在时写入默认配置；已有的 `config.json` 永远不会被覆盖 |
| `data` | `taskmgr-remote describe store`（只读）：新代码无法处理的作业目录（协议 1 的 `attempts/`、`job.json` 缺少当前字段） | 停止服务，然后把这些目录（以及对应的 `submission-keys/*.json`）移到 `<remote_root>/backups/<UTC 时间>/`；不删除任何东西。可能仍在运行的作业（running、stopping、unknown，或位于 `in-progress/`）永远不会被移动：这一步失败并列出它们 |
| `unit` | 比较 `~/.config/systemd/user/taskmgr-remote.service` 和生成的 unit（显示 diff） | 写入文件，执行 `systemctl --user daemon-reload` |
| `service` | 是否 enabled、是否 active，以及正在运行的守护进程是否早于已安装的代码或 unit | `enable`；`start`；`restart`（正在运行的任务不受影响，`KillMode=process`）；之后等待守护进程拿到 `locks/daemon.lock`。代码有变化时，等守护进程运行新代码后，删除 `versions/` 中除当前版本、上一个版本和仍被运行中进程使用的压缩包以外的文件（运行中作业的 shim 从它启动时的压缩包导入） |
| `linger` | `loginctl show-user` | `loginctl enable-linger <user>`；被拒绝时这一步失败，并给出管理员需要执行一次的命令，后续步骤继续执行 |
| `verify` | `taskmgr-remote --protocol-version N version` 有应答；服务的主 PID 持有 `locks/daemon.lock` | 无 |
| `machines` | `~/.taskmgr/machines.json` 中的条目 | 新增或更新该条目（显示改动）；其他条目保持原样 |
| `old_code` | 旧版本装在 `remote_root` 之外的代码：代码目录 `~/taskmgr`（仅当其中有标记文件 `.taskmgr-code-version` 且没有 `.git`）和没有 `.git` 的 `~/taskmgr.old-*` 目录；第一版 zipapp 布局的 `~/taskmgr-remote.pyz`（符号链接）和 `~/.taskmgr-remote/<版本>.pyz` | 删除它们，只在加 `--prune-old-code` 或在终端里输入 `y` 时执行（`--yes` 不回答这一项）；否则报告 `kept`。其他电脑上的机器列表如果还写着这些路径，会无法使用，需要在那里执行一次 `task create host NAME`。回答 no 时这一步报告 declined，下次运行会再问 |

进度和询问输出到 stderr。stdout 输出各步骤的表格（STEP、STATUS、DETAIL）和一行 `done:` 或
`incomplete:`；加 `-o json` 时改为输出 JSON 对象
`{"host", "steps": [{"name", "status", "detail"}], "complete"}`。`status` 取值为 `ok`、
`changed`、`kept`（未获允许的删除，见上文）、`planned`（`--plan`）、`skipped`、`declined`、`failed`。
所有步骤都是 `ok`、`changed` 或 `kept` 时退出码为 0（`--plan` 见上文），否则为 2。

新条目使用绝对路径（`remote_root`，`remote_command = <remote_root>/taskmgr-remote.pyz`），
并带有以下 ssh 选项，与 `--ssh-option` 合并（同名选项以后者为准）：`ServerAliveInterval=15`、
`ConnectTimeout=10`、`ControlMaster=auto`、`ControlPath=~/.ssh/cm-%r@%h:%p`、
`ControlPersist=10m`。对已有条目，省略的参数取条目中保存的值；它的 ssh 选项保持不变，
`--ssh-option` 在其上追加。`--disabled` / `--enabled` 设置 `enabled`；不给时，新条目为启用，
已有条目保持原值。

`taskmgr-remote` 在服务器上的所有东西都在一个目录 `remote_root` 里（默认 `~/.taskmgr-remote`），
只有 systemd unit 在它外面：

```
~/.taskmgr-remote/
  config.json
  taskmgr-remote.pyz -> versions/<版本>.pyz       # 服务运行的代码
  versions/<版本>.pyz                             # 当前版本、上一个版本和仍在使用的版本
  locks/ incoming/ queue/ in-progress/ done/ submission-keys/ daemon/
  backups/<UTC 时间>/                             # data 步骤移走的作业目录
```

要换一个目录，运行 `task create host` 时设置环境变量 `TASKMGR_REMOTE_ROOT`（例如
`TASKMGR_REMOTE_ROOT=/scratch/tuner/taskmgr-remote task create host gpu1`）；`~` 和相对路径在服务器上
解析。不设置时，已有条目沿用它的 `remote_root`。`remote_root` 必须位于本地文件系统，不能放在 NFS
上，因为所有进程间协调都依赖 `flock`。其他 `remote_root` 中的数据永远不会被移动。`ssh` 步骤如果
在本次运行将不再使用的目录（已有条目的 `remote_root`、已安装 unit 的 `--root`，或者对新条目而言
旧版本的默认值 `~/taskmgr-root`）中发现 `config.json`，服务将看不到那里的作业：不加 `--yes` 时
给出警告并询问；加 `--yes` 时运行失败，不做任何修改。要继续使用那个目录，把 `TASKMGR_REMOTE_ROOT`
设为它再运行一次（例如 `TASKMGR_REMOTE_ROOT=~/taskmgr-root`）。

说明：

- 生成的 unit 用 ssh 会话找到的 `python3`（写成绝对路径）运行守护进程，所以 `ssh` 步骤检查的
  版本就是实际运行的版本。
- `remote_root` 中的数据永远不会被删除。不兼容的作业目录在你确认后移走；仍在运行的作业留在原处，
  直到结束。
- 只有服务器账户能读 `remote_root`：作业包、`task.json` 的 `env`、日志和结果对服务器上的其他用户
  不可见。`taskmgr-remote` 创建的目录权限是 0700，文件是 0600（日志、上传文件、各种记录）；作业的
  `package/` 只保留属主的读和执行权限，复制出的 `workdir/` 是 0700 / 0600（可执行文件 0700）。除
  `describe store` 和 `version` 之外，每条 `taskmgr-remote` 命令还会去掉用户自己拥有的已有 `remote_root`
  的组和其他用户权限，所以旧版本创建的目录也受保护。任务自己写的文件使用守护进程的 umask，位于这个
  目录树之内。
- 对同一服务器用户的两次 `create host` 不会重叠：从 `ssh` 步骤结束到整次运行结束，服务器上有一个
  进程持有锁文件（`$XDG_RUNTIME_DIR/taskmgr-hostsetup-<uid>.lock`，否则在 `/tmp` 下），锁被占用时
  第二次运行在 `ssh` 步骤失败。
- 服务器上的代码是 `<remote_root>/taskmgr-remote.pyz`，它是指向 `versions/<版本>.pyz` 的符号链接；
  代码版本读自压缩包内的 `.taskmgr-code-version`。已安装的压缩包从不修改。Python 的 zipimport
  按路径读取压缩包，每导入一个模块读一次，并沿用第一次读到的偏移量，所以在进程启动期间（例如
  守护进程刚拉起的 shim）替换文件，会让它的导入失败。因此更新时总是新增一个文件再切换链接，
  每个进程都从解析后的路径导入（`sys.path[0]` 换成 realpath；守护进程给 shim 的 `PYTHONPATH`
  也是解析后的路径）。手工回滚：`ln -sfn versions/<旧版本>.pyz ~/.taskmgr-remote/taskmgr-remote.pyz`，
  再执行 `systemctl --user restart taskmgr-remote`。

### 机器列表：`task get hosts`、`task enable|disable|ping|delete host`

```sh
task get hosts                # NAME、ENABLED、DESTINATION（user@host[:port]）、REMOTE_ROOT
task ping host                # 所有服务器；`task ping host gpu1` 只检查一台
task disable host gpu1        # gpu1 宕机：暂时不向它提交，也不查询它
task enable host gpu1         # 恢复
task delete host gpu1         # 询问 [y/N]；--yes 跳过询问
```

`task disable host NAME` / `task enable host NAME` 设置条目中的 `enabled`（与 `task delete host` 一样在
`machines.lock` 下原子地重写文件）。禁用的服务器不参与轮询提交，也不参与向所有服务器发出的查询（`get jobs`、按 jobid
查找、`get user`、`get version`）；`--host NAME` 仍然可以访问它。见 [[docs/usage.zh-CN]]。

`task ping host` 在各台已启用的服务器上并行执行 `taskmgr-remote version`，每台服务器输出一行：`ok` 或
`unreachable`、往返时间、`protocol_version`、已安装的代码版本（服务器运行的不是
`task create host` 安装的代码时显示 `none`），以及远端运行时的用户名和主机名。无法连接的服务器的
错误信息列在表格之后。所有被检查的服务器都有应答时退出码为 0，否则为 2。已禁用的服务器被跳过，名字列在表格之后；`task ping host NAME` 即使该服务器已禁用也会检查它。

`task delete host NAME` 把条目从 `machines.json` 中删除（在 `machines.lock` 下原子地重写文件；
其他条目保持不变）。它不改动服务器：代码、服务、`remote_root` 及其中的作业都保留，
`task create host NAME --ssh ...` 可以重新加入这台服务器。不在终端中运行时需要 `--yes`；名字不存在时
以 `unknown_host` 失败（退出码 2）。如果还要停止服务器上的服务，见下面的手工步骤。

### `task create host` 做了什么，以及手工方式

下面是用户 `tuner` 的手工配置方式。它安装随代码提供的 unit
（`taskmgr/deploy/taskmgr-remote.user.service`，使用 `%h` 路径）；之后执行 `task create host`
会把它替换为生成的 unit（并显示 diff）。

```sh
bin/task build pyz                                    # 在本机执行
ssh gpu1 mkdir -p .taskmgr-remote/versions
scp dist/taskmgr-remote.pyz gpu1:.taskmgr-remote/versions/<版本>.pyz
scp taskmgr/deploy/config.example.json gpu1:.taskmgr-remote/config.json
scp taskmgr/deploy/taskmgr-remote.user.service gpu1:
ln -s versions/<版本>.pyz ~/.taskmgr-remote/taskmgr-remote.pyz   # 在服务器上执行
```

`<版本>` 可以是任意名字；`task create host` 用 code version 的前 16 个字符。
随附的 unit 启动的是 `/usr/bin/python3`：把 `ExecStart` 中的它换成在 ssh 会话里执行
`command -v python3` 的输出（Python >= 3.10），与生成的 unit 一样。
机器列表中要写绝对路径（不能用 `~`）：
`"remote_root": "/home/tuner/.taskmgr-remote"`，
`"remote_command": "/home/tuner/.taskmgr-remote/taskmgr-remote.pyz"`。如果 `remote_root` 不是
`~/.taskmgr-remote`，相应修改 unit 中 `ExecStart` 的两处路径。

#### 启动 / 停止 / 开机自启（systemd 用户服务）

```sh
mkdir -p ~/.config/systemd/user
mv ~/taskmgr-remote.user.service ~/.config/systemd/user/taskmgr-remote.service
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
systemd 之外（tmux、nohup）也能运行：`nohup ~/.taskmgr-remote/taskmgr-remote.pyz
--root ~/.taskmgr-remote run daemon > /dev/null 2>&1 &`；停止时执行 `kill <pid>`（pid 记录在 `locks/daemon.lock` 中）。

在同一个 root 上再启动一个 `run daemon` 会以退出码 2 结束（它拿不到 `locks/daemon.lock`）。

## 远程 `config.json`

位于 `<remote_root>/config.json`，所有键都是可选的，未知的键会被忽略。值会做类型检查：类型不对的
值（例如 `stop_grace_seconds` 写成 `"30"`，或数字写成 `true`）会让守护进程和每个读取配置的
`taskmgr-remote` 命令失败，错误码 `bad_config`。

| 键 | 默认值 | 含义 |
| --- | --- | --- |
| `max_package_bytes` | 1073741824 | 接收的任务包解压后的最大总大小；上传数据超过它加 1/16 再加 1 MiB 时，在接收完之前就被拒绝 |
| `max_cpu_jobs` | `null`（不限制） | `gpus: []` 的作业和预编译的最大并发数（统计 running/stopping/unknown） |
| `max_auto_restarts` | 2 | 因重启或入口命令从未启动而中断后，最多自动重跑的次数 |
| `stop_grace_seconds` | 30 | 停止流程中从 SIGTERM 到 SIGKILL 的等待时间（用于超时和残留进程清理；取消时直接发送 SIGKILL） |
| `kill_confirm_seconds` | 10 | SIGKILL 后等待多久仍未确认停止就判定为 `unknown` |
| `control_poll_seconds` | 2 | shim / 守护进程重新读取 `job.json` 检查取消请求的间隔；也是健康检查周期 |
| `scheduler_poll_seconds` | 1 | 调度轮的间隔（假设：需求文档未规定） |
| `gpu_busy_exit_code` | 75 | 表示"GPU 忙、没有做任何工作"的入口退出码：shim 重新启动入口命令，不结束作业；`null` 关闭此功能（75 此时就是普通的失败）；其他取值必须是 1 到 255 的整数，否则服务器报 `bad_config` |
| `gpu_retry_interval_seconds` | 1 | GPU 忙退出到重新启动之间的等待时间 |
| `base_env` | `{}` | 覆盖或扩展入口进程的基础环境变量 |
