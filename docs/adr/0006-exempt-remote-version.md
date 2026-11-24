# 0006 豁免 `taskmgr-remote version` 的 kubectl 式结构

状态：采纳

## 背景

ADR 0004 要求每个命令行入口都是 `<tool> <动词> <资源> [名字] [flags]`，豁免要人类批准并写 ADR。`taskmgr-remote` 的其余命令在协议 3 都改成了这个结构，`version` 没改：`task get version`、`task ping host`、`task create host` / `task update host` 的 verify 步骤都通过它探测服务器的协议版本。2026-11-24 的 repo 审查（REVIEW.md M4）指出 ADR 0004 只写了保留的理由，没有写成豁免。

## 决策

- 豁免的入口：`taskmgr-remote --root R [--protocol-version N] version [-o json]`，只豁免「没有资源」这一点；它仍然用 `-o` 选输出格式。
- 理由：版本探测要跨协议工作。协议 2 的服务器只认 `version`，新 client 靠它的回复得到 `protocol_version` 并报 `protocol_mismatch`，提示 `task update host NAME`；把它改成 `get version` 会让新 client 无法识别旧服务器，以后再升协议时也一样。
- 批准人：Junyi Hou，2026-11-24 在 Claude Code 会话中批准（回复「M4 可以豁免」）。
- 本地 `task` 的 `version` 不在豁免范围内，已改为 `task get version`（见 0004 的更新）。

## 后果

- `taskmgr-remote` 以后新增命令仍按 0004 的结构写，不能援引这篇 ADR。
- 如果以后有别的方式做跨协议探测（例如固定的探测协议），可以取消这条豁免。

## 验证

（只由人类填写。状态改成「已验证」时填：验证人、日期、测了什么、结果。）
