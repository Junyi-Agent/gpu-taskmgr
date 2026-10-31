"""GPU discovery via nvidia-smi and resolution of task GPU declarations."""

import os
import shutil
import subprocess
from collections.abc import Sequence

from .common import TaskmgrError
from .records import GpuDecl


def nvidia_smi_path() -> str:
    # TASKMGR_NVIDIA_SMI: test-only override pointing at a fake nvidia-smi script.
    return (
        os.environ.get("TASKMGR_NVIDIA_SMI") or shutil.which("nvidia-smi") or "/usr/bin/nvidia-smi"
    )


def query_gpus() -> list[tuple[int, str]]:
    """Return [(index, uuid), ...] in nvidia-smi index (PCI bus) order."""
    cmd = [nvidia_smi_path(), "--query-gpu=index,uuid", "--format=csv,noheader"]
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
    gpus: list[tuple[int, str]] = []
    for line in out.decode(errors="replace").splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(",")]
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
