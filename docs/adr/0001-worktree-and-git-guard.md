# 0001 在仓库外的 worktree 开发，用 PreToolUse hook 强制

状态：采纳

## 背景

agent 在主目录直接切分支或在 `main` 上提交，会打乱用户正在用的工作区，也绕过 PR 和 CI。2026-11-24 之前本仓库的提交都直接落在 `main` 上。只写在文档里，agent 可能不遵守。

## 决策

- 每个新功能或修改在仓库目录之外建 worktree：`git worktree add ../gpu-taskmgr-<feature> -b <feature> origin/main`，在那个目录里开发、提交、push，开 PR 合并。
- 不在当前目录 `git switch` / `git checkout <branch>`，不在 `main` 上提交，不 push 到 `main`，不用 `--no-verify` / `SKIP=` / `core.hooksPath` 跳过 git hook。
- `.claude/hooks/git_guard.py`（PreToolUse，Bash）拒绝上面这些操作，并在 `gh pr merge` 前跑 `gh pr checks`，检查没全过就拒绝。脚本从 `.claude/jy-skills` submodule（Junyi-Agent/skills）原样复制过来，规则细节见那边的 ADR 0001。hook 用的是复制到 `.claude/hooks/` 的文件，不直接指向 submodule：submodule 没初始化时 hook 也能运行。
- 同步上游：升 submodule 的 tag 后整文件复制，跑 `.claude/hooks/test_git_guard.py`；pre-commit 的 `jy-skills-copies` 检查副本和 submodule 一致（2026-11-24 起，见 CLAUDE.md）。

## 后果

- 拦截只对 Claude Code 的 Bash 工具生效；人在终端里操作不受限。
- 拦不住 `export SKIP=...` 后单独 push、`eval`、脚本文件里的 git 命令；`main` 的服务端分支保护（0003）兜底。
- hook 出错会拦下所有 Bash 命令（fail closed），改 hook 时先跑它的测试。
- 副本要随 submodule 手动同步；pre-commit 拦住副本和 submodule 不一致的提交，所以新 worktree 要先 `git submodule update --init .claude/jy-skills`。

## 验证

（只由人类填写。状态改成「已验证」时填：验证人、日期、测了什么、结果。）
