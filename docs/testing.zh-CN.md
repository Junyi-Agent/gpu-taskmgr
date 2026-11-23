---
title: 测试
tags: [taskmgr, testing]
lang: zh-CN
up: "[[taskmgr/README.zh-CN]]"
---

# 测试

其他语言：[[docs/testing]]

```sh
python -m unittest discover -s taskmgr/tests      # Linux，无需 GPU，无需网络
# 在 macOS 上：
docker run --rm --init -v "$PWD":/src:ro python:3.10-slim \
    sh -c 'cp -a /src /w && cd /w && python -m unittest discover -s taskmgr/tests'
# 只跑一个模块：... python -m unittest taskmgr.tests.test_query
```

测试需要 Linux（`/proc`、进程组、`setsid`）。在其他平台上，所有基于 `helpers.Env` 的测试类都会
被跳过，跳过信息指向本页；只有少量纯单元测试会在本机运行。在 macOS 上请用上面的 Docker 命令。

代码目录以只读方式挂载，再复制进容器（`cp -a /src /w`）：测试不会往工作目录写任何东西（没有
`__pycache__`，没有 `dist/`），从不同 worktree 同时运行也不会共用文件。`python:3.10-slim` 对应
项目支持的最低 Python 版本（`.python-version`）。

在容器中运行时要加 `--init`（或其他会回收孤儿进程的 init）：测试会杀掉 shim，产生的孤儿任务
进程需要被回收。

清理：每个测试用 `addCleanup` 注册清理（`setUp` 中途失败也会执行）；运行被中断时，`atexit` 钩子
以及 SIGTERM / SIGHUP 处理函数会清理剩下的部分。清理会停掉该测试的 daemon，杀掉所有在该测试临时
目录中运行或命令行包含该目录的进程，给只读的任务包副本恢复 `u+w`，然后删除目录。被 `SIGKILL`
的测试进程无法清理；在 Docker 中容器会随之一起消失。

测试环境会去掉继承来的所有 `TASKMGR_*` 变量和 `NO_COLOR`，再设置测试需要的值，因此 shell 中的
设置不会影响测试结果。

要断言某个任务在调度器处理过之后"仍在排队"，用 `Env.wait_scheduler_passes(n)` 代替固定的
sleep：它提交空操作的探测任务并等每个都成功；调度出探测任务的那一轮已经考虑过排在它前面的所有任务。

要在任务处于某个状态时读取它，让任务脚本等待一个 gate 文件（`while [ ! -f GATE ]; do sleep 0.05; done`），
断言完成后再创建这个文件；脚本里用固定的 sleep，测试在慢机器上会失败。要在命令执行到某一步之后再改变环境
（例如第一次查询失败后让服务器恢复），用 `Env.task_when(marker, action, *args)`：它运行 `task`，
在 stderr 出现 `marker` 时调用 `action`。

故障注入在 root 下也必须成立（上面的 Docker 命令以 root 运行）：root 不受权限位限制，`chmod 0500`
挡不住写入。要让写入某个目录失败，把这个目录换成一个普通文件（写入报 ENOTDIR 或 EEXIST）。

**仅用于测试的覆盖项**（不要在生产环境使用）：

| 变量 / 配置 | 作用 |
| --- | --- |
| `TASKMGR_NVIDIA_SMI` | 伪造的 `nvidia-smi` 路径，输出 `index, uuid` 行 |
| `TASKMGR_BOOT_ID_FILE` | 从该文件读取 boot_id（模拟机器重启） |
| 机器条目 `"local": {"fail": "msg"}` | `transport: local` 抛出连接错误 |
| 机器条目 `"local": {"fail_if_exists": "/path"}` | 该文件存在期间报连接错误 |
| `TASKMGR_TEST_PROTOCOL_VERSION` | 远程端报告这个协议版本 |
| `TASKMGR_TEST_FEATURES` | 远程端报告这些功能（逗号分隔；`-` 表示没有） |
| `TASKMGR_TEST_SKIP_DAEMON_LOCK=1` | 守护进程跳过 `daemon.lock`（多守护进程测试） |
| `TASKMGR_SHIM_EXECUTABLE` | 用于 exec shim 的解释器（模拟 shim 启动失败） |
| `TASKMGR_TEST_SHIM_CRASH=<point>` | shim 在 `before_identity`、`before_exec`、`after_exit_recorded`、`before_finalize` 处退出（`test_recovery.py` 对每个点检查之后的恢复） |
| `TASKMGR_TEST_IGNORE_KILL=1` | 停止流程不发送任何信号（模拟停止无法确认） |
| `TASKMGR_TEST_RECEIVE_PAUSE=<s>` | `receive` 在持有上传锁时休眠 |
| 机器条目 `"local": {"home": "/dir"}` | `transport: local` 以 `HOME=/dir` 运行所有命令（包括 `task create host` 的脚本） |
| `TASKMGR_TEST_LOCAL_HOME=/dir` | `task create host` 新建的条目使用 `transport: local` 和 `"local": {"home": "/dir"}` |
| `TASKMGR_TEST_INTERACTIVE=1` | stdin 不是终端时，`task create host` 也从 stdin 读取回答 |

systemd 相关行为的测试方式：检查用户 unit 中有 `KillMode=process`，并向守护进程的整个进程组
发送 SIGTERM 来停止它，验证 shim 和任务继续运行，且记录下真实退出码。

`task create host` 的测试不需要 ssh，也不需要 systemd（`taskmgr/tests/test_add_host.py`）。“服务器”
是一个 local 传输的条目，它的 HOME 是测试目录（即上表中仅用于测试的 `"local": {"home"}`）。测试把
伪造的 `systemctl` 和 `loginctl` 脚本写到一个目录里，并把该目录放在 PATH 最前面。它们的状态保存在
`$HOME/.fake-systemd/`，每次调用也记录在那里：`start` 启动 unit 的 `ExecStart`（真实的守护进程），
`stop` 只向主 PID 发送 SIGTERM（与 `KillMode=process` 相同），unit 文件改动后必须先
`daemon-reload`，`linger-refuse` 文件存在时 `enable-linger` 失败。这些伪造脚本只存在于测试中，
`taskmgr/` 中的代码不引用它们。同一个文件还用普通的 local 传输服务器测试 `task get hosts`、
`task delete host` 和 `task ping host`；无法连接的服务器是带有 `"local": {"fail": "message"}` 的机器条目。

这些测试像在真实服务器上一样安装 `taskmgr-remote.pyz`（`$HOME/.taskmgr-remote/taskmgr-remote.pyz`，
指向 `versions/` 的符号链接），伪造的 `start` 运行它。“已安装旧版本”的模拟方式：安装一份 `.taskmgr-code-version` 不同的
压缩包副本并切换链接，与 `create host` 的做法相同。测试类只构建一次 `taskmgr-remote.pyz`，每次运行都用
`--pyz` 传给它；压缩包不存在、code version 不同、把 `task.pyz` 当作 `--pyz` 传入，这三种情况下
`code` 步骤失败，服务器和机器列表都不改动。另有测试覆盖 `TASKMGR_REMOTE_ROOT`、清理第一版 zipapp
布局（`~/taskmgr-remote.pyz`、`~/.taskmgr-remote/<版本>.pyz`），以及对 `~/taskmgr-root` 下旧数据的警告。代码更新测试让一个任务在两次更新中持续运行；
如果 shim 从启动期间被替换的压缩包导入，这个测试会失败。

`taskmgr/tests/test_pyz.py` 用 `task build pyz` 构建 `task.pyz` 和 `taskmgr-remote.pyz`，
在没有 `PYTHONPATH`、不在 checkout 目录的情况下运行它们，检查：压缩包内容、退出码、代码版本（与
checkout 相同）、`task create package`（与 checkout 生成的包相同）、经符号链接运行、从 `task.pyz` 运行时 `task create host` 去哪里找 `taskmgr-remote.pyz`（`task.pyz` 同一
目录，先解析符号链接），以及用 `task.pyz` 提交、
由 `taskmgr-remote.pyz` 的守护进程和 shim 运行到 `succeeded` 的作业。

## 可选：使用真实 SSH 的冒烟测试

```sh
task create host gpu1 --ssh tuner@<server>                    # 每一步都是 ok 或 changed
task create host gpu1                                         # 第二次：每一步都是 ok，不询问
task ping host gpu1                                           # ok，代码版本与本地一致
ssh gpu1 ls -l .taskmgr-remote/                               # taskmgr-remote.pyz -> versions/<版本>.pyz
task create job taskmgr/examples/succeed --host gpu1
task wait job <jobid> --host gpu1 --poll-interval 2           # 退出码 0，outputs 存在
task create job taskmgr/examples/multi-gpu --host gpu1        # 需要至少 2 块 GPU
task logs job <jobid> --host gpu1 --follow
task fetch job <jobid> --host gpu1 --dest ./results/
```
