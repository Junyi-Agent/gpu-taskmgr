"""`task build package`: build a task package from project files and options, validate it,
and print its path (a directory, .tar.gz or .tgz), so that
`task create job "$(task build package ...)"` composes.

Sources are copied into the package: a directory's contents go to the package root, a file
to the root under its name, and SRC=DEST puts a file or directory at DEST inside the
package. Symlinks are followed (their contents are copied), so the package is
self-contained. A task.json among the sources is the base that the options change;
without one, task.json is written from the options. TASK.md and progress.json are written
when no source provides them. taskmgr_task.py always comes from the installed template, so
it matches the taskmgr that runs the job.
"""

import json
import os
import shlex
import shutil
import tempfile
from collections.abc import Sequence
from typing import Protocol, cast

from . import package
from .common import (
    TASK_ID_RE,
    TaskmgrError,
    copy_package_tree,
)
from .records import JsonDict, JsonValue
from .textout import warn

HELPER = "taskmgr_task.py"
CUTE_HELPERS = ("precompile.py", "cutecache.py")
PACKED_AT = "1970-01-01T00:00:00Z"  # updated_at of a generated progress.json


class PackArgs(Protocol):
    """The `task build package` options (cliparse.Args has them)."""

    sources: list[str]

    @property
    def task_id(self) -> str | None: ...  # read-only: `create package` declares it as str

    entry: str | None
    precompile: str | None
    precompile_timeout: int | None
    gpus: str | None
    venv: str | None
    require_path: list[str] | None
    env: list[str] | None
    timeout: float | None
    output_key: list[str] | None
    task_md: str | None
    with_cute_helpers: bool
    exclude: list[str] | None
    dest: str | None
    force: bool


def parse_gpus(text: str) -> JsonValue:
    """--gpus: "none" or "" (no GPU), "0,1" / "GPU-..." (a list), or JSON (a list or an
    object such as {"count": 1, "min_free_gb": 40})."""
    t = text.strip()
    if t in ("", "none", "[]"):
        return []
    if t[0] in "[{":
        try:
            return cast(JsonValue, json.loads(t))
        except ValueError as e:
            raise TaskmgrError("bad_args", "--gpus is not valid JSON: %s" % e) from e
    out: list[JsonValue] = []
    for part in (p.strip() for p in t.split(",")):
        out.append(int(part) if part.isdigit() else part)
    return out


def parse_env(items: Sequence[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in items:
        name, sep, value = item.partition("=")
        if not sep or not name:
            raise TaskmgrError("bad_args", "--env must be NAME=VALUE, got %r" % item)
        env[name] = value
    return env


def split_source(arg: str) -> tuple[str, str]:
    """(path on disk, destination in the package; "" = the root) of one SRC argument."""
    if os.path.lexists(arg) or "=" not in arg:
        src, dest = arg, ""
    else:
        src, dest = arg.split("=", 1)
    if not os.path.exists(src):
        raise TaskmgrError("not_found", "source does not exist: %s" % src)
    dest = dest.strip("/")
    if dest and not package.is_inside(dest):
        raise TaskmgrError("bad_args", "destination %r is outside the package" % dest)
    if not dest and os.path.isfile(src):
        dest = os.path.basename(src)
    return src, dest


def copy_sources(sources: Sequence[str], root: str, exclude: Sequence[str]) -> dict[str, str]:
    """Copy every source into root; returns {package path: source} of the files written.
    Two sources that write the same file are an error."""
    try:
        return _copy_sources(sources, root, exclude)
    except (OSError, shutil.Error) as e:  # e.g. a dangling symlink among the sources
        raise TaskmgrError("copy_failed", "cannot copy the sources: %s" % e) from e


def _copy_sources(sources: Sequence[str], root: str, exclude: Sequence[str]) -> dict[str, str]:
    written: dict[str, str] = {}
    for arg in sources:
        src, dest = split_source(arg)
        target = os.path.join(root, *dest.split("/")) if dest else root
        if os.path.isdir(src):
            tmp = tempfile.mkdtemp(prefix=".pack-", dir=root)
            try:
                staged = os.path.join(tmp, "d")
                ignore = shutil.ignore_patterns(*exclude) if exclude else None
                _ = shutil.copytree(src, staged, symlinks=False, ignore=ignore)
                _merge(staged, target, dest, src, written)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        else:
            _place(src, target, dest, written)
    return written


def _place(src: str, target: str, rel: str, written: dict[str, str]) -> None:
    if rel in written:
        raise TaskmgrError(
            "conflict", "%s is written by two sources: %s and %s" % (rel, written[rel], src)
        )
    if os.path.isdir(target):
        raise TaskmgrError(
            "conflict", "%s is a directory in the package; %s is a file" % (rel, src)
        )
    os.makedirs(os.path.dirname(target), exist_ok=True)
    _ = shutil.copy2(src, target)  # follows symlinks
    written[rel] = src


def _merge(staged: str, target: str, dest: str, src: str, written: dict[str, str]) -> None:
    for dirpath, _dirs, files in os.walk(staged):
        rel_dir = os.path.relpath(dirpath, staged)
        for name in files:
            rel = name if rel_dir == "." else os.path.join(rel_dir, name)
            full = (dest + "/" if dest else "") + rel.replace(os.sep, "/")
            _place(os.path.join(dirpath, name), os.path.join(target, rel), full, written)
            written[full] = os.path.join(src, rel)  # name the source, not the staged copy


def _load_task(root: str) -> JsonDict:
    p = os.path.join(root, "task.json")
    if not os.path.isfile(p):
        return {}
    try:
        with open(p) as f:
            obj = cast(JsonValue, json.load(f))
    except ValueError as e:
        raise TaskmgrError("bad_task_json", "task.json from the sources is not JSON: %s" % e) from e
    if not isinstance(obj, dict):
        raise TaskmgrError("bad_task_json", "task.json from the sources is not a JSON object")
    return obj


def build_task(base: JsonDict, a: PackArgs) -> JsonDict:
    """task.json: the sources' task.json (if any) changed by the options."""
    task: JsonDict = dict(base)
    task["schema_version"] = 1
    if a.task_id:
        task["task_id"] = a.task_id
    if not isinstance(task.get("task_id"), str):
        raise TaskmgrError("bad_args", "--task-id is required (no task.json among the sources)")
    if not TASK_ID_RE.match(str(task["task_id"])):
        raise TaskmgrError("bad_task_id", "task_id must match ^[A-Z]{2}-[0-9]{2,4}$")
    if a.entry is not None:
        task["entry"] = {"argv": cast(list[JsonValue], shlex.split(a.entry))}
    if "entry" not in task:
        raise TaskmgrError("bad_args", "--entry is required (no task.json among the sources)")
    if a.precompile is not None:
        pre: JsonDict = {"argv": cast(list[JsonValue], shlex.split(a.precompile))}
        if a.precompile_timeout is not None:
            pre["timeout_seconds"] = a.precompile_timeout
        task["precompile"] = pre
    elif a.precompile_timeout is not None:
        raise TaskmgrError("bad_args", "--precompile-timeout needs --precompile")
    if a.gpus is not None:
        task["gpus"] = parse_gpus(a.gpus)
    _ = task.setdefault("gpus", [])
    environment = task.get("environment")
    envi: JsonDict = dict(environment) if isinstance(environment, dict) else {}
    if a.venv is not None:
        envi["venv"] = a.venv
    if a.require_path:
        old = envi.get("required_paths")
        paths: list[JsonValue] = list(old) if isinstance(old, list) else []
        paths += [p for p in a.require_path if p not in paths]
        envi["required_paths"] = paths
    if envi:
        task["environment"] = envi
    if a.env:
        env_old = task.get("env")
        env: JsonDict = dict(env_old) if isinstance(env_old, dict) else {}
        env.update(parse_env(a.env))
        task["env"] = env
    if a.timeout is not None:
        task["timeout_seconds"] = int(a.timeout)
    if a.output_key:
        res_old = task.get("result")
        res: JsonDict = dict(res_old) if isinstance(res_old, dict) else {}
        keys_old = res.get("required_outputs")
        keys: list[JsonValue] = list(keys_old) if isinstance(keys_old, list) else []
        keys += [k for k in a.output_key if k not in keys]
        res["required_outputs"] = keys
        task["result"] = res
    return task


def task_md_stub(task: JsonDict, sources: Sequence[str]) -> str:
    argv = task.get("entry")
    cmd = argv.get("argv") if isinstance(argv, dict) else None
    shown = shlex.join(str(a) for a in cmd) if isinstance(cmd, list) else "?"
    lines = [
        "# %s" % task.get("task_id"),
        "",
        "Packed by `task build package` from: %s." % (", ".join(sources) or "options only"),
        "",
        "Entry: `%s`" % shown,
        "",
        "taskmgr never reads this file; replace it with --task-md FILE to describe the task.",
        "",
    ]
    return "\n".join(lines)


def write_json(path: str, obj: object) -> None:
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)
        _ = f.write("\n")


def add_helpers(root: str, cute: bool) -> list[str]:
    """Copy taskmgr_task.py (and with `cute` precompile.py, cutecache.py) from the installed
    template; returns warnings about source files they replaced."""
    warnings: list[str] = []
    names = (HELPER,) + (CUTE_HELPERS if cute else ())
    with tempfile.TemporaryDirectory(prefix="taskmgr-pack-tpl-") as tmp:
        tpl = os.path.join(tmp, "t")
        _ = copy_package_tree("template", tpl, ("__pycache__", "*.pyc"))
        for name in names:
            dst = os.path.join(root, name)
            with open(os.path.join(tpl, name), "rb") as f:
                data = f.read()
            if os.path.isfile(dst):
                with open(dst, "rb") as f:
                    if f.read() != data:
                        warnings.append(
                            "%s from the sources was replaced by the installed template's" % name
                        )
            with open(dst, "wb") as f:
                _ = f.write(data)
    return warnings


def cmd_pack(a: PackArgs) -> tuple[dict[str, object], int]:
    out_path = os.path.abspath(a.dest) if a.dest else None
    with tempfile.TemporaryDirectory(prefix="taskmgr-pack-") as tmp:
        root = os.path.join(tmp, "pkg")
        os.makedirs(root)
        written = copy_sources(a.sources, root, a.exclude or [])
        task = build_task(_load_task(root), a)
        write_json(os.path.join(root, "task.json"), task)
        md = os.path.join(root, "TASK.md")
        if a.task_md is not None:
            _ = shutil.copyfile(a.task_md, md)
        elif not os.path.isfile(md):
            with open(md, "w") as f:
                _ = f.write(task_md_stub(task, a.sources))
        prog = os.path.join(root, "progress.json")
        if not os.path.isfile(prog):
            write_json(
                prog,
                # a fixed time: the same inputs give the same package (and content hash)
                {"schema_version": 1, "stage": "not_started", "updated_at": PACKED_AT},
            )
        warnings = add_helpers(root, a.with_cute_helpers)
        errors, _ = package.validate_dir(root)
        if errors:  # on stderr: stdout stays empty, so "$(task build package ...)" is no path
            for e in errors:
                warn(
                    "%s%s (%s)" % (e["path"] + ": " if "path" in e else "", e["message"], e["code"])
                )
            return {"valid": False, "errors": errors, "path": None}, 1
        target = out_path or os.path.abspath(str(task["task_id"]))
        archive = target.endswith((".tar.gz", ".tgz"))
        if os.path.lexists(target):
            if not a.force:
                raise TaskmgrError(
                    "exists",
                    "%s already exists; nothing was written (--force replaces it)" % target,
                )
            if os.path.isdir(target) and not os.path.islink(target):
                shutil.rmtree(target)
            else:
                os.unlink(target)
        chash = package.content_hash(root)
        features = package.task_features(task)
        files = len(written)
        if archive:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            package.pack_dir(root, target)
        else:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            _ = shutil.copytree(root, target, symlinks=True)
    for w in warnings:
        warn(w)
    return {
        "valid": True,
        "path": target,
        "archive": archive,
        "task_id": task["task_id"],
        "content_hash": chash,
        "features": features,
        "source_files": files,
        "warnings": warnings,
    }, 0
