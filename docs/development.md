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
| `ruff` | code formatting (`ruff format`), line length 100 |
| `basedpyright` | type checking, `typeCheckingMode = "standard"`, Python 3.10, platform Linux |
| `pre-commit` | runs the hooks below on each commit |

## Setup

```sh
uv sync                          # creates .venv with Python 3.10 and the dev tools
uv run pre-commit install        # installs the git hook
```

## Hooks (`.pre-commit-config.yaml`)

| Hook | Command | Notes |
| --- | --- | --- |
| `uv-lock` | `uv lock` | keeps `uv.lock` in sync with `pyproject.toml` |
| `ruff-format` | `uv run --frozen ruff format` | reformats staged Python files; the commit fails if files changed, re-stage them |
| `basedpyright` | `uv run --frozen basedpyright` | checks the whole project (not only staged files) |

ruff and basedpyright run from the project venv (`language: system` + `uv run --frozen`),
so the versions in `uv.lock` are the ones the hooks use.

```sh
uv run pre-commit run --all-files    # run every hook on the whole repo
uv run ruff format .                 # format by hand
uv run basedpyright                  # type-check by hand
```

basedpyright is configured with `pythonPlatform = "Linux"` because the remote side uses
`/proc`, `fork` and `setsid`; it type-checks on macOS too. The tests themselves need
Linux, see [[docs/testing]].

## Tests

```sh
uv run python -m unittest discover -s taskmgr/tests     # on Linux
```

From macOS use Docker as described in [[docs/testing]].
