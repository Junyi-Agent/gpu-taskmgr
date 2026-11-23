"""The parts of `task create host` that run on the server, as `python3 -c TEXT PARAMS`.

hostsetup reads this file's text (common.read_code_file) and runs it with the server's
python3; nothing imports it. It must run on any Python 3, so that the probe can report an
old one: no f-strings, no variable annotations, annotations only as strings. PARAMS is a
JSON object; its "mode" selects:
  probe  read-only; prints one JSON object (hostsetup.Probe) about the paths in PARAMS
  lock   takes the per-user setup lock, prints "locked PATH" (or "busy PATH PID"), and
         holds the lock until stdin is closed (the end of the `task create host` run)
  prune  removes code archives from a store that neither PARAMS["keep"] names nor a
         running process uses; prints {"removed": [...]}
"""

import fcntl
import json
import os
import pwd
import re
import shlex
import subprocess
import sys
import time
from typing import cast


def run(argv: "list[str]") -> "tuple[int, str, str]":
    try:
        p = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
    except OSError as e:
        return 127, "", str(e)
    try:
        out, err = p.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        p.kill()
        out, _ = p.communicate()
        return 124, out.strip(), "timed out"
    return p.returncode, out.strip(), err.strip()


def read(path: str) -> "str | None":
    try:
        with open(path) as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def zip_text(path: str, name: str) -> "str | None":
    try:
        import zipfile

        with zipfile.ZipFile(path) as z:
            return z.read(name).decode()
    except Exception:  # noqa: BLE001 - any failure means "no readable version"
        return None


def mtime(path: str) -> "float | None":
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def started(pid: int) -> "float | None":
    """Wall-clock start of pid: now - (uptime - start ticks), precise to a clock tick."""
    try:
        with open("/proc/%d/stat" % pid) as f:
            data = f.read()
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
        ticks = float(data[data.rfind(")") + 2 :].split()[19])
    except (OSError, ValueError, IndexError):
        return None
    return time.time() - (uptime - ticks / os.sysconf("SC_CLK_TCK"))


def resolve(home: str, spec: str) -> str:
    """A remote_root as given (may start with ~ or be relative to HOME), made absolute."""
    if spec == "~" or spec.startswith("~/"):
        path = home + spec[1:]
    elif os.path.isabs(spec):
        path = spec
    else:
        path = os.path.join(home, spec)
    return os.path.normpath(path)


def unit_root(unit: "str | None", home: str) -> "str | None":
    """The --root of the installed unit's ExecStart (%h and %% expanded), or None."""
    for line in (unit or "").splitlines():
        if not line.startswith("ExecStart="):
            continue
        try:
            argv = shlex.split(line[len("ExecStart=") :])
        except ValueError:
            return None
        if "--root" in argv[:-1]:
            arg = argv[argv.index("--root") + 1].replace("%%", "\0").replace("$$", "$")
            return os.path.normpath(arg.replace("%h", home).replace("\0", "%"))
    return None


def has_git(path: str) -> bool:
    return os.path.lexists(os.path.join(path, ".git"))


def legacy(home: str, code: str, p: "dict[str, str]") -> "dict[str, object]":
    """Code that older versions installed outside remote_root (see hostlegacy)."""
    tree = os.path.join(home, p["tree"])
    link = os.path.join(home, p["link"])
    store = os.path.join(home, p["store"])
    names = sorted(os.listdir(home))
    return {
        # only a tree the old installer marked, and never a git checkout
        "tree": os.path.isdir(tree)
        and not os.path.islink(tree)
        and os.path.isfile(os.path.join(tree, p["marker"]))
        and not has_git(tree),
        "backups": [
            n
            for n in names
            if n.startswith(p["backup_prefix"])
            and os.path.isdir(os.path.join(home, n))
            and not os.path.islink(os.path.join(home, n))
            and not has_git(os.path.join(home, n))
        ],
        # the first zipapp layout: ~/taskmgr-remote.pyz -> .taskmgr-remote/<v>.pyz
        "link": os.path.islink(link) and link != code,
        "files": sorted(
            n
            for n in (os.listdir(store) if os.path.isdir(store) else [])
            if re.match(r"^[0-9a-f]{16}\.pyz(\.new-.*)?$", n)
            and os.path.isfile(os.path.join(store, n))
        ),
    }


def daemon_lock(root: str) -> "tuple[bool, int | None]":
    """(held, pid) of <root>/locks/daemon.lock."""
    try:
        fd = os.open(os.path.join(root, "locks", "daemon.lock"), os.O_RDONLY)
    except OSError:
        return False, None
    held = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            held = True
        text = os.read(fd, 64).decode(errors="replace").strip()
    finally:
        os.close(fd)
    return held, (int(text) if held and text.isdigit() else None)


def probe(params: "dict[str, object]") -> None:
    home = os.path.expanduser("~")
    user = pwd.getpwuid(os.getuid()).pw_name
    root = resolve(home, cast(str, params["root"]))
    code = os.path.join(root, cast(str, params["code"]))
    store = os.path.join(root, cast(str, params["store"]))
    unit_name = cast(str, params["unit_name"])
    unit_file = os.path.join(home, cast(str, params["unit"]))
    unit = read(unit_file)
    # other directories that may hold taskmgr data: switching away from them hides their jobs
    uroot = unit_root(unit, home)
    others = [resolve(home, s) for s in cast("list[str]", params["data_roots"])]
    others += [uroot] if uroot else []
    out = cast(
        "dict[str, object]",
        {
            "python": sys.executable,
            "python_version": list(sys.version_info[:2]),
            "uname": os.uname()[0],
            "home": home,
            "user": user,
            "root": root,
            "root_exists": os.path.isdir(root),
            "config_exists": os.path.lexists(os.path.join(root, "config.json")),
            "unit_root": uroot,
            "data_roots": sorted(
                {d for d in others if d != root and os.path.exists(os.path.join(d, "config.json"))}
            ),
            "code_exists": os.path.lexists(code),
            "code_store": sorted(os.listdir(store)) if os.path.isdir(store) else [],
            "unit": unit,
            "unit_mtime": mtime(unit_file),
            "legacy": legacy(home, code, cast("dict[str, str]", params["legacy"])),
        },
    )
    version = zip_text(code, cast(str, params["version_file"]))
    out["code_version"] = version.strip() if version is not None else None
    try:
        # the link is made anew at every switch, so its own mtime is the time of the install
        out["code_version_mtime"] = os.lstat(code).st_mtime
    except OSError:
        out["code_version_mtime"] = None
    existing = root
    while not os.path.exists(existing):
        existing = os.path.dirname(existing)
    rc, fs, _ = run(["stat", "-f", "-c", "%T", existing])
    out["fs_path"] = existing
    out["fs_type"] = fs if rc == 0 else None
    rc, _, err = run(["systemctl", "--user", "show-environment"])
    out["systemd_ok"] = rc == 0
    out["systemd_error"] = err
    out["active"] = run(["systemctl", "--user", "is-active", unit_name])[1]
    out["enabled"] = run(["systemctl", "--user", "is-enabled", unit_name])[1]
    pid = run(["systemctl", "--user", "show", unit_name, "-p", "MainPID"])[1].split("=", 1)[-1]
    main_pid = int(pid) if pid.isdigit() else 0
    out["main_pid"] = main_pid
    out["main_started"] = started(main_pid) if main_pid else None
    rc, linger, err = run(["loginctl", "show-user", user, "-p", "Linger"])
    out["linger"] = linger.split("=", 1)[1] if rc == 0 and linger.startswith("Linger=") else None
    out["linger_error"] = err
    out["daemon_running"], out["daemon_pid"] = daemon_lock(root)
    print(json.dumps(out))


def hold_lock() -> None:
    """One `task create host` at a time per server user: the lock file is on local storage
    ($XDG_RUNTIME_DIR, else /tmp), not in remote_root, which may not exist yet."""
    d = os.environ.get("XDG_RUNTIME_DIR") or ""
    if not os.path.isdir(d):
        d = "/tmp"  # noqa: S108 - per-user name, O_NOFOLLOW and an owner check below
    path = os.path.join(d, "taskmgr-hostsetup-%d.lock" % os.getuid())
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    if os.fstat(fd).st_uid != os.getuid():
        print("error %s belongs to another user" % path)
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        holder = os.pread(fd, 32, 0).decode(errors="replace").strip()
        print("busy %s %s" % (path, holder or "?"))
        return
    os.ftruncate(fd, 0)
    _ = os.pwrite(fd, str(os.getpid()).encode(), 0)
    print("locked %s" % path)
    _ = sys.stdout.flush()
    _ = sys.stdin.read()  # until `task create host` closes the pipe or its connection ends


def used_paths() -> "set[str]":
    """Resolved paths named on the command line or PYTHONPATH of this user's processes.

    The daemon gives each shim its code archive (resolved) on PYTHONPATH; a running job
    keeps importing from it."""
    used = cast("set[str]", set())
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                args = f.read().split(b"\0")
            with open("/proc/%s/environ" % pid, "rb") as f:
                env = f.read().split(b"\0")
        except OSError:
            continue
        for e in env:
            if e.startswith(b"PYTHONPATH="):
                args += e[len(b"PYTHONPATH=") :].split(b":")
        for a in args:
            if a.startswith(b"/"):
                used.add(os.path.realpath(a.decode(errors="replace")))
    return used


def prune(params: "dict[str, object]") -> None:
    store = cast(str, params["store"])
    keep = cast("list[str]", params["keep"])
    used = used_paths()
    removed = cast("list[str]", [])
    for name in sorted(os.listdir(store)):
        path = os.path.join(store, name)
        if name in keep or ".new-" in name or os.path.realpath(path) in used:
            continue
        os.unlink(path)
        removed.append(name)
    print(json.dumps({"removed": removed}))


def main() -> None:
    params = cast("dict[str, object]", json.loads(sys.argv[1]))
    mode = params.get("mode")
    if mode == "lock":
        hold_lock()
    elif mode == "prune":
        prune(params)
    else:
        probe(params)


if __name__ == "__main__":
    main()
