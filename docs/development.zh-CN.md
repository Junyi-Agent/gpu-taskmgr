---
title: 开发
tags: [taskmgr, development, uv, pre-commit]
lang: zh-CN
up: "[[taskmgr/README.zh-CN]]"
---

# 开发

其他语言：[[docs/development]]

仓库由 [uv](https://docs.astral.sh/uv/) 管理。`pyproject.toml` 使用 `uv_build` 构建后端，平铺布局
（包目录为 `./taskmgr`）。`.python-version` 固定为 3.10，即需求文档允许的最低版本，这样本地运行就能
发现用到了过新语法或标准库 API 的代码。`uv.lock` 锁定开发工具的版本。

运行时没有第三方依赖。`dev` 依赖组包含以下工具：

| 工具 | 用途 |
| --- | --- |
| `ruff` | 代码检查（`ruff check`，规则见 `[tool.ruff.lint]`）和格式化（`ruff format`，行宽 100）；覆盖 `taskmgr/` 和 `bin/` 下无扩展名的脚本 |
| `basedpyright` | 类型检查，`typeCheckingMode = "all"`，Python 3.10，平台 Linux |
| `pre-commit` | 每次提交时运行下面的 hook |

## 环境准备

```sh
uv sync                          # 创建 .venv（Python 3.10 + 开发工具）
uv run pre-commit install        # 安装 git hook
```

## Hook（`.pre-commit-config.yaml`）

| Hook | 命令 | 说明 |
| --- | --- | --- |
| `uv-lock` | `uv lock` | 保持 `uv.lock` 与 `pyproject.toml` 一致 |
| `ruff-check` | `uv run --frozen ruff check --fix` | 检查暂存的 Python 文件并应用安全修复；有修复或仍有错误时提交失败 |
| `ruff-format` | `uv run --frozen ruff format` | 格式化暂存的 Python 文件；有文件被改动时提交失败，重新 add 后再提交 |
| `basedpyright` | `uv run --frozen basedpyright` | 检查整个项目（不只检查暂存文件） |

ruff 和 basedpyright 在项目 venv 中运行（`language: system` + `uv run --frozen`），因此 hook 使用的
就是 `uv.lock` 中锁定的版本。

```sh
uv run pre-commit run --all-files    # 对整个仓库运行所有 hook
uv run ruff check .                  # 手动代码检查
uv run ruff format .                 # 手动格式化
uv run basedpyright                  # 手动类型检查
```

basedpyright 配置了 `pythonPlatform = "Linux"`，因为远程端用到 `/proc`、`fork` 和 `setsid`；在 macOS
上也能执行类型检查。测试本身需要 Linux，见 [[docs/testing.zh-CN]]。

### 类型约定（`typeCheckingMode = "all"`）

`all` 模式禁止 `Any`（无论是显式写出的还是推断出的）、未使用的调用返回值和隐式 override。

- JSON 相关类型在 `taskmgr/records.py`：`JsonValue`（递归类型）、`JsonDict`，以及每种文件格式
  对应的 `TypedDict` 记录（`Job`、`Runner`、`TaskSpec`、`TaskmgrMeta`、`Server`、`RemoteConfig` 等）。
  Python 3.10 没有 `NotRequired`，可选键的写法是“必需键的基类 + `total=False` 的子类”。
- `json.loads(...)` 用 `cast(JsonValue, ...)` 包住；从磁盘读到的值先用 `isinstance` 收窄，再 cast
  成对应的记录类型。
- `argparse` 的结果放进带类型的 `argparse.Namespace` 子类（`Args`）。
- 有意忽略的返回值赋给 `_`。
- `@override` 从 `taskmgr.records` 导入：类型检查器看到的是 `typing_extensions.override`，运行时是
  空操作，因此运行时仍只依赖标准库。
- `taskmgr_task.py` 和 `run.py` 会随任务包分发，不能 import `taskmgr`；JSON 值透传的地方用
  `object`。`pyproject.toml` 通过 `executionEnvironments` 的 `extraPaths` 让 `run.py` 旁边的
  `import taskmgr_task` 能被解析。
- 只有在类型检查器无法用其他方式满足时才用 `# type: ignore` / `# pyright: ignore`，并写明规则名和
  原因。目前的用处：`examples/multi-gpu/run.py` 中可选的 `torch` import，以及 `taskmgr.py` 遍历
  parser 打印帮助时用到的 `argparse._SubParsersAction` / `argparse._HelpAction`
  （`reportPrivateUsage`）。

## 测试

```sh
uv run python -m unittest discover -s taskmgr/tests     # 在 Linux 上
```

在 macOS 上用 Docker，方法见 [[docs/testing.zh-CN]]。

## Zipapp

```sh
uv run python -m taskmgr.pyz -o dist    # dist/task.pyz、dist/taskmgr-remote.pyz
```

`taskmgr/pyz.py` 用包（不含 `tests/` 和 `__pycache__`）构建这两个压缩包。`task host add` 不构建：
它上传已构建好的 `taskmgr-remote.pyz`（`pyz.default_remote_pyz()` 或 `--pyz`），code version 与
当前运行的代码不同时失败，所以每次改代码后都要重新构建。从压缩包运行的代码不能用
`open()` 打开包内文件：请用 `common.copy_package_tree` 和 `common.read_code_file`，它们在 checkout
和 `.pyz` 中都能用。`taskmgr` 模块的 import 一律放在模块顶层或进程启动时（见 `pyz.MAIN_PY`）。
