---
title: Development
tags: [taskmgr, development, uv, pre-commit]
lang: en
up: "[[taskmgr/README]]"
---

# Development

Other language: [[docs/development.zh-CN]]

The repository is managed with [uv](https://docs.astral.sh/uv/). `pyproject.toml` uses the
`uv_build` backend with a flat layout (the package is `./taskmgr`). `.python-version` pins
3.10, the oldest Python the spec allows, so local runs catch syntax and stdlib APIs that
are too new. `uv.lock` pins the dev tools.

The runtime has no third-party dependencies. The `dev` dependency group holds the tools:

| Tool | Use |
| --- | --- |
| `ruff` | linting (`ruff check`, rules in `[tool.ruff.lint]`) and formatting (`ruff format`, line length 100); covers `taskmgr/` and the extensionless scripts in `bin/` |
| `basedpyright` | type checking, `typeCheckingMode = "all"`, Python 3.10, platform Linux |
| `pre-commit` | runs the hooks below on each commit and before each push |

## Setup

```sh
uv sync                          # creates .venv with Python 3.10 and the dev tools
git submodule update --init .claude/jy-skills   # the jy-skills-copies hook compares against it
uv run pre-commit install        # installs the pre-commit and pre-push git hooks
```

Run them in every new clone and every new worktree. The workflow (a worktree outside the
repository per change, PRs into `main`, ADRs) is in `CLAUDE.md`; decisions are in
`docs/adr/`.

## Hooks (`.pre-commit-config.yaml`)

| Hook | Command | Notes |
| --- | --- | --- |
| `uv-lock` | `uv lock` | keeps `uv.lock` in sync with `pyproject.toml` |
| `ruff-check` | `uv run --frozen ruff check --fix` | lints staged Python files and applies safe fixes; the commit fails on fixes or remaining errors |
| `ruff-format` | `uv run --frozen ruff format` | reformats staged Python files; the commit fails if files changed, re-stage them |
| `basedpyright` | `uv run --frozen basedpyright` | checks the whole project (not only staged files) |
| `adr-index` | `python3 scripts/repo_check.py check adrs` | the list in `docs/adr/README.md` matches the files; an ADR in 已验证 has a human-filled 验证 section |
| `jy-skills-copies` | `cmp` against `.claude/jy-skills/` | the hooks and scripts copied from Junyi-Agent/skills are unchanged; this repo's differences live in `.claude/review_gate.json` |
| `git-guard-tests`, `adr-guard-tests`, `repo-check-tests`, `review-gate-tests` | the unit tests of `.claude/hooks/` and `scripts/` | run when those files change |
| `review-gate` (pre-push) | `python3 scripts/review_gate.py` | 3 Claude reviewers (the focuses in `.claude/review_gate.json`: correctness, design and docs, workflow and safety) review `origin/main..` the pushed ref; the push needs 2 approvals (`docs/adr/0002-pre-push-review-gate.md`); needs the `claude` CLI, logged in |

ruff and basedpyright run from the project venv (`language: system` + `uv run --frozen`),
so the versions in `uv.lock` are the ones the hooks use.

```sh
uv run pre-commit run --all-files    # run every hook on the whole repo
uv run ruff check .                  # lint by hand
uv run ruff format .                 # format by hand
uv run basedpyright                  # type-check by hand
```

basedpyright is configured with `pythonPlatform = "Linux"` because the remote side uses
`/proc`, `fork` and `setsid`; it type-checks on macOS too. The tests themselves need
Linux, see [[docs/testing]].

### Typing conventions (`typeCheckingMode = "all"`)

`all` forbids `Any` (explicit or inferred), unused call results and implicit overrides.

- JSON types live in `taskmgr/records.py`: `JsonValue` (recursive), `JsonDict`, and
  `TypedDict` records for every file format (`Job`, `Runner`, `TaskSpec`, `TaskmgrMeta`,
  `Server`, `RemoteConfig`, ...). Python 3.10 has no `NotRequired`, so optional keys use a
  required base class plus a `total=False` subclass.
- `json.loads(...)` is wrapped in `cast(JsonValue, ...)`; values from disk are narrowed with
  `isinstance` and then cast to the matching record.
- `argparse` results go into typed `argparse.Namespace` subclasses (`Args`).
- Ignored results are assigned to `_`.
- `@override` comes from `taskmgr.records`: the real `typing_extensions.override` for the
  checker, a no-op at runtime, so the runtime stays standard-library only.
- `taskmgr_task.py` and the `run.py` scripts ship inside task packages and must not import
  `taskmgr`; they use `object` where JSON values pass through. `pyproject.toml` adds
  `executionEnvironments` with `extraPaths` so `import taskmgr_task` next to `run.py`
  resolves.
- `# type: ignore` / `# pyright: ignore` only where the checker cannot be satisfied
  otherwise, always with the rule name and a reason. Current uses: the optional `torch`
  import in `examples/multi-gpu/run.py`, and `argparse._SubParsersAction` /
  `argparse._HelpAction` (`reportPrivateUsage`) in `cliparse.py`, which walks the parser to
  print help, and `Remote._env` in `hostsetup.py` (the setup lock's ssh process needs the
  same environment as other remote calls).

## Tests

```sh
uv run python -m unittest discover -s taskmgr/tests     # on Linux
```

From macOS use Docker as described in [[docs/testing]].

## Zipapps

```sh
bin/task build pyz    # dist/task.pyz, dist/taskmgr-remote.pyz
```

`taskmgr/pyz.py` builds both archives from the package (without `tests/` and
`__pycache__`). `task create host` does not build: it uploads a prebuilt
`taskmgr-remote.pyz` (`pyz.default_remote_pyz()` or `--pyz`) and fails when its code
version differs from the running code, so rebuild after every code change. Code that runs from an archive cannot
open package files with `open()`: use `common.copy_package_tree` and
`common.read_code_file`, which work in a checkout and in a `.pyz`. Keep every import of
`taskmgr` modules at module level or at process start (see `pyz.MAIN_PY`).

## Releases

A pushed `v<version>` tag publishes a GitHub release through `.github/workflows/release.yml`
(`docs/adr/0005-release-on-tag.md`):

```sh
# 1. in a PR: set `version` in pyproject.toml, merge it
# 2. tag the merged commit of main and push the tag
git fetch origin
git tag -a v0.2.0 -m v0.2.0 origin/main
git push origin v0.2.0
```

The workflow runs the CI checks, then checks that the tag is `v` + the `pyproject.toml`
version, is on `main` and has no release yet. It builds `task.pyz` and `taskmgr-remote.pyz`,
checks the version, commit and protocol they report, and creates the release with both
archives, `SHA256SUMS` and generated notes. When `PROTOCOL_VERSION` changed since the
previous tag, the notes start with the `task update host NAME` reminder. Running the workflow
by hand (Actions → Release → Run workflow) is a dry run: everything but the release, with
the build kept as an artifact.
