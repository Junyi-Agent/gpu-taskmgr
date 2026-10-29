"""Report the GPUs this task was given."""

import os

import taskmgr_task as tt


def main():
    devices = [d for d in os.environ.get("CUDA_VISIBLE_DEVICES", "").split(",") if d]
    tt.write_progress(
        "inspecting", percent=50, message="order=%s" % os.environ.get("CUDA_DEVICE_ORDER")
    )
    count = len(devices)
    try:
        import torch  # pyright: ignore[reportMissingImports]  # optional; real GPU servers only

        if torch.cuda.is_available():
            count = torch.cuda.device_count()
    except ImportError:
        pass
    print("CUDA_VISIBLE_DEVICES=%s" % ",".join(devices))
    tt.update_outputs(devices=devices, device_count=count)
    tt.write_progress("done", percent=100)


if __name__ == "__main__":
    main()
