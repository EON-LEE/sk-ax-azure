#!/usr/bin/env bash
# A.X-K2 multi-node serving launcher for one Azure ML command job
# (distribution: pytorch, process_count_per_instance: 1, so this runs once per node).
#   node 0  : kernel tests, single-node rehearsals, Ray head, `vllm serve` (tensor parallel inside a
#             node, pipeline parallel across nodes, one OpenAI-compatible endpoint), client suites
#   node >0 : Ray worker; stays up until the head goes away
#
# usage: entry.sh <smoke model> <full model>
#   <model> = downloaded input directory | hf:<revision> | hf-smoke:<revision> | none
#   The smoke model is the 2-layer real-weight cut used for rehearsals and numerical checks.
# MODES (default "native dense"):
#   native = the published config: DeepSeek Sparse Attention via the TRITON_MLA_SPARSE port
#   dense  = indexer keys removed; identical to DSA while the context fits in index_topk tokens
# Every phase fails softly: the failure is reported and the following phases still run.
set -uo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SMOKE_IN="${1:-none}"
FULL_IN="${2:-none}"
RANK="${NODE_RANK:-0}"
NNODES="${WORLD_SIZE:-1}"
TP="${TP:-8}"; PP="${PP:-2}"; MODES="${MODES:-native dense}"
SMOKE_MAX_MODEL_LEN="${SMOKE_MAX_MODEL_LEN:-16384}"; SMOKE_MAX_NUM_SEQS="${SMOKE_MAX_NUM_SEQS:-16}"
SMOKE_GPU_MEM_UTIL="${SMOKE_GPU_MEM_UTIL:-0.85}"; SMOKE_HEALTH_TIMEOUT="${SMOKE_HEALTH_TIMEOUT:-2400}"
FULL_MAX_MODEL_LEN="${FULL_MAX_MODEL_LEN:-65536}"; FULL_MAX_NUM_SEQS="${FULL_MAX_NUM_SEQS:-128}"
FULL_GPU_MEM_UTIL="${FULL_GPU_MEM_UTIL:-0.90}"; FULL_HEALTH_TIMEOUT="${FULL_HEALTH_TIMEOUT:-3600}"
RUN_KERNEL_TESTS="${RUN_KERNEL_TESTS:-1}"; JOIN_TIMEOUT="${JOIN_TIMEOUT:-5400}"
OUT="$PWD/outputs/node$RANK"; mkdir -p "$OUT"
PKGS=/tmp/axk2-report-pkgs
TESTS_DIR=/tmp/axk2-dsa-tests
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
log "host=$(hostname) ip=$NODE_IP iface=$IFACE head=$HEAD_IP gpus=$NGPU nodes=$NNODES tp=$TP pp=$PP modes='$MODES'"
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
  "packages": {p: v(p) for p in ("vllm", "transformers", "triton", "flashinfer-python", "nvidia-nccl-cu12", "huggingface-hub", "hf-xet")},
  "infiniband_devices": sorted(os.listdir("/dev/infiniband")) if os.path.isdir("/dev/infiniband") else [],
  "disk_free_gb": round(shutil.disk_usage(os.getcwd()).free / 2**30, 1),
  "mem_total_gb": round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30, 1),
  "shm_gb": round(shutil.disk_usage("/dev/shm").total / 2**30, 1),
  "smoke_model": "$SMOKE_IN", "full_model": "$FULL_IN", "modes": "$MODES",
}))
EOF
cat "$OUT/env.json"
report "node$RANK.env" --file "$OUT/env.json"

if ! python3 "$SRC/apply_overlay.py" --report "$OUT/overlay.json" --tests-dir "$TESTS_DIR" > "$OUT/overlay.log" 2>&1; then
  tail -n 40 "$OUT/overlay.log"
  report "node$RANK.overlay_failure" --text "$(tail -c 3500 "$OUT/overlay.log")"
  exit 10
fi
report "node$RANK.overlay" --file "$OUT/overlay.json"

# vLLM 0.23 no longer ships Ray; its multi-node guide installs it explicitly. pytest runs the
# kernel tests of the DSA port. Install before any vLLM process starts.
( (python3 -c "import ray" 2>/dev/null || uv pip install --system -q "ray[cgraph]" \
     || python3 -m pip install -q --no-cache-dir "ray[cgraph]") &&
  (uv pip install --system -q pytest || python3 -m pip install -q --no-cache-dir pytest) ) > "$OUT/ray-install.log" 2>&1 &
RAY_INSTALL_PID=$!

# fetch_model <spec> <destination>: prints the local model directory ("" for none)
fetch_model() {
  local spec=$1 dest=$2
  case "$spec" in
    none) echo "" ;;
    hf-smoke:*)
      HF_XET_HIGH_PERFORMANCE=1 python3 "$SRC/build_smoke_checkpoint.py" --revision "${spec#*:}" \
        --out "$dest" --work "$PWD/hub-source-shards" > "$OUT/smoke-download.log" 2>&1 || return 1
      echo "$dest" ;;
    hf:*)
      HF_XET_HIGH_PERFORMANCE=1 python3 "$SRC/stage_weights.py" --revision "${spec#*:}" --out "$dest" \
        > "$OUT/full-download.log" 2>&1 || return 1
      echo "$dest" ;;
    *) echo "$spec" ;;
  esac
}

FULL_NATIVE=""; FULL_DENSE=/tmp/axk2-full-dense
# InfiniBand: install the RDMA userland the vLLM image lacks, then measure NCCL between the two
# nodes with IB and with TCP before any download competes for the network. Both nodes run the
# probe in lockstep (rendezvous at MASTER_ADDR); serving uses IB only if NCCL actually chose it.
IB_MODE=tcp
if [ "${IB_PROBE:-0}" = "1" ] && [ "$NNODES" -gt 1 ]; then
  began=$(date +%s)
  if ! python3 -c "import ctypes; ctypes.CDLL('libibverbs.so.1')" 2>/dev/null; then
    (apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
       libibverbs1 ibverbs-providers librdmacm1 ibverbs-utils) > "$OUT/rdma-install.log" 2>&1 \
       || log "rdma userland install failed: $(tail -n 2 "$OUT/rdma-install.log" | tr '\n' ' ')"
  fi
  ibv_devinfo -l > "$OUT/ibv-devices.txt" 2>&1 || true
  ulimit -l unlimited 2>/dev/null || true
  export AXK2_MEMLOCK="$(ulimit -l)"
  # Azure's NDv4 NCCL topology (the VM's PCI tree is virtualized); used only if every GPU and
  # visible HCA PCI address of this VM appears in it.
  if python3 - /tmp/ndv4-topo.xml > "$OUT/topo-check.json" 2>&1 <<'EOF'
import glob, json, os, subprocess, sys, urllib.request
url = "https://raw.githubusercontent.com/Azure/azhpc-images/master/topology/ndv4-topo.xml"
xml = urllib.request.urlopen(url, timeout=30).read().decode()
ids = subprocess.run(["nvidia-smi", "--query-gpu=pci.bus_id", "--format=csv,noheader"],
                     capture_output=True, text=True).stdout.split()
gpus = [(i.split(":", 1)[0][-4:] + ":" + i.split(":", 1)[1]).lower() for i in ids]
hcas = sorted(os.path.basename(os.path.realpath(p)).lower() for p in glob.glob("/sys/class/infiniband/*/device"))
missing = [b for b in gpus + hcas if b not in xml.lower()]
print(json.dumps({"url": url, "gpus": gpus, "hcas": hcas, "missing": missing}))
if missing or not gpus:
    sys.exit(1)
open(sys.argv[1], "w").write(xml)
EOF
  then
    export NCCL_TOPO_FILE=/tmp/ndv4-topo.xml
  fi
  export NCCL_IB_PCI_RELAXED_ORDERING=1
  port=$(( ${MASTER_PORT:-29500} + 100 ))
  for mode in tcp ib; do
    port=$((port + 1))
    timeout 420 env NCCL_SOCKET_IFNAME="$IFACE" GLOO_SOCKET_IFNAME="$IFACE" \
      python3 "$SRC/ib_probe.py" --mode "$mode" --port "$port" --out "$OUT/ib-probe-$mode.json" \
      > "$OUT/ib-probe-$mode.log" 2>&1 || log "ib probe $mode failed: $(tail -n 3 "$OUT/ib-probe-$mode.log" | tr '\n' ' ')"
  done
  python3 - "$OUT" > "$OUT/ib-probe.json" <<'EOF'
import json, os, sys
out = {}
for mode in ("tcp", "ib"):
    result, log = (os.path.join(sys.argv[1], f"ib-probe-{mode}.{ext}") for ext in ("json", "log"))
    if os.path.exists(result):
        out[mode] = json.load(open(result))
    else:
        out[mode] = {"failed": True, "log_tail": open(log, errors="replace").read()[-1500:] if os.path.exists(log) else ""}
devices = os.path.join(sys.argv[1], "ibv-devices.txt")
out["ibv_devices"] = open(devices, errors="replace").read()[-800:] if os.path.exists(devices) else ""
topo = os.path.join(sys.argv[1], "topo-check.json")
out["topology_check"] = open(topo, errors="replace").read()[-1200:] if os.path.exists(topo) else ""
out["nccl_topo_file"] = os.environ.get("NCCL_TOPO_FILE")
out["memlock"] = os.environ.get("AXK2_MEMLOCK")
print(json.dumps(out))
EOF
  if python3 -c "import json, sys; d = json.load(open('$OUT/ib-probe.json')); sys.exit(0 if d.get('ib', {}).get('transport') == 'IB' else 1)"; then
    IB_MODE=ib
  fi
  report "node$RANK.ib_probe" --file "$OUT/ib-probe.json"
  log "ib probe done in $(( $(date +%s) - began ))s; serving transport: $IB_MODE"
fi

if [ "$FULL_IN" != "none" ]; then
  (
    began=$(date +%s)
    dir=$(fetch_model "$FULL_IN" "$PWD/hub-model") &&
      python3 "$SRC/make_dense_dir.py" "$dir" "$FULL_DENSE" --report "$OUT/dense-full.json" >> "$OUT/full-download.log" 2>&1 &&
      echo "$dir" > /tmp/axk2-full.dir && echo $(( $(date +%s) - began )) > /tmp/axk2-full.ready ||
      touch /tmp/axk2-full.failed
  ) &
  log "full checkpoint download started in the background"
  (
    while [ ! -f /tmp/axk2-full.ready ] && [ ! -f /tmp/axk2-full.failed ]; do
      sleep 120
      log "full download: $(tr '\r' '\n' < "$OUT/full-download.log" 2>/dev/null | grep '^\[download\]' | tail -n 1)"
    done
  ) &
fi

# Optional EAGLE3 drafter (skt/A.X-K2-EAGLE3, 6 GB) for the speculative-decoding phases.
EAGLE3_DIR=/tmp/axk2-eagle3
if [ -n "${EAGLE3_SOURCE:-}" ]; then
  ( HF_XET_HIGH_PERFORMANCE=1 python3 - "${EAGLE3_SOURCE#hf:}" "$EAGLE3_DIR" > "$OUT/eagle3-download.log" 2>&1 <<'EOF'
import sys
from huggingface_hub import snapshot_download
snapshot_download("skt/A.X-K2-EAGLE3", revision=sys.argv[1], local_dir=sys.argv[2], max_workers=8)
EOF
  ) && touch /tmp/axk2-eagle3.ready || touch /tmp/axk2-eagle3.failed &
fi

began=$(date +%s)
SMOKE_NATIVE=$(fetch_model "$SMOKE_IN" "$PWD/hub-smoke-model") || { tail -n 30 "$OUT/smoke-download.log"; exit 12; }
SMOKE_DENSE=/tmp/axk2-smoke-dense
if [ -n "$SMOKE_NATIVE" ]; then
  python3 "$SRC/make_dense_dir.py" "$SMOKE_NATIVE" "$SMOKE_DENSE" --report "$OUT/dense-smoke.json" > /dev/null || exit 11
  report "node$RANK.smoke_download" --text "{\"seconds\": $(( $(date +%s) - began ))}"
fi
wait "$RAY_INSTALL_PID"
RAY_VERSION=$(python3 -c "import ray; print(ray.__version__)" 2>/dev/null) || { tail -n 20 "$OUT/ray-install.log"; exit 29; }
log "ray $RAY_VERSION installed"
report "node$RANK.ray_install" --text "{\"ray\": \"$RAY_VERSION\"}"

export VLLM_HOST_IP="$NODE_IP" NCCL_SOCKET_IFNAME="$IFACE" GLOO_SOCKET_IFNAME="$IFACE"
if [ "$IB_MODE" = ib ]; then
  export NCCL_IB_DISABLE=0 NCCL_DEBUG=INFO NCCL_DEBUG_SUBSYS=INIT,NET
else
  export NCCL_IB_DISABLE="${NCCL_IB_DISABLE:-1}" NCCL_DEBUG="${NCCL_DEBUG:-WARN}"
fi
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
  report "node$RANK.full_download" --text "$(tail -c 1500 "$OUT/full-download.log" 2>/dev/null)"
  exit 0
fi

PHASES="$OUT/phases.jsonl"; : > "$PHASES"
phase_result() {  # <phase> <status> <began epoch> [note]
  python3 -c "import json, sys; print(json.dumps({'phase': sys.argv[1], 'status': sys.argv[2], 'seconds': int(sys.argv[3]), 'note': sys.argv[4]}))" \
    "$1" "$2" "$(( $(date +%s) - $3 ))" "${4:-}" >> "$PHASES"
  log "phase $1: $2 ${4:-}"
}

# start_server <tag> <eager flag or ""> <model dir> <max len> <max seqs> <gpu mem util> [vllm args...]
start_server() {
  local tag=$1 eager=$2 model=$3 len=$4 seqs=$5 mem=$6; shift 6
  log "vllm serve [$tag] model=$model len=$len seqs=$seqs mem=$mem $eager $*"
  vllm serve "$model" --served-model-name axk2 --host 0.0.0.0 --port 8000 \
    --max-model-len "$len" --gpu-memory-utilization "$mem" --max-num-seqs "$seqs" \
    --reasoning-parser deepseek_v3 --no-enable-log-requests $eager "$@" > "$OUT/vllm-$tag.log" 2>&1 &
  SERVER_PID=$!
}

wait_healthy() {  # <tag> <timeout seconds>
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
  grep -iE "backend|marlin|fp8|kv cache|maximum concurrency|loading weights took|model loading took|graph|placement|rank|pipeline|torch.compile|sparse|indexer|triton|warning" \
    "$OUT/vllm-$1.log" | grep -v -E "Avg prompt throughput|NCCL INFO" | tail -n 70 | cut -c1-300
}

failure_tail() { grep -v "NCCL INFO" "$OUT/vllm-$1.log" | tail -c 7600; }

stop_server() {
  [ -n "$SERVER_PID" ] || return 0
  kill -INT "$SERVER_PID" 2>/dev/null
  for _ in $(seq 1 45); do kill -0 "$SERVER_PID" 2>/dev/null || break; sleep 2; done
  kill -9 "$SERVER_PID" 2>/dev/null
  SERVER_PID=""
  sleep 10
}

# serve <tag> <timeout> <model dir> <max len> <max seqs> <gpu mem util> [vllm args...]
# Starts and waits; if torch.compile/CUDA graphs fail, keeps the evidence and retries this phase
# eagerly. Returns non-zero when the server never became healthy.
serve() {
  local tag=$1 limit=$2; shift 2
  export VLLM_ENGINE_READY_TIMEOUT_S="$limit"
  start_server "$tag" "" "$@"
  wait_healthy "$tag" "$limit" && return 0
  report "node0.$tag.compiled_failure" --text "$(failure_tail "$tag")"
  stop_server
  cp "$OUT/vllm-$tag.log" "$OUT/vllm-$tag-compiled.log"
  log "[$tag] retrying with --enforce-eager"
  start_server "$tag" "--enforce-eager" "$@"
  if wait_healthy "$tag" "$limit"; then
    report "node0.$tag.eager_fallback" --text "{\"eager\": true}"
    return 0
  fi
  report "node0.$tag.failure" --text "$(failure_tail "$tag")"
  report "node0.$tag.facts" --text "$(server_facts "$tag" | tail -c 7600)"
  stop_server
  return 1
}

series() {
  python3 - "$1" > "$2" <<'EOF'
import json, sys
data = json.load(open(sys.argv[1]))
print(json.dumps({f"{n}.{k}": v[k] for n, v in data["prompts"].items() for k in ("chosen_logprob", "top1")}))
EOF
}

# smoke_phase <tag> <model dir> [vllm args...]: logprob series of the 2-layer cut for every prompt
smoke_phase() {
  local tag=$1 model=$2 began; shift 2; began=$(date +%s)
  if ! serve "$tag" "$SMOKE_HEALTH_TIMEOUT" "$model" "$SMOKE_MAX_MODEL_LEN" "$SMOKE_MAX_NUM_SEQS" \
       "$SMOKE_GPU_MEM_UTIL" --no-enable-prefix-caching "$@"; then
    phase_result "$tag" failed "$began" "server never became healthy"; return 1
  fi
  report "node0.$tag.facts" --text "$(server_facts "$tag" | tail -c 7600)"
  if python3 "$SRC/client_tests.py" smoke --tag "$tag" --prompts "$model/smoke_prompts.json" \
       --out "$OUT/smoke-$tag.json" > "$OUT/client-$tag.log" 2>&1; then
    series "$OUT/smoke-$tag.json" "$OUT/series-$tag.json"
    report "node0.$tag" --series --file "$OUT/series-$tag.json"
    python3 -c "import json; d = json.load(open('$OUT/smoke-$tag.json')); print(json.dumps({k: d[k] for k in ('determinism', 'concurrency')}))" \
      > "$OUT/smoke-summary-$tag.json" && report "node0.$tag.smoke" --file "$OUT/smoke-summary-$tag.json"
    phase_result "$tag" passed "$began"
    stop_server; return 0
  fi
  report "node0.$tag.client_failure" --text "$(tail -c 2500 "$OUT/client-$tag.log"; failure_tail "$tag" | tail -c 5000)"
  phase_result "$tag" failed "$began" "client failed"
  stop_server; return 1
}

# full_phase <tag> <model dir>: the complete model on the Ray cluster, then the full client suite
full_phase() {
  local tag=$1 model=$2 began; began=$(date +%s)
  if ! serve "$tag" "$FULL_HEALTH_TIMEOUT" "$model" "$FULL_MAX_MODEL_LEN" "$FULL_MAX_NUM_SEQS" "$FULL_GPU_MEM_UTIL" \
       --tensor-parallel-size "$TP" --pipeline-parallel-size "$PP" --distributed-executor-backend ray; then
    phase_result "$tag" failed "$began" "server never became healthy"; return 1
  fi
  report "node0.$tag.facts" --text "$(server_facts "$tag" | tail -c 7600)"
  report "node0.$tag.startup" --text "{\"seconds_to_healthy\": $(cat "$OUT/startup-$tag.seconds")}"
  python3 "$SRC/client_tests.py" full --tag "$tag" --tokenizer "$model" --max-model-len "$FULL_MAX_MODEL_LEN" \
    --out "$OUT/full-$tag.json" > "$OUT/client-$tag.log" 2>&1
  local rc=$?
  [ -f "$OUT/full-$tag.json" ] && report "node0.$tag.full" --file "$OUT/full-$tag.json"
  if [ "$rc" = 0 ]; then
    phase_result "$tag" passed "$began"
  else
    report "node0.$tag.client_failure" --text "$(tail -c 2500 "$OUT/client-$tag.log"; failure_tail "$tag" | tail -c 5000)"
    phase_result "$tag" failed "$began" "client failed"
  fi
  stop_server
}

engine_config() {  # the engine's own record of every effective setting
  grep -m1 "Initializing a V1 LLM engine" "$OUT/vllm-$1.log" | sed 's/.*with config: //' | cut -c1-3800
}

nccl_transport() {
  grep -h -E "NET/IB : Using|NET/Socket : Using|via NET/IB|via NET/Socket|GDRDMA" "$OUT/vllm-$1.log" \
    | sed -E 's/^.*NCCL INFO //' | sort | uniq -c | sort -rn | head -n 12
}

# plan_phase <name>: one server configuration of the verification plan, then its client suites.
# Every configuration serves with the model card's parsers plus the flag vLLM needs to apply the
# tool parser automatically (--enable-auto-tool-choice), and with vLLM's B200/H100 default of
# 8192 batched tokens per step (vLLM lowers its default to 2048 on A100), so the throughput sweep
# runs with the same engine settings as the tech report's B200 measurement.
plan_phase() {
  local name=$1 began model len seqs suites bi=0 rc
  began=$(date +%s)
  local -a args=(--tool-call-parser hermes --enable-auto-tool-choice --max-num-batched-tokens 8192)
  local -a pp2=(--tensor-parallel-size 8 --pipeline-parallel-size 2 --distributed-executor-backend ray)
  local -a tp16=(--tensor-parallel-size 16 --pipeline-parallel-size 1 --distributed-executor-backend ray)
  case "$name" in
    native-pp2) model=$FULL_NATIVE; len=$FULL_MAX_MODEL_LEN; seqs=$FULL_MAX_NUM_SEQS
                suites=tools,niah,determinism,docsweep; args+=("${pp2[@]}") ;;
    dense-pp2) model=$FULL_DENSE; len=$FULL_MAX_MODEL_LEN; seqs=$FULL_MAX_NUM_SEQS
               suites=tools,niah,docsweep; args+=("${pp2[@]}") ;;
    native-pp2-bi) model=$FULL_NATIVE; len=32768; seqs=32; suites=determinism; bi=1; args+=("${pp2[@]}") ;;
    dense-pp2-bi) model=$FULL_DENSE; len=32768; seqs=32; suites=determinism; bi=1; args+=("${pp2[@]}") ;;
    dense-tp16) model=$FULL_DENSE; len=32768; seqs=64; suites=functional,sweepsub; args+=("${tp16[@]}") ;;
    dense-tp16-eagle3) model=$FULL_DENSE; len=32768; seqs=64; suites=functional,sweepsub,specstats
                       args+=("${tp16[@]}" --speculative-config
                              "{\"method\": \"eagle3\", \"model\": \"$EAGLE3_DIR\", \"num_speculative_tokens\": 3}") ;;
    *) phase_result "$name" failed "$began" "unknown phase"; return 1 ;;
  esac
  if [[ "$name" == *eagle3 ]] && [ ! -f /tmp/axk2-eagle3.ready ]; then
    phase_result "$name" failed "$began" "EAGLE3 drafter not downloaded"; return 1
  fi
  [ "$bi" = 1 ] && export VLLM_BATCH_INVARIANT=1
  if ! serve "$name" "$FULL_HEALTH_TIMEOUT" "$model" "$len" "$seqs" "$FULL_GPU_MEM_UTIL" "${args[@]}"; then
    unset VLLM_BATCH_INVARIANT
    phase_result "$name" failed "$began" "server never became healthy"; return 1
  fi
  report "node0.$name.facts" --text "$(server_facts "$name" | tail -c 7600)"
  report "node0.$name.engine_config" --text "$(engine_config "$name")"
  report "node0.$name.nccl" --text "$(nccl_transport "$name")"
  report "node0.$name.startup" --text "{\"seconds_to_healthy\": $(cat "$OUT/startup-$name.seconds"), \"nccl_ib\": \"$IB_MODE\"}"
  AXK2_REPORT_PKGS="$PKGS" python3 "$SRC/verify_suites.py" --tag "$name" --suites "$suites" --tokenizer "$model" \
    --max-model-len "$len" --out-dir "$OUT/verify" > "$OUT/client-$name.log" 2>&1
  rc=$?
  if [ "$rc" = 0 ]; then
    phase_result "$name" passed "$began"
  else
    report "node0.$name.client_failure" --text "$(tail -c 2500 "$OUT/client-$name.log"; failure_tail "$name" | tail -c 5000)"
    phase_result "$name" failed "$began" "client failed"
  fi
  stop_server
  unset VLLM_BATCH_INVARIANT
}

if [ "$RUN_KERNEL_TESTS" = "1" ]; then
  began=$(date +%s)
  if timeout 1200 python3 -m pytest -q -p no:cacheprovider --tb=short \
       "$TESTS_DIR/tests/kernels/attention/test_mqa_logits_triton.py" \
       "$TESTS_DIR/tests/kernels/attention/test_triton_mla_sparse_kernel.py" > "$OUT/kernel-tests.log" 2>&1; then
    phase_result kernel_tests passed "$began" "$(tail -n 1 "$OUT/kernel-tests.log")"
  else
    phase_result kernel_tests failed "$began" "$(tail -n 1 "$OUT/kernel-tests.log")"
  fi
  report "node0.kernel_tests" --text "$(tail -c 6000 "$OUT/kernel-tests.log")"
fi

if [ -n "$SMOKE_NATIVE" ]; then
  for mode in $MODES; do
    dir=$SMOKE_NATIVE; [ "$mode" = dense ] && dir=$SMOKE_DENSE
    smoke_phase "smoke-$mode-tp$TP" "$dir" --tensor-parallel-size "$TP" --pipeline-parallel-size 1
  done
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

if [ -n "$SMOKE_NATIVE" ]; then
  first_mode=${MODES%% *}
  dir=$SMOKE_NATIVE; [ "$first_mode" = dense ] && dir=$SMOKE_DENSE
  smoke_phase "smoke-$first_mode-tp$TP-pp$PP" "$dir" \
    --tensor-parallel-size "$TP" --pipeline-parallel-size "$PP" --distributed-executor-backend ray
fi

if [ "$FULL_IN" != "none" ]; then
  began=$(date +%s)
  log "waiting for the full checkpoint on every node"
  if python3 - "${DOWNLOAD_TIMEOUT:-5400}" > "$OUT/full-download-status.json" <<'EOF'
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
  then
    report "node0.full_download" --file "$OUT/full-download-status.json"
    phase_result full_download passed "$began"
    FULL_NATIVE=$(cat /tmp/axk2-full.dir)
    if [ -n "${PHASE_PLAN:-}" ]; then
      for name in $PHASE_PLAN; do
        plan_phase "$name"
        report "node0.phases" --text "$(python3 -c "import json; print(json.dumps([json.loads(l) for l in open('$PHASES')]))")"
      done
    else
      for mode in $MODES; do
        dir=$FULL_NATIVE; [ "$mode" = dense ] && dir=$FULL_DENSE
        full_phase "full-$mode-tp$TP-pp$PP" "$dir"
      done
    fi
  else
    report "node0.full_download_failure" --text "$(cat "$OUT/full-download-status.json" 2>/dev/null; tail -c 3000 "$OUT/full-download.log")"
    phase_result full_download failed "$began"
  fi
fi

ray stop --force > /dev/null 2>&1
kill "$USAGE_PID" 2>/dev/null
report "node0.gpu" --text "$(gpu_peak)"
report "node0.phases" --text "$(python3 -c "import json, sys; print(json.dumps([json.loads(l) for l in open('$PHASES')]))")"
failed=$(grep -c '"status": "failed"' "$PHASES")
log "finished; failed phases: $failed"
exit 0
