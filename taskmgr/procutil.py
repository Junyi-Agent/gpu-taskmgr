"""Linux /proc helpers: boot id, process start time, process-group membership."""

import os


class ProcUnavailable(Exception):
    """/proc cannot be read, so liveness cannot be determined."""


def boot_id():
    # TASKMGR_BOOT_ID_FILE: test-only override to simulate a machine reboot.
    path = os.environ.get("TASKMGR_BOOT_ID_FILE") or "/proc/sys/kernel/random/boot_id"
    with open(path) as f:
        return f.read().strip()


def read_stat(pid):
    """Return (state, pgid, starttime) from /proc/<pid>/stat, or None if gone."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            data = f.read().decode(errors="replace")
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError as e:
        raise ProcUnavailable(str(e))
    rest = data[data.rfind(")") + 2 :].split()
    # rest[0] is field 3 (state); field 5 is pgrp; field 22 is starttime.
    return rest[0], int(rest[2]), int(rest[19])


def start_time(pid):
    st = read_stat(pid)
    return None if st is None else st[2]


def group_members(pgid, min_start):
    """PIDs of live (non-zombie) processes in process group pgid started at/after min_start."""
    try:
        names = os.listdir("/proc")
    except OSError as e:
        raise ProcUnavailable(str(e))
    if not os.path.exists("/proc/self/stat"):
        raise ProcUnavailable("/proc is not mounted")
    pids = []
    for n in names:
        if not n.isdigit():
            continue
        st = read_stat(int(n))
        if st is None:
            continue
        state, pg, start = st
        if pg == pgid and start >= min_start and state not in ("Z", "X", "x"):
            pids.append(int(n))
    return sorted(pids)
