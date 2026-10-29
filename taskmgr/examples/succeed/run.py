"""Toy block-size sweep that always succeeds."""
import argparse
import os
import time

import taskmgr_task as tt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, default=64)
    args = ap.parse_args()
    blocks = [32, 64, 128, 256]
    lat = {}
    os.makedirs("artifacts", exist_ok=True)
    for i, b in enumerate(blocks):
        lat[b] = abs(b - 128) / 64.0 + 1.0 + args.size / 1000.0
        tt.write_progress("sweep", percent=100 * (i + 1) / len(blocks),
                          message="block %d/%d" % (i + 1, len(blocks)))
        time.sleep(0.2)
    with open("artifacts/sweep.csv", "w") as f:
        f.write("block,latency\n")
        for b in blocks:
            f.write("%d,%.4f\n" % (b, lat[b]))
    best = min(lat, key=lat.get)
    tt.update_outputs(best_config={"block": best}, speedup=round(lat[32] / lat[best], 3))
    print("best block", best, "mode", os.environ.get("EXAMPLE_MODE"))


if __name__ == "__main__":
    main()
