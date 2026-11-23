"""GPU discovery via nvidia-smi and resolution of task GPU declarations."""

import contextlib
import os
import pwd
import shutil
import subprocess
import time
from collections.abc import Iterable, Sequence
from typing import NamedTuple

from . import procutil
from .common import TaskmgrError
from .records import GpuDecl, GpuHolder, GpuRequest, GpuSelect

# The command line of a GPU holder is cut to this many characters.
HOLDER_COMMAND_MAX = 200


def nvidia_smi_path() -> str:
    # TASKMGR_NVIDIA_SMI: test-only override pointing at a fake nvidia-smi script.
    return (
        os.environ.get("TASKMGR_NVIDIA_SMI") or shutil.which("nvidia-smi") or "/usr/bin/nvidia-smi"
    )


# How long one nvidia-smi call may take before it is killed and counts as failed.
SMI_TIMEOUT = 60.0


def _smi(query: str, timeout: float = SMI_TIMEOUT) -> list[list[str]]:
    """Run `nvidia-smi <query> --format=csv,noheader,nounits`: the rows, split on commas.

    After `timeout` seconds nvidia-smi is killed and not waited for: one stuck in the
    driver (state D) survives SIGKILL, and subprocess.run would wait for it forever."""
    cmd = [nvidia_smi_path(), query, "--format=csv,noheader,nounits"]
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                _ = proc.wait(1)  # else subprocess reaps it later (its _active list)
            raise
        if proc.returncode:
            raise subprocess.CalledProcessError(proc.returncode, cmd)
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


def query_compute_apps(timeout: float = SMI_TIMEOUT) -> list[tuple[str, int, int | None]]:
    """The processes that hold a CUDA context: [(gpu uuid, pid, used memory MiB or None)].

    The pids are those of the server's pid namespace; a process in another namespace (a
    container) has a pid that /proc here may not show."""
    apps: list[tuple[str, int, int | None]] = []
    for parts in _smi("--query-compute-apps=gpu_uuid,pid,used_memory", timeout):
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


# ---------------------------------------------------------------------------
# GPU selection (task.json `gpus` as an object, chosen at launch)

MIB_PER_GB = 1024  # min_free_gb is in GiB, as nvidia-smi counts memory in MiB


class GpuState(NamedTuple):
    idx: int  # nvidia-smi index
    uuid: str
    free_mib: int | None
    total_mib: int | None
    mode: str  # compute mode: Default, Exclusive_Process, Prohibited, ...
    apps: int  # processes with a CUDA context on it


def query_gpu_state(timeout: float = SMI_TIMEOUT) -> list[GpuState]:
    """Every GPU with its free memory, compute mode and number of compute processes; the
    two nvidia-smi calls share one `timeout`."""
    deadline = time.monotonic() + timeout
    apps: dict[str, int] = {}
    for uuid, _, _ in query_compute_apps(timeout):
        apps[uuid] = apps.get(uuid, 0) + 1

    left = max(0.1, deadline - time.monotonic())
    out: list[GpuState] = []
    for parts in _smi("--query-gpu=index,uuid,memory.free,memory.total,compute_mode", left):
        if len(parts) < 5:
            continue
        index = _int(parts[0])
        if index is None:
            continue
        uuid = parts[1]
        out.append(
            GpuState(index, uuid, _int(parts[2]), _int(parts[3]), parts[4], apps.get(uuid, 0))
        )
    return out


def request_from_task(spec: GpuSelect) -> GpuRequest:
    """job.json gpu_request for a task.json `gpus` object (checked by validation):
    `from` is resolved to UUIDs now, and the request must be possible on this machine."""
    req: GpuRequest = {"count": spec["count"]}
    if "min_free_gb" in spec:
        req["min_free_gb"] = spec["min_free_gb"]

    available = query_gpu_state()
    if "from" in spec:
        req["from_uuids"] = [g["uuid"] for g in resolve_declarations(list(spec["from"]))]

    cand = [g for g in available if g.uuid in req.get("from_uuids", [g.uuid])]
    if "min_free_gb" in req:
        need = req["min_free_gb"] * MIB_PER_GB
        cand = [g for g in cand if g.total_mib is None or g.total_mib >= need]

    if len(cand) < req["count"]:
        raise TaskmgrError(
            "invalid_gpus",
            "gpus asks for %s, but this machine has only %d such GPUs"
            % (describe_request(req), len(cand)),
        )
    return req


def describe_request(req: GpuRequest) -> str:
    """E.g. "1 GPU with >= 40 GiB free" / "2 idle GPUs among 3 listed"."""
    n = req["count"]
    if "min_free_gb" in req:
        text = "%d GPU%s with >= %g GiB free" % (n, "" if n == 1 else "s", req["min_free_gb"])
    else:
        text = "%d idle GPU%s" % (n, "" if n == 1 else "s")
    listed = req.get("from_uuids")
    if listed:
        text += " among %d listed" % len(listed)
    return text


def select_gpus(
    req: GpuRequest, state: Sequence[GpuState], unavailable: Iterable[str]
) -> tuple[list[GpuDecl] | None, str]:
    """Choose GPUs for a request: (decls, "") or (None, why none). `unavailable` are the
    UUIDs taskmgr's own jobs hold or reserve.

    A GPU qualifies when it is a candidate (`from`), not unavailable, not Prohibited, has
    no compute process if it is in Exclusive_Process mode (no context could be created),
    and either has min_free_gb free or, without min_free_gb, no compute process at all.
    Among the qualifying GPUs the ones with the most free memory win, then the lowest index.
    """
    busy = set(unavailable)
    allowed = req.get("from_uuids")
    need = req["min_free_gb"] * MIB_PER_GB if "min_free_gb" in req else None
    ok: list[GpuState] = []
    for g in state:
        if allowed is not None and g.uuid not in allowed:
            continue
        if g.uuid in busy or g.mode.lower() == "prohibited":
            continue
        if g.apps and (need is None or "exclusive" in g.mode.lower()):
            continue
        if need is not None and (g.free_mib is None or g.free_mib < need):
            continue
        ok.append(g)

    if len(ok) < req["count"]:
        return None, "waiting for %s (%d qualify now)" % (describe_request(req), len(ok))
    ok.sort(key=lambda g: (-(g.free_mib or 0), g.idx))
    chosen = sorted(ok[: req["count"]], key=lambda g: g.idx)
    return [{"declared": g.idx, "uuid": g.uuid} for g in chosen], ""
