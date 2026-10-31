"""`task host add` step old_code: remove what older versions installed outside remote_root.

Everything about the layouts of older versions is here (and in hostprobe.legacy, the probe
key "legacy"); delete both once no server has them. Older layouts, all under the server's
$HOME:
  ~/taskmgr/              the code tree before the zipapp; the old installer wrote
                          LEGACY_MARKER into it. A tree without the marker, or a git
                          checkout, is someone's own directory and is never touched.
  ~/taskmgr.old-<time>/   trees the old installer replaced
  ~/taskmgr-remote.pyz    the first zipapp layout: a symlink into ~/.taskmgr-remote/<v>.pyz
                          (same basename as hostsetup.CODE_FILE, but in $HOME, not in
                          remote_root)
  ~/taskmgr-root/         the default remote_root of older versions (data; never deleted,
                          only reported by step ssh)
"""

import shlex
from typing import Protocol, TypedDict

from .common import TaskmgrError
from .prompt import Ui

LEGACY_DIR = "taskmgr"
LEGACY_MARKER = ".taskmgr-code-version"
BACKUP_PREFIX = "taskmgr.old-"
OLD_CODE_LINK = "taskmgr-remote.pyz"
OLD_CODE_STORE = ".taskmgr-remote"
OLD_REMOTE_ROOT = "taskmgr-root"


class LegacyProbe(TypedDict, total=False):
    """hostprobe.legacy: what of the older layouts the server has."""

    tree: bool  # ~/taskmgr is a marked code tree (and no git checkout)
    backups: list[str]  # ~/taskmgr.old-* directories (no git checkouts)
    link: bool  # ~/taskmgr-remote.pyz is a symlink (and not the current code)
    files: list[str]  # <version>.pyz files in ~/.taskmgr-remote


def probe_params() -> dict[str, str]:
    """The "legacy" part of the probe parameters (names under $HOME)."""
    return {
        "tree": LEGACY_DIR,
        "marker": LEGACY_MARKER,
        "backup_prefix": BACKUP_PREFIX,
        "link": OLD_CODE_LINK,
        "store": OLD_CODE_STORE,
    }


class Setup(Protocol):
    """The parts of hostsetup.HostSetup that the step uses."""

    ui: Ui

    def legacy_probe(self) -> LegacyProbe: ...
    def sh(self, script: str, stdin: bytes | None = None, timeout: float = 300) -> str: ...
    def p_home(self) -> str: ...
    def root(self) -> str: ...
    def code_path(self) -> str: ...
    def host_name(self) -> str: ...


def step_old_code(s: Setup) -> tuple[str, str]:
    p = s.legacy_probe()
    home = s.p_home()
    trees = ["%s/%s" % (home, LEGACY_DIR)] if p.get("tree") else []
    trees += ["%s/%s" % (home, d) for d in p.get("backups") or []]
    files = ["%s/%s" % (home, OLD_CODE_LINK)] if p.get("link") else []
    old_store = "%s/%s" % (home, OLD_CODE_STORE)
    files += ["%s/%s" % (old_store, n) for n in p.get("files") or []]
    if not trees and not files:
        return "ok", "no code of an older version"
    s.ui.info(
        "the service runs %s; this code from older versions is not used any more:" % s.code_path()
    )
    for t in trees + files:
        s.ui.info("  " + t)
    s.ui.confirm(
        "delete %s. Machine lists on other computers that still name " % ", ".join(trees + files)
        + "it stop working; run `task host add %s` there" % s.host_name()
    )
    # a git checkout is never deleted, even if it appeared since the probe
    script = "".join(
        "if [ -e %s ]; then echo %s is a git checkout, not deleted >&2; exit 1; fi\n"
        % (shlex.quote(t + "/.git"), shlex.quote(t))
        for t in trees
    )
    script += "rm -rf -- %s\n" % " ".join(shlex.quote(t) for t in trees + files)
    if old_store != s.root():
        script += "rmdir %s 2>/dev/null || true\n" % shlex.quote(old_store)
    _ = s.sh(script)
    left = s.legacy_probe()
    if left.get("tree") or left.get("backups") or left.get("link") or left.get("files"):
        raise TaskmgrError("old_code", "still there after rm -rf: %s" % ", ".join(trees + files))
    return "changed", "deleted %s" % ", ".join(trees + files)
