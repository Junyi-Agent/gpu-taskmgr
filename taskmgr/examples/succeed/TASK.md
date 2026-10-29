# EX-01

## Objective

Demonstrate a successful CPU-only task: sweep a toy "block size" and report the best one.

## Background

No GPU is used. The sweep is simulated with a short sleep per candidate.

## Steps

1. For each block size in 32, 64, 128, 256 compute a fake latency.
2. Report progress after each candidate.
3. Write the best block size and its speedup to result.json, and a CSV under artifacts/.

## Expected outputs

- `best_config`: object, e.g. `{"block": 128}`
- `speedup`: number, speedup over block 32

## Success criteria

The job ends as `succeeded` with both outputs present.
