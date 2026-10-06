"""Tests for the demo frontend (demo/frontend): request validation, the supervisor against a fake Azure, and the
app on loopback with uvicorn, the real backend_link.Link and a fake vLLM: passwords and cookies, static files
and security headers, chat streaming, the queue, cancelling, link loss, link authentication and evals."""
import asyncio
import contextlib
import io
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path

import aiohttp
import uvicorn
from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "demo" / "frontend", ROOT / "aml" / "src", ROOT / "tests"):
    sys.path.insert(0, str(path))
try:
    import app as frontend
    import backend_link
    from hub import Hub
    from store import Store, hash_password, token_digest
    from supervisor import Supervisor
    from test_backend_link import STUB, TEXT, FakeVLLM
finally:
    del sys.path[:3]

TOKEN = "job-token-for-frontend-tests"
DEMO, ADMIN = "demo-password", "admin-password"
USER = {"messages": [{"role": "user", "content": "안녕"}]}
HOLD = {"messages": [{"role": "user", "content": "hold"}]}


class ChatBodyTests(unittest.TestCase):
    def test_allow_list_and_fixed_sampling(self):
        body = frontend.chat_body({"messages": [{"role": "user", "content": "hi", "name": "x"}], "temperature": 2,
                                   "model": "other", "max_tokens": 10**9, "thinking": False, "tools": True})
        self.assertEqual(body["messages"], [{"role": "user", "content": "hi"}])
        self.assertEqual((body["model"], body["temperature"], body["top_p"]), ("axk2", 0.6, 0.95))
        self.assertEqual(body["max_tokens"], 8192)
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual([t["function"]["name"] for t in body["tools"]], ["calculator", "get_current_time",
                                                                           "get_weather"])
        body = frontend.chat_body(dict(USER, max_tokens=1))
        self.assertEqual((body["max_tokens"], "tools" in body), (16, False))
        self.assertTrue(body["chat_template_kwargs"]["enable_thinking"])

    def test_tool_turns(self):
        call = {"id": "call_1", "type": "function", "extra": 1,
                "function": {"name": "calculator", "arguments": "{\"expression\": \"1+1\"}"}}
        body = frontend.chat_body({"messages": [
            {"role": "user", "content": "1+1?"},
            {"role": "assistant", "content": None, "reasoning_content": "think", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": "call_1", "content": "2"}]})
        assistant, tool = body["messages"][1:]
        self.assertEqual(assistant, {"role": "assistant", "content": "", "reasoning_content": "think",
                                     "tool_calls": [{"id": "call_1", "type": "function", "function": call["function"]}]})
        self.assertEqual(tool, {"role": "tool", "content": "2", "tool_call_id": "call_1"})

    def test_rejects(self):
        bad_call = lambda **change: {"messages": [{"role": "assistant", "content": "", "tool_calls": [  # noqa: E731
            dict({"id": "c1", "function": {"name": "calculator", "arguments": "{}"}}, **change)]}]}
        for body in ({}, {"messages": []}, {"messages": "hi"}, {"messages": [{"role": "root", "content": "x"}]},
                     {"messages": [{"role": "user", "content": ["x"]}]}, {"messages": [USER["messages"][0]] * 401},
                     {"messages": [{"role": "tool", "content": "x", "tool_call_id": "bad id"}]},
                     bad_call(id="no spaces"), bad_call(function={"name": "shell", "arguments": "{}"}),
                     bad_call(function={"name": "calculator", "arguments": {}}),
                     dict(USER, max_tokens="many")):
            with self.subTest(body=str(body)[:80]), self.assertRaises(frontend.Problem):
                frontend.chat_body(body)

    def test_eval_spec(self):
        spec = frontend.eval_spec({"suites": "kobalt,niah", "repeats": "aime=4", "limit": 20, "run": "r-1"})
        self.assertEqual(spec, {"run": "r-1", "suites": "kobalt,niah", "repeats": "aime=4", "limit": 20})
        self.assertEqual(frontend.eval_spec({})["suites"], "aime,kobalt,click,ifbench,niah")
        self.assertRegex(frontend.eval_spec({})["run"], r"^ev\d{8}-\d{6}$")
        for body in ({"suites": "mmlu"}, {"suites": ["aime"]}, {"repeats": "aime=4;rm"}, {"limit": -1},
                     {"limit": "5"}, {"limit": 100001}, {"run": "../x"}, {"run": 5}):
            with self.subTest(body=body), self.assertRaises(frontend.Problem):
                frontend.eval_spec(body)

    def test_config_from_env(self):
        config = frontend.Config.from_env({"WEBSITE_HOSTNAME": "demo.example.net", "AXK2_WORKSPACES": "uks=w1, itn=w2",
                                           "AXK2_PRICES": "uks=9.5", "AXK2_MAX_ACTIVE": "4"})
        self.assertEqual(config.link_url, "wss://demo.example.net/ws/link")
        self.assertEqual(list(config.workspaces.items()), [("uks", "w1"), ("itn", "w2")])
        self.assertEqual((config.node_price("uks"), config.node_price("itn"), config.max_active), (9.5, 8.192, 4))


class FakeAzure:
    def __init__(self):
        self.statuses, self.counts, self.submitted, self.cancelled = {}, {}, [], []

    def submit(self, region, text):
        name = f"job-{region}-{len(self.submitted)}"
        self.submitted.append((region, text))
        self.statuses[name] = "Queued"
        return name

    def status(self, region, name):
        return self.statuses[name]

    def nodes(self, region):
        return self.counts.get(region, (0, 0))

    def cancel(self, region, name):
        self.cancelled.append(name)
        self.statuses[name] = "CancelRequested"


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.now = 1000.0
        self.config = frontend.Config(data=Path(self.tmp.name), workspaces={"uks": "w1", "itn": "w2", "frc": "w3"},
                                      partial_limit=100, cooldown=600, retry_window=300)
        self.store, self.azure, self.hub = Store(self.tmp.name), FakeAzure(), Hub()
        self.hub.active = lambda: self.store.data["active"]
        self.sup = Supervisor(self.config, self.store, self.hub, azure=self.azure, clock=lambda: self.now)
        self.sup.job_text = lambda token: f"token={token}"

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def regions(self):
        return sorted(job["region"] for job in self.store.data["jobs"].values())

    async def test_race_win_and_off(self):
        data = self.store.data
        await self.sup.tick()
        self.assertEqual(self.azure.submitted, [])  # off: nothing to do
        data["desired"] = "on"
        await self.sup.tick()
        self.assertEqual(self.regions(), ["frc", "itn", "uks"])
        token = self.azure.submitted[0][1].split("=", 1)[1]
        self.assertIn(token_digest(token), data["jobs"])  # only the digest is kept
        self.assertNotIn(token, json.dumps(data))
        self.azure.counts["itn"] = (2, 2)
        self.now += 45
        await self.sup.tick()
        self.assertEqual(self.regions(), ["itn"])
        self.assertEqual(data["jobs"][data["active"]]["region"], "itn")
        self.assertEqual(sorted(self.azure.cancelled), ["job-frc-2", "job-uks-0"])
        self.now += 45
        await self.sup.tick()
        self.assertGreater(data["cost"]["node_seconds"]["itn"], 0)
        data["desired"] = "off"
        await self.sup.tick()
        self.assertEqual((data["jobs"], data["active"]), ({}, None))
        self.assertIn("job-itn-1", self.azure.cancelled)

    async def test_ended_job_retries_its_region_then_races(self):
        data = self.store.data
        data["desired"] = "on"
        await self.sup.tick()
        self.azure.statuses["job-uks-0"] = "Running"
        await self.sup.tick()
        self.assertEqual(self.regions(), ["uks"])
        self.azure.statuses["job-uks-0"] = "Failed"
        self.now += 45
        await self.sup.tick()
        self.assertEqual(self.regions(), ["uks"])  # warm nodes: retry the same region first
        self.assertEqual((data["mode"], len(self.azure.submitted)), ("retry", 4))
        self.azure.statuses["job-uks-3"] = "Failed"
        self.now += 400
        await self.sup.tick()
        self.assertEqual(self.regions(), ["frc", "itn", "uks"])
        self.assertEqual(data["mode"], "race")

    async def test_partial_capacity_cools_down_and_failed_submit(self):
        data = self.store.data
        data["desired"] = "on"
        failing = self.azure.submit
        self.azure.submit = lambda region, text: (_ for _ in ()).throw(RuntimeError("quota")) if region == "frc" \
            else failing(region, text)
        await self.sup.tick()
        self.assertEqual(self.regions(), ["itn", "uks"])
        self.assertIn("frc", data["cooldown"])
        self.azure.counts["uks"] = (1, 1)
        await self.sup.tick()
        self.now += 101
        await self.sup.tick()
        self.assertEqual(self.regions(), ["itn"])
        self.assertIn("uks", data["cooldown"])
        self.assertIn("job-uks-0", self.azure.cancelled)

    async def test_stale_active_job_is_released(self):
        data = self.store.data
        data["desired"] = "on"
        await self.sup.tick()
        self.azure.counts["uks"] = (2, 2)
        await self.sup.tick()
        self.assertEqual(self.regions(), ["uks"])
        self.azure.counts["uks"] = (2, 1)  # a node was preempted and the link is gone
        self.now += self.config.stale_limit + 1
        await self.sup.tick()
        self.assertEqual(data["mode"], "race")
        self.assertEqual(self.regions(), ["frc", "itn", "uks"])


class ServerCase(unittest.IsolatedAsyncioTestCase):
    """The app on loopback; a link (backend_link.Link) dials in like node 0 of the GPU job does."""
    max_active, max_queue = 24, 50

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.tmp.name)
        (root / "results").mkdir()
        (root / "results" / "fig.png").write_bytes(b"\x89PNG\r\n")
        (root / "evals_stub.py").write_text(STUB, encoding="utf-8")
        self.root, self.runners, self.link = root, [], None
        self.stdout = contextlib.redirect_stdout(io.StringIO())
        self.stdout.__enter__()
        self.config = frontend.Config(
            data=root / "data", results_dir=root / "results", aml_dir=ROOT / "aml", session_secret="s" * 32,
            demo_hash=hash_password(DEMO, rounds=1000), admin_hash=hash_password(ADMIN, rounds=1000),
            workspaces={"uks": "w1"}, max_active=self.max_active, max_queue=self.max_queue)
        self.app = frontend.create_app(self.config, azure=FakeAzure(), start_supervisor=False)
        self.store = self.app.state.store
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.base = "http://127.0.0.1:%d" % sock.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level="critical", lifespan="on"))
        self.server.capture_signals = contextlib.nullcontext
        self.serving = asyncio.create_task(self.server.serve(sockets=[sock]))
        while not self.server.started:
            await asyncio.sleep(0.02)
        self.vllm = FakeVLLM()
        runner = web.AppRunner(self.vllm.app(), shutdown_timeout=1)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        self.runners.append(runner)
        self.vllm_url = "http://%s:%d" % runner.addresses[0][:2]
        self.clients = []

    async def asyncTearDown(self):
        try:
            await self.stop_link()
            for client in self.clients:
                await client.close()
            self.server.should_exit = True
            await asyncio.wait_for(self.serving, 10)
            for runner in self.runners:
                await runner.cleanup()
        finally:
            self.stdout.__exit__(None, None, None)
            self.tmp.cleanup()

    def client(self):
        client = aiohttp.ClientSession(self.base, cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.clients.append(client)
        return client

    async def login(self, password=DEMO, path="/api/login"):
        client = self.client()
        async with client.post(path, json={"password": password}) as response:
            self.assertEqual(response.status, 200)
        return client

    async def until(self, predicate, timeout=10):
        async def poll():
            while not predicate():
                await asyncio.sleep(0.05)
        await asyncio.wait_for(poll(), timeout)

    async def start_link(self):
        data = self.store.data
        digest = token_digest(TOKEN)
        data["desired"], data["active"] = "on", digest
        data["jobs"][digest] = {"region": "uks", "name": "job-1", "status": "Running"}
        argv = ["--url", "ws" + self.base[4:] + "/ws/link", "--token", TOKEN, "--vllm", self.vllm_url,
                "--out", str(self.root), "--status", str(self.root / "status.json"),
                "--eval-pkgs-ready", str(self.root / "evalpkgs.ready"), "--evals", str(self.root / "evals_stub.py"),
                "--interval", "0.1"]
        self.link = backend_link.Link(backend_link.parse_args(argv))
        self.link.backoff = (0.05, 0.2)
        self.link_task = asyncio.create_task(self.link.run())
        hub = self.app.state.hub
        await self.until(lambda: hub.ready_link() is not None)

    async def stop_link(self):
        if self.link is None:
            return
        self.link_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.link_task
        if self.link.eval_task:
            await asyncio.wait_for(self.link.eval_task, 10)
        self.link = None


async def events(response):
    """(event, data) pairs of an SSE response; vLLM's own events have no name."""
    buffer = ""
    async for chunk in response.content.iter_any():
        buffer += chunk.decode("utf-8")
        *complete, buffer = buffer.split("\n\n")
        for block in complete:
            lines = block.split("\n")
            name = next((line[7:] for line in lines if line.startswith("event: ")), "")
            payload = next((line[6:] for line in lines if line.startswith("data: ")), None)
            if payload is not None or name:
                yield name, payload


class AppTests(ServerCase):
    async def test_static_files_and_headers(self):
        client = self.client()
        async with client.get("/") as response:
            self.assertEqual(response.status, 200)
            self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        for name in ("app.js", "admin.js", "chat.js", "common.js", "markdown.js", "tools.js", "style.css"):
            async with client.get("/static/" + name) as response:
                self.assertEqual(response.status, 200, name)
        for path in ("/static/app.py", "/static/..%2Fapp.py", "/static/results", "/docs", "/openapi.json"):
            async with client.get(path) as response:
                self.assertEqual(response.status, 404, path)
        async with client.get("/admin") as response:
            self.assertIn("admin.js", await response.text())
        async with client.get("/api/me") as response:
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(await response.json(), {"demo": False, "admin": False})

    async def test_passwords_cookies_and_rotation(self):
        anonymous = self.client()
        for path in ("/api/status", "/api/results", "/api/results/figure/fig.png", "/api/admin/state"):
            async with anonymous.get(path) as response:
                self.assertEqual(response.status, 401, path)
        async with anonymous.post("/api/login", data="password=x") as response:
            self.assertEqual(response.status, 415)
        demo = await self.login()
        async with demo.get("/api/me") as response:
            self.assertEqual(await response.json(), {"demo": True, "admin": False})
        async with demo.get("/api/status") as response:
            self.assertEqual((await response.json())["power"], "off")
        async with demo.get("/api/results/figure/fig.png") as response:
            self.assertEqual(response.status, 200)
        async with demo.get("/api/admin/state") as response:
            self.assertEqual(response.status, 401)
        async with self.client().post("/api/admin/login", json={"password": DEMO}) as response:
            self.assertEqual(response.status, 401)
        admin = await self.login(ADMIN, "/api/admin/login")
        async with admin.get("/api/admin/state") as response:
            self.assertEqual(response.status, 200)
        async with self.client().get("/api/admin/state", headers={"Authorization": "Bearer " + ADMIN}) as response:
            self.assertEqual(response.status, 200)
        async with admin.post("/api/admin/password", json={"role": "demo"}) as response:
            fresh = (await response.json())["password"]
        async with demo.get("/api/status") as response:
            self.assertEqual(response.status, 401)  # rotating signs everyone out
        async with self.client().post("/api/login", json={"password": DEMO}) as response:
            self.assertEqual(response.status, 401)
        await self.login(fresh)
        await self.login(ADMIN)  # the admin password opens the demo too
        async with admin.post("/api/admin/password", json={"role": "admin"}) as response:
            self.assertEqual(response.status, 200)
        async with admin.get("/api/admin/state") as response:
            self.assertEqual(response.status, 200)  # the rotating admin stays signed in
        self.assertNotIn(fresh, Path(self.config.data, "state.json").read_text(encoding="utf-8"))

    async def test_login_rate_limit(self):
        client = self.client()
        for _ in range(8):
            async with client.post("/api/login", json={"password": "wrong"}) as response:
                self.assertEqual(response.status, 401)
        async with client.post("/api/login", json={"password": DEMO}) as response:
            self.assertEqual(response.status, 429)

    async def test_chat_needs_a_ready_cluster(self):
        demo = await self.login()
        async with demo.post("/api/chat", json=USER) as response:
            self.assertEqual((response.status, (await response.json())["error"]), (503, "not_ready"))
        async with demo.post("/api/chat", json={"messages": []}) as response:
            self.assertEqual(response.status, 400)

    async def test_link_needs_a_known_token(self):
        async with self.client() as client:
            for headers in ({}, {"Authorization": "Bearer wrong"}):
                with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
                    await client.ws_connect("/ws/link", headers=headers)
                self.assertEqual(caught.exception.status, 403)
            async with client.get("/api/link/evals", headers={"Authorization": "Bearer wrong"}) as response:
                self.assertEqual(response.status, 403)
            async with client.get("/api/link/src", headers={"Authorization": "Bearer wrong"}) as response:
                self.assertEqual(response.status, 403)

class LinkedTests(ServerCase):
    max_active, max_queue = 1, 1

    async def test_chat_streams_through_the_link(self):
        demo = await self.login()
        async with demo.get("/api/status") as response:
            self.assertEqual((await response.json())["power"], "off")
        await self.start_link()
        async with demo.get("/api/status") as response:
            status = await response.json()
        self.assertEqual((status["power"], status["region"], status["healthy"]), ("ready", "uks", True))
        self.assertEqual(status["metrics"]["running"], 3.0)
        async with demo.post("/api/chat", json=dict(USER, thinking=False, temperature=2)) as response:
            self.assertEqual(response.status, 200)
            got = [item async for item in events(response)]
        self.assertEqual(got[0], ("start", "{}"))
        self.assertEqual(got[-1], ("end", "{}"))
        text = "".join(json.loads(data)["choices"][0]["delta"]["content"] for name, data in got
                       if not name and data != "[DONE]")
        self.assertEqual(text, TEXT)
        sent = self.vllm.requests[-1]
        self.assertEqual((sent["model"], sent["temperature"], sent["chat_template_kwargs"]),
                         ("axk2", 0.6, {"enable_thinking": False}))

    async def test_queue_busy_and_cancel(self):
        await self.start_link()
        first, second, third = await self.login(), await self.login(), await self.login()
        hold = await first.post("/api/chat", json=HOLD)
        stream = events(hold)
        self.assertEqual(await anext(stream), ("start", "{}"))
        waiting = await second.post("/api/chat", json=USER)
        queued = events(waiting)
        name, data = await anext(queued)
        self.assertEqual((name, json.loads(data)["position"]), ("queue", 1))
        async with third.post("/api/chat", json=USER) as response:
            self.assertEqual((response.status, (await response.json())["error"]), (503, "busy"))
        hold.close()  # the browser stops: the link cancels the vLLM request and the queue moves on
        await asyncio.wait_for(self.vllm.disconnected.wait(), 10)
        rest = [item async for item in queued]
        waiting.close()
        self.assertIn(("start", "{}"), rest)
        self.assertEqual(rest[-1], ("end", "{}"))
        gate = self.app.state.gate
        await self.until(lambda: gate.active == 0 and not gate.waiting)

    async def test_link_loss_ends_the_stream(self):
        await self.start_link()
        demo = await self.login()
        async with demo.post("/api/chat", json=HOLD) as response:
            stream = events(response)
            self.assertEqual(await anext(stream), ("start", "{}"))
            await self.stop_link()
            rest = [item async for item in stream]
        name, data = rest[-1]
        self.assertEqual((name, json.loads(data)["code"]), ("error", "link"))
        async with demo.get("/api/status") as response:
            self.assertEqual((await response.json())["power"], "starting")

    async def test_eval_wish_runs_on_the_link_and_uploads(self):
        (self.root / "evalpkgs.ready").write_text("ok", encoding="utf-8")
        admin = await self.login(ADMIN, "/api/admin/login")
        for body in ({"action": "start", "suites": "mmlu"}, {"action": "go"}):
            async with admin.post("/api/admin/eval", json=body) as response:
                self.assertEqual(response.status, 400)
        async with admin.post("/api/admin/eval", json={"action": "start", "suites": "kobalt", "limit": 3,
                                                        "run": "r1"}) as response:
            self.assertEqual((await response.json())["eval_wish"]["run"], "r1")
        await self.start_link()  # the wish waits for a link and is sent when it says hello
        data = self.store.data
        await self.until(lambda: (data["eval_runs"].get("r1") or {}).get("state") == "done")
        self.assertIsNone(data["eval_wish"])
        argv = json.loads((self.root / "stub.jsonl").read_text(encoding="utf-8").splitlines()[-1])["argv"]
        self.assertEqual(argv[argv.index("--suites") + 1], "kobalt")
        self.assertEqual(argv[argv.index("--limit") + 1], "3")
        auth = {"Authorization": "Bearer " + TOKEN}
        client = self.client()
        record = {"suite": "kobalt", "id": "q1", "repeat": 0, "correct": True}
        async with client.post("/api/link/evals", json={"run": "r1", "records": [record]}, headers=auth) as response:
            self.assertEqual(response.status, 200)
        async with client.post("/api/link/evals", json={"run": "../r", "records": []}, headers=auth) as response:
            self.assertEqual(response.status, 400)
        async with client.get("/api/link/evals?run=r1", headers=auth) as response:
            self.assertEqual((await response.json())["records"], [record])
        async with client.get("/api/link/src", headers=auth) as response:
            self.assertEqual(response.status, 200)
            source = await response.read()
        import hashlib, io, re, tarfile
        with tarfile.open(fileobj=io.BytesIO(source), mode="r:gz") as tar:
            self.assertIn("entry.sh", tar.getnames())
        supervisor = self.app.state.supervisor  # the digest the job checks matches what is served
        job = supervisor.renderer({"AXK2_LINK_URL": "wss://demo.example.net/ws/link", "AXK2_LINK_TOKEN": "tok"})
        self.assertIn(f'AXK2_SRC_SHA256: "{hashlib.sha256(source).hexdigest()}"', job)
        self.assertIn('AXK2_SRC_URL: "https://demo.example.net/api/link/src"', job)
        self.assertIsNone(re.search(r"AXK2_SRC_B64", job))
        async with admin.get("/api/results") as response:
            results = await response.json()
        self.assertEqual(([r["run"] for r in results["runs"]], results["current"]), (["r1"], "r1"))
        self.assertEqual(results["figures"], ["fig.png"])
