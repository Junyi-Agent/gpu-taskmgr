"""Linux /proc helpers: boot id, process start time, process-group membership, and the
signal dispositions a started program gets."""

import os
import signal


class ProcUnavailable(Exception):
    """/proc cannot be read, so liveness cannot be determined."""


def boot_id() -> str:
    # TASKMGR_BOOT_ID_FILE: test-only override to simulate a machine reboot.
    path = os.environ.get("TASKMGR_BOOT_ID_FILE") or "/proc/sys/kernel/random/boot_id"
    with open(path) as f:
        return f.read().strip()


def read_stat(pid: int) -> tuple[str, int, int] | None:
    """Return (state, pgid, starttime) from /proc/<pid>/stat, or None if gone."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            data = f.read().decode(errors="replace")
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError as e:
        raise ProcUnavailable(str(e)) from e
    rest = data[data.rfind(")") + 2 :].split()
    # rest[0] is field 3 (state); field 5 is pgrp; field 22 is starttime.
    return rest[0], int(rest[2]), int(rest[19])


def start_time(pid: int) -> int | None:
    st = read_stat(pid)
    return None if st is None else st[2]


def job_tag(pid: int) -> str | None:
    """TASKMGR_JOBID from /proc/<pid>/environ (the environment the process was exec'd
    with); None when it is not set or the environ cannot be read."""
    try:
        with open("/proc/%d/environ" % pid, "rb") as f:
            data = f.read()
    except OSError:
        return None
    for item in data.split(b"\0"):
        if item.startswith(b"TASKMGR_JOBID="):
            return item[len(b"TASKMGR_JOBID=") :].decode(errors="replace")
    return None


def group_members(pgid: int, min_start: int, jobid: str | None = None) -> list[int]:
    """PIDs of live (non-zombie) processes in process group pgid started at/after min_start.

    min_start is the start time of the group leader (pid pgid). With `jobid`, a process
    whose environment carries a different TASKMGR_JOBID is not a member: after the group
    is gone, its pid can become the pgid of an unrelated new session (another job's task
    calls setsid too). A process without a readable tag (environ unreadable because it is
    not dumpable or belongs to another user, or the task cleared its environment) still
    counts, unless the pid pgid is now held by a process that is not the recorded leader;
    the kernel does not reuse a pid while a process group of that id exists, so then the
    whole group is a new one. Counting untagged processes keeps a job from being finished
    while processes it started may still run (and hold its GPUs).
    """
    try:
        names = os.listdir("/proc")
    except OSError as e:
        raise ProcUnavailable(str(e)) from e
    if not os.path.exists("/proc/self/stat"):
        raise ProcUnavailable("/proc is not mounted")
    pids: list[int] = []
    for n in names:
        if not n.isdigit():
            continue
        st = read_stat(int(n))
        if st is None:
            continue
        state, pg, start = st
        if pg == pgid and start >= min_start and state not in ("Z", "X", "x"):
            pids.append(int(n))
    if jobid is None or not pids:
        return sorted(pids)
    leader = read_stat(pgid)
    foreign_leader = leader is not None and leader[2] != min_start
    members: list[int] = []
    for pid in pids:
        tag = job_tag(pid)
        if tag == jobid or (tag is None and not foreign_leader):
            members.append(pid)
    return sorted(members)


def default_signals() -> None:
    """Give every ignored signal its default action back and clear the signal mask.

    Ignored dispositions survive execve: Python ignores SIGPIPE and SIGXFSZ, the shim
    ignores SIGHUP, so a program started from here would inherit those (e.g. `yes | head`
    would print "Broken pipe" errors). Called in a forked child right before execve.
    """
    for sig in signal.valid_signals():
        try:
            if signal.getsignal(sig) == signal.SIG_IGN:
                _ = signal.signal(sig, signal.SIG_DFL)
        except (OSError, ValueError):
            pass
    _ = signal.pthread_sigmask(signal.SIG_SETMASK, [])
