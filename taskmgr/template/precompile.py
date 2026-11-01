"""Compile a file's CuTe DSL kernels on the CPU (no CUDA context), for GPU processes to load through cutecache.py.

    task.json: "precompile": {"argv": ["python3", "precompile.py", "kernels.py"]}

taskmgr runs this before the job gets its GPUs, in the job's workdir. Delete this file and cutecache.py from a
package that compiles nothing.

The file is loaded as "kernels" (the name is part of each kernel's cache key, so load it under the same name with
cutecache.load_precompiled) and its compile steps run with cute.compile exporting each of the file's own kernels,
with its cubin, under kernel-cache/<fingerprint>/ in the workdir:
- every module-level _<name>compile_all(), the convention prepare() compiles through, with fake tensors;
- _pr_prepare(None), if the file has it;
- the quack GEMM plans of _dn_plans("cpu"), if the file has it, with the launch stubbed out: this fills
  QUACK_CACHE_DIR with the keys prepare() derives on the GPU;
- every module-level _<name>build_extensions(), the convention for the torch extensions prepare() builds with
  load_inline: they build into torch's extension cache (TORCH_EXTENSIONS_DIR, by default ~/.cache/torch_extensions),
  where the GPU process's load_inline finds them built (cutecache.guard_extension_builds refuses any other build).
A kernel compiled anywhere else is missed, and the GPU process then refuses to run it.

Hardware queries are answered as on an H200 by default: sm_90a (quack's GPU-blind table), a file's _occupancy(...)
returns 1, during the _*build_extensions steps torch.cuda.get_device_capability() returns (9, 0), and with
h200_props=True torch.cuda.get_device_properties() reports 132 SMs (off by default: libraries such as flashinfer
probe the device at import and must see none). For other hardware set PRECOMPILE_ARCH, PRECOMPILE_QUACK_ARCH,
PRECOMPILE_N_SM and PRECOMPILE_CAPABILITY (major.minor).

Then the spill check FlashMLA's build runs (tests/kernelkit/build.py): `cuobjdump -res-usage` on every kept cubin,
plus a count of local loads and stores in its SASS (spills through a stack frame report LOCAL 0). A spilling kernel
fails the run with exit status 3 unless PRECOMPILE_ALLOW_SPILL=1. spill="local" counts LOCAL > 0 only.

A task that needs more wraps this: its own script calls main(argv, install=...) with its own cache module.
"""

import os
import re
import shutil
import subprocess
import sys
import time
import types
from pathlib import Path

ARCH = os.environ.get("PRECOMPILE_ARCH", "sm_90a")
os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ["CUTE_DSL_ARCH"] = ARCH
os.environ["CUTE_DSL_DISABLE_FILE_CACHING"] = (
    "1"  # a kernel the DSL's own cache serves leaves no cubin
)
os.environ["QUACK_ARCH"] = os.environ.get("PRECOMPILE_QUACK_ARCH", "90")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cutecache  # noqa: E402

N_SM = int(os.environ.get("PRECOMPILE_N_SM", "132"))
CAPABILITY = tuple(int(n) for n in os.environ.get("PRECOMPILE_CAPABILITY", "9.0").split("."))
STEP = re.compile(r"^_\w*compile_all$")
EXT_STEP = re.compile(r"^_\w*build_extensions$")
RES = re.compile(r"REG:(\d+)\s+STACK:(\d+)\s+SHARED:(\d+)\s+LOCAL:(\d+)")


def precompile(
    path: Path, install=cutecache.install, spill: str = "sass", h200_props: bool = False
) -> list[str]:
    import torch

    try:
        from quack.cache.async_compile import _install_gpu_blind_device_attrs, _pin_dsl_arch
    except ImportError:  # a file without quack GEMMs
        pass
    else:
        _pin_dsl_arch(ARCH)
        _install_gpu_blind_device_attrs()
    if h200_props:
        torch.cuda.get_device_properties = lambda *_: types.SimpleNamespace(
            multi_processor_count=N_SM, major=CAPABILITY[0], minor=CAPABILITY[1]
        )
    install("export", path)
    out_dir = cutecache.CACHE / cutecache._state["layers"][str(path)]
    keep = out_dir / "cubin"
    shutil.rmtree(keep, ignore_errors=True)
    cutecache._state["keep_dir"] = keep
    cutecache._state["exported"] = []
    started = time.time()
    mod = cutecache.load_module("kernels", str(path))
    for name in vars(
        mod
    ):  # v11's are named _zm__occupancy, and _c_<case>__zm__occupancy once merged
        if re.fullmatch(r"_\w*occupancy", name) and callable(vars(mod)[name]):
            setattr(mod, name, lambda *_: 1)
    steps = sorted(name for name in vars(mod) if STEP.match(name))
    for step in steps:
        getattr(mod, step)()
    if hasattr(mod, "_pr_prepare"):
        mod._pr_prepare(None)
        steps.append("_pr_prepare")
    ext_steps = sorted(name for name in vars(mod) if EXT_STEP.match(name))
    if ext_steps:
        import torch.utils.cpp_extension as ext

        capability = torch.cuda.get_device_capability
        torch.cuda.get_device_capability = lambda *_: CAPABILITY
        try:
            for step in ext_steps:
                getattr(mod, step)()
        finally:
            torch.cuda.get_device_capability = capability
        steps += ext_steps + [
            f"extensions {', '.join(sorted(ext.JIT_EXTENSION_VERSIONER.entries))}"
        ]
    plans = {}
    if hasattr(mod, "_dn_plans"):
        import quack.gemm as qg

        run, clusters = qg.run_gemm_plan, qg.get_max_active_clusters
        qg.run_gemm_plan = lambda *a, **k: None
        qg.get_max_active_clusters = lambda *a, **k: N_SM
        try:
            plans = mod._dn_plans("cpu")
        finally:
            qg.run_gemm_plan, qg.get_max_active_clusters = run, clusters
    n = len(cutecache._state["exported"])
    print(
        f"{path}: {n} kernels in {time.time() - started:.0f} s -> {out_dir} ({', '.join(steps)}"
        + (f"; {len(plans)} quack plans" if plans else "")
        + ")"
    )
    return spills(keep, spill)


def spills(keep: Path, spill: str = "sass") -> list[str]:
    """Every kept cubin's kernels by `cuobjdump -res-usage`; prints the ones with a stack frame or spills and returns
    the spilling ones."""
    cuobjdump = Path(os.environ.get("CUDA_HOME", "/usr/local/cuda")) / "bin" / "cuobjdump"
    rows, bad = [], []
    for cubin in sorted(keep.rglob("*.cubin")):
        out = subprocess.run(
            [str(cuobjdump), "-res-usage", str(cubin)], capture_output=True, text=True
        ).stdout
        name = None
        for line in out.splitlines():
            if line.strip().startswith("Function "):
                name = line.split("Function", 1)[1].strip().rstrip(":")
            elif (m := RES.search(line)) and name:
                reg, stack, shared, local = map(int, m.groups())
                ls = 0
                if spill == "sass":
                    sass = subprocess.run(
                        [str(cuobjdump), "-sass", str(cubin)], capture_output=True, text=True
                    ).stdout
                    ls = len(re.findall(r"\b(?:STL|LDL)\b", sass))
                rows.append((cubin.parent.name, reg, stack, local))
                if stack or local or ls:
                    print(
                        f"  {'SPILL' if local or ls else 'stack'} {cubin.parent.name}: REG {reg} STACK {stack} "
                        f"LOCAL {local}" + (f" STL/LDL {ls}" if spill == "sass" else "")
                    )
                if local or ls:
                    bad.append(cubin.parent.name)
                name = None
    regs = sorted({r[1] for r in rows})
    print(
        f"  spill check: {len(rows)} kernels, registers {regs[0] if regs else '-'}-{regs[-1] if regs else '-'}, "
        f"{sum(1 for r in rows if r[2])} with a stack frame, {len(bad)} spilling "
        f"({'STL/LDL or local memory' if spill == 'sass' else 'local memory'})"
    )
    return bad


def main(
    argv: list[str], install=cutecache.install, spill: str = "sass", h200_props: bool = False
) -> int:
    bad = []
    for arg in argv:
        bad += precompile(Path(arg).resolve(), install, spill, h200_props)
    return 3 if bad and os.environ.get("PRECOMPILE_ALLOW_SPILL") != "1" else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], h200_props=True))  # v11 reads the SM count while compiling
