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
| `ruff` | 代码检查（`ruff check`，规则见 `[tool.ruff.lint]`）和格式化（`ruff format`，行宽 100）；覆盖 `taskmgr/` 和 `bin/` 下无扩展名的脚本；`ruff format` 也格式化 Markdown 文件里的 `python` 代码块 |
| `basedpyright` | 类型检查，`typeCheckingMode = "all"`，Python 3.10，平台 Linux |
| `pre-commit` | 每次提交和 push 前运行下面的 hook |

## 环境准备

```sh
uv sync                          # 创建 .venv（Python 3.10 + 开发工具）
git submodule update --init .claude/jy-skills   # jy-skills-copies hook 拿它做对比
uv run pre-commit install        # 安装 pre-commit 和 pre-push 两个 git hook
```

每个新 clone 和新 worktree 都要跑这几条。开发流程（每个改动在仓库外建 worktree、PR 合并到 `main`、
ADR）见 `CLAUDE.md`；决策记录在 `docs/adr/`。

## Hook（`.pre-commit-config.yaml`）

| Hook | 命令 | 说明 |
| --- | --- | --- |
| `uv-lock` | `uv lock` | 保持 `uv.lock` 与 `pyproject.toml` 一致 |
| `ruff-check` | `uv run --frozen ruff check --fix` | 检查暂存的 Python 文件并应用安全修复；有修复或仍有错误时提交失败 |
| `ruff-format` | `uv run --frozen ruff format` | 格式化暂存的 Python 文件，以及暂存的 Markdown 文件里的 `python` 代码块；有文件被改动时提交失败，重新 add 后再提交 |
| `basedpyright` | `uv run --frozen basedpyright` | 检查整个项目（不只检查暂存文件） |
| `adr-index` | `python3 scripts/repo_check.py check adrs` | `docs/adr/README.md` 的列表和文件一致；已验证的 ADR 由人类填了「验证」一节 |
| `jy-skills-copies` | 和 `.claude/jy-skills/` 逐个 `cmp` | 从 Junyi-Agent/skills 复制的 hook 和脚本没有改动；本仓库的差异放在 `.claude/review_gate.json` |
| `git-guard-tests`、`adr-guard-tests`、`repo-check-tests`、`review-gate-tests` | `.claude/hooks/` 和 `scripts/` 的单元测试 | 改到这些文件时运行 |
| `review-gate`（pre-push） | `python3 scripts/review_gate.py` | 3 个 Claude 按 `.claude/review_gate.json` 里的方向（正确性、设计与文档、流程与安全）审查 `origin/main..` 被 push 的 ref，2 个同意才放行（`docs/adr/0002-pre-push-review-gate.md`）；需要已登录的 `claude` CLI |

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
  原因。目前的用处：`examples/multi-gpu/run.py` 中可选的 `torch` import，以及 `cliparse.py` 遍历
  parser 打印帮助时用到的 `argparse._SubParsersAction` / `argparse._HelpAction`
  （`reportPrivateUsage`），还有 `hostsetup.py` 中的 `Remote._env`（持有 setup 锁的 ssh 进程需要与其他
  远程调用相同的环境变量）。

## 测试

```sh
uv run python -m unittest discover -s taskmgr/tests     # 在 Linux 上
```

在 macOS 上用 Docker，方法见 [[docs/testing.zh-CN]]。

## Zipapp

```sh
bin/task build pyz    # dist/task.pyz、dist/taskmgr-remote.pyz
```

`taskmgr/pyz.py` 用包（不含 `tests/` 和 `__pycache__`）构建这两个压缩包。`task create host` 不构建：
它上传已构建好的 `taskmgr-remote.pyz`（`pyz.default_remote_pyz()` 或 `--pyz`），code version 与
当前运行的代码不同时失败，所以每次改代码后都要重新构建。从压缩包运行的代码不能用
`open()` 打开包内文件：请用 `common.copy_package_tree` 和 `common.read_code_file`，它们在 checkout
和 `.pyz` 中都能用。`taskmgr` 模块的 import 一律放在模块顶层或进程启动时（见 `pyz.MAIN_PY`）。

## 发版

push `v<版本>` tag 时，`.github/workflows/release.yml` 发布 GitHub release
（`docs/adr/0005-release-on-tag.md`）：

```sh
# 1. 在 PR 里改 pyproject.toml 的 version，合并
# 2. 对 main 上合并后的 commit 打 tag 并 push
git fetch origin
git tag -a v0.2.0 -m v0.2.0 origin/main
git push origin v0.2.0
```

workflow 先跑 CI 检查，再检查 tag 等于 `v` + `pyproject.toml` 的版本、在 `main` 上、还没有同名
release。然后构建 `task.pyz` 和 `taskmgr-remote.pyz`，核对它们报告的版本、commit 和协议，创建 release，
附上两个压缩包、`SHA256SUMS` 和自动生成的说明。`PROTOCOL_VERSION` 和上一个 tag 不同时，说明开头写明
要跑 `task update host NAME`。手动运行这个 workflow（Actions → Release → Run workflow）是演练：除了
发 release 都做，构建结果存为 artifact。演练按“现在 push `v<版本>`”来检查：这个 tag 还不存在、commit 在
`main` 上、没有同名 release。
