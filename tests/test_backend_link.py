"""Tests for aml/src/backend_link.py against a fake vLLM server and a fake demo frontend on loopback: relaying
(UTF-8 split across reads, GET, rejected requests, cancel and a dropped socket aborting the upstream request),
status (phase, start-up progress, metrics, health), the eval lifecycle with a stub evals script, and
reconnecting."""
import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import WSMsgType, web

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "aml" / "src"))
try:
    import backend_link
finally:
    sys.path.pop(0)

TOKEN = "link-token-for-tests"
PROMPT = "한국의 수도는 어디인가요?"
TEXT = "안녕하세요. A.X K2 데모입니다."
SSE = ("data: " + json.dumps({"choices": [{"delta": {"content": TEXT}}]}, ensure_ascii=False)
       + "\n\ndata: [DONE]\n\n").encode("utf-8")
CUT = SSE.index("녕".encode("utf-8")) + 1  # the first read ends inside a Hangul syllable
HOLD = {"messages": [{"role": "user", "content": "hold"}]}
METRICS_TEXT = """# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{engine="0",model_name="axk2"} 2.0
vllm:num_requests_running{engine="1",model_name="axk2"} 1.0
vllm:num_requests_waiting{engine="0",model_name="axk2"} 3.0
vllm:kv_cache_usage_perc{engine="0",model_name="axk2"} 0.25
vllm:kv_cache_usage_perc{engine="1",model_name="axk2"} 0.5
vllm:num_preemptions_total{engine="0",model_name="axk2"} 4.0
vllm:prompt_tokens_total{engine="0",model_name="axk2"} 1000.0
vllm:generation_tokens_total{engine="0",model_name="axk2"} 2000.0
vllm:generation_tokens_created{engine="0",model_name="axk2"} 1.7e+09
vllm:num_requests_running_extra 99
not a metric line
vllm:num_requests_waiting{engine="1",model_name="axk2"} NaNx
"""
METRICS = {"running": 3.0, "waiting": 3.0, "kv": 0.5, "preemptions": 4.0, "prompt_tokens": 1000.0,
           "generation_tokens": 2000.0}
STUB = """import json, os, sys, time
argv = sys.argv[1:]
opts = dict(zip(argv[::2], argv[1::2]))
with open(os.path.join(opts["--out-dir"], "stub.jsonl"), "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"argv": argv, "token": os.environ.get("AXK2_LINK_TOKEN"),
                             "http": os.environ.get("AXK2_LINK_HTTP")}) + "\\n")
print("stub evals", opts["--run"], flush=True)
if opts["--run"].startswith("hold"):
    time.sleep(60)
sys.exit(5 if opts["--run"].startswith("fail") else 0)
"""


class HelperTests(unittest.TestCase):
    def test_parse_metrics(self):
        self.assertEqual(backend_link.parse_metrics(METRICS_TEXT), METRICS)
        self.assertEqual(backend_link.parse_metrics(""), dict.fromkeys(METRICS, 0.0))

    def test_eval_spec(self):
        self.assertEqual(backend_link.eval_spec({"run": "r1"}),
                         {"run": "r1", "suites": "aime,click,ifbench,kobalt,niah", "repeats": "", "limit": 0})
        self.assertEqual(
            backend_link.eval_spec({"run": "r2", "suites": "kobalt,niah", "repeats": "aime=4", "limit": "20"}),
            {"run": "r2", "suites": "kobalt,niah", "repeats": "aime=4", "limit": 20})
        for bad in ({"run": ""}, {"run": "a b"}, {"run": "x" * 65}, {"run": "r", "suites": "kobalt,mmlu"},
                    {"run": "r", "suites": ["aime"]}, {"run": "r", "repeats": "aime=4;rm"},
                    {"run": "r", "limit": -1}, {"run": "r", "limit": 100001}, {"run": "r", "limit": "x"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                backend_link.eval_spec(bad)

    def test_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(backend_link.progress(tmp), {})
            Path(tmp, "full-download.log").write_text(
                "[download] attempt 1: 10.0 GB on disk\n[download] attempt 2: 352.5 GB on disk of 704.3 GB\n",
                encoding="utf-8")
            Path(tmp, "vllm-demo.log").write_text(
                "Loading safetensors checkpoint shards:  45% Completed | 72/160\r(RayWorkerWrapper pid=1) "
                "Capturing CUDA graphs (mixed prefill-decode, PIECEWISE): 100%|##| 67/67\n", encoding="utf-8")
            self.assertEqual(backend_link.progress(tmp), {
                "download_gb": 352.5, "download_of_gb": 704.3,
                "load_step": "Capturing CUDA graphs (mixed prefill-decode, PIECEWISE)", "load_pct": 100})
            Path(tmp, "full-download.log").write_text("[download] attempt 1: 10.0 GB on disk\n", encoding="utf-8")
            self.assertIsNone(backend_link.progress(tmp)["download_of_gb"])

    def test_args(self):
        link = backend_link.Link(backend_link.parse_args(["--url", "wss://demo.example.net/ws/link", "--token", "t"]))
        self.assertEqual(link.http_base, "https://demo.example.net")
        link = backend_link.Link(backend_link.parse_args(["--url", "ws://127.0.0.1:8080/ws/link", "--token", "t"]))
        self.assertEqual(link.http_base, "http://127.0.0.1:8080")
        with mock.patch.dict(os.environ, {"AXK2_LINK_URL": "", "AXK2_LINK_TOKEN": ""}), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            backend_link.parse_args([])


class FakeVLLM:
    def __init__(self):
        self.health, self.requests, self.disconnected = 200, [], asyncio.Event()

    def app(self):
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self.chat)
        app.router.add_get("/v1/models", self.models)
        app.router.add_get("/health", self.healthz)
        app.router.add_get("/metrics", self.metrics)
        return app

    async def models(self, request):
        return web.json_response({"object": "list", "data": [{"id": "axk2", "object": "model",
                                                              "max_model_len": 262144}]})

    async def healthz(self, request):
        return web.Response(status=self.health)

    async def metrics(self, request):
        return web.Response(text=METRICS_TEXT)

    async def chat(self, request):
        body = await request.json()
        self.requests.append(body)
        prompt = body["messages"][-1]["content"]
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        if prompt == "hold":  # streams until the client goes away, like a long generation
            try:
                while True:
                    await response.write(b": keep-alive\n\n")
                    await asyncio.sleep(0.05)
            except ConnectionError:
                return response
            finally:
                self.disconnected.set()
        if prompt == "boom":  # the server dies mid-response
            await response.write(b"data: partial\n\n")
            request.transport.close()
            return response
        await response.write(SSE[:CUT])
        await asyncio.sleep(0.05)
        await response.write(SSE[CUT:])
        await response.write_eof()
        return response


class FakeFrontend:
    def __init__(self):
        self.inbox, self.headers, self.rejected, self.ws = asyncio.Queue(), [], 0, None

    def app(self):
        app = web.Application()
        app.router.add_get("/ws/link", self.link)
        return app

    async def link(self, request):
        if request.headers.get("Authorization") != "Bearer " + TOKEN:
            self.rejected += 1
            return web.Response(status=401)
        self.headers.append({key.lower(): value for key, value in request.headers.items()})
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws = ws
        async for message in ws:
            if message.type == WSMsgType.TEXT:
                self.inbox.put_nowait(json.loads(message.data))
        return ws

    async def expect(self, predicate, timeout=10):
        async def scan():
            while True:
                message = await self.inbox.get()
                if predicate(message):
                    return message
        return await asyncio.wait_for(scan(), timeout)

    async def send(self, **message):
        await self.ws.send_str(json.dumps(message, ensure_ascii=False))

    def drain(self):
        messages = []
        while not self.inbox.empty():
            messages.append(self.inbox.get_nowait())
        return messages


class LinkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.out = Path(self.tmp.name)
        (self.out / "evals_stub.py").write_text(STUB, encoding="utf-8")
        self.stdout = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.stdout)
        self.redirect.__enter__()
        self.vllm, self.fe, self.runners = FakeVLLM(), FakeFrontend(), []
        self.vllm_url = await self.serve(self.vllm.app())
        self.fe_url = await self.serve(self.fe.app())
        self.link = self.task = None

    async def asyncTearDown(self):
        try:
            if self.task:
                self.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.task
            if self.link and self.link.eval_task:
                if self.link.eval_proc and self.link.eval_proc.returncode is None:
                    self.link.eval_proc.kill()
                await asyncio.wait_for(self.link.eval_task, 10)
            for runner in self.runners:
                await runner.cleanup()
        finally:
            self.redirect.__exit__(None, None, None)
            self.tmp.cleanup()

    async def serve(self, app):
        runner = web.AppRunner(app, shutdown_timeout=1)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        self.runners.append(runner)
        host, port = runner.addresses[0][:2]
        return f"http://{host}:{port}"

    async def start(self, *extra, hello=True):
        argv = ["--url", "ws" + self.fe_url[4:] + "/ws/link", "--token", TOKEN, "--vllm", self.vllm_url,
                "--out", str(self.out), "--status", str(self.out / "status.json"),
                "--eval-pkgs-ready", str(self.out / "evalpkgs.ready"), "--evals", str(self.out / "evals_stub.py"),
                "--interval", "0.1", *extra]
        self.link = backend_link.Link(backend_link.parse_args(argv))
        self.link.backoff = (0.05, 0.2)
        self.task = asyncio.create_task(self.link.run())
        return await self.fe.expect(lambda m: m["type"] == "hello") if hello else None

    async def collect(self, rid):
        messages = []
        while not messages or messages[-1]["type"] not in ("end", "error"):
            messages.append(await self.fe.expect(lambda m: m.get("id") == rid))
        return messages

    def stub_calls(self):
        path = self.out / "stub.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    async def test_relay_streams_utf8_split_across_reads(self):
        await self.start()
        body = {"model": "axk2", "messages": [{"role": "user", "content": PROMPT}], "stream": True}
        await self.fe.send(type="req", id="c1", path="/v1/chat/completions", body=body)
        messages = await self.collect("c1")
        self.assertEqual(messages[0], {"type": "head", "id": "c1", "status": 200})
        chunks = [m["chunk"] for m in messages if m["type"] == "data"]
        self.assertGreaterEqual(len(chunks), 2)
        self.assertEqual("".join(chunks), SSE.decode("utf-8"))
        self.assertNotIn("\ufffd", "".join(chunks))
        self.assertEqual(messages[-1], {"type": "end", "id": "c1"})
        self.assertEqual(self.vllm.requests, [body])
        self.assertEqual(self.link.tasks, {})
        self.assertNotIn(PROMPT, self.stdout.getvalue(), "prompts are never logged")
        self.assertNotIn(TEXT, self.stdout.getvalue(), "replies are never logged")

    async def test_relay_get_and_upstream_failure(self):
        await self.start()
        await self.fe.send(type="req", id="m1", path="/v1/models")
        messages = await self.collect("m1")
        self.assertEqual((messages[0]["status"], messages[-1]["type"]), (200, "end"))
        models = json.loads("".join(m["chunk"] for m in messages if m["type"] == "data"))
        self.assertEqual(models["data"][0]["id"], "axk2")
        await self.fe.send(type="req", id="b1", path="/v1/chat/completions",
                           body={"messages": [{"role": "user", "content": "boom"}]})
        messages = await self.collect("b1")
        self.assertEqual(messages[-1]["type"], "error")
        self.assertTrue(messages[-1]["message"])
        self.assertEqual(self.link.tasks, {})

    async def test_rejects_bad_requests_and_ignores_junk(self):
        await self.start()
        await self.fe.ws.send_str("not json")
        await self.fe.ws.send_str("[1, 2]")
        await self.fe.send(type="req", id="x1", path="/v1/completions/../../admin", body={})
        self.assertEqual((await self.fe.expect(lambda m: m.get("id") == "x1"))["message"],
                         "request rejected by the link")
        await self.fe.send(type="req", id="", path="/v1/models")
        self.assertEqual((await self.fe.expect(lambda m: m.get("id") == ""))["type"], "error")
        await self.fe.send(type="req", id="d1", path="/v1/chat/completions", body=HOLD)
        await self.fe.expect(lambda m: m.get("id") == "d1" and m["type"] == "head")
        await self.fe.send(type="req", id="d1", path="/v1/models")
        message = await self.fe.expect(lambda m: m.get("id") == "d1" and m["type"] == "error")
        self.assertEqual(message["message"], "request rejected by the link")
        self.assertIn("d1", self.link.tasks, "the original request keeps streaming")
        await self.fe.send(type="cancel", id="d1")
        await asyncio.wait_for(self.vllm.disconnected.wait(), 10)

    async def test_cancel_aborts_the_upstream_request(self):
        await self.start()
        await self.fe.send(type="req", id="h1", path="/v1/chat/completions", body=HOLD)
        await self.fe.expect(lambda m: m.get("id") == "h1" and m["type"] == "data")
        await self.fe.send(type="cancel", id="h1")
        await asyncio.wait_for(self.vllm.disconnected.wait(), 10)
        await asyncio.sleep(0.3)
        self.assertEqual([m for m in self.fe.drain() if m.get("id") == "h1" and m["type"] in ("end", "error")], [])
        self.assertEqual(self.link.tasks, {})

    async def test_dropped_socket_cancels_requests_and_reconnects(self):
        with mock.patch.dict(os.environ, {"AZUREML_RUN_ID": "axk2-demo-job-1"}):
            hello = await self.start()
            self.assertEqual(hello["job"], "axk2-demo-job-1")
            await self.fe.send(type="req", id="h2", path="/v1/chat/completions", body=HOLD)
            await self.fe.expect(lambda m: m.get("id") == "h2" and m["type"] == "head")
            await self.fe.ws.close()
            await asyncio.wait_for(self.vllm.disconnected.wait(), 10)
            await self.fe.expect(lambda m: m["type"] == "hello")
        self.assertEqual(len(self.fe.headers), 2)
        for headers in self.fe.headers:
            self.assertEqual((headers["authorization"], headers["x-axk2-job"]), ("Bearer " + TOKEN, "axk2-demo-job-1"))
        self.assertEqual(self.link.tasks, {})
        await self.fe.send(type="req", id="m2", path="/v1/models")
        self.assertEqual((await self.collect("m2"))[-1]["type"], "end")

    async def test_wrong_token_is_retried_with_backoff(self):
        await self.start("--token", "wrong", hello=False)
        async with asyncio.timeout(5):
            while self.fe.rejected < 2:
                await asyncio.sleep(0.05)
        self.assertGreaterEqual(self.fe.rejected, 2)
        self.assertEqual(self.fe.headers, [])

    async def test_status_reports_phase_progress_metrics_and_health(self):
        (self.out / "status.json").write_text(json.dumps({"phase": "loading", "since": 1700000000, "note": ""}),
                                              encoding="utf-8")
        (self.out / "full-download.log").write_text("[download] attempt 1: 703.9 GB on disk of 704.3 GB\n",
                                                    encoding="utf-8")
        (self.out / "vllm-demo.log").write_text("Loading safetensors checkpoint shards:  45% Completed | 72/160\n",
                                                encoding="utf-8")
        await self.start()
        status = await self.fe.expect(lambda m: m["type"] == "status")
        self.assertEqual(
            {k: status[k] for k in ("phase", "since", "download_gb", "download_of_gb", "load_step", "load_pct")},
            {"phase": "loading", "since": 1700000000, "download_gb": 703.9, "download_of_gb": 704.3,
             "load_step": "Loading safetensors checkpoint shards", "load_pct": 45})
        self.assertEqual((status["healthy"], status["metrics"]), (True, METRICS))
        self.assertEqual((status["inflight"], status["eval"], status["eval_pending"], status["eval_packages"]),
                         (0, None, False, "installing"))
        self.vllm.health = 503
        (self.out / "status.json").write_text("{broken", encoding="utf-8")
        status = await self.fe.expect(lambda m: m["type"] == "status" and not m["healthy"] and m["phase"] == "boot")
        self.assertIsNone(status["metrics"])

    async def test_eval_rejected(self):
        await self.start()
        await self.fe.send(type="eval", action="start", run="bad run")
        self.assertEqual((await self.fe.expect(lambda m: m["type"] == "eval"))["state"], "rejected")
        self.assertIsNone(self.link.pending_eval)

    async def test_eval_waits_for_packages_and_health_then_runs_once(self):
        self.vllm.health = 503
        await self.start()
        wish = {"type": "eval", "action": "start", "run": "r1", "suites": "kobalt,niah", "repeats": "niah=2",
                "limit": 3}
        await self.fe.send(**wish)
        status = await self.fe.expect(lambda m: m["type"] == "status" and m["eval_pending"])
        self.assertEqual(status["eval_packages"], "installing")
        (self.out / "evalpkgs.ready").touch()
        await self.fe.expect(lambda m: m["type"] == "status" and m["eval_packages"] == "ready")
        await asyncio.sleep(0.3)
        self.assertEqual(self.stub_calls(), [], "the eval waits for a healthy server")
        self.vllm.health = 200
        done = await self.fe.expect(lambda m: m["type"] == "eval")
        self.assertEqual({k: done[k] for k in ("run", "state", "rc", "suites", "repeats", "limit")},
                         {"run": "r1", "state": "done", "rc": 0, "suites": "kobalt,niah", "repeats": "niah=2",
                          "limit": 3})
        [call] = self.stub_calls()
        self.assertEqual(call["argv"], ["--base", self.vllm_url, "--run", "r1", "--suites", "kobalt,niah",
                                        "--out-dir", str(self.out), "--link", self.fe_url, "--repeats", "niah=2",
                                        "--limit", "3"])
        self.assertEqual((call["token"], call["http"]), (TOKEN, self.fe_url))
        self.assertIn("stub evals r1", (self.out / "evals-r1.log").read_text(encoding="utf-8"))
        # the frontend repeats its wish after every reconnect: a finished run is reported again, not rerun
        await self.fe.send(**wish)
        again = await self.fe.expect(lambda m: m["type"] == "eval")
        self.assertEqual((again["run"], again["state"]), ("r1", "done"))
        status = await self.fe.expect(lambda m: m["type"] == "status")
        self.assertEqual((status["eval"]["state"], status["eval_pending"]), ("done", False))
        self.assertEqual(len(self.stub_calls()), 1)

    async def test_eval_stop_and_failure(self):
        (self.out / "evalpkgs.ready").touch()
        await self.start()
        await self.fe.send(type="eval", action="start", run="hold1")
        await self.fe.expect(lambda m: m["type"] == "status" and (m["eval"] or {}).get("state") == "running")
        for _ in range(200):
            if self.stub_calls():
                break
            await asyncio.sleep(0.05)
        await self.fe.send(type="eval", action="start", run="other")  # one run at a time
        await self.fe.send(type="eval", action="stop")
        stopped = await self.fe.expect(lambda m: m["type"] == "eval")
        self.assertEqual((stopped["run"], stopped["state"]), ("hold1", "stopped"))
        self.assertNotEqual(stopped["rc"], 0)
        await self.fe.send(type="eval", action="start", run="fail1", suites="aime")
        failed = await self.fe.expect(lambda m: m["type"] == "eval")
        self.assertEqual((failed["run"], failed["state"], failed["rc"]), ("fail1", "failed", 5))
        self.assertIn("stub evals fail1", failed["tail"])
        self.assertEqual([call["argv"][3] for call in self.stub_calls()], ["hold1", "fail1"])

    async def test_eval_fails_when_the_packages_fail(self):
        (self.out / "evalpkgs.failed").touch()
        await self.start()
        await self.fe.send(type="eval", action="start", run="r9")
        failed = await self.fe.expect(lambda m: m["type"] == "eval")
        self.assertEqual((failed["run"], failed["state"]), ("r9", "failed"))
        self.assertIn("failed to install", failed["tail"])
        self.assertEqual(self.stub_calls(), [])


if __name__ == "__main__":
    unittest.main()
