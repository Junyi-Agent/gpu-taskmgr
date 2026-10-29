# MG-01

## Objective

Demonstrate a task that declares two GPUs (nvidia-smi indices 0 and 1).

## Background

taskmgr resolves the indices to UUIDs, waits until both GPUs are free, and starts the
task with CUDA_VISIBLE_DEVICES set to the two UUIDs in declaration order, so cuda:0 is
GPU index 0 and cuda:1 is GPU index 1.

## Steps

1. Read CUDA_VISIBLE_DEVICES and CUDA_DEVICE_ORDER.
2. If torch is importable and CUDA is available, report torch.cuda.device_count().
3. Write the device list to result.json.

## Expected outputs

- `devices`: list of GPU UUIDs visible to the task
- `device_count`: number of visible devices

## Success criteria

`device_count` is 2 and the job ends as `succeeded`.
