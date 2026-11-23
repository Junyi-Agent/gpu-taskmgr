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

CLI 是一个单文件 `task.pyz`，只需要 `python3` >= 3.10（[[docs/install.zh-CN]]，单文件 zipapp）。

```sh
# 构建（已有 dist/task.pyz 和 dist/taskmgr-remote.pyz 时可跳过）
bin/task build pyz                   # 在 checkout 里运行：写入 dist/

# 安装（用符号链接，task create host 才能在 dist/ 找到 taskmgr-remote.pyz）
ln -s "$PWD/dist/task.pyz" ~/.local/bin/task
task --help
```

**2. 本机：配置服务器**

```sh
task create host gpu1 --ssh tuner@10.0.0.12
# 主要步骤，依次执行（全部十步见 docs/install.zh-CN.md）；每处改动先询问 [y/N]（--yes 回答 yes，但不删除：
# remote_root 之外的旧代码需要 --prune-old-code；--plan 只输出会改什么，不做任何改动）：
#  1. 检查 ssh、Linux、python3 >= 3.10、systemctl --user
#  2. 上传 dist/taskmgr-remote.pyz 到 ~/.taskmgr-remote/versions/<version>.pyz，
#     把 ~/.taskmgr-remote/taskmgr-remote.pyz 链接过去（不在本地构建；code version 与 task 不同时报错）
#  3. 创建 ~/.taskmgr-remote/config.json
#  4. 写 ~/.config/systemd/user/taskmgr-remote.service，启用并启动服务
#  5. loginctl enable-linger
#  6. 把 gpu1 加入本地 ~/.taskmgr/machines.json
```

更新服务器（`git pull` 或改代码之后）：重新运行 `task build pyz`，再运行
`task update host gpu1`。`task create host` 和 `task update host` 都是幂等的：服务器已是最新时，它们不做改动，也不提问。
每一步、数据检查和手动部署方式见 [[docs/install.zh-CN]]（"远程服务器"一节）。

**3. 使用**

```sh
task get hosts                       # 确认机器列表
task ping host                       # 确认每台服务器都有应答
task create package AA-01            # 生成模板；填写所有 <REQUIRED...> 标记
task validate package AA-01/
task create job AA-01/               # 输出 JOBID TASK_ID HOST STATE
task wait job <jobid>
task fetch job <jobid> --dest ./results/
# 或者不用模板，直接用项目文件构建任务包（docs/usage.zh-CN.md）：
task create job "$(task build package src/ --task-id AA-02 --entry 'python3 run.py' --gpus 0)"
```

命令的形式是 `task <动词> <资源>`，和 kubectl 一样；`task -h` 列出全部命令，每种资源也可以用复数名
（`task get jobs`），`-o json` 输出完整的 JSON 对象。
