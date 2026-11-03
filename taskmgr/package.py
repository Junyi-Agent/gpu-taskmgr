"""Task package handling: validation, deterministic packing, content hash, safe extraction."""

import gzip
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import stat
import tarfile
from typing import IO, TypeGuard, cast

from .common import (
    DEFAULT_REMOTE_CONFIG,
    SCHEMA_VERSION,
    TASK_ID_RE,
    TaskmgrError,
    parse_utc_iso,
)
from .records import JsonDict, JsonValue, Problem, SkippedEntry, TaskSpec

REQUIRED_FILES = ("task.json", "TASK.md", "progress.json")
PLACEHOLDER = "<REQUIRED"
RESERVED_ENV = ("CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER", "VIRTUAL_ENV")  # and TASKMGR_*
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
DEFAULT_MAX_PACKAGE_BYTES: int | None = DEFAULT_REMOTE_CONFIG["max_package_bytes"]

# Every task.json key this code knows, per object ("" is the top level). Any other key is
# an error (code unknown_key): a key this code does not know may be a feature of a newer
# taskmgr (or a typo) and must never be ignored silently. Keys starting with "x-" are free
# for annotations and are ignored.
TASK_KEYS: dict[str, tuple[str, ...]] = {
    "": (
        "schema_version",
        "task_id",
        "entry",
        "precompile",
        "gpus",
        "environment",
        "env",
        "timeout_seconds",
        "result",
    ),
    "entry": ("argv",),
    "precompile": ("argv", "timeout_seconds"),
    "environment": ("venv", "required_paths"),
    "result": ("required_outputs",),
    "gpus": ("count", "min_free_gb", "from"),  # the object form (GPU selection)
}
ANNOTATION_PREFIX = "x-"

# Never packed or hashed: caches, VCS data, editor junk and files a run leaves behind.
EXCLUDED_NAMES = ("__pycache__", ".git", ".DS_Store")
EXCLUDED_SUFFIXES = (".pyc", ".json.lock")  # .json.lock: flock files of taskmgr_task.py
EXCLUDED_PREFIXES = (".tmp-",)  # in-flight atomic writes
# At the package root only: runner output and the result.json.corrupt[.N] copies the
# server keeps of a corrupt result.json.
EXCLUDED_ROOT_NAMES = ("result.json",)
EXCLUDED_ROOT_PREFIXES = ("result.json.corrupt",)


def is_excluded(rel: str) -> bool:
    """True if a package-relative POSIX path is left out of packing and the content hash.

    A path is excluded when any component is excluded, so callers may prune whole
    directories. `*.json.lock` is excluded at any depth; `result.json` and
    `result.json.corrupt*` only at the package root. `task init` copies the template with
    a subset of these rules.
    """
    parts = rel.split("/")
    if parts[0] in EXCLUDED_ROOT_NAMES or parts[0].startswith(EXCLUDED_ROOT_PREFIXES):
        return True
    return any(
        p in EXCLUDED_NAMES or p.endswith(EXCLUDED_SUFFIXES) or p.startswith(EXCLUDED_PREFIXES)
        for p in parts
    )


def excluded_entries(root: str) -> list[str]:
    """The paths below root that is_excluded() leaves out, sorted; below an excluded
    directory only the directory itself is listed."""
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        prefix = "" if rel_dir == "." else rel_dir + "/"
        out.extend(prefix + n for n in dirnames + filenames if is_excluded(prefix + n))
        dirnames[:] = [d for d in dirnames if not is_excluded(prefix + d)]
    return sorted(out)


def remove_excluded(root: str) -> list[str]:
    """Delete what excluded_entries() lists (an extracted archive may carry a stale
    result.json); returns the removed paths."""
    removed = excluded_entries(root)
    for rel in removed:
        p = os.path.join(root, *rel.split("/"))
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        else:
            os.unlink(p)
    return removed


def size_exceeded(what: str, size: int, limit: int) -> str:
    """The one wording of a package_too_large message: what is e.g. "package size of"."""
    return "%s %d bytes exceeds max_package_bytes (%d)" % (what, size, limit)


def _err(errors: list[Problem], code: str, message: str, path: str | None = None) -> None:
    e: Problem = {"code": code, "message": message}
    if path is not None:
        e["path"] = path
    errors.append(e)


def _is_int(v: object) -> TypeGuard[int]:
    return isinstance(v, int) and not isinstance(v, bool)


def _link_resolves_inside(root: str, link: str) -> bool:
    """True if a symlink's physical target (following chains) stays inside root."""
    real_root = os.path.realpath(root)
    real = os.path.realpath(link)
    return real == real_root or real.startswith(real_root + os.sep)


def entry_argv(task: object) -> list[object] | None:
    """task.json entry.argv when it is a list, else None (not further checked)."""
    if not isinstance(task, dict):
        return None
    entry = cast(JsonDict, task).get("entry")
    argv = cast(JsonDict, entry).get("argv") if isinstance(entry, dict) else None
    return cast("list[object]", argv) if isinstance(argv, list) else None


def precompile_argv(task: object) -> list[object] | None:
    """task.json precompile.argv when it is a list, else None (not further checked)."""
    if not isinstance(task, dict):
        return None
    spec = cast(JsonDict, task).get("precompile")
    argv = cast(JsonDict, spec).get("argv") if isinstance(spec, dict) else None
    return cast("list[object]", argv) if isinstance(argv, list) else None


def _inside(rel: str) -> bool:
    """True if a normalized relative POSIX path stays inside the package root."""
    n = posixpath.normpath(rel)
    return not (n == ".." or n.startswith(("../", "/")))


# ---------------------------------------------------------------------------
# Schemas


def task_features(task: object) -> list[str]:
    """The common.FEATURES a task.json needs from the server that runs it (sorted)."""
    if not isinstance(task, dict):
        return []
    task = cast(JsonDict, task)
    used: list[str] = []
    if "precompile" in task:
        used.append("precompile")
    if isinstance(task.get("gpus"), dict):
        used.append("gpu_select")
    return sorted(used)


def _check_keys(task: JsonDict, errors: list[Problem]) -> None:
    """unknown_key errors for keys TASK_KEYS does not list (x-* annotations excepted)."""
    for where, known in TASK_KEYS.items():
        obj = task if not where else task.get(where)
        if not isinstance(obj, dict):
            continue
        for k in obj:
            if k in known or k.startswith(ANNOTATION_PREFIX):
                continue
            name = k if not where else "%s.%s" % (where, k)
            _err(
                errors,
                "unknown_key",
                "unknown key %r: not a task.json key of this taskmgr (a newer feature, or a " % name
                + "typo?); update taskmgr or remove it; annotations start with %r"
                % ANNOTATION_PREFIX,
                "task.json",
            )


def _check_gpu_list(gpus: list[JsonValue], what: str, errors: list[Problem]) -> None:
    """Entries of a GPU list: nvidia-smi indices or full GPU-... UUIDs, no duplicates."""
    for g in gpus:
        if _is_int(g):
            if g < 0:
                _err(errors, "schema", "GPU index must be >= 0: %r" % g, "task.json")
        elif isinstance(g, str):
            if g.startswith("MIG-"):
                _err(errors, "gpu", "MIG instances are not supported: %s" % g, "task.json")
            elif not g.startswith("GPU-"):
                _err(
                    errors,
                    "schema",
                    "GPU string must be a full UUID starting with GPU-: %r" % g,
                    "task.json",
                )
        else:
            _err(
                errors,
                "schema",
                "GPU entry must be an integer index or UUID string: %r" % (g,),
                "task.json",
            )
    keys = [json.dumps(g) for g in gpus]
    if len(set(keys)) != len(keys):
        _err(errors, "gpu", "%s contains duplicate entries" % what, "task.json")


def _check_gpu_select(spec: JsonDict, errors: list[Problem]) -> None:
    """`gpus` as an object: {"count": N, "min_free_gb"?: X, "from"?: [GPUs]}."""
    count = spec.get("count")
    if not (_is_int(count) and count >= 1):
        _err(errors, "schema", "gpus.count must be an integer >= 1", "task.json")
    if "min_free_gb" in spec:
        m = spec["min_free_gb"]
        if isinstance(m, bool) or not isinstance(m, (int, float)) or not m > 0:
            _err(errors, "schema", "gpus.min_free_gb must be a number > 0", "task.json")
    if "from" in spec:
        cand = spec["from"]
        if not (isinstance(cand, list) and cand):
            _err(errors, "schema", "gpus.from must be a non-empty array of GPUs", "task.json")
        else:
            _check_gpu_list(cand, "gpus.from", errors)
            if _is_int(count) and count > len(cand):
                _err(
                    errors,
                    "gpu",
                    "gpus.count %d is more than the %d GPUs in gpus.from" % (count, len(cand)),
                    "task.json",
                )


def check_task_json(task: object, errors: list[Problem]) -> None:
    """Schema checks for task.json (Section 2.2)."""
    if not isinstance(task, dict):
        _err(errors, "schema", "task.json must be a JSON object", "task.json")
        return
    task = cast(JsonDict, task)
    _check_keys(task, errors)
    if task.get("schema_version") != SCHEMA_VERSION:
        _err(errors, "schema", "schema_version must be %d" % SCHEMA_VERSION, "task.json")
    tid = task.get("task_id")
    if not isinstance(tid, str) or not TASK_ID_RE.match(tid):
        _err(
            errors,
            "task_id",
            "task_id %r does not match ^[A-Z]{2}-[0-9]{2,4}$" % (tid,),
            "task.json",
        )
    argv = entry_argv(task)
    if not (argv and all(isinstance(a, str) for a in argv) and argv[0]):
        _err(errors, "schema", "entry.argv must be a non-empty array of strings", "task.json")
    if "precompile" in task:
        spec = task["precompile"]
        if not isinstance(spec, dict):
            _err(errors, "schema", "precompile must be an object (omit it if none)", "task.json")
        else:
            pargv = precompile_argv(task)
            if not (pargv and all(isinstance(a, str) for a in pargv) and pargv[0]):
                _err(
                    errors,
                    "schema",
                    "precompile.argv must be a non-empty array of strings",
                    "task.json",
                )
            t = spec.get("timeout_seconds")
            if t is not None and not (_is_int(t) and t > 0):
                _err(
                    errors,
                    "schema",
                    "precompile.timeout_seconds must be a positive integer",
                    "task.json",
                )
    gpus = task.get("gpus")
    if isinstance(gpus, dict):
        _check_gpu_select(gpus, errors)
    elif not isinstance(gpus, list):
        _err(
            errors,
            "schema",
            "gpus must be an array (use [] for no GPU) or an object such as "
            + '{"count": 1, "min_free_gb": 40}',
            "task.json",
        )
    else:
        _check_gpu_list(gpus, "gpus", errors)
    envi = task.get("environment")
    if envi is not None:
        if not isinstance(envi, dict):
            _err(errors, "schema", "environment must be an object", "task.json")
        else:
            venv = envi.get("venv")
            if venv is not None and not (isinstance(venv, str) and venv.startswith("/")):
                _err(errors, "schema", "environment.venv must be an absolute path", "task.json")
            rp = envi.get("required_paths")
            if rp is not None and not (
                isinstance(rp, list) and all(isinstance(p, str) and p.startswith("/") for p in rp)
            ):
                _err(
                    errors,
                    "schema",
                    "environment.required_paths must be a list of absolute paths",
                    "task.json",
                )
    env = task.get("env")
    if env is not None:
        if not isinstance(env, dict):
            _err(errors, "schema", "env must be an object of strings", "task.json")
        else:
            for k, v in env.items():
                if not ENV_NAME_RE.match(k):
                    _err(
                        errors,
                        "env",
                        "env name %r must match ^[A-Za-z_][A-Za-z0-9_]*$" % k,
                        "task.json",
                    )
                if not isinstance(v, str):
                    _err(errors, "schema", "env value for %s must be a string" % k, "task.json")
                if k in RESERVED_ENV or k.startswith("TASKMGR_"):
                    _err(errors, "env", "env must not set %s (set by the runner)" % k, "task.json")
    if "timeout_seconds" in task and task["timeout_seconds"] is not None:
        t = task["timeout_seconds"]
        if not (_is_int(t) and t > 0):
            _err(errors, "schema", "timeout_seconds must be a positive integer", "task.json")
    res = task.get("result")
    if res is not None:
        if not isinstance(res, dict):
            _err(errors, "schema", "result must be an object", "task.json")
        else:
            ro = res.get("required_outputs")
            if ro is not None and not (
                isinstance(ro, list) and all(isinstance(k, str) for k in ro)
            ):
                _err(
                    errors,
                    "schema",
                    "result.required_outputs must be a list of strings",
                    "task.json",
                )


def check_progress_json(p: object, errors: list[Problem], path: str = "progress.json") -> None:
    if not isinstance(p, dict):
        _err(errors, "schema", "progress.json must be a JSON object", path)
        return
    p = cast(JsonDict, p)
    if p.get("schema_version") != SCHEMA_VERSION:
        _err(errors, "schema", "progress.json schema_version must be %d" % SCHEMA_VERSION, path)
    if not isinstance(p.get("stage"), str):
        _err(errors, "schema", "progress.json stage must be a string", path)
    if parse_utc_iso(p.get("updated_at")) is None:
        _err(errors, "schema", "progress.json updated_at must be a UTC ISO 8601 timestamp", path)
    if "percent" in p:
        pc = p["percent"]
        if isinstance(pc, bool) or not isinstance(pc, (int, float)) or not 0 <= pc <= 100:
            _err(errors, "schema", "progress.json percent must be a number from 0 to 100", path)
    if "message" in p and not isinstance(p["message"], str):
        _err(errors, "schema", "progress.json message must be a string", path)


# ---------------------------------------------------------------------------
# Validation


def scan_tree(root: str, errors: list[Problem]) -> int:
    """Check every packed entry for unsafe symlinks and special files (Section 2.4).

    Returns the total size of the packed regular files.
    """
    total = 0
    for rel, full in _sorted_entries(root, exclude=True):
        st = os.lstat(full)
        if stat.S_ISREG(st.st_mode):
            total += st.st_size
        if stat.S_ISLNK(st.st_mode):
            target = os.readlink(full)
            if target.startswith("/"):
                _err(errors, "symlink", "absolute symlink -> %s" % target, rel)
            elif not _inside(
                posixpath.join(posixpath.dirname(rel), target)
            ) or not _link_resolves_inside(root, full):
                _err(errors, "symlink", "symlink points outside the package -> %s" % target, rel)
        elif not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
            _err(errors, "special_file", "special files are not allowed", rel)
    return total


def validate_dir(root: str, max_bytes: int | None = None) -> tuple[list[Problem], TaskSpec | None]:
    """Validate an extracted task package. Returns (errors, task_or_None).

    max_bytes caps the total size of the packed regular files (None: no cap). The client
    passes None: the server's max_package_bytes decides, and `task validate` only warns
    (see packed_size).

    The task is the parsed task.json whenever it is a JSON object; it only matches
    TaskSpec when errors is empty, so callers must check errors before using it.
    """
    errors: list[Problem] = []
    if not os.path.isdir(root):
        raise TaskmgrError("not_found", "not a directory: %s" % root)
    for f in REQUIRED_FILES:
        if not os.path.isfile(os.path.join(root, f)) or os.path.islink(os.path.join(root, f)):
            _err(errors, "missing_file", "required file is missing", f)
    raw: dict[str, str] = {}
    for f in REQUIRED_FILES:
        p = os.path.join(root, f)
        if os.path.isfile(p) and not os.path.islink(p):
            with open(p, "rb") as fh:
                raw[f] = fh.read().decode("utf-8", errors="replace")
            if PLACEHOLDER in raw[f]:
                lines = [i + 1 for i, line in enumerate(raw[f].splitlines()) if PLACEHOLDER in line]
                _err(
                    errors,
                    "placeholder",
                    "unfilled <REQUIRED> placeholder on line(s) %s" % ",".join(map(str, lines)),
                    f,
                )
    task: JsonValue = None
    if "task.json" in raw:
        try:
            task = cast(JsonValue, json.loads(raw["task.json"]))
        except ValueError as e:
            _err(errors, "json", "task.json is not valid JSON: %s" % e, "task.json")
        else:
            check_task_json(task, errors)
    if "progress.json" in raw:
        try:
            prog = cast(JsonValue, json.loads(raw["progress.json"]))
        except ValueError as e:
            _err(errors, "json", "progress.json is not valid JSON: %s" % e, "progress.json")
        else:
            check_progress_json(prog, errors)
    for what, argv in (("entry", entry_argv(task)), ("precompile", precompile_argv(task))):
        if argv and isinstance(argv[0], str) and "/" in argv[0] and not argv[0].startswith("/"):
            a0 = argv[0]
            real = os.path.realpath(os.path.join(root, a0))
            rroot = os.path.realpath(root)
            if not _inside(a0):
                why = "points outside the package"
            elif not (
                real.startswith(rroot + os.sep)
                and os.path.isfile(real)
                and not is_excluded(os.path.relpath(real, rroot).replace(os.sep, "/"))
            ):
                why = "is not a file in the package"
            elif not os.access(real, os.X_OK):  # the shim checks the same before starting it
                why = "is not executable (chmod +x, or run it through an interpreter)"
            else:
                why = None
            if why:
                _err(errors, "entry", "%s argv[0] %s: %s" % (what, why, a0), "task.json")
    total = scan_tree(root, errors)
    if max_bytes is not None and total > max_bytes:
        _err(
            errors,
            "package_too_large",
            size_exceeded("package size of", total, max_bytes),
        )
    # Converted via object: a JsonDict and a TypedDict do not overlap for the checker.
    return errors, (cast(TaskSpec, cast(object, task)) if isinstance(task, dict) else None)


def packed_size(root: str) -> int:
    """Total size of the regular files pack_dir would pack."""
    return sum(
        st.st_size
        for st in (os.lstat(full) for _, full in _sorted_entries(root, exclude=True))
        if stat.S_ISREG(st.st_mode)
    )


# ---------------------------------------------------------------------------
# Packing and hashing


def _sorted_entries(root: str, exclude: bool = False) -> list[tuple[str, str]]:
    """(relative POSIX path, full path) of every entry below root, sorted by path.

    With exclude, entries matching is_excluded() are left out (directories are pruned).
    """
    out: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        prefix = "" if rel_dir == "." else rel_dir + "/"
        if exclude:
            dirnames[:] = [d for d in dirnames if not is_excluded(prefix + d)]
        dirnames.sort()
        for name in dirnames + filenames:
            if exclude and is_excluded(prefix + name):
                continue
            out.append((prefix + name, os.path.join(dirpath, name)))
    out.sort(key=lambda e: e[0])
    return out


def pack_dir(root: str, dest: str) -> None:
    """Deterministically pack a directory into a tar.gz (Section 5.1), minus is_excluded()."""
    with (
        open(dest, "wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        for rel, full in _sorted_entries(root, exclude=True):
            st = os.lstat(full)
            ti = tarfile.TarInfo(rel)
            ti.mtime = 0
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            ti.mode = stat.S_IMODE(st.st_mode)
            if stat.S_ISLNK(st.st_mode):
                ti.type = tarfile.SYMTYPE
                ti.linkname = os.readlink(full)
                tar.addfile(ti)
            elif stat.S_ISDIR(st.st_mode):
                ti.type = tarfile.DIRTYPE
                tar.addfile(ti)
            elif stat.S_ISREG(st.st_mode):
                ti.type = tarfile.REGTYPE
                ti.size = st.st_size
                with open(full, "rb") as f:
                    tar.addfile(ti, f)
            else:
                raise TaskmgrError("invalid_package", "special file in package: %s" % rel)


def content_hash(root: str) -> str:
    """Normalized content hash over regular files and symlinks (Section 6.1).

    Covers exactly what pack_dir packs: entries matching is_excluded() are left out.
    """
    manifest = io.StringIO()
    for rel, full in _sorted_entries(root, exclude=True):
        st = os.lstat(full)
        if stat.S_ISLNK(st.st_mode):
            digest = hashlib.sha256(os.readlink(full).encode()).hexdigest()
            row = [rel, "symlink", False, digest]
        elif stat.S_ISREG(st.st_mode):
            h = hashlib.sha256()
            with open(full, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            row = [rel, "file", bool(st.st_mode & 0o111), h.hexdigest()]
        else:
            continue
        _ = manifest.write(json.dumps(row) + "\n")
    return hashlib.sha256(manifest.getvalue().encode()).hexdigest()


# ---------------------------------------------------------------------------
# Safe extraction


def _member_name(m: tarfile.TarInfo) -> str:
    name = m.name
    while name.startswith("./"):
        name = name[2:]
    return name.rstrip("/")


def safe_extract(
    tar_path_or_fileobj: str | IO[bytes],
    dest: str,
    max_bytes: int | None = None,
    skip_unsafe: bool = False,
) -> list[SkippedEntry]:
    """Extract a tar(.gz) refusing unsafe entries (Section 5.2).

    Returns a list of skipped entries when skip_unsafe is set; otherwise raises
    TaskmgrError on the first unsafe entry.
    """
    os.makedirs(dest, exist_ok=True)
    skipped: list[SkippedEntry] = []
    total = 0
    seen: set[str] = set()  # case-folded: "Y" and "y" are one file on macOS

    def reject(name: str, why: str) -> None:
        if skip_unsafe:
            skipped.append({"path": name, "reason": why})
            return
        raise TaskmgrError("unsafe_package", "unsafe archive entry %r: %s" % (name, why))

    links: list[tuple[str, str]] = []
    try:
        if isinstance(tar_path_or_fileobj, (str, os.PathLike)):
            tar = tarfile.open(tar_path_or_fileobj, mode="r:*")
        else:
            tar = tarfile.open(fileobj=tar_path_or_fileobj, mode="r|*")
    except (tarfile.TarError, EOFError, OSError, gzip.BadGzipFile) as e:
        raise TaskmgrError("bad_archive", "cannot read archive: %s" % e) from e
    try:
        for m in tar:
            name = _member_name(m)
            if name in ("", "."):
                continue
            if m.name.startswith("/") or name.startswith("/"):
                reject(m.name, "absolute path")
                continue
            parts = name.split("/")
            if ".." in parts:
                reject(m.name, "path contains ..")
                continue
            key = name.casefold()
            if key in seen:
                reject(m.name, "duplicate entry")
                continue
            target = os.path.join(dest, *parts)
            # Never write through a symlink created by an earlier entry: every existing
            # parent must be a real directory (lstat also sees "y" when asked for "Y" on a
            # case-insensitive file system), and files and links are created with O_EXCL
            # semantics below, so an existing path is never followed or reused.
            cur = dest
            bad_parent = False
            for p in parts[:-1]:
                cur = os.path.join(cur, p)
                try:
                    pst = os.lstat(cur)
                except FileNotFoundError:
                    break  # this and deeper parents are created below
                if not stat.S_ISDIR(pst.st_mode):
                    bad_parent = True
                    break
            if bad_parent:
                reject(m.name, "parent is not a directory")
                continue
            if m.islnk():
                reject(m.name, "hard links are not allowed")
                continue
            if m.ischr() or m.isblk():
                reject(m.name, "device files are not allowed")
                continue
            if m.isfifo():
                reject(m.name, "FIFOs are not allowed")
                continue
            if m.issym():
                ln = m.linkname
                if ln.startswith("/"):
                    reject(m.name, "absolute symlink")
                    continue
                if not _inside(posixpath.join(posixpath.dirname(name), ln)):
                    reject(m.name, "symlink points outside the package")
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                try:
                    os.symlink(ln, target)
                except FileExistsError:
                    reject(m.name, "path already exists")
                    continue
                # Chains such as x -> . and y -> x/x/../../F pass the lexical check.
                if not _link_resolves_inside(dest, target):
                    os.unlink(target)
                    reject(m.name, "symlink resolves outside the package")
                    continue
                links.append((m.name, target))
            elif m.isdir():
                os.makedirs(os.path.dirname(target), exist_ok=True)
                try:
                    os.mkdir(target)
                except FileExistsError:
                    pass  # created as the parent of an earlier entry
                if not stat.S_ISDIR(os.lstat(target).st_mode):
                    reject(m.name, "path already exists and is not a directory")
                    continue
                os.chmod(target, (m.mode & 0o777) | 0o700)
            elif m.isreg():
                total += m.size
                if max_bytes is not None and total > max_bytes:
                    raise TaskmgrError(
                        "package_too_large", size_exceeded("extracted size of", total, max_bytes)
                    )
                os.makedirs(os.path.dirname(target), exist_ok=True)
                src = tar.extractfile(m)
                if src is None:
                    reject(m.name, "unreadable regular file")
                    continue
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                try:
                    fd = os.open(target, flags, 0o600)
                except FileExistsError:
                    reject(m.name, "path already exists")
                    continue
                with os.fdopen(fd, "wb") as out:
                    shutil.copyfileobj(src, out)
                    os.fchmod(out.fileno(), (m.mode & 0o777) | 0o600)
            else:
                reject(m.name, "unsupported entry type")
                continue
            seen.add(key)
    except (tarfile.TarError, EOFError, OSError, gzip.BadGzipFile) as e:
        raise TaskmgrError("bad_archive", "cannot read archive: %s" % e) from e
    finally:
        tar.close()
    # A later link can redirect an earlier one (y -> a/../F, then a -> .). Nothing was
    # written through a link above; now drop every link that escapes, until none does.
    changed = True
    while changed:
        changed = False
        for name, target in links:
            if os.path.islink(target) and not _link_resolves_inside(dest, target):
                os.unlink(target)
                changed = True
                reject(name, "symlink resolves outside the package")
    return skipped


def package_root(extracted: str) -> str:
    """Accept archives with a single top-level directory wrapping the package."""
    if os.path.isfile(os.path.join(extracted, "task.json")):
        return extracted
    entries = os.listdir(extracted)
    if len(entries) == 1:
        only = os.path.join(extracted, entries[0])
        if (
            os.path.isdir(only)
            and not os.path.islink(only)
            and os.path.isfile(os.path.join(only, "task.json"))
        ):
            return only
    return extracted


class _FixedSize:
    """Read exactly `size` bytes from a file a running task may still be changing.

    Bytes past `size` are left out; a file that shrank (or fails to read) is padded with
    NULs, so the tar header written before the data always stays correct.
    """

    def __init__(self, f: IO[bytes], size: int) -> None:
        self.f: IO[bytes] = f
        self.left: int = size

    def read(self, n: int = -1) -> bytes:
        n = self.left if n < 0 else min(n, self.left)
        try:
            data = self.f.read(n)
        except OSError:
            data = b""
        self.left -= n
        return data + b"\0" * (n - len(data))


# `fetch`: the last tar member, {"skipped": [{"path", "reason"}]} for the special files the
# server left out. Not a name in remote.FETCH_FILES, so it cannot clash with job files.
FETCH_MANIFEST = "taskmgr-fetch.json"


def _special_kind(mode: int) -> str:
    if stat.S_ISFIFO(mode):
        return "FIFO"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "device"
    return "special file"


def add_tree_to_tar(tar: tarfile.TarFile, root: str, arcroot: str) -> list[SkippedEntry]:
    """Add a file or directory tree to an open tar without following symlinks.

    Special files (FIFOs, sockets, devices) are left out and returned; a root that does
    not exist is skipped silently.
    """
    skipped: list[SkippedEntry] = []
    try:
        entries = [("", root)] + _sorted_entries(root)
    except OSError:
        return skipped
    for rel, full in entries:
        if posixpath.basename(rel).startswith(".tmp-"):
            continue  # in-flight atomic writes
        arc = arcroot if not rel else arcroot + "/" + rel
        try:
            st = os.lstat(full)
            if not (
                stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode)
            ):
                kind = _special_kind(st.st_mode)
                skipped.append({"path": arc, "reason": "%s, not fetched" % kind})
                continue
            ti = tar.gettarinfo(full, arcname=arc)
            f = None
            if ti.isreg() or ti.islnk():
                f = open(full, "rb")  # noqa: SIM115 -- closed below; a task may be writing it
                ti.type = tarfile.REGTYPE  # never emit hard links
                ti.linkname = ""
                ti.size = os.fstat(f.fileno()).st_size
        except OSError:
            continue  # vanished while walking a running job
        if f is None:
            tar.addfile(ti)
            continue
        with f:  # streamed: a large file is never read into memory whole
            tar.addfile(ti, cast(IO[bytes], cast(object, _FixedSize(f, ti.size))))
    return skipped
