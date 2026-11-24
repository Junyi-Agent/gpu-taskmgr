# 0002 push 前 3 个 Claude 审查，≥2 同意才放行

状态：采纳

## 背景

pre-commit 和 CI 只做格式、lint、类型检查和单元测试，查不出 client 和 remote 之间的协议不一致、崩溃恢复时状态文件不一致、文档和代码对不上这类问题。

## 决策

- `scripts/review_gate.py` 作为 pre-commit 的 `pre-push` 阶段 hook 运行（`.pre-commit-config.yaml` 设了 `default_install_hook_types: [pre-commit, pre-push]`，`uv run pre-commit install` 两个阶段都装）。
- 审查范围：`merge-base(origin/main, 被 push 的 ref)..被 push 的 ref`，squash merge 后再 push 也走同一范围。
- 并行跑 3 个 `claude -p`，方向分别是：正确性；设计与文档一致性（`agent/desc-*.md`、ADR、中英文文档）；流程与安全。只给 Read/Grep/Glob 工具，用 `--json-schema` 要求结构化输出。
- ≥2 个 approve 才 exit 0；报错、超时、输出解析失败都算 reject，否则非零退出阻止 push。
- 本地找不到 `origin/main`（或没有共同祖先）时阻止 push，提示先 `git fetch origin`。
- 脚本从 Junyi-Agent/skills 的 `scripts/review_gate.py` 原样复制。本仓库的 3 个审查方向写在 `.claude/review_gate.json`（skills 1.2.0 起支持），从 merge-base 读取，push 改不了审查自己的 reviewer。
- CI 不跑这个 hook（`pre-commit run --all-files` 只跑 pre-commit 阶段）。

## 后果

- 每次 push 调用 3 次 Claude，要花 token，等几分钟。
- 机器上没有 `claude` CLI 或没登录时 push 会被拦住。
- 人可以在终端用 `git push --no-verify` 跳过；agent 走 Bash 时被 0001 的 hook 拦住。

## 验证

（只由人类填写。状态改成「已验证」时填：验证人、日期、测了什么、结果。）
