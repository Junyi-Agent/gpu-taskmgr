# 0003 `main` 开分支保护：必须走 PR，CI 通过才能合并

状态：采纳

## 背景

`.github/workflows/ci.yml` 在 PR 和 push 到 `main` 时跑 pre-commit 和单元测试，但 `main` 没有分支保护，CI 失败也能合并或直接 push。

## 决策

用户 2026-11-24 决定，照 Junyi-Agent/skills 的配置开启 GitHub 分支保护：

- 合并到 `main` 必须走 PR。
- 必需的状态检查：`pre-commit hooks`、`unit tests (Linux, Python 3.10)`。
- 禁止 force push 和删除 `main`。
- 对管理员同样生效。

`git_guard.py`（0001）在本地也拦截 push 到 `main` 和 CI 没过的 `gh pr merge`，让 agent 不用等服务端报错。

## 后果

- 人也不能再直接 push 到 `main`，改动都走 PR。
- 改 CI job 的 `name` 时要同步改分支保护里的必需检查，否则 PR 会一直等一个不存在的检查。

## 验证

（只由人类填写。状态改成「已验证」时填：验证人、日期、测了什么、结果。）
