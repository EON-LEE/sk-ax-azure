"""NCCL transport probe between two Azure ML nodes: InfiniBand versus TCP.

Run on both nodes at the same time with the same arguments:
  python ib_probe.py --mode ib|tcp --port <port> --out result.json
One process per node uses GPU 0. It measures small-message round-trip latency and 256 MiB
send/receive and all-reduce bandwidth, and records which NCCL network plugin and transport were
used (parsed from NCCL's own INFO log). The process group is torn down before vLLM starts.
"""
import argparse
import glob
import json
import os
import re
import socket
import time
from datetime import timedelta


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["ib", "tcp"], required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--rendezvous-seconds", type=int, default=300,
                        help="how long to wait for the other node (the first probe absorbs start-up skew)")
    args = parser.parse_args()
    log_glob = f"/tmp/nccl-probe-{args.mode}.*.log"
    for old in glob.glob(log_glob):
        os.remove(old)
    os.environ["NCCL_IB_DISABLE"] = "0" if args.mode == "ib" else "1"
    os.environ["NCCL_DEBUG"] = "INFO"
    os.environ["NCCL_DEBUG_SUBSYS"] = "INIT,NET"
    os.environ["NCCL_DEBUG_FILE"] = f"/tmp/nccl-probe-{args.mode}.%h.%p.log"

    import torch
    import torch.distributed as dist

    rank = int(os.environ.get("NODE_RANK", os.environ.get("RANK", "0")))
    world = int(os.environ.get("WORLD_SIZE", "2"))
    master = os.environ["MASTER_ADDR"]
    torch.cuda.set_device(0)
    result = {"mode": args.mode, "rank": rank, "world_size": world, "host": socket.gethostname()}
    began = time.time()
    dist.init_process_group("nccl", init_method=f"tcp://{master}:{args.port}", rank=rank, world_size=world,
                            timeout=timedelta(seconds=args.rendezvous_seconds))
    peer = 1 - rank
    small = torch.ones(2, device="cuda")
    dist.all_reduce(small)
    torch.cuda.synchronize()
    result["init_seconds"] = round(time.time() - began, 1)

    ping = torch.zeros(2, device="cuda")
    for _ in range(20):
        if rank == 0:
            dist.send(ping, peer)
            dist.recv(ping, peer)
        else:
            dist.recv(ping, peer)
            dist.send(ping, peer)
    torch.cuda.synchronize()
    rounds = 500
    began = time.time()
    for _ in range(rounds):
        if rank == 0:
            dist.send(ping, peer)
            dist.recv(ping, peer)
        else:
            dist.recv(ping, peer)
            dist.send(ping, peer)
    torch.cuda.synchronize()
    result["round_trip_us"] = round((time.time() - began) / rounds * 1e6, 1)

    big = torch.ones(256 * 1024 * 1024 // 2, dtype=torch.bfloat16, device="cuda")
    for _ in range(3):
        if rank == 0:
            dist.send(big, peer)
        else:
            dist.recv(big, peer)
    torch.cuda.synchronize()
    dist.barrier()
    repeats = 20
    began = time.time()
    for _ in range(repeats):
        if rank == 0:
            dist.send(big, peer)
        else:
            dist.recv(big, peer)
    torch.cuda.synchronize()
    dist.barrier()
    seconds = time.time() - began
    result["send_recv_GBps"] = round(big.numel() * 2 * repeats / seconds / 1e9, 2)

    dist.all_reduce(big)
    torch.cuda.synchronize()
    began = time.time()
    for _ in range(10):
        dist.all_reduce(big)
    torch.cuda.synchronize()
    seconds = time.time() - began
    result["all_reduce_algbw_GBps"] = round(big.numel() * 2 * 10 / seconds / 1e9, 2)
    dist.destroy_process_group()

    lines = []
    for path in glob.glob(log_glob):
        lines += open(path, errors="replace").read().splitlines()
    keep = [re.sub(r"^\S+:\d+:\d+ ", "", line) for line in lines
            if re.search(r"NET/(IB|Socket|Plugin)|Using network|via NET|GDRDMA|NCCL version|IbDev|ib_|NET : ", line)]
    result["nccl_log"] = list(dict.fromkeys(keep))[:40]
    joined = "\n".join(lines)
    result["transport"] = ("IB" if re.search(r"via NET/IB", joined) or re.search(r"Using network IB", joined)
                           else "Socket" if re.search(r"via NET/Socket|Using network Socket", joined) else "unknown")
    result["gpudirect_rdma"] = bool(re.search(r"GDRDMA", joined))
    with open(args.out, "w") as handle:
        json.dump(result, handle, indent=1)
    print(json.dumps({k: v for k, v in result.items() if k != "nccl_log"}), flush=True)


if __name__ == "__main__":
    main()
