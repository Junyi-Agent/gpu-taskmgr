"""GPU discovery via nvidia-smi and resolution of task GPU declarations."""

import os
import pwd
import shutil
import subprocess
from collections.abc import Iterable, Sequence

from . import procutil
from .common import TaskmgrError
from .records import GpuDecl, GpuHolder

# The command line of a GPU holder is cut to this many characters.
HOLDER_COMMAND_MAX = 200


def nvidia_smi_path() -> str:
    # TASKMGR_NVIDIA_SMI: test-only override pointing at a fake nvidia-smi script.
    return (
        os.environ.get("TASKMGR_NVIDIA_SMI") or shutil.which("nvidia-smi") or "/usr/bin/nvidia-smi"
    )


def _smi(query: str) -> list[list[str]]:
    """Run `nvidia-smi <query> --format=csv,noheader,nounits`: the rows, split on commas."""
    cmd = [nvidia_smi_path(), query, "--format=csv,noheader,nounits"]
    try:
        out = subprocess.run(
            cmd,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=60,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as e:
        raise TaskmgrError("nvidia_smi_failed", "cannot query GPUs with nvidia-smi: %s" % e) from e
    return [
        [p.strip() for p in line.split(",")]
        for line in out.decode(errors="replace").splitlines()
        if line.strip()
    ]


def _int(text: str) -> int | None:
    try:
        return int(float(text))
    except ValueError:  # "[N/A]", "[Not Supported]", ...
        return None


def query_gpus() -> list[tuple[int, str]]:
    """Return [(index, uuid), ...] in nvidia-smi index (PCI bus) order."""
    gpus: list[tuple[int, str]] = []
    for parts in _smi("--query-gpu=index,uuid"):
        if len(parts) < 2:
            continue
        try:
            gpus.append((int(parts[0]), parts[1]))
        except ValueError:
            continue
    return gpus


def resolve_declarations(decls: Sequence[object]) -> list[GpuDecl]:
    """Resolve declared GPU indices/UUIDs to [{"declared": d, "uuid": u}, ...]."""
    if not decls:
        return []
    available = query_gpus()
    by_index = dict(available)
    uuids = {u for _, u in available}
    out: list[GpuDecl] = []
    errors: list[str] = []
    for d in decls:
        if isinstance(d, str) and d.startswith("MIG-"):
            errors.append("MIG instances are not supported: %s" % d)
        elif isinstance(d, bool) or not isinstance(d, (int, str)):
            errors.append("invalid GPU declaration: %r" % (d,))
        elif isinstance(d, int):
            if d not in by_index:
                errors.append("GPU index %d does not exist on this machine" % d)
            else:
                out.append({"declared": d, "uuid": by_index[d]})
        elif not d.startswith("GPU-"):
            errors.append("GPU string must be a full UUID starting with GPU-: %s" % d)
        elif d not in uuids:
            errors.append("GPU %s does not exist on this machine" % d)
        else:
            out.append({"declared": d, "uuid": d})
    resolved = [g["uuid"] for g in out]
    if len(set(resolved)) != len(resolved):
        errors.append("gpus resolves to duplicate devices: %s" % resolved)
    if errors:
        raise TaskmgrError("invalid_gpus", "; ".join(errors), details=errors)
    return out


def query_compute_apps() -> list[tuple[str, int, int | None]]:
    """The processes that hold a CUDA context: [(gpu uuid, pid, used memory MiB or None)].

    The pids are those of the server's pid namespace; a process in another namespace (a
    container) has a pid that /proc here may not show."""
    apps: list[tuple[str, int, int | None]] = []
    for parts in _smi("--query-compute-apps=gpu_uuid,pid,used_memory"):
        if len(parts) < 2 or not parts[0].startswith("GPU-"):
            continue  # also "No running processes found"
        pid = _int(parts[1])
        if pid is not None:
            apps.append((parts[0], pid, _int(parts[2]) if len(parts) > 2 else None))
    return apps


def process_owner_and_command(pid: int) -> tuple[str | None, str | None]:
    """(user name, command line cut to HOLDER_COMMAND_MAX characters) from /proc, or None
    for what cannot be read (gone, another pid namespace, hidepid)."""
    user: str | None = None
    command: str | None = None
    try:
        uid = os.stat("/proc/%d" % pid).st_uid
        try:
            user = pwd.getpwuid(uid).pw_name
        except KeyError:
            user = str(uid)
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            raw = f.read()
        args = [a.decode(errors="replace") for a in raw.split(b"\0") if a]
        command = " ".join(args) or None
    except OSError:
        pass
    if command is not None and len(command) > HOLDER_COMMAND_MAX:
        command = command[: HOLDER_COMMAND_MAX - 3] + "..."
    return user, command


def gpu_holders(uuids: Iterable[str] | None = None) -> list[GpuHolder]:
    """Who holds a CUDA context on the given GPUs (every GPU for None), as nvidia-smi and
    /proc report it now, sorted by GPU index and pid. A holder that taskmgr started has
    the jobid of its job in `taskmgr_jobid` (when its environment can be read)."""
    index = {u: i for i, u in query_gpus()}
    wanted = set(index) if uuids is None else set(uuids)
    out: list[GpuHolder] = []
    for uuid, pid, mib in query_compute_apps():
        if uuid not in wanted:
            continue
        user, command = process_owner_and_command(pid)
        h: GpuHolder = {
            "gpu": index.get(uuid),
            "uuid": uuid,
            "pid": pid,
            "user": user,
            "command": command,
            "used_memory_mib": mib,
        }
        tag = procutil.job_tag(pid)
        if tag:
            h["taskmgr_jobid"] = tag
        out.append(h)
    out.sort(key=lambda h: (h["gpu"] if h["gpu"] is not None else 1 << 30, h["pid"]))
    return out
