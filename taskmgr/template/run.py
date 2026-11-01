"""Entry script stub. Replace the body with the real tuning work."""

import taskmgr_task as tt


def main() -> None:
    # Claim the GPU before slow imports: if another process holds it, this exits 75 and
    # taskmgr starts the task again later instead of failing it. Do not compile here: the
    # job holds its GPUs now. task.json `precompile` built the kernels on the CPU; load them:
    #     import cutecache
    #     kernels = cutecache.load_precompiled("kernels.py")
    #     cutecache.guard_extension_builds()
    tt.claim_gpu()
    _ = tt.write_progress("starting", percent=0)
    # ... do the work, writing artifacts under ./artifacts/ ...
    _ = tt.update_outputs(example=True)
    _ = tt.write_progress("done", percent=100)


if __name__ == "__main__":
    main()
