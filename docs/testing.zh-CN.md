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

systemd 相关行为的测试方式：检查用户 unit 中有 `KillMode=process`，并向守护进程的整个进程组
发送 SIGTERM 来停止它，验证 shim 和任务继续运行，且记录下真实退出码。

## 可选：使用真实 SSH 的冒烟测试

```sh
# 在服务器上
git clone <repo> ~/taskmgr && mkdir -p ~/taskmgr-root
nohup ~/taskmgr/bin/taskmgr-remote --root ~/taskmgr-root serve >/dev/null 2>&1 &
# 在本地，machines.json 中配置一个指向该服务器的 "ssh" transport 条目
task submit --host gpu1 taskmgr/examples/succeed
task status <jobid> --host gpu1 --wait --poll-interval 2      # 退出码 0，outputs 存在
task submit --host gpu1 taskmgr/examples/multi-gpu            # 需要至少 2 块 GPU
task logs <jobid> --host gpu1 --follow
task fetch <jobid> --host gpu1 --output ./results/
```
