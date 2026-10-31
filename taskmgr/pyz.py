"""Single-file zipapps: `task.pyz` (local CLI) and `taskmgr-remote.pyz` (server side).

    python3 -m taskmgr.pyz [-o DIR]

Each archive holds the package (taskmgr/ without tests/ and __pycache__) plus, at the top,
the code version, pyproject.toml and the commit. The code version is the content hash of
taskmgr/, so it is the same for a checkout and for every archive built from it.
`task host add` installs taskmgr-remote.pyz on the server (see hostsetup).
"""

import argparse
import dataclasses
import os
import shutil
import subprocess
import sys
import tempfile
import zipapp
from typing import cast

from . import package
from .common import CODE_ROOT, code_is_zipapp, copy_package_tree, read_code_file

INTERPRETER = "/usr/bin/env python3"
VERSION_FILE = ".taskmgr-code-version"
COMMIT_FILE = ".taskmgr-commit"
# Package parts that are not put into the archives.
EXCLUDE = ("__pycache__", "tests", ".DS_Store", "*.pyc")
LOCAL_PYZ = "task.pyz"
REMOTE_PYZ = "taskmgr-remote.pyz"
TARGETS = ((LOCAL_PYZ, "taskmgr.taskmgr"), (REMOTE_PYZ, "taskmgr.remote"))
# zipapp's own `main=` entry drops main()'s return value (every exit code would be 0).
# zipimport reads an archive by path, again for every module, with offsets from the first
# read: if the file at that path is replaced while a process starts, its imports fail. On a
# server the path is a symlink that `task host add` switches to a new, never modified file;
# importing from the resolved path makes each process read one file only.
MAIN_PY = """import os
import sys

sys.path[0] = os.path.realpath(sys.path[0])

from %s import main  # noqa: E402

sys.exit(main())
"""


def _commit() -> str | None:
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


def stage(dest: str) -> tuple[str, int]:
    """Lay out an archive's content in the new directory dest: (code version, files)."""
    files = copy_package_tree("", os.path.join(dest, "taskmgr"), EXCLUDE)
    version = package.content_hash(dest)  # only taskmgr/ exists at this point
    extras = {VERSION_FILE: version + "\n"}
    pyproject = read_code_file("pyproject.toml")
    if pyproject is not None:
        extras["pyproject.toml"] = pyproject
    commit = _commit()
    if commit is not None:
        extras[COMMIT_FILE] = commit + "\n"
    for name, text in extras.items():
        with open(os.path.join(dest, name), "w") as f:
            _ = f.write(text)
    return version, files


def code_version() -> str:
    """The code version of the running code (checkout or archive)."""
    with tempfile.TemporaryDirectory(prefix="taskmgr-version-") as tmp:
        files = os.path.join(tmp, "stage")
        _ = copy_package_tree("", os.path.join(files, "taskmgr"), EXCLUDE)
        return package.content_hash(files)


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


def build(out_dir: str, targets: tuple[tuple[str, str], ...] = TARGETS) -> Built:
    """Write the archives into out_dir (created if needed; existing files are replaced)."""
    os.makedirs(out_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="taskmgr-pyz-") as tmp:
        staged = os.path.join(tmp, "stage")
        version, files = stage(staged)
        outputs: list[tuple[str, int]] = []
        for name, module in targets:
            target = os.path.join(out_dir, name)
            part = os.path.join(tmp, name)
            write_archive(staged, part, module)
            _ = shutil.move(part, target + ".tmp")
            os.replace(target + ".tmp", target)
            outputs.append((os.path.abspath(target), os.path.getsize(target)))
    return Built(version, _commit(), files, outputs)


def build_remote(out_dir: str) -> Built:
    """Only taskmgr-remote.pyz (what `task host add` uploads)."""
    return build(out_dir, tuple(t for t in TARGETS if t[0] == REMOTE_PYZ))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m taskmgr.pyz",
        description="Build task.pyz and taskmgr-remote.pyz (single-file zipapps).",
    )
    _ = ap.add_argument("-o", "--output", default="dist", help="output directory (default dist)")
    args = ap.parse_args(argv)
    out_dir = cast(str, args.output)
    try:
        info = build(out_dir)
    except OSError as e:
        print("build failed: %s" % e, file=sys.stderr)
        return 2
    for path, size in info.outputs:
        print("%s  %d KB" % (path, size // 1024))
    print("code_version %s  commit %s" % (info.code_version, info.commit or "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
