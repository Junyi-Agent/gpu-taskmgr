"""Task package handling: validation, deterministic packing, content hash, safe extraction."""

import gzip
import hashlib
import io
import json
import os
import posixpath
import shutil
import stat
import tarfile

from .common import SCHEMA_VERSION, TASK_ID_RE, TaskmgrError, parse_utc_iso

REQUIRED_FILES = ("task.json", "TASK.md", "progress.json")
PLACEHOLDER = "<REQUIRED"
RESERVED_ENV = ("CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER", "VIRTUAL_ENV")


def _err(errors, code, message, path=None):
    e = {"code": code, "message": message}
    if path is not None:
        e["path"] = path
    errors.append(e)


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _link_resolves_inside(root, link):
    """True if a symlink's physical target (following chains) stays inside root."""
    real_root = os.path.realpath(root)
    real = os.path.realpath(link)
    return real == real_root or real.startswith(real_root + os.sep)


def _inside(rel):
    """True if a normalized relative POSIX path stays inside the package root."""
    n = posixpath.normpath(rel)
    return not (n == ".." or n.startswith("../") or n.startswith("/"))


# ---------------------------------------------------------------------------
# Schemas

def check_task_json(task, errors):
    """Schema checks for task.json (Section 2.2)."""
    if not isinstance(task, dict):
        _err(errors, "schema", "task.json must be a JSON object", "task.json")
        return
    if task.get("schema_version") != SCHEMA_VERSION:
        _err(errors, "schema", "schema_version must be %d" % SCHEMA_VERSION, "task.json")
    tid = task.get("task_id")
    if not isinstance(tid, str) or not TASK_ID_RE.match(tid):
        _err(errors, "task_id", "task_id %r does not match ^[A-Z]{2}-[0-9]{2,4}$" % (tid,),
             "task.json")
    entry = task.get("entry")
    argv = entry.get("argv") if isinstance(entry, dict) else None
    if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)
            and argv[0]):
        _err(errors, "schema", "entry.argv must be a non-empty array of strings", "task.json")
    gpus = task.get("gpus")
    if not isinstance(gpus, list):
        _err(errors, "schema", "gpus must be an array (use [] for no GPU)", "task.json")
    else:
        for g in gpus:
            if _is_int(g):
                if g < 0:
                    _err(errors, "schema", "GPU index must be >= 0: %r" % g, "task.json")
            elif isinstance(g, str):
                if g.startswith("MIG-"):
                    _err(errors, "gpu", "MIG instances are not supported: %s" % g, "task.json")
                elif not g.startswith("GPU-"):
                    _err(errors, "schema", "GPU string must be a full UUID starting with GPU-: %r"
                         % g, "task.json")
            else:
                _err(errors, "schema", "GPU entry must be an integer index or UUID string: %r"
                     % (g,), "task.json")
        keys = [json.dumps(g) for g in gpus]
        if len(set(keys)) != len(keys):
            _err(errors, "gpu", "gpus contains duplicate entries", "task.json")
    envi = task.get("environment")
    if envi is not None:
        if not isinstance(envi, dict):
            _err(errors, "schema", "environment must be an object", "task.json")
        else:
            venv = envi.get("venv")
            if venv is not None and not (isinstance(venv, str) and venv.startswith("/")):
                _err(errors, "schema", "environment.venv must be an absolute path", "task.json")
            rp = envi.get("required_paths")
            if rp is not None and not (isinstance(rp, list) and all(
                    isinstance(p, str) and p.startswith("/") for p in rp)):
                _err(errors, "schema", "environment.required_paths must be a list of absolute "
                     "paths", "task.json")
    env = task.get("env")
    if env is not None:
        if not isinstance(env, dict):
            _err(errors, "schema", "env must be an object of strings", "task.json")
        else:
            for k, v in env.items():
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
            if ro is not None and not (isinstance(ro, list) and all(isinstance(k, str)
                                                                    for k in ro)):
                _err(errors, "schema", "result.required_outputs must be a list of strings",
                     "task.json")


def check_progress_json(p, errors, path="progress.json"):
    if not isinstance(p, dict):
        _err(errors, "schema", "progress.json must be a JSON object", path)
        return
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

def scan_tree(root, errors):
    """Check every entry for unsafe symlinks and special files (Section 2.4)."""
    for dirpath, dirnames, filenames in os.walk(root):
        for name in sorted(dirnames + filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            st = os.lstat(full)
            if stat.S_ISLNK(st.st_mode):
                target = os.readlink(full)
                if target.startswith("/"):
                    _err(errors, "symlink", "absolute symlink -> %s" % target, rel)
                elif not _inside(posixpath.join(posixpath.dirname(rel), target)) \
                        or not _link_resolves_inside(root, full):
                    _err(errors, "symlink", "symlink points outside the package -> %s" % target,
                         rel)
            elif not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
                _err(errors, "special_file", "special files are not allowed", rel)


def validate_dir(root):
    """Validate an extracted task package. Returns (errors, task_or_None)."""
    errors = []
    if not os.path.isdir(root):
        raise TaskmgrError("not_found", "not a directory: %s" % root)
    for f in REQUIRED_FILES:
        if not os.path.isfile(os.path.join(root, f)) or os.path.islink(os.path.join(root, f)):
            _err(errors, "missing_file", "required file is missing", f)
    raw = {}
    for f in REQUIRED_FILES:
        p = os.path.join(root, f)
        if os.path.isfile(p) and not os.path.islink(p):
            with open(p, "rb") as fh:
                raw[f] = fh.read().decode("utf-8", errors="replace")
            if PLACEHOLDER in raw[f]:
                lines = [i + 1 for i, line in enumerate(raw[f].splitlines()) if PLACEHOLDER in line]
                _err(errors, "placeholder", "unfilled <REQUIRED> placeholder on line(s) %s"
                     % ",".join(map(str, lines)), f)
    task = None
    if "task.json" in raw:
        try:
            task = json.loads(raw["task.json"])
        except ValueError as e:
            _err(errors, "json", "task.json is not valid JSON: %s" % e, "task.json")
        else:
            check_task_json(task, errors)
    if "progress.json" in raw:
        try:
            prog = json.loads(raw["progress.json"])
        except ValueError as e:
            _err(errors, "json", "progress.json is not valid JSON: %s" % e, "progress.json")
        else:
            check_progress_json(prog, errors)
    if isinstance(task, dict):
        argv = (task.get("entry") or {}).get("argv") if isinstance(task.get("entry"), dict) else None
        if isinstance(argv, list) and argv and isinstance(argv[0], str) and "/" in argv[0] \
                and not argv[0].startswith("/"):
            a0 = argv[0]
            if not _inside(a0):
                _err(errors, "entry", "entry argv[0] points outside the package: %s" % a0,
                     "task.json")
            else:
                real = os.path.realpath(os.path.join(root, a0))
                rroot = os.path.realpath(root)
                if not (real.startswith(rroot + os.sep) and os.path.isfile(real)):
                    _err(errors, "entry", "entry argv[0] is not a file in the package: %s" % a0,
                         "task.json")
    scan_tree(root, errors)
    return errors, (task if isinstance(task, dict) else None)


# ---------------------------------------------------------------------------
# Packing and hashing

def _sorted_entries(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            out.append((os.path.relpath(full, root).replace(os.sep, "/"), full))
    out.sort(key=lambda e: e[0])
    return out


def pack_dir(root, dest):
    """Deterministically pack a directory into a tar.gz (Section 5.1)."""
    with open(dest, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
                for rel, full in _sorted_entries(root):
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


def content_hash(root):
    """Normalized content hash over regular files and symlinks (Section 6.1)."""
    manifest = io.StringIO()
    for rel, full in _sorted_entries(root):
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
        manifest.write(json.dumps(row) + "\n")
    return hashlib.sha256(manifest.getvalue().encode()).hexdigest()


# ---------------------------------------------------------------------------
# Safe extraction

def _member_name(m):
    name = m.name
    while name.startswith("./"):
        name = name[2:]
    return name.rstrip("/")


def safe_extract(tar_path_or_fileobj, dest, max_bytes=None, skip_unsafe=False):
    """Extract a tar(.gz) refusing unsafe entries (Section 5.2).

    Returns a list of skipped entries when skip_unsafe is set; otherwise raises
    TaskmgrError on the first unsafe entry.
    """
    os.makedirs(dest, exist_ok=True)
    skipped = []
    total = 0
    seen = set()

    def reject(name, why):
        if skip_unsafe:
            skipped.append({"path": name, "reason": why})
            return
        raise TaskmgrError("unsafe_package", "unsafe archive entry %r: %s" % (name, why))

    links = []
    try:
        if isinstance(tar_path_or_fileobj, (str, os.PathLike)):
            tar = tarfile.open(tar_path_or_fileobj, mode="r:*")
        else:
            tar = tarfile.open(fileobj=tar_path_or_fileobj, mode="r|*")
    except (tarfile.TarError, EOFError, OSError, gzip.BadGzipFile) as e:
        raise TaskmgrError("bad_archive", "cannot read archive: %s" % e)
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
            if name in seen:
                reject(m.name, "duplicate entry")
                continue
            target = os.path.join(dest, *parts)
            # Never write through a symlink created by an earlier entry.
            cur = dest
            bad_parent = False
            for p in parts[:-1]:
                cur = os.path.join(cur, p)
                if os.path.islink(cur) or (os.path.exists(cur) and not os.path.isdir(cur)):
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
                os.symlink(ln, target)
                links.append((m.name, target))
            elif m.isdir():
                os.makedirs(target, exist_ok=True)
                os.chmod(target, (m.mode & 0o777) | 0o700)
            elif m.isreg():
                total += m.size
                if max_bytes is not None and total > max_bytes:
                    raise TaskmgrError("package_too_large",
                                       "extracted size exceeds max_package_bytes (%d)" % max_bytes)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                src = tar.extractfile(m)
                if src is None:
                    reject(m.name, "unreadable regular file")
                    continue
                with open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                os.chmod(target, (m.mode & 0o777) | 0o600)
            else:
                reject(m.name, "unsupported entry type")
                continue
            seen.add(name)
    except (tarfile.TarError, EOFError, OSError, gzip.BadGzipFile) as e:
        raise TaskmgrError("bad_archive", "cannot read archive: %s" % e)
    finally:
        tar.close()
    # Chains such as x -> . and y -> x/.. pass the lexical check; check physical targets.
    for name, target in links:
        if not _link_resolves_inside(dest, target):
            os.unlink(target)
            reject(name, "symlink resolves outside the package")
    return skipped


def package_root(extracted):
    """Accept archives with a single top-level directory wrapping the package."""
    if os.path.isfile(os.path.join(extracted, "task.json")):
        return extracted
    entries = os.listdir(extracted)
    if len(entries) == 1:
        only = os.path.join(extracted, entries[0])
        if os.path.isdir(only) and not os.path.islink(only) \
                and os.path.isfile(os.path.join(only, "task.json")):
            return only
    return extracted


def add_tree_to_tar(tar, root, arcroot):
    """Add a directory tree to an open tar without following symlinks; skip special files."""
    try:
        entries = [("", root)] + _sorted_entries(root)
    except OSError:
        return
    for rel, full in entries:
        if posixpath.basename(rel).startswith(".tmp-"):
            continue  # in-flight atomic writes
        arc = arcroot if not rel else arcroot + "/" + rel
        try:
            st = os.lstat(full)
            if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)
                    or stat.S_ISLNK(st.st_mode)):
                continue
            ti = tar.gettarinfo(full, arcname=arc)
            data = None
            if ti.isreg() or ti.islnk():
                with open(full, "rb") as f:  # a running task may still be writing it
                    data = f.read()
                ti.type = tarfile.REGTYPE  # never emit hard links
                ti.linkname = ""
                ti.size = len(data)
        except OSError:
            continue  # vanished while walking a running attempt
        tar.addfile(ti, io.BytesIO(data) if data is not None else None)
