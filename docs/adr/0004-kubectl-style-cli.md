# 0004 `task` 和 `taskmgr-remote` 都用 kubectl 式命令结构，协议升到 3

状态：采纳

## 背景

`task` 已经是 `task <动词> <资源> [名字] [flags]` 结构（`get jobs`、`create job`、`delete host`），输出格式用 `-o text|json` 选。`taskmgr-remote` 是 `task` 通过 SSH 调用的服务端入口，子命令却是只有动词或名词的写法（`jobs`、`status`、`cancel`、`receive`），也没有 `-o`。repo 约束检查（jy-repo-constrain-check 第 8 项）要求每个按名字调用的命令行入口都用 kubectl 式结构，豁免要人类批准并写 ADR。

## 决策

用户 2026-11-24 选择改造，不豁免：

- 新的命令行入口一律用 `<tool> <动词> <资源> [名字] [flags]`，不同资源复用同一套动词（get / create / delete / describe / cancel / retry / logs / fetch / run 等），输出格式用 `-o` / `--output` 选。
- `taskmgr-remote` 改成同样结构：`version` 保持不变（旧服务器也能回答版本探测），`get jobs`、`get job ID`、`create job`、`cancel job ID`、`retry job ID`、`resolve job ID`、`logs job ID`、`fetch job ID`、`describe store`、`run daemon`、`run shim`，JSON 输出加 `-o json`。
- 子命令变了，`PROTOCOL_VERSION` 从 2 升到 3。新 client 遇到协议 2 的服务器时报 `protocol_mismatch`，提示 `task update host NAME`。
- 不满足这个结构的入口，只有人类批准并写 ADR（写明豁免的入口、理由、批准人，状态为采纳或已验证）才算豁免。

## 后果

- 合并后每台 GPU 服务器都要跑一次 `task update host NAME`，之前不能提交或查询。
- `review_gate.py` 的设计审查方向会检查新命令是否符合这条。

## 验证

（只由人类填写。状态改成「已验证」时填：验证人、日期、测了什么、结果。）
