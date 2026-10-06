"""Outbound link from the demo job to the demo frontend; runs on node 0 next to `vllm serve`.

The AML managed VNet gives the nodes no inbound path, so this process dials the frontend over WSS with the
per-job token the supervisor put into the job's environment, and multiplexes on that one socket:
  frontend -> link  req {id, path, body}   relayed to the local vLLM (allow-listed paths; GET without a body)
                    cancel {id}            closes that upstream request, so vLLM aborts the generation
                    eval {action: start|stop, run, suites, repeats, limit}   evals.py in the background, started
                                           once the server is healthy and the eval packages are installed
  link -> frontend  hello; status every few seconds (start-up phase and progress, health, vLLM /metrics,
                    eval state); head {id, status}, data {id, chunk} (response text as it arrives),
                    end {id}, error {id, message}; eval {state, ...} when a run ends
It reconnects with backoff; requests in flight when the socket drops are cancelled. Prompts and replies are
never logged.
"""
import argparse
import asyncio
import codecs
import json
import os
import re
import signal
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

HERE = Path(__file__).resolve().parent
PATHS = ("/v1/chat/completions", "/v1/models")
METRICS = {"running": "vllm:num_requests_running", "waiting": "vllm:num_requests_waiting",
           "kv": "vllm:kv_cache_usage_perc", "preemptions": "vllm:num_preemptions_total",
           "prompt_tokens": "vllm:prompt_tokens_total", "generation_tokens": "vllm:generation_tokens_total"}
SUITES = {"aime", "kobalt", "click", "ifbench", "niah"}
RUN = re.compile(r"[A-Za-z0-9._-]{1,64}")
REPEATS = re.compile(r"[a-z0-9=,]{0,80}")
DOWNLOAD = re.compile(r"\[download\] attempt \d+: ([\d.]+) GB on disk(?: of ([\d.]+) GB)?")
BAR = re.compile(r"(Loading safetensors checkpoint shards|Capturing CUDA graphs[^:\r\n]*):\s*(\d+)%")


def parse_metrics(text):
    """vLLM's Prometheus text summed over engines; the KV cache usage is the fullest engine's."""
    keys = {metric: key for key, metric in METRICS.items()}
    values = dict.fromkeys(METRICS, 0.0)
    for line in text.splitlines():
        key = keys.get(line.split("{", 1)[0].split(" ", 1)[0])
        if key:
            try:
                value = float(line.rsplit(" ", 1)[1])
            except (IndexError, ValueError):
                continue
            values[key] = max(values[key], value) if key == "kv" else values[key] + value
    return values


def tail(path, size=65536):
    try:
        with open(path, "rb") as handle:
            handle.seek(max(0, os.path.getsize(path) - size))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def progress(out):
    """Start-up progress from the logs: checkpoint bytes on disk and vLLM's own progress bars."""
    result = {}
    downloads = DOWNLOAD.findall(tail(Path(out) / "full-download.log", 8192))
    if downloads:
        result["download_gb"] = float(downloads[-1][0])
        result["download_of_gb"] = float(downloads[-1][1]) if downloads[-1][1] else None
    bars = BAR.findall(tail(Path(out) / "vllm-demo.log"))
    if bars:
        result["load_step"], result["load_pct"] = bars[-1][0], int(bars[-1][1])
    return result


def eval_spec(message):
    spec = {"run": str(message.get("run", "")), "suites": str(message.get("suites") or ",".join(sorted(SUITES))),
            "repeats": str(message.get("repeats") or ""), "limit": int(message.get("limit") or 0)}
    if not (RUN.fullmatch(spec["run"]) and REPEATS.fullmatch(spec["repeats"])
            and set(spec["suites"].split(",")) <= SUITES and 0 <= spec["limit"] <= 100000):
        raise ValueError("invalid eval request")
    return spec


class Link:
    backoff = (1.0, 30.0)

    def __init__(self, args):
        self.args, self.started = args, time.time()
        parts = urlsplit(args.url)
        self.http_base = f"{'https' if parts.scheme == 'wss' else 'http'}://{parts.netloc}"
        self.tasks, self.outbox, self.http = {}, None, None
        self.pending_eval = self.eval_task = self.eval_proc = self.eval_info = None
        self.eval_stopping = False

    def send(self, message):
        if self.outbox is not None:
            self.outbox.put_nowait(json.dumps(message, ensure_ascii=False))

    async def relay(self, rid, path, body):
        decoder = codecs.getincrementaldecoder("utf-8")("replace")  # a chunk may end inside a character
        try:
            async with self.http.request("GET" if body is None else "POST", self.args.vllm + path,
                                         json=body) as response:
                self.send({"type": "head", "id": rid, "status": response.status})
                async for chunk in response.content.iter_any():
                    text = decoder.decode(chunk)
                    if text:
                        self.send({"type": "data", "id": rid, "chunk": text})
            text = decoder.decode(b"", final=True)
            if text:
                self.send({"type": "data", "id": rid, "chunk": text})
            self.send({"type": "end", "id": rid})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.send({"type": "error", "id": rid, "message": f"{type(exc).__name__}: {exc}"[:300]})
        finally:
            if self.tasks.get(rid) is asyncio.current_task():
                del self.tasks[rid]

    def on_message(self, message):
        if not isinstance(message, dict):
            return
        kind, rid = message.get("type"), str(message.get("id", ""))[:64]
        if kind == "req":
            if message.get("path") not in PATHS or not rid or rid in self.tasks:
                self.send({"type": "error", "id": rid, "message": "request rejected by the link"})
            else:
                self.tasks[rid] = asyncio.create_task(self.relay(rid, message["path"], message.get("body")))
        elif kind == "cancel":
            task = self.tasks.pop(rid, None)
            if task:
                task.cancel()
        elif kind == "eval" and message.get("action") == "stop":
            self.pending_eval = None
            if self.eval_task and not self.eval_task.done():
                self.eval_stopping = True
                self.eval_info["state"] = "stopping"
                self.terminate_eval()
        elif kind == "eval" and message.get("action") == "start":
            try:
                spec = eval_spec(message)
            except (TypeError, ValueError) as exc:
                self.send({"type": "eval", "state": "rejected", "message": str(exc)[:200]})
                return
            if self.eval_task and not self.eval_task.done():
                return  # one run at a time; the frontend repeats its wish after every reconnect
            if self.eval_info and self.eval_info.get("run") == spec["run"] and self.eval_info["state"] == "done":
                self.send(dict(self.eval_info, type="eval"))
            else:
                self.pending_eval = spec

    def terminate_eval(self):
        if self.eval_proc and self.eval_proc.returncode is None:
            try:
                self.eval_proc.send_signal(signal.SIGTERM)  # evals.py uploads what is finished, then exits
            except ProcessLookupError:
                pass

    async def run_eval(self, spec):
        log = Path(self.args.out) / f"evals-{spec['run']}.log"
        command = [sys.executable, self.args.evals, "--base", self.args.vllm, "--run", spec["run"],
                   "--suites", spec["suites"], "--out-dir", self.args.out, "--link", self.http_base]
        if spec["repeats"]:
            command += ["--repeats", spec["repeats"]]
        if spec["limit"]:
            command += ["--limit", str(spec["limit"])]
        code = None
        try:
            with open(log, "ab") as handle:
                self.eval_proc = await asyncio.create_subprocess_exec(
                    *command, stdout=handle, stderr=asyncio.subprocess.STDOUT,
                    env=dict(os.environ, AXK2_LINK_TOKEN=self.args.token, AXK2_LINK_HTTP=self.http_base))
            if self.eval_stopping:  # the stop arrived while the process was starting
                self.terminate_eval()
            print(f"AXK2_LINK eval {spec['run']} started (pid {self.eval_proc.pid})", flush=True)
            code = await self.eval_proc.wait()
        except OSError as exc:
            print(f"AXK2_LINK eval {spec['run']} could not start: {exc}", flush=True)
        state = "done" if code == 0 else "stopped" if self.eval_stopping else "failed"
        self.eval_info.update(state=state, rc=code, ended=round(time.time()),
                              tail="" if code == 0 else tail(log, 1500))
        print(f"AXK2_LINK eval {spec['run']} {state} (exit {code})", flush=True)
        self.send(dict(self.eval_info, type="eval"))

    async def get(self, path):
        async with self.http.get(self.args.vllm + path, timeout=aiohttp.ClientTimeout(total=5)) as response:
            return response.status, await response.text()

    async def status(self):
        state = {"phase": "boot"}
        try:
            state.update(json.loads(Path(self.args.status).read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            pass
        healthy, metrics = False, None
        try:
            healthy = (await self.get("/health"))[0] == 200
            if healthy:
                metrics = parse_metrics((await self.get("/metrics"))[1])
        except Exception:
            pass
        ready = Path(self.args.eval_pkgs_ready)
        packages = "ready" if ready.exists() else "failed" if ready.with_suffix(".failed").exists() else "installing"
        if self.pending_eval and packages == "failed":
            self.eval_info = dict(self.pending_eval, state="failed", tail="the eval packages failed to install")
            self.pending_eval = None
            self.send(dict(self.eval_info, type="eval"))
        elif self.pending_eval and healthy and packages == "ready":
            spec, self.pending_eval = self.pending_eval, None
            self.eval_stopping, self.eval_info = False, dict(spec, state="running", started=round(time.time()))
            self.eval_task = asyncio.create_task(self.run_eval(spec))
        return dict(state, **progress(self.args.out), type="status", healthy=healthy, metrics=metrics,
                    inflight=len(self.tasks), uptime=round(time.time() - self.started), eval=self.eval_info,
                    eval_pending=self.pending_eval is not None, eval_packages=packages, t=round(time.time(), 1))

    async def ticker(self):
        while True:
            try:
                self.send(await self.status())
            except Exception as exc:
                print(f"AXK2_LINK status failed: {exc!r}", flush=True)
            await asyncio.sleep(self.args.interval)

    async def writer(self, ws):
        try:
            while True:
                await ws.send_str(await self.outbox.get())
        finally:
            await ws.close()  # a failed send must also end the reader

    async def session(self, client):
        headers = {"Authorization": "Bearer " + self.args.token, "X-AXK2-Job": os.environ.get("AZUREML_RUN_ID", "")}
        ws = await asyncio.wait_for(client.ws_connect(self.args.url, headers=headers, heartbeat=20,
                                                      max_msg_size=0), 30)
        print(f"AXK2_LINK connected to {self.http_base}", flush=True)
        self.outbox = asyncio.Queue()
        self.send({"type": "hello", "job": os.environ.get("AZUREML_RUN_ID", ""), "host": socket.gethostname(),
                   "started": round(self.started)})
        helpers = [asyncio.create_task(self.writer(ws)), asyncio.create_task(self.ticker())]
        try:
            async for message in ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    try:
                        self.on_message(json.loads(message.data))
                    except ValueError:
                        pass
        finally:
            self.outbox = None
            pending = helpers + list(self.tasks.values())
            self.tasks.clear()
            for task in pending:
                task.cancel()  # closing a relayed response makes vLLM abort that generation
            await asyncio.gather(*pending, return_exceptions=True)
            await ws.close()

    async def run(self):
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=10),
                                          connector=aiohttp.TCPConnector(limit=0))
        client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=15))
        delay = self.backoff[0]
        try:
            while True:
                began = time.time()
                try:
                    await self.session(client)
                    why = "connection closed"
                except Exception as exc:
                    why = f"{type(exc).__name__}: {exc}"[:200]
                delay = self.backoff[0] if time.time() - began > 60 else min(self.backoff[1], delay * 2)
                print(f"AXK2_LINK disconnected ({why}); reconnecting in {delay:g}s", flush=True)
                await asyncio.sleep(delay)
        finally:
            await client.close()
            await self.http.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", default=os.environ.get("AXK2_LINK_URL", ""), help="wss://<frontend>/ws/link")
    parser.add_argument("--token", default=os.environ.get("AXK2_LINK_TOKEN", ""))
    parser.add_argument("--vllm", default="http://127.0.0.1:8000")
    parser.add_argument("--out", default=os.environ.get("OUT", "outputs"))
    parser.add_argument("--status", default="/tmp/axk2-status.json", help="written by entry.sh's status()")
    parser.add_argument("--eval-pkgs-ready", default="/tmp/axk2-evalpkgs.ready")
    parser.add_argument("--evals", default=str(HERE / "evals.py"))
    parser.add_argument("--interval", type=float, default=5)
    args = parser.parse_args(argv)
    if not args.url or not args.token:
        parser.error("AXK2_LINK_URL and AXK2_LINK_TOKEN are required")
    return args


if __name__ == "__main__":
    asyncio.run(Link(parse_args()).run())
