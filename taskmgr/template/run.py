"""Entry script stub. Replace the body with the real tuning work."""

import taskmgr_task as tt


def main() -> None:
    _ = tt.write_progress("starting", percent=0)
    # ... do the work, writing artifacts under ./artifacts/ ...
    _ = tt.update_outputs(example=True)
    _ = tt.write_progress("done", percent=100)


if __name__ == "__main__":
    main()
