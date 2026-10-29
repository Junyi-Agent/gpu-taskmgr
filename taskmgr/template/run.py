"""Entry script stub. Replace the body with the real tuning work."""

import taskmgr_task as tt


def main() -> None:
    tt.write_progress("starting", percent=0)
    # ... do the work, writing artifacts under ./artifacts/ ...
    tt.update_outputs(example=True)
    tt.write_progress("done", percent=100)


if __name__ == "__main__":
    main()
