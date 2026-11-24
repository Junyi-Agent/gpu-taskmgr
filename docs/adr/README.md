---
title: ADR（架构决策记录）
tags: [taskmgr, adr, process]
---

# ADR（架构决策记录）

每个影响流程或架构的决策写一篇 `NNNN-<slug>.md`，从 [`template.md`](template.md) 复制。

2026-11-24 之前的设计决策记在 `agent/desc-2.md` 的决策表里（每条注明用户决定的日期），不迁过来；之后的新决策和对它们的修改写在这里。

## 状态

| 状态 | 含义 | 谁来改 |
|---|---|---|
| 提议 | 只写了 ADR 文档 | 写 ADR 的人或 agent |
| 采纳 | 允许去实现，可能已经实现 | 用户决定后，人或 agent 改 |
| 已验证 | 人类测试了代码，确认了实现的内容 | 只能由人类改，并填写「验证」一节 |

- agent 不能把状态改成「已验证」，也不能代填「验证」一节。实现完成后停在「采纳」，请用户测试。`.claude/hooks/adr_guard.py` 拦截 agent 用 Edit/Write 做这两件事；`scripts/repo_check.py check adrs`（pre-commit 和 CI）要求已验证的 ADR 在「验证」一节写了验证人和日期，并检查下面的列表和各文件的状态一致。
- 被取代的 ADR 状态写「已取代（见 NNNN）」，不删除。

## 列表

| # | 决策 | 状态 |
|---|---|---|
| [0001](0001-worktree-and-git-guard.md) | 在仓库外的 worktree 开发，用 PreToolUse hook 强制 | 采纳 |
| [0002](0002-pre-push-review-gate.md) | push 前 3 个 Claude 审查，≥2 同意才放行 | 采纳 |
| [0003](0003-branch-protection.md) | `main` 开分支保护：必须走 PR，CI 通过才能合并 | 采纳 |
| [0004](0004-kubectl-style-cli.md) | `task` 和 `taskmgr-remote` 都用 kubectl 式命令结构，协议升到 3 | 采纳 |
| [0005](0005-release-on-tag.md) | push `v<版本>` tag 时由 GitHub Actions 发布 release | 采纳 |
