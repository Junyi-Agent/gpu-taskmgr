# 0005 push `v<版本>` tag 时由 GitHub Actions 发布 release

状态：采纳

## 背景

v0.1.0 是手动发的：本地跑 `task build pyz`、算校验和、`gh release create`。手动发版可能用错 commit、tag 和 `pyproject.toml` 的版本对不上，或者漏写协议升级提示。

## 决策

用户 2026-11-24 决定加发布流程（`.github/workflows/release.yml`）：

- 触发：push `v*` tag。先调用 `ci.yml`（pre-commit、单元测试），通过后才构建。
- 检查：tag 等于 `v` + `pyproject.toml` 的 `version`；tag 指向的 commit 在 `main` 上；同名 release 不存在。
- 构建：`task build pyz` 生成 `task.pyz` 和 `taskmgr-remote.pyz`，并核对两个 archive 报告的版本、commit、协议和 code_version；生成 `SHA256SUMS`。
- 发布：`gh release create`，附上三个文件，说明用 GitHub 自动生成的 PR 列表。`PROTOCOL_VERSION` 和上一个 tag 不同时，说明开头写明要对每台服务器跑 `task update host NAME`。
- 手动运行（workflow_dispatch）是演练：做全部检查和构建，构建结果存为 artifact，不发 release。演练检查的是将要打的 tag `v<版本>`：它还不存在、commit 在 `main` 上、没有同名 release（2026-11-24 补上，之前演练跳过了这些检查）。

发版步骤：在 PR 里改 `pyproject.toml` 的 `version` 并合并，然后对 `main` 上那个 commit 打 tag 并 push：`git tag -a v<版本> -m v<版本> origin/main && git push origin v<版本>`。

## 后果

- 发版只需要打 tag；版本号还要人决定，并在 PR 里改。
- tag 打错（版本不符、不在 main 上）时 workflow 失败，不会发 release；删掉 tag 重打即可。
- 两次构建的 zip 时间戳不同，pyz 的校验和不能复现，以 release 附带的 `SHA256SUMS` 为准。

## 验证

（只由人类填写。状态改成「已验证」时填：验证人、日期、测了什么、结果。）
