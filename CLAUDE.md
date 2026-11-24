# gpu-taskmgr

GPU 任务管理工具：本地 `task` CLI 通过 SSH 调用服务器上的 `taskmgr-remote`。需求和设计在 `agent/desc-*.md`（按编号读），使用说明在 `taskmgr/README.md`，开发说明在 `docs/development.md`。

## 开发流程

- 每个新功能或修改在仓库目录之外建 worktree 开发：
  `git worktree add ../gpu-taskmgr-<feature> -b <feature> origin/main`
- 不在当前目录切换分支（`git checkout <branch>` / `git switch`）。
- 不在 `main` 上提交，不 push 到 `main`。push feature 分支后开 PR，CI 通过后合并。
- 不用 `--no-verify`、`SKIP=`、`core.hooksPath` 跳过 git hook。
- 以上规则由 `.claude/hooks/git_guard.py`（PreToolUse hook）强制执行；`gh pr merge` 会先跑 `gh pr checks`，检查没全过就拒绝。`main` 在 GitHub 上开了分支保护（[[docs/adr/0001-worktree-and-git-guard]]、[[docs/adr/0003-branch-protection]]）。

## push 审查门禁

push 时 pre-push hook 跑 `scripts/review_gate.py`：3 个 Claude 分别从正确性、设计与文档一致性、流程与安全审查 `origin/main` 之后的改动，≥2 个同意才放行。被拒时按输出里的 blocking issues 改完再 push。需要本机装好并登录 `claude` CLI。见 [[docs/adr/0002-pre-push-review-gate]]。

## 本地检查

新机器或新 worktree 先装环境和 hook（同时装 pre-commit 和 pre-push 两个阶段）：

```bash
uv sync
uv run pre-commit install
```

提交前跑 uv lock、ruff check、ruff format、basedpyright 和 ADR 列表检查，改到对应文件时还会跑 git guard、ADR guard、审查门禁和 repo_check 的单元测试。手动全量跑：`uv run pre-commit run --all-files`；单元测试：`uv run python -m unittest discover -s taskmgr/tests`（需要 Linux，见 `docs/testing.md`）。CI（`.github/workflows/ci.yml`）跑同一套配置和单元测试。

## 约定

- 命令行入口用 kubectl 式结构：`<tool> <动词> <资源> [名字] [flags]`，动词跨资源复用，输出格式用 `-o` / `--output` 选；改了 `task` 和 `taskmgr-remote` 之间的接口要升 `PROTOCOL_VERSION`（[[docs/adr/0004-kubectl-style-cli]]）。
- 文档有中英两份（`*.md` 和 `*.zh-CN.md`），改一份同步改另一份。
- 流程或架构决策写 ADR（`docs/adr/`）。状态分三种：提议 = 只写了文档；采纳 = 允许实现，可能已实现；已验证 = 人类测试了代码并确认。agent 最多改到「采纳」，「已验证」只由人类改。
- `.claude/hooks/git_guard.py`、`adr_guard.py` 和 `scripts/review_gate.py`、`repo_check.py` 从 `.claude/jy-skills`（Junyi-Agent/skills v1.1.0）复制而来，同步上游时整文件替换后跑对应测试。
