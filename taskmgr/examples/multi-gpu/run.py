"""Report the GPUs this task was given."""

import os
from collections.abc import Callable
from typing import cast

import taskmgr_task as tt


def main() -> None:
    devices = [d for d in os.environ.get("CUDA_VISIBLE_DEVICES", "").split(",") if d]
    _ = tt.write_progress(
        "inspecting", percent=50, message="order=%s" % os.environ.get("CUDA_DEVICE_ORDER")
    )
    count = len(devices)
    try:
        import torch  # pyright: ignore[reportMissingImports]  # optional; real GPU servers only

        torch_mod: object = torch
        cuda = cast(object, getattr(torch_mod, "cuda"))
        is_available = cast(Callable[[], object], getattr(cuda, "is_available"))
        device_count = cast(Callable[[], int], getattr(cuda, "device_count"))
        if is_available():
            count = int(device_count())
    except ImportError:
        pass
    print("CUDA_VISIBLE_DEVICES=%s" % ",".join(devices))
    _ = tt.update_outputs(devices=devices, device_count=count)
    _ = tt.write_progress("done", percent=100)


if __name__ == "__main__":
    main()
