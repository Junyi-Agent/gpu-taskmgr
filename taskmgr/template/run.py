"""Entry script stub. Replace the body with the real tuning work.

Order matters on exclusive-process GPUs (the GPU servers run in that mode). If another
process holds one of this job's GPUs, no CUDA context can be created on it; the entry must
notice that before it does anything else, exit 75 (taskmgr starts it again after about 1 s)
and leave no side effects. So:

1. Import only the standard library and taskmgr_task at module level. No torch, numpy,
   cutlass, triton, ... here: importing torch alone takes seconds, and every relaunch
   would pay it again while the GPU is still busy.
2. Call tt.claim_gpu() first in main(). It loads libcuda.so.1 with ctypes and calls cuInit
   and cuDevicePrimaryCtxRetain for every visible GPU: this creates the CUDA context now,
   in milliseconds, and exits 75 if a GPU is held by another process. Do not replace it
   with a "is the GPU free?" check that releases the GPU again: another process can take
   the GPU between such a check and the import below.
3. Then import the heavy libraries. The context claim_gpu() created is the device's primary
   context; the CUDA runtime (and so torch, triton, CuTe, cupy) uses the primary context, so
   torch reuses it instead of creating a second one, and the GPU stays this process's from
   step 2 on.

If the CUDA work runs in a child process (torchrun, multiprocessing, a compiled binary),
do NOT call claim_gpu() here: in exclusive-process mode this parent's context would block
the child. Let the child create its context first thing and exit 75 when it gets
CUDA_ERROR_DEVICE_UNAVAILABLE / cudaErrorDevicesUnavailable (46); forward that here with
tt.gpu_busy().
"""

import taskmgr_task as tt  # standard library only: safe to import before claim_gpu()


def main() -> None:
    tt.claim_gpu()  # first: CUDA context in milliseconds, or exit 75 if the GPU is held

    # Heavy imports only now, after the GPU is ours (torch reuses the primary context):
    #     import torch
    # Do not compile here: the job holds its GPUs now. task.json `precompile` built the
    # kernels on the CPU; load them:
    #     import cutecache
    #     kernels = cutecache.load_precompiled("kernels.py")
    #     cutecache.guard_extension_builds()
    _ = tt.write_progress("starting", percent=0)
    # ... do the work, writing artifacts under ./artifacts/ ...
    _ = tt.update_outputs(example=True)
    _ = tt.write_progress("done", percent=100)


if __name__ == "__main__":
    main()
