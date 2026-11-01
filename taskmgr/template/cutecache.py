"""A layer's CuTe DSL kernels compile in a CPU process and load in GPU processes.

The H200 is shared and exclusive, and a CUDA context is held only to run. A layer's prepare() compiles every
kernel in-process (tracing alone takes ~25 s with a warm DSL file cache), so a GPU job must not run it as is:
task.json `precompile` runs precompile.py on the CPU first, and the entry loads what it built. See TASK.md.
Delete this file and precompile.py from a package that compiles nothing.

install("export", layer) wraps cute.compile in a CPU process (CUDA_VISIBLE_DEVICES=""): each compile of a
callable defined in the layer file also exports a .o under CACHE/<fingerprint>/. install("load",
layer) in a GPU process loads those .o files instead (no tracing, no compile) and refuses every other
cute.compile except cutlass's occupancy probe, so a miss fails loudly instead of compiling on the GPU. quack's
GEMMs keep their own .o cache (QUACK_CACHE_DIR), which precompile.py fills on the CPU too.

A compile's key is the layer file's sha256, the callable and a description of its arguments: fake tensors by
their type, shape, strides and alignment, DSL numbers by repr, kernel objects by their attributes. A real tensor
(from_dlpack) with a static layout in global memory and an explicit assumed_align is described as the fake tensor
with the same type, shape, strides and alignment: a file whose prepare() compiles on real tensors precompiles on the
CPU with those fakes, which give the same kernel.

Tracing can set attributes on the kernel object that prepare() reads after compiling (a GEMM's stage count): the
export writes the plain-data ones a compile set beside the .o (<name>.attrs), and a load sets them again.

guard_extension_builds() is the load-only guard for torch extensions (load_inline / load): precompile.py builds
them on the CPU into torch's extension cache, and a guarded GPU process refuses any build ninja would still have to run.
"""

import ast
import hashlib
import importlib.util
import inspect
import os
import re
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

# The .o cache directory: CUTECACHE_DIR if set, else kernel-cache/ in the job's workdir, which both phases share.
CACHE: Path | None = None
_state = {}
# Files that compile as they like in a GPU process (none by default); load_precompiled() leaves them alone.
UNGUARDED_FILES: set[str] = set()


def load_module(name: str, path):
    """Import the file `path` as the module `name`."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _refuse(message: str):
    """Raise, and record the refusal in _state["refused"]: a caller that catches it still fails the run."""
    _state.setdefault("refused", []).append(message.splitlines()[0][:300])
    raise RuntimeError(message)


def _desc(x, depth=0):
    if isinstance(x, (bool, int, float, str, bytes, type(None))):
        return repr(x)
    if isinstance(x, (tuple, list)):
        return type(x).__name__ + "(" + ",".join(_desc(v, depth + 1) for v in x) + ")"
    if isinstance(x, dict):
        return (
            "{"
            + ",".join(f"{_desc(k)}:{_desc(v, depth + 1)}" for k, v in sorted(x.items(), key=repr))
            + "}"
        )
    if type(x).__name__ == "_FakeTensor":
        return f"T[{x}|{x._assumed_align}|{x._use_32bit_stride}]"
    if type(x).__name__ == "_Tensor" and (fake := _fake_twin(x)) is not None:
        return _desc(fake, depth)
    if inspect.isfunction(x) or inspect.isbuiltin(x) or isinstance(x, type):
        return f"{x.__module__}.{x.__qualname__}"
    if inspect.ismethod(x):
        return f"{_desc(x.__self__, depth + 1)}.{x.__func__.__qualname__}"
    r = repr(x)
    if " at 0x" not in r:
        return f"{type(x).__qualname__}:{r}"
    if depth < 4 and hasattr(x, "__dict__"):
        return (
            type(x).__qualname__
            + "{"
            + ",".join(f"{k}={_desc(v, depth + 1)}" for k, v in sorted(vars(x).items()))
            + "}"
        )
    raise TypeError(f"cutecache: no stable key for a {type(x).__qualname__} argument")


def _plain_attrs(fn) -> dict:
    """A kernel object's attributes that are plain Python data (numbers, strings, tuples of them), as repr() writes
    and ast.literal_eval() reads them back."""

    def plain(v):
        if type(v) is float:
            return v == v and abs(v) != float("inf")
        return type(v) in (bool, int, str, type(None)) or (
            type(v) in (tuple, list) and all(plain(x) for x in v)
        )

    if (
        inspect.isfunction(fn)
        or inspect.ismethod(fn)
        or isinstance(fn, type)
        or not hasattr(fn, "__dict__")
    ):
        return {}
    return {k: v for k, v in vars(fn).items() if plain(v)}


def _fake_twin(t):
    """The fake tensor a CPU compile passes for the real tensor t, or None when t has none: its MLIR type (and so the
    kernel) depends only on these fields, never on the data pointer."""
    import cutlass.cute as cute
    from cutlass.base_dsl.enums import AddressSpace

    if (
        t._assumed_align is None
        or t.memspace != AddressSpace.gmem
        or any(t.dynamic_shapes_mask)
        or any(t.dynamic_strides_mask)
    ):
        return None
    fake = cute.runtime.make_fake_tensor(
        t.element_type, tuple(t.shape), stride=tuple(t.stride), assumed_align=t._assumed_align
    )
    fake._use_32bit_stride = t._use_32bit_stride
    return fake


def _owner(fn):
    """The installed layer file that defines fn, or None."""
    target = fn.__func__ if inspect.ismethod(fn) else fn if inspect.isfunction(fn) else type(fn)
    module = sys.modules.get(getattr(target, "__module__", ""), None)
    path = getattr(module, "__file__", None)
    return path if path in _state["layers"] else None


def _path(fn, args, kwargs, owner) -> Path:
    if (
        "options" in kwargs
    ):  # v11's --dump-dir is a fresh mkdtemp per process and does not change the kernel
        kwargs = dict(kwargs, options=re.sub(r"\s*--dump-dir=\S+", "", str(kwargs["options"])))
    key = hashlib.sha256(repr((_desc(fn), _desc(args), _desc(kwargs))).encode()).hexdigest()[:24]
    name = (
        getattr(fn, "__qualname__", type(fn).__qualname__)
        .replace(".", "_")
        .replace("<", "")
        .replace(">", "")
    )
    return CACHE / _state["layers"][owner] / f"{name}_{key}.o"


def install(mode: str, layer):
    """Wrap cute.compile for the layer file `layer` in mode "export" (CPU process) or "load" (GPU process).
    Several layer files may be installed in one process: each compile is keyed by the file defining it."""
    global CACHE
    if CACHE is None:
        default = Path(os.environ.get("TASKMGR_WORKDIR", ".")) / "kernel-cache"
        CACHE = Path(os.environ.get("CUTECACHE_DIR") or default).resolve()
    import cutlass
    import cutlass.cute as cute

    if mode == "export":
        assert os.environ.get("CUDA_VISIBLE_DEVICES") == "", "export runs in a CPU process only"
    assert _state.get("mode", mode) == mode, "one process is either a CPU export or a GPU load"
    layer = str(Path(layer).resolve())
    h = hashlib.sha256(Path(layer).read_bytes())
    h.update(cutlass.__version__.encode())
    _state.setdefault("layers", {})[layer] = h.hexdigest()[:16]
    _state.update(layer=layer, fingerprint=_state["layers"][layer], mode=mode)
    if "real" in _state:
        return
    real = _state["real"] = cute.compile

    def compile_(fn, *args, **kwargs):
        owner = _owner(fn)
        if owner is None:
            # Another library's kernel. In a GPU process, refuse it when a layer's code asked for it (a quack
            # config missing from QUACK_CACHE_DIR), except cutlass's occupancy probe.
            stack = traceback.extract_stack()
            if (
                mode == "load"
                and any(f.filename in _state["layers"] for f in stack)
                and not any("hardware_info.py" in f.filename for f in stack[-4:])
            ):
                _refuse(
                    f"cute.compile of {_desc(fn)} from a layer file in a GPU process: precompile "
                    f"it on the CPU (precompile.py <file>) first"
                )
            return real(fn, *args, **kwargs)
        path = _path(fn, args, kwargs, owner)
        if mode == "export":
            if _state.get(
                "keep_dir"
            ):  # keep the cubin for precompile's spill check; the key stays the options'
                kwargs = dict(kwargs)
                d = _state["keep_dir"] / path.stem
                d.mkdir(parents=True, exist_ok=True)
                kwargs["options"] = f"{kwargs.get('options', '')} --keep-cubin --dump-dir={d}"
            before = _plain_attrs(fn)
            compiled = real(fn, *args, **kwargs)
            if getattr(compiled, "kernel_info", None):  # for occupancy() in the GPU process
                path.parent.mkdir(parents=True, exist_ok=True)
                path.with_suffix(".kernel").write_text(next(iter(compiled.kernel_info)))
            set_by_compile = {
                k: v for k, v in _plain_attrs(fn).items() if k not in before or before[k] != v
            }
            if set_by_compile:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.with_suffix(".attrs").write_text(repr(set_by_compile))
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(f".tmp{os.getpid()}")
                if (
                    "object_file_path" in inspect.signature(compiled.export_to_c).parameters
                ):  # a TVM-FFI function
                    compiled.export_to_c(object_file_path=str(tmp), function_name="func")
                    os.replace(tmp, path)
                else:  # a C-ABI function writes <dir>/<name>.o and .h; load_module finds it by the same prefix
                    tmp.mkdir()
                    compiled.export_to_c(str(tmp), "k", function_prefix="func")
                    os.replace(tmp / "k.o", path)
                    shutil.rmtree(tmp)
            _state.setdefault("exported", []).append(path.name)
            return compiled
        if not path.exists():
            _refuse(
                f"{path.name} ({_desc(fn)}) is not precompiled for this layer file: "
                f"run precompile.py {owner} on the CPU first; its arguments: "
                f"{_desc(args)[:2000]} {_desc(kwargs)[:400]}"
            )
        tvm_ffi = "--enable-tvm-ffi" in str(kwargs.get("options", ""))
        _state.setdefault("loaded", []).append(path.name)
        if path.with_suffix(
            ".attrs"
        ).exists():  # what tracing set on the kernel object, which prepare() may read
            for k, v in ast.literal_eval(path.with_suffix(".attrs").read_text()).items():
                setattr(fn, k, v)
        loaded = cute.runtime.load_module(str(path), enable_tvm_ffi=tvm_ffi)["func"]
        _state.setdefault("objects", {})[id(loaded)] = path
        return loaded

    cute.compile = compile_


def occupancy(fn, threads: int, smem: int) -> int:
    """v11's _zm__occupancy for a kernel loaded from the cache: the same driver query on the cubin precompile kept."""
    import torch
    from cuda.bindings import driver

    path = _state["objects"][id(fn)]
    cubin = next((path.parent / "cubin" / path.stem).rglob("*.cubin")).read_bytes()
    torch.cuda.current_stream()  # torch's context is current on this thread
    results = []
    err, module = driver.cuModuleLoadData(cubin)
    results.append(err)
    err, func = driver.cuModuleGetFunction(module, path.with_suffix(".kernel").read_text().encode())
    results.append(err)
    (err,) = driver.cuFuncSetAttribute(
        func, driver.CUfunction_attribute.CU_FUNC_ATTRIBUTE_MAX_DYNAMIC_SHARED_SIZE_BYTES, smem
    )
    results.append(err)
    err, per_sm = driver.cuOccupancyMaxActiveBlocksPerMultiprocessor(func, threads, smem)
    results.append(err)
    if any(err != driver.CUresult.CUDA_SUCCESS for err in results):
        raise RuntimeError(f"occupancy query failed: {results}")
    return max(per_sm, 1)


def load_precompiled(path, name: str = "kernels"):
    """In a GPU process, import the file `path` with its kernels loaded from the .o cache. Use the same `name` that
    precompile.py gave it (the name is part of each kernel's cache key)."""
    path = str(Path(path).resolve())  # install() and _owner() compare resolved paths
    if Path(path).name in UNGUARDED_FILES:
        return load_module(name, path)
    install("load", path)
    module = load_module(name, path)
    for attr in list(
        vars(module)
    ):  # a loaded kernel has no fn.artifacts: query its kept cubin instead
        if re.fullmatch(r"_\w*occupancy", attr) and callable(vars(module)[attr]):
            setattr(module, attr, occupancy)
    return module


def guard_extension_builds():
    """In a GPU process, refuse to build a torch extension: load_inline then only loads what precompile.py
    built on the CPU (ninja reports no work to do). The extensions loaded are listed in _state["extensions"]."""
    import torch.utils.cpp_extension as ext

    if os.environ.get("CUDA_VISIBLE_DEVICES") == "" or "run_ninja" in _state:
        return
    run = _state["run_ninja"] = ext._run_ninja_build

    def run_ninja(build_directory, verbose, error_prefix):
        plan = subprocess.run(
            ["ninja", "-n", "-C", build_directory], capture_output=True, text=True
        )
        if plan.returncode != 0 or "ninja: no work to do." not in plan.stdout:
            _refuse(
                f"{error_prefix}: {build_directory} would build in a GPU process; build it on the CPU "
                f"first (precompile.py <file>, whose _*build_extensions steps run load_inline)\n"
                + (plan.stdout + plan.stderr)[-2000:]
            )
        _state.setdefault("extensions", []).append(build_directory)
        return run(build_directory, verbose, error_prefix)

    ext._run_ninja_build = run_ninja
