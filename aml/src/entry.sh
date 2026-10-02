#!/usr/bin/env bash
# A.X-K2 multi-node serving launcher for one Azure ML command job
# (distribution: pytorch, process_count_per_instance: 1, so this runs once per node).
#   node 0  : Ray head, `vllm serve` (tensor parallel inside a node, pipeline parallel across
#             nodes, one OpenAI-compatible endpoint), then the client test suites
#   node >0 : Ray worker; stays up until the head goes away
#
# usage: entry.sh <model>   <model> = downloaded input directory | hf:<revision> | hf-smoke:<revision>
#   SUITE=full  : serve <model> and run the full test suite
#   SUITE=smoke : rehearse with <model> (single-node TP baseline, then TP x PP across nodes). With
#                 FULL_SOURCE=hf:<revision>, every node downloads the full checkpoint in the
#                 background meanwhile, and if the rehearsal passes the full model is served on the
#                 same Ray cluster and tested. One allocation then does everything.
# Settings: TP, PP, MAX_MODEL_LEN, MAX_NUM_SEQS, GPU_MEM_UTIL, HEALTH_TIMEOUT, FULL_MAX_MODEL_LEN,
# FULL_MAX_NUM_SEQS, FULL_GPU_MEM_UTIL, FULL_HEALTH_TIMEOUT, EAGER, NCCL_IB_DISABLE, JOIN_TIMEOUT.
set -uo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODEL_IN="${1:?model input}"
RANK="${NODE_RANK:-0}"
NNODES="${WORLD_SIZE:-1}"
TP="${TP:-8}"; PP="${PP:-2}"; SUITE="${SUITE:-full}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-8192}"; GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.90}"; MAX_NUM_SEQS="${MAX_NUM_SEQS:-64}"
EAGER="${EAGER:-0}"; HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-5400}"; JOIN_TIMEOUT="${JOIN_TIMEOUT:-5400}"
FULL_SOURCE="${FULL_SOURCE:-}"
FULL_MAX_MODEL_LEN="${FULL_MAX_MODEL_LEN:-8192}"; FULL_MAX_NUM_SEQS="${FULL_MAX_NUM_SEQS:-64}"
FULL_GPU_MEM_UTIL="${FULL_GPU_MEM_UTIL:-0.90}"; FULL_HEALTH_TIMEOUT="${FULL_HEALTH_TIMEOUT:-3600}"
OUT="$PWD/outputs/node$RANK"; mkdir -p "$OUT"
PKGS=/tmp/axk2-report-pkgs
MODEL_DIR=/tmp/axk2-model
FULL_DIR=/tmp/axk2-full
SERVER_PID=""

log() { echo "[$(date -u +%FT%TZ)] [node$RANK] $*"; }

(python3 -m pip install -q --no-cache-dir --target "$PKGS" "mlflow-skinny>=2.16" azureml-mlflow \
   > "$OUT/reporter-install.log" 2>&1 || uv pip install -q --target "$PKGS" mlflow-skinny azureml-mlflow \
   >> "$OUT/reporter-install.log" 2>&1) &
REPORTER_PID=$!
report() {
  if [ -n "${REPORTER_PID:-}" ]; then wait "$REPORTER_PID" 2>/dev/null; REPORTER_PID=""; fi
  PYTHONPATH="$PKGS" python3 "$SRC/report.py" "$@" || true
}

read -r IFACE NODE_IP HEAD_IP < <(python3 - <<'EOF'
import fcntl, os, socket, struct
iface = next(p[0] for p in (l.split() for l in open("/proc/net/route").readlines()[1:])
             if p[1] == "00000000" and int(p[3], 16) & 2)
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
ip = socket.inet_ntoa(fcntl.ioctl(sock.fileno(), 0x8915, struct.pack("256s", iface[:15].encode()))[20:24])
master = os.environ.get("MASTER_ADDR", "")
head = socket.gethostbyname(master) if master and os.environ.get("WORLD_SIZE", "1") != "1" else ip
print(iface, ip, head)
EOF
)
NGPU=$(nvidia-smi -L | wc -l)
log "host=$(hostname) ip=$NODE_IP iface=$IFACE head=$HEAD_IP gpus=$NGPU nodes=$NNODES suite=$SUITE tp=$TP pp=$PP"
nvidia-smi --query-gpu=timestamp,index,utilization.gpu,memory.used,memory.total --format=csv -l 15 \
  > "$OUT/gpu-usage.csv" 2>&1 &
USAGE_PID=$!

python3 - > "$OUT/env.json" <<EOF
import json, os, shutil, subprocess, socket
from importlib.metadata import version
def v(p):
    try: return version(p)
    except Exception: return None
gpus = subprocess.run(["nvidia-smi", "--query-gpu=index,name,memory.total,driver_version",
                       "--format=csv,noheader"], capture_output=True, text=True).stdout.strip().splitlines()
import torch
print(json.dumps({
  "hostname": socket.gethostname(), "node_ip": "$NODE_IP", "iface": "$IFACE", "head_ip": "$HEAD_IP",
  "node_rank": $RANK, "nnodes": $NNODES, "gpus": gpus,
  "torch": torch.__version__, "torch_cuda": torch.version.cuda,
  "capability": list(torch.cuda.get_device_capability(0)),
  "packages": {p: v(p) for p in ("vllm", "ray", "transformers", "triton", "flashinfer-python", "nvidia-nccl-cu12", "huggingface-hub", "hf-xet")},
  "infiniband_devices": sorted(os.listdir("/dev/infiniband")) if os.path.isdir("/dev/infiniband") else [],
  "disk_free_gb": round(shutil.disk_usage(os.getcwd()).free / 2**30, 1),
  "mem_total_gb": round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30, 1),
  "shm_gb": round(shutil.disk_usage("/dev/shm").total / 2**30, 1),
  "model_input": "$MODEL_IN", "full_source": "$FULL_SOURCE",
}))
EOF
cat "$OUT/env.json"
report "node$RANK.env" --file "$OUT/env.json"

if ! python3 "$SRC/apply_overlay.py" --report "$OUT/overlay.json" > "$OUT/overlay.log" 2>&1; then
  tail -n 40 "$OUT/overlay.log"
  report "node$RANK.overlay_failure" --text "$(tail -c 3500 "$OUT/overlay.log")"
  exit 10
fi
report "node$RANK.overlay" --file "$OUT/overlay.json"

if [ -n "$FULL_SOURCE" ]; then
  (
    began=$(date +%s)
    if HF_XET_HIGH_PERFORMANCE=1 python3 "$SRC/stage_weights.py" --revision "${FULL_SOURCE#hf:}" \
         --out "$PWD/hub-model" > "$OUT/full-download.log" 2>&1 &&
       python3 "$SRC/make_dense_dir.py" "$PWD/hub-model" "$FULL_DIR" --report "$OUT/dense-full.json" \
         >> "$OUT/full-download.log" 2>&1; then
      echo $(( $(date +%s) - began )) > /tmp/axk2-full.ready
    else
      touch /tmp/axk2-full.failed
    fi
  ) &
  log "full checkpoint download started in the background"
fi

if [[ "$MODEL_IN" == hf:* || "$MODEL_IN" == hf-smoke:* ]]; then
  REVISION="${MODEL_IN#*:}"
  began=$(date +%s)
  if [[ "$MODEL_IN" == hf-smoke:* ]]; then
    MODEL_IN="$PWD/hub-smoke-model"
    HF_XET_HIGH_PERFORMANCE=1 python3 "$SRC/build_smoke_checkpoint.py" --revision "$REVISION" \
      --out "$MODEL_IN" --work "$PWD/hub-source-shards" > "$OUT/hub-download.log" 2>&1 || { tail -n 30 "$OUT/hub-download.log"; exit 12; }
  else
    MODEL_IN="$PWD/hub-model-direct"
    HF_XET_HIGH_PERFORMANCE=1 python3 "$SRC/stage_weights.py" --revision "$REVISION" --out "$MODEL_IN" \
      > "$OUT/hub-download.log" 2>&1 || { tail -n 30 "$OUT/hub-download.log"; exit 12; }
  fi
  log "downloaded model from the Hub in $(( $(date +%s) - began ))s"
  report "node$RANK.hub_download" --text "{\"seconds\": $(( $(date +%s) - began ))}"
fi
python3 "$SRC/make_dense_dir.py" "$MODEL_IN" "$MODEL_DIR" --report "$OUT/dense.json" || exit 11

export VLLM_HOST_IP="$NODE_IP" NCCL_SOCKET_IFNAME="$IFACE" GLOO_SOCKET_IFNAME="$IFACE"
export NCCL_IB_DISABLE="${NCCL_IB_DISABLE:-1}" NCCL_DEBUG="${NCCL_DEBUG:-WARN}"
export VLLM_CACHE_ROOT=/tmp/vllm-cache TRITON_CACHE_DIR=/tmp/triton-cache TORCHINDUCTOR_CACHE_DIR=/tmp/inductor-cache
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
export VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS=1800
export RAY_DEDUP_LOGS=0 RAY_USAGE_STATS_ENABLED=0
unset VLLM_USE_DEEP_GEMM

gpu_peak() {
  python3 - "$OUT/gpu-usage.csv" <<'EOF'
import csv, json, sys
peak = {}
for row in csv.reader(open(sys.argv[1])):
    try:
        peak[row[1].strip()] = max(peak.get(row[1].strip(), 0), int(row[3].split()[0]))
    except (IndexError, ValueError):
        pass
print(json.dumps({"peak_memory_used_mib_per_gpu": peak}))
EOF
}

if [ "$RANK" != "0" ]; then
  python3 - "$HEAD_IP" "$JOIN_TIMEOUT" <<'EOF' || { log "head never opened 6379"; exit 31; }
import socket, sys, time
deadline = time.time() + int(sys.argv[2])
while time.time() < deadline:
    try:
        socket.create_connection((sys.argv[1], 6379), 5).close(); sys.exit(0)
    except OSError:
        time.sleep(10)
sys.exit(1)
EOF
  ray start --address="$HEAD_IP:6379" --node-ip-address="$NODE_IP" --num-gpus="$NGPU" \
    --disable-usage-stats > "$OUT/ray-worker.log" 2>&1 || { cat "$OUT/ray-worker.log"; exit 32; }
  log "joined Ray cluster at $HEAD_IP"
  misses=0
  while [ "$misses" -lt 3 ]; do
    if python3 -c "import socket; socket.create_connection(('$HEAD_IP', 6379), 5).close()" 2>/dev/null; then
      misses=0
    else
      misses=$((misses + 1))
    fi
    sleep 20
  done
  log "head is gone; stopping worker"
  ray stop --force > /dev/null 2>&1
  kill "$USAGE_PID" 2>/dev/null
  report "node$RANK.gpu" --text "$(gpu_peak)"
  [ -n "$FULL_SOURCE" ] && report "node$RANK.full_download" --text "$(tail -c 1500 "$OUT/full-download.log" 2>/dev/null)"
  exit 0
fi

# start_server <tag> <model dir> <max len> <max seqs> <gpu mem util> [extra vllm args...]
start_server() {
  local tag=$1 model=$2 len=$3 seqs=$4 mem=$5; shift 5
  local eager=""; [ "$EAGER" = "1" ] && eager="--enforce-eager"
  log "vllm serve [$tag] model=$model len=$len seqs=$seqs mem=$mem $* $eager"
  vllm serve "$model" --served-model-name axk2 --host 0.0.0.0 --port 8000 \
    --max-model-len "$len" --gpu-memory-utilization "$mem" --max-num-seqs "$seqs" \
    --reasoning-parser deepseek_v3 --no-enable-log-requests $eager "$@" > "$OUT/vllm-$tag.log" 2>&1 &
  SERVER_PID=$!
}

# wait_healthy <tag> <timeout seconds>
wait_healthy() {
  local tag=$1 limit=$2 began; began=$(date +%s)
  while true; do
    if python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)" 2>/dev/null; then
      echo $(( $(date +%s) - began )) > "$OUT/startup-$tag.seconds"
      log "[$tag] healthy after $(cat "$OUT/startup-$tag.seconds")s"
      return 0
    fi
    kill -0 "$SERVER_PID" 2>/dev/null || { log "[$tag] server exited"; return 1; }
    [ $(( $(date +%s) - began )) -gt "$limit" ] && { log "[$tag] health timeout"; return 1; }
    sleep 10
  done
}

server_facts() {
  grep -iE "backend|marlin|fp8|kv cache|maximum concurrency|loading weights took|model loading took|graph|placement|rank|pipeline|torch.compile|warning" \
    "$OUT/vllm-$1.log" | grep -v "Avg prompt throughput" | tail -n 60 | cut -c1-300
}

stop_server() {
  [ -n "$SERVER_PID" ] || return 0
  kill -INT "$SERVER_PID" 2>/dev/null
  for _ in $(seq 1 45); do kill -0 "$SERVER_PID" 2>/dev/null || break; sleep 2; done
  kill -9 "$SERVER_PID" 2>/dev/null
  SERVER_PID=""
  sleep 10
}

fail_server() {
  local tag=$1 code=$2
  tail -n 80 "$OUT/vllm-$tag.log"
  report "node0.$tag.failure" --text "$(tail -c 7600 "$OUT/vllm-$tag.log")"
  report "node0.$tag.facts" --text "$(server_facts "$tag" | tail -c 7600)"
  stop_server
  ray stop --force > /dev/null 2>&1
  exit "$code"
}

series() {
  python3 - "$1" > "$2" <<'EOF'
import json, sys
data = json.load(open(sys.argv[1]))
print(json.dumps({f"{n}.{k}": v[k] for n, v in data["prompts"].items() for k in ("chosen_logprob", "top1")}))
EOF
}

export VLLM_ENGINE_READY_TIMEOUT_S="$HEALTH_TIMEOUT"
if [ "$SUITE" = "smoke" ]; then
  start_server pp1 "$MODEL_DIR" "$MAX_MODEL_LEN" "$MAX_NUM_SEQS" "$GPU_MEM_UTIL" \
    --tensor-parallel-size "$TP" --pipeline-parallel-size 1
  wait_healthy pp1 "$HEALTH_TIMEOUT" || fail_server pp1 20
  report "node0.pp1.facts" --text "$(server_facts pp1 | tail -c 7600)"
  python3 "$SRC/client_tests.py" smoke --tag "tp${TP}-pp1-single-node" --prompts "$MODEL_DIR/smoke_prompts.json" \
    --out "$OUT/smoke-pp1.json" || fail_server pp1 21
  series "$OUT/smoke-pp1.json" "$OUT/series-pp1.json"
  report "node0.pp1" --series --file "$OUT/series-pp1.json"
  stop_server
fi

ray start --head --node-ip-address="$NODE_IP" --port=6379 --num-gpus="$NGPU" --include-dashboard=false \
  --disable-usage-stats > "$OUT/ray-head.log" 2>&1 || { cat "$OUT/ray-head.log"; exit 30; }
python3 - "$NNODES" "$((TP * PP))" "$JOIN_TIMEOUT" > "$OUT/ray-nodes.json" <<'EOF' || { log "Ray cluster incomplete"; ray stop --force; exit 33; }
import json, sys, time
import ray
nodes_needed, gpus_needed, timeout = map(int, sys.argv[1:4])
ray.init(address="auto", logging_level="ERROR")
deadline = time.time() + timeout
while True:
    nodes = [n for n in ray.nodes() if n["Alive"]]
    gpus = sum(n["Resources"].get("GPU", 0) for n in nodes)
    if len(nodes) >= nodes_needed and gpus >= gpus_needed:
        break
    if time.time() > deadline:
        sys.exit(1)
    time.sleep(10)
print(json.dumps([{"ip": n["NodeManagerAddress"], "gpus": n["Resources"].get("GPU", 0)} for n in nodes]))
EOF
log "Ray cluster: $(cat "$OUT/ray-nodes.json")"
report "node0.ray" --file "$OUT/ray-nodes.json"

TAG="tp${TP}-pp${PP}"
start_server "$TAG" "$MODEL_DIR" "$MAX_MODEL_LEN" "$MAX_NUM_SEQS" "$GPU_MEM_UTIL" \
  --tensor-parallel-size "$TP" --pipeline-parallel-size "$PP" --distributed-executor-backend ray
wait_healthy "$TAG" "$HEALTH_TIMEOUT" || fail_server "$TAG" 40
report "node0.$TAG.facts" --text "$(server_facts "$TAG" | tail -c 7600)"
report "node0.$TAG.startup" --text "{\"seconds_to_healthy\": $(cat "$OUT/startup-$TAG.seconds")}"

status=0
if [ "$SUITE" = "smoke" ]; then
  python3 "$SRC/client_tests.py" smoke --tag "$TAG" --prompts "$MODEL_DIR/smoke_prompts.json" \
    --out "$OUT/smoke-$TAG.json" || status=50
  if [ "$status" = 0 ]; then
    series "$OUT/smoke-$TAG.json" "$OUT/series-$TAG.json"
    report "node0.$TAG" --series --file "$OUT/series-$TAG.json"
    python3 "$SRC/client_tests.py" compare --a "$OUT/smoke-pp1.json" --b "$OUT/smoke-$TAG.json" \
      --out "$OUT/compare.json" && report "node0.compare_pp1_vs_$TAG" --file "$OUT/compare.json"
    python3 -c "import json; d=json.load(open('$OUT/smoke-$TAG.json')); print(json.dumps({k: d[k] for k in ('determinism', 'concurrency')}))" \
      > "$OUT/smoke-summary.json" && report "node0.$TAG.smoke" --file "$OUT/smoke-summary.json"
  fi
else
  python3 "$SRC/client_tests.py" full --tag "$TAG" --tokenizer "$MODEL_DIR" --out "$OUT/full-$TAG.json" || status=60
  [ -f "$OUT/full-$TAG.json" ] && report "node0.$TAG.full" --file "$OUT/full-$TAG.json"
fi
[ "$status" = 0 ] || report "node0.$TAG.client_failure" --text "$(tail -c 7600 "$OUT/vllm-$TAG.log")"
stop_server

if [ "$SUITE" = "smoke" ] && [ -n "$FULL_SOURCE" ] && [ "$status" = 0 ]; then
  log "rehearsal passed; waiting for the full checkpoint on every node"
  python3 - "$FULL_HEALTH_TIMEOUT" > "$OUT/full-download-status.json" <<'EOF' || { log "full checkpoint not ready"; report "node0.full_download_failure" --text "$(cat "$OUT/full-download-status.json" 2>/dev/null; tail -c 3000 "$OUT/full-download.log")"; ray stop --force; exit 70; }
import json, sys, time
import ray
ray.init(address="auto", logging_level="ERROR")

@ray.remote(num_cpus=0)
def probe():
    import os, socket
    ready = open("/tmp/axk2-full.ready").read().strip() if os.path.exists("/tmp/axk2-full.ready") else None
    return {"host": socket.gethostname(), "seconds": ready, "failed": os.path.exists("/tmp/axk2-full.failed")}

deadline = time.time() + int(sys.argv[1])
while True:
    ips = [n["NodeManagerAddress"] for n in ray.nodes() if n["Alive"]]
    states = ray.get([probe.options(resources={f"node:{ip}": 0.001}).remote() for ip in ips])
    if any(s["failed"] for s in states) or time.time() > deadline:
        print(json.dumps(states)); sys.exit(1)
    if all(s["seconds"] for s in states):
        print(json.dumps(states)); break
    time.sleep(20)
EOF
  report "node0.full_download" --file "$OUT/full-download-status.json"
  FTAG="full-tp${TP}-pp${PP}"
  export VLLM_ENGINE_READY_TIMEOUT_S="$FULL_HEALTH_TIMEOUT"
  start_server "$FTAG" "$FULL_DIR" "$FULL_MAX_MODEL_LEN" "$FULL_MAX_NUM_SEQS" "$FULL_GPU_MEM_UTIL" \
    --tensor-parallel-size "$TP" --pipeline-parallel-size "$PP" --distributed-executor-backend ray
  wait_healthy "$FTAG" "$FULL_HEALTH_TIMEOUT" || fail_server "$FTAG" 80
  report "node0.$FTAG.facts" --text "$(server_facts "$FTAG" | tail -c 7600)"
  report "node0.$FTAG.startup" --text "{\"seconds_to_healthy\": $(cat "$OUT/startup-$FTAG.seconds")}"
  python3 "$SRC/client_tests.py" full --tag "$FTAG" --tokenizer "$FULL_DIR" --out "$OUT/full-$FTAG.json" || status=90
  [ -f "$OUT/full-$FTAG.json" ] && report "node0.$FTAG.full" --file "$OUT/full-$FTAG.json"
  [ "$status" = 0 ] || report "node0.$FTAG.client_failure" --text "$(tail -c 7600 "$OUT/vllm-$FTAG.log")"
  stop_server
fi

ray stop --force > /dev/null 2>&1
kill "$USAGE_PID" 2>/dev/null
report "node0.gpu" --text "$(gpu_peak)"
log "finished with status $status"
exit "$status"
