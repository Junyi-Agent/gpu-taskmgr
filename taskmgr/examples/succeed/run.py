"""Toy block-size sweep that always succeeds."""

import argparse
import os
import time

import taskmgr_task as tt


class Args(argparse.Namespace):
    size: int = 64


def main() -> None:
    ap = argparse.ArgumentParser()
    _ = ap.add_argument("--size", type=int, default=64)
    args = ap.parse_args(namespace=Args())
    blocks = [32, 64, 128, 256]
    lat: dict[int, float] = {}
    os.makedirs("artifacts", exist_ok=True)
    for i, b in enumerate(blocks):
        lat[b] = abs(b - 128) / 64.0 + 1.0 + args.size / 1000.0
        _ = tt.write_progress(
            "sweep",
            percent=100 * (i + 1) / len(blocks),
            message="block %d/%d" % (i + 1, len(blocks)),
        )
        time.sleep(0.2)
    with open("artifacts/sweep.csv", "w") as f:
        _ = f.write("block,latency\n")
        for b in blocks:
            _ = f.write("%d,%.4f\n" % (b, lat[b]))
    best = min(lat, key=lambda b: lat[b])
    _ = tt.update_outputs(best_config={"block": best}, speedup=round(lat[32] / lat[best], 3))
    print("best block", best, "mode", os.environ.get("EXAMPLE_MODE"))


if __name__ == "__main__":
    main()
