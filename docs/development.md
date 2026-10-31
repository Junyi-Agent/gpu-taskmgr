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
| `basedpyright` | type checking, `typeCheckingMode = "all"`, Python 3.10, platform Linux |
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
- No `# type: ignore` / `# pyright: ignore`, except the optional `torch` import in
  `examples/multi-gpu/run.py`.

## Tests

```sh
uv run python -m unittest discover -s taskmgr/tests     # on Linux
```

From macOS use Docker as described in [[docs/testing]].

## Zipapps

```sh
uv run python -m taskmgr.pyz -o dist    # dist/task.pyz, dist/taskmgr-remote.pyz
```

`taskmgr/pyz.py` builds both archives from the package (without `tests/` and
`__pycache__`). `task host add` builds `taskmgr-remote.pyz` the same way before each
upload, so a server never needs a manual build. Code that runs from an archive cannot
open package files with `open()`: use `common.copy_package_tree` and
`common.read_code_file`, which work in a checkout and in a `.pyz`. Keep every import of
`taskmgr` modules at module level or at process start (see `pyz.MAIN_PY`).
