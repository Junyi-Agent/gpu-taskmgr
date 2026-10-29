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
| `ruff` | 代码格式化（`ruff format`），行宽 100 |
| `basedpyright` | 类型检查，`typeCheckingMode = "standard"`，Python 3.10，平台 Linux |
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
| `ruff-format` | `uv run --frozen ruff format` | 格式化暂存的 Python 文件；有文件被改动时提交失败，重新 add 后再提交 |
| `basedpyright` | `uv run --frozen basedpyright` | 检查整个项目（不只检查暂存文件） |

ruff 和 basedpyright 在项目 venv 中运行（`language: system` + `uv run --frozen`），因此 hook 使用的
就是 `uv.lock` 中锁定的版本。

```sh
uv run pre-commit run --all-files    # 对整个仓库运行所有 hook
uv run ruff format .                 # 手动格式化
uv run basedpyright                  # 手动类型检查
```

basedpyright 配置了 `pythonPlatform = "Linux"`，因为远程端用到 `/proc`、`fork` 和 `setsid`；在 macOS
上也能执行类型检查。测试本身需要 Linux，见 [[docs/testing.zh-CN]]。

## 测试

```sh
uv run python -m unittest discover -s taskmgr/tests     # 在 Linux 上
```

在 macOS 上用 Docker，方法见 [[docs/testing.zh-CN]]。
