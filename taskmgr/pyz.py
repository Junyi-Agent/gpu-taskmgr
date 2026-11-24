"""Single-file zipapps: `task.pyz` (local CLI) and `taskmgr-remote.pyz` (server side),
built by `task build pyz [--dest DIR]` (build()).

Each archive holds the package (taskmgr/ without tests/ and __pycache__) plus, at the top,
the code version, pyproject.toml and the commit. The code version is a hash of the files of
taskmgr/ that change what runs (code_hash), so it is the same for a checkout and for every
archive built from it, and an edit of the docs does not make servers restart.
`task create host` uploads a taskmgr-remote.pyz built here to the server (see hostsetup); it
never builds one itself.
"""

import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import zipapp
import zipfile

from .common import (
    CODE_ROOT,
    COMMIT_FILE,
    VERSION_FILE,
    code_is_zipapp,
    copy_package_tree,
    read_code_file,
)

INTERPRETER = "/usr/bin/env python3"
# Package parts that are not put into the archives.
EXCLUDE = ("__pycache__", "tests", ".DS_Store", "*.pyc")
# Top-level parts of taskmgr/ outside the code version: documentation and samples that no
# code reads (deploy/ holds examples to copy by hand; the README files are docs).
NOT_CODE = ("deploy", "examples")
LOCAL_PYZ = "task.pyz"
REMOTE_PYZ = "taskmgr-remote.pyz"
TARGETS = ((LOCAL_PYZ, "taskmgr.taskmgr"), (REMOTE_PYZ, "taskmgr.remote"))
# zipapp's own `main=` entry drops main()'s return value (every exit code would be 0).
# zipimport reads an archive by path, again for every module, with offsets from the first
# read: if the file at that path is replaced while a process starts, its imports fail. On a
# server the path is a symlink that `task create host` switches to a new, never modified file;
# importing from the resolved path makes each process read one file only.
MAIN_PY = """import os
import sys

sys.path[0] = os.path.realpath(sys.path[0])

from %s import main  # noqa: E402

sys.exit(main())
"""


def commit() -> str | None:
    """The git commit of the running code: HEAD of the checkout, or the one a .pyz was built
    from; None when unknown (no git, not a repository)."""
    if code_is_zipapp():
        return (read_code_file(COMMIT_FILE) or "").strip() or None

    try:
        p = subprocess.run(
            ["git", "-C", CODE_ROOT, "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (p.stdout.strip() or None) if p.returncode == 0 else None


def _in_code_version(rel: str) -> bool:
    """Whether taskmgr/<rel> counts for the code version: every .py file, and all of
    template/ (`task create package` copies it into new task packages)."""
    top = rel.split("/", 1)[0]
    if top in NOT_CODE:
        return False
    return rel.endswith(".py") or top == "template"


def code_hash(pkg: str) -> str:
    """The code version of a copy of the package at pkg (a taskmgr/ directory)."""
    rows: list[str] = []
    for dirpath, dirnames, filenames in os.walk(pkg):
        dirnames.sort()
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, pkg).replace(os.sep, "/")
            if not _in_code_version(rel) or not os.path.isfile(full):
                continue
            with open(full, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
            executable = bool(os.stat(full).st_mode & 0o111)
            rows.append(json.dumps([rel, executable, digest]))
    return hashlib.sha256("".join(r + "\n" for r in sorted(rows)).encode()).hexdigest()


def stage(dest: str) -> tuple[str, int]:
    """Lay out an archive's content in the new directory dest: (code version, files)."""
    files = copy_package_tree("", os.path.join(dest, "taskmgr"), EXCLUDE)
    version = code_hash(os.path.join(dest, "taskmgr"))
    extras = {VERSION_FILE: version + "\n"}

    pyproject = read_code_file("pyproject.toml")
    if pyproject is not None:
        extras["pyproject.toml"] = pyproject

    sha = commit()
    if sha is not None:
        extras[COMMIT_FILE] = sha + "\n"

    for name, text in extras.items():
        with open(os.path.join(dest, name), "w") as f:
            _ = f.write(text)
    return version, files


def code_version() -> str:
    """The code version of the running code (checkout or archive)."""
    with tempfile.TemporaryDirectory(prefix="taskmgr-version-") as tmp:
        pkg = os.path.join(tmp, "taskmgr")
        _ = copy_package_tree("", pkg, EXCLUDE)
        return code_hash(pkg)


def write_archive(staged: str, path: str, module: str) -> None:
    """Pack a stage() directory into an executable zipapp at path that runs module.main."""
    with open(os.path.join(staged, "__main__.py"), "w") as f:
        _ = f.write(MAIN_PY % module)
    zipapp.create_archive(staged, path, interpreter=INTERPRETER, compressed=True)
    os.chmod(path, 0o755)


@dataclasses.dataclass
class Built:
    code_version: str
    commit: str | None
    files: int  # files under taskmgr/ in each archive
    outputs: list[tuple[str, int]]  # (absolute path, size in bytes)


def build(out_dir: str) -> Built:
    """Write the archives into out_dir (created if needed; existing files are replaced)."""
    os.makedirs(out_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="taskmgr-pyz-") as tmp:
        staged = os.path.join(tmp, "stage")
        version, files = stage(staged)

        outputs: list[tuple[str, int]] = []
        for name, module in TARGETS:
            target = os.path.join(out_dir, name)
            part = os.path.join(tmp, name)
            write_archive(staged, part, module)
            _ = shutil.move(part, target + ".tmp")
            os.replace(target + ".tmp", target)
            outputs.append((os.path.abspath(target), os.path.getsize(target)))

    return Built(version, commit(), files, outputs)


def default_remote_pyz() -> str:
    """Where `task create host` looks for taskmgr-remote.pyz when --pyz is not given.

    From a checkout: <checkout>/dist/taskmgr-remote.pyz (where `task build pyz` writes).
    From task.pyz: taskmgr-remote.pyz next to the archive (symlinks resolved).
    """
    if code_is_zipapp():
        return os.path.join(os.path.dirname(os.path.realpath(CODE_ROOT)), REMOTE_PYZ)
    return os.path.join(CODE_ROOT, "dist", REMOTE_PYZ)


def remote_archive_info(path: str) -> tuple[str, int]:
    """(code version, files under taskmgr/) of a taskmgr-remote.pyz built by this module.

    Raises ValueError when path is not such an archive (OSError when it cannot be read).
    """
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            main = z.read("__main__.py").decode("utf-8") if "__main__.py" in names else ""
            version = z.read(VERSION_FILE).decode("utf-8").strip() if VERSION_FILE in names else ""
    except zipfile.BadZipFile:
        raise ValueError("%s is not a zip archive" % path) from None

    if "from taskmgr.remote import main" not in main:
        raise ValueError("%s is not a taskmgr-remote.pyz (wrong entry point)" % path)
    if not version:
        raise ValueError("%s has no %s" % (path, VERSION_FILE))

    files = sum(1 for n in names if n.startswith("taskmgr/") and not n.endswith("/"))
    return version, files
