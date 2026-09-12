#!/usr/bin/env python3
"""Minimal TorchNPU/HCCL collective smoke test launched by torchrun."""

import argparse
import os
from datetime import timedelta

import torch
import torch.distributed as dist
import torch_npu


OPS = ("all_reduce", "broadcast", "all_gather", "all_to_all")


def check(name, actual, expected, rank):
    torch_npu.npu.synchronize()
    torch.testing.assert_close(actual.detach().cpu(), expected, rtol=0, atol=0)
    print(f"[rank {rank}] PASS {name}", flush=True)


def all_reduce(rank, world_size, device):
    value = torch.tensor([rank + 1.0], dtype=torch.float32, device=device)
    dist.all_reduce(value, op=dist.ReduceOp.SUM)
    expected = torch.tensor([world_size * (world_size + 1) / 2], dtype=torch.float32)
    check("all_reduce", value, expected, rank)


def broadcast(rank, _world_size, device):
    value = torch.tensor(
        [2026.0, -9.0] if rank == 0 else [0.0, 0.0],
        dtype=torch.float32,
        device=device,
    )
    dist.broadcast(value, src=0)
    check("broadcast", value, torch.tensor([2026.0, -9.0]), rank)


def all_gather(rank, world_size, device):
    value = torch.tensor([rank, rank + 0.5], dtype=torch.float32, device=device)
    gathered = [torch.empty_like(value) for _ in range(world_size)]
    dist.all_gather(gathered, value)
    expected = torch.tensor([[r, r + 0.5] for r in range(world_size)])
    check("all_gather", torch.stack(gathered), expected, rank)


def all_to_all(rank, world_size, device):
    value = torch.arange(world_size, dtype=torch.float32, device=device)
    value += rank * world_size
    output = torch.empty_like(value)
    dist.all_to_all_single(output, value)
    expected = torch.arange(world_size, dtype=torch.float32) * world_size + rank
    check("all_to_all", output, expected, rank)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--op", choices=("all",) + OPS, default="all")
    parser.add_argument("--device-ids", required=True, help="NPU IDs in LOCAL_RANK order")
    parser.add_argument("--timeout-s", type=int, default=180)
    args = parser.parse_args()

    local_rank = int(os.environ["LOCAL_RANK"])
    rank = int(os.environ["RANK"])
    world_size = int(os.environ["WORLD_SIZE"])
    device_ids = [int(item) for item in args.device_ids.split(",")]
    device_id = device_ids[local_rank]
    device = torch.device(f"npu:{device_id}")
    torch_npu.npu.set_device(device_id)

    tests = {
        "all_reduce": all_reduce,
        "broadcast": broadcast,
        "all_gather": all_gather,
        "all_to_all": all_to_all,
    }
    try:
        dist.init_process_group(
            backend="hccl",
            init_method="env://",
            timeout=timedelta(seconds=args.timeout_s),
        )
        selected = OPS if args.op == "all" else (args.op,)
        for name in selected:
            tests[name](rank, world_size, device)
        dist.barrier()
        if rank == 0:
            print(f"[PASS] {world_size} ranks completed: {', '.join(selected)}", flush=True)
    finally:
        if dist.is_initialized():
            dist.destroy_process_group()


if __name__ == "__main__":
    main()
