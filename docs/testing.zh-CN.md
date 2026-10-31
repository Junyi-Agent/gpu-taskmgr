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
docker run --rm --init -v "$PWD":/src -w /src python:3.11-slim \
    python -m unittest discover -s taskmgr/tests
```

在容器中运行时要加 `--init`（或其他会回收孤儿进程的 init）：测试会杀掉 shim，产生的孤儿任务
进程需要被回收。

**仅用于测试的覆盖项**（不要在生产环境使用）：

| 变量 / 配置 | 作用 |
| --- | --- |
| `TASKMGR_NVIDIA_SMI` | 伪造的 `nvidia-smi` 路径，输出 `index, uuid` 行 |
| `TASKMGR_BOOT_ID_FILE` | 从该文件读取 boot_id（模拟机器重启） |
| 机器条目 `"local": {"fail": "msg"}` | `transport: local` 抛出连接错误 |
| 机器条目 `"local": {"fail_if_exists": "/path"}` | 该文件存在期间报连接错误 |
| `TASKMGR_TEST_PROTOCOL_VERSION` | 远程端报告这个协议版本 |
| `TASKMGR_TEST_SKIP_DAEMON_LOCK=1` | 守护进程跳过 `daemon.lock`（多守护进程测试） |
| `TASKMGR_SHIM_EXECUTABLE` | 用于 exec shim 的解释器（模拟 shim 启动失败） |
| `TASKMGR_TEST_SHIM_CRASH=<point>` | shim 在 `before_identity`、`before_exec`、`after_exit_recorded`、`before_finalize` 处退出 |
| `TASKMGR_TEST_IGNORE_KILL=1` | 停止流程不发送任何信号（模拟停止无法确认） |
| `TASKMGR_TEST_RECEIVE_PAUSE=<s>` | `receive` 在持有上传锁时休眠 |
| 机器条目 `"local": {"home": "/dir"}` | `transport: local` 以 `HOME=/dir` 运行所有命令（包括 `task host add` 的脚本） |
| `TASKMGR_TEST_LOCAL_HOME=/dir` | `task host add` 新建的条目使用 `transport: local` 和 `"local": {"home": "/dir"}` |
| `TASKMGR_TEST_INTERACTIVE=1` | stdin 不是终端时，`task host add` 也从 stdin 读取回答 |

systemd 相关行为的测试方式：检查用户 unit 中有 `KillMode=process`，并向守护进程的整个进程组
发送 SIGTERM 来停止它，验证 shim 和任务继续运行，且记录下真实退出码。

`task host add` 的测试不需要 ssh，也不需要 systemd（`taskmgr/tests/test_add_host.py`）。“服务器”
是一个 local 传输的条目，它的 HOME 是测试目录（即上表中仅用于测试的 `"local": {"home"}`）。测试把
伪造的 `systemctl` 和 `loginctl` 脚本写到一个目录里，并把该目录放在 PATH 最前面。它们的状态保存在
`$HOME/.fake-systemd/`，每次调用也记录在那里：`start` 启动 unit 的 `ExecStart`（真实的守护进程），
`stop` 只向主 PID 发送 SIGTERM（与 `KillMode=process` 相同），unit 文件改动后必须先
`daemon-reload`，`linger-refuse` 文件存在时 `enable-linger` 失败。这些伪造脚本只存在于测试中，
`taskmgr/` 中的代码不引用它们。同一个文件还用普通的 local 传输服务器测试 `task host list`、
`task host remove` 和 `task host ping`；无法连接的服务器是带有 `"local": {"fail": "message"}` 的机器条目。

这些测试像在真实服务器上一样安装 `taskmgr-remote.pyz`（指向 `$HOME/.taskmgr-remote/` 的符号
链接），伪造的 `start` 运行它。“已安装旧版本”的模拟方式：安装一份 `.taskmgr-code-version` 不同的
压缩包副本并切换链接，与 `host add` 的做法相同。代码更新测试让一个任务在两次更新中持续运行；
如果 shim 从启动期间被替换的压缩包导入，这个测试会失败。

`taskmgr/tests/test_pyz.py` 用 `python3 -m taskmgr.pyz` 构建 `task.pyz` 和 `taskmgr-remote.pyz`，
在没有 `PYTHONPATH`、不在 checkout 目录的情况下运行它们，检查：压缩包内容、退出码、代码版本（与
checkout 相同）、`task init`（与 checkout 生成的包相同）、经符号链接运行，以及用 `task.pyz` 提交、
由 `taskmgr-remote.pyz` 的守护进程和 shim 运行到 `succeeded` 的作业。

## 可选：使用真实 SSH 的冒烟测试

```sh
task host add gpu1 --ssh tuner@<server>                       # 每一步都是 ok 或 changed
task host add gpu1                                            # 第二次：每一步都是 ok，不询问
task host ping gpu1                                           # ok，代码版本与本地一致
ssh gpu1 ls -l taskmgr-remote.pyz .taskmgr-remote/            # 链接 -> .taskmgr-remote/<版本>.pyz
task submit --host gpu1 taskmgr/examples/succeed
task status <jobid> --host gpu1 --wait --poll-interval 2      # 退出码 0，outputs 存在
task submit --host gpu1 taskmgr/examples/multi-gpu            # 需要至少 2 块 GPU
task logs <jobid> --host gpu1 --follow
task fetch <jobid> --host gpu1 --output ./results/
```
