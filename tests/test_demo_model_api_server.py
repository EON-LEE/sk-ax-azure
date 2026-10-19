"""The model-only API on loopback: the real app under uvicorn with a key digest configured, the real
backend_link.Link as the GPU side and a scripted fake vLLM behind it. This file has the shared fixtures
(ApiCase, ScriptedVLLM, Reply) and covers authentication (also against the demo's own credentials), the static
model list, the health route, correlation headers and both profiles. What a chat call does is in
test_demo_model_api_chat.py, failures, limits and the log are in test_demo_model_api_failures.py, and the smoke
tool is tested in test_demo_model_api_smoke.py."""
import asyncio
import collections
import contextlib
import dataclasses
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "demo" / "frontend", ROOT / "aml" / "src", ROOT / "tests"):
    sys.path.insert(0, str(path))
try:
    import backend_link
    import model_api as api
    import test_demo_frontend as base
    import test_demo_model_api as units
finally:
    del sys.path[:3]

KEY, OTHER = "axk2-test-key-0123456789abcdef", "axk2-rotated-key-fedcba9876543210"
CHILD_TOKEN = "job-token-for-the-nvfp4-profile"
SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
NVFP4_FACTS = {"checkpoint": "skt/A.X-K2-NVFP4", "revision": "9e2e804e80f8d1b3afba5d7938173cec1ed46b49",
               "tp": 8, "pp": 1, "nodes": 1, "source_sha": SOURCE_SHA}
USER = base.USER  # the demo's own chat body, which has no model
ASK = {"model": "ax-k2-fp8", "messages": [{"role": "user", "content": "안녕"}]}
ASK_NVFP4 = dict(ASK, model="ax-k2-nvfp4")


def settings(*keys, **options):
    return api.Settings(key_hashes=tuple(api.key_digest(key) for key in keys), **options)


def say(*pieces, finish="stop", usage=True, reasoning=(), done=True):
    """The SSE blocks vLLM streams for a plain answer, one string per event."""
    items = [units.chunk({"role": "assistant", "content": ""})]
    items += [units.chunk({"reasoning_content": piece}) for piece in reasoning]
    items += [units.chunk({"content": piece}) for piece in pieces]
    items.append(units.chunk({}, finish))
    if usage:
        items.append(units.USAGE)
    return [units.sse(item) for item in items] + ([units.sse("[DONE]")] if done else [])


def call(name="get_weather", arguments='{"city": "Seoul"}', call_id="chatcmpl-tool-1"):
    """The SSE blocks vLLM streams for one tool call (hermes parser: the id and name first, then the arguments)."""
    half = len(arguments) // 2
    items = [units.chunk({"role": "assistant", "content": ""}),
             units.chunk({"tool_calls": [{"index": 0, "id": call_id, "type": "function",
                                          "function": {"name": name, "arguments": ""}}]}),
             units.chunk(units.calls(0, arguments=arguments[:half])),
             units.chunk(units.calls(0, arguments=arguments[half:])),
             units.chunk({}, "tool_calls"), units.USAGE]
    return [units.sse(item) for item in items] + [units.sse("[DONE]")]


class Reply:
    """One scripted answer of the fake vLLM: SSE text and pauses (seconds), or an error status; `ends` says how
    a 200 answer finishes: "eof" (normally), "hold" (comments until the caller goes away) or "cut" (the
    connection dies)."""

    def __init__(self, *items, status=200, text="", delay=0.0, ends="eof"):
        self.items, self.status, self.text, self.delay, self.ends = items, status, text, delay, ends


class ScriptedVLLM(base.FakeVLLM):
    def __init__(self):
        super().__init__()
        self.script, self.hits = collections.deque(), collections.Counter()

    async def models(self, request):
        self.hits["models"] += 1
        return await super().models(request)

    async def chat(self, request):
        self.hits["chat"] += 1
        if not self.script:
            return await super().chat(request)
        self.requests.append(await request.json())
        reply = self.script.popleft()
        if reply.delay:
            await asyncio.sleep(reply.delay)
        if reply.status != 200:
            return web.Response(status=reply.status, text=reply.text, content_type="application/json")
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        try:
            for item in reply.items:
                if isinstance(item, str):
                    await response.write(item.encode("utf-8"))
                else:
                    await asyncio.sleep(item)
            if reply.ends == "hold":
                while True:
                    await response.write(b": hold\n\n")
                    await asyncio.sleep(0.05)
            if reply.ends == "cut":
                request.transport.close()
                return response
            await response.write_eof()
        except ConnectionError:
            pass
        finally:
            if reply.ends == "hold":
                self.disconnected.set()
        return response


class Answer:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    @property
    def text(self):
        return self.body.decode("utf-8")

    def json(self):
        return json.loads(self.body)

    @property
    def error(self):
        return self.json()["error"]


class ApiCase(base.ServerCase):
    """The demo app with the model API on (keys / options say how), the fake vLLM scripted and no link yet."""
    keys, options = (KEY,), {}

    async def asyncSetUp(self):
        real, wanted = base.frontend.create_app, settings(*self.keys, **self.options)

        def create(config, *args, **kwargs):  # the NVFP4 profile is created by the same function
            return real(dataclasses.replace(config, model_api=wanted), *args, **kwargs)

        with mock.patch.object(base.frontend, "create_app", create), \
                mock.patch.object(base, "FakeVLLM", ScriptedVLLM):
            await super().asyncSetUp()
        self.model_api = getattr(self.app.state, "model_api", None)
        self.child_link = None
        self.plain = self.client()

    async def asyncTearDown(self):
        try:
            await self.stop_child_link()
        finally:
            await super().asyncTearDown()

    async def call(self, method, path, *, key=KEY, header="bearer", headers=None, **kwargs):
        sent = dict(headers or {})
        if key is not None:
            if header == "bearer":
                sent["Authorization"] = "Bearer " + key
            else:
                sent["api-key"] = key
        async with self.plain.request(method, path, headers=sent, **kwargs) as response:
            return Answer(response.status, response.headers, await response.read())

    async def chat(self, payload=ASK, *, path="/v1/chat/completions", **kwargs):
        return await self.call("POST", path, json=payload, **kwargs)

    def refused(self, answer, status, code, kind=None, param=None):
        """`answer` is an OpenAI-style error: shape, status, code and, when given, type and param."""
        self.assertEqual(answer.status, status, answer.text)
        self.assertEqual(answer.headers["Content-Type"].split(";")[0], "application/json")
        error = answer.json()["error"]
        self.assertEqual(set(error), {"message", "type", "param", "code"})
        self.assertEqual(error["code"], code, error)
        self.assertTrue(error["message"])
        if kind is not None:
            self.assertEqual(error["type"], kind)
        if param is not None:
            self.assertEqual(error["param"], param)
        return error

    def logs(self):
        """The model API's log lines in the captured stdout, parsed."""
        records = []
        for line in sys.stdout.getvalue().splitlines():
            with contextlib.suppress(ValueError):
                record = json.loads(line)
                if isinstance(record, dict) and record.get("event") == "model_api":
                    records.append(record)
        return records

    async def start_child_link(self, **facts):
        """A GPU link for the NVFP4 profile; its provenance is faked (it comes from a Linux-only staging file)."""
        child = self.app.state.profiles["nvfp4"]
        digest = base.token_digest(CHILD_TOKEN)
        data = child.state.store.data
        data["desired"], data["active"] = "on", digest
        data["jobs"][digest] = {"region": "uks", "name": "job-nvfp4", "status": "Running", "source_sha": SOURCE_SHA}
        self.enterContext(mock.patch.object(backend_link, "staged_provenance",
                                            return_value=dict(NVFP4_FACTS, **facts)))
        argv = ["--url", "ws" + self.base[4:] + "/models/nvfp4/ws/link", "--token", CHILD_TOKEN,
                "--vllm", self.vllm_url, "--out", str(self.root), "--status", str(self.root / "status-nvfp4.json"),
                "--eval-pkgs-ready", str(self.root / "evalpkgs.ready"), "--evals", str(self.root / "evals_stub.py"),
                "--interval", "0.1"]
        self.child_link = backend_link.Link(backend_link.parse_args(argv))
        self.child_link.backoff = (0.05, 0.2)
        self.child_task = asyncio.create_task(self.child_link.run())
        await self.until(lambda: child.state.hub.ready_link() is not None)
        return child

    async def stop_child_link(self):
        if self.child_link is None:
            return
        self.child_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.child_task
        if self.child_link.eval_task:
            await asyncio.wait_for(self.child_link.eval_task, 10)
        self.child_link = None


class AuthTests(ApiCase):
    async def test_a_key_is_required_everywhere(self):
        for method, path in (("GET", "/v1/models"), ("GET", "/v1/models/ax-k2-fp8"), ("GET", "/v1/health"),
                             ("POST", "/v1/chat/completions")):
            answer = await self.call(method, path, key=None, json=ASK if method == "POST" else None)
            self.refused(answer, 401, "missing_api_key", "invalid_request_error")
            self.assertEqual(answer.headers["WWW-Authenticate"], "Bearer")
        self.assertFalse(self.vllm.hits)

    async def test_wrong_keys_are_refused(self):
        for key in ("wrong", KEY[:-1], KEY + "x", "x" * 600):
            for header in ("bearer", "api-key"):
                answer = await self.call("GET", "/v1/models", key=key, header=header)
                self.refused(answer, 401, "invalid_api_key", "invalid_request_error")
                self.assertNotIn(KEY, answer.text)
        answer = await self.chat(key="wrong")
        self.refused(answer, 401, "invalid_api_key")
        self.assertFalse(self.vllm.hits)

    async def test_the_key_can_come_in_any_accepted_form(self):
        forms = ({}, {"header": "api-key"}, {"key": None, "headers": {"Authorization": "bearer " + KEY}},
                 {"key": None, "headers": {"Authorization": KEY}},  # without the scheme
                 {"key": None, "headers": {"Authorization": "Bearer wrong", "api-key": KEY}},  # one match is enough
                 {"key": None, "headers": {"Authorization": "Basic Zm9vOmJhcg==", "api-key": KEY}})
        for form in forms:
            answer = await self.call("GET", "/v1/models", **form)
            self.assertEqual(answer.status, 200, form)

    async def test_the_key_in_the_url_does_not_count(self):
        for path in ("/v1/models?api_key=" + KEY, "/v1/models?key=" + KEY, "/v1/models?api-key=" + KEY):
            self.refused(await self.call("GET", path, key=None), 401, "missing_api_key")

    async def test_repeated_wrong_keys_are_limited_per_address(self):
        for _ in range(10):
            self.refused(await self.call("GET", "/v1/models", key="wrong"), 401, "invalid_api_key")
        blocked = await self.call("GET", "/v1/models")  # even the right key waits now
        error = self.refused(blocked, 429, "rate_limit_exceeded", "rate_limit_error")
        self.assertNotIn(KEY, error["message"])
        self.assertGreater(int(blocked.headers["Retry-After"]), 0)
        self.refused(await self.chat(), 429, "rate_limit_exceeded")
        # another address (App Service appends the caller's address to X-Forwarded-For) is not affected
        elsewhere = await self.call("GET", "/v1/models", headers={"X-Forwarded-For": "203.0.113.7"})
        self.assertEqual(elsewhere.status, 200)
        await self.login()  # nor is the demo's own login limiter touched
        self.assertFalse(self.vllm.hits)

    async def test_missing_keys_are_not_counted_as_attempts(self):
        for _ in range(12):
            self.refused(await self.call("GET", "/v1/models", key=None), 401, "missing_api_key")
        self.assertEqual((await self.call("GET", "/v1/models")).status, 200)


class RotationTests(ApiCase):
    keys = (KEY, OTHER)

    async def test_old_and_new_key_work_while_both_digests_are_configured(self):
        for key in (KEY, OTHER):
            for header in ("bearer", "api-key"):
                self.assertEqual((await self.call("GET", "/v1/models", key=key, header=header)).status, 200)
        self.refused(await self.call("GET", "/v1/models", key="third"), 401, "invalid_api_key")


class DisabledTests(ApiCase):
    keys = ()

    async def test_without_a_key_digest_nothing_is_served(self):
        self.assertIsNone(self.model_api)
        self.assertIsNone(getattr(self.app.state.profiles["nvfp4"].state, "model_api", None))
        for prefix in ("", "/models/nvfp4"):
            for method, path in (("GET", "/v1/models"), ("GET", "/v1/models/ax-k2-fp8"), ("GET", "/v1/health"),
                                 ("POST", "/v1/chat/completions")):
                for key in (None, KEY):
                    answer = await self.call(method, prefix + path, key=key,
                                             json=ASK if method == "POST" else None)
                    self.assertEqual(answer.status, 404, (prefix, path, key))
                    self.assertNotIn(KEY, answer.text)
        self.assertFalse(self.vllm.hits)

    async def test_the_demo_is_unchanged(self):
        demo = await self.login()
        async with demo.get("/api/me") as response:
            self.assertEqual((response.status, (await response.json())["demo"]), (200, True))


class CrossAuthTests(ApiCase):
    async def test_demo_credentials_do_not_open_the_model_api(self):
        demo = await self.login()
        admin = await self.login(base.ADMIN, "/api/admin/login")
        for client in (demo, admin):  # their cookies are sent along
            async with client.get("/v1/models") as response:
                self.assertEqual(response.status, 401)
            async with client.post("/v1/chat/completions", json=ASK) as response:
                self.assertEqual(response.status, 401)
        passwords = (base.DEMO, base.ADMIN, "Bearer " + base.ADMIN)
        for secret in passwords[:2]:
            for header in ({"Authorization": "Bearer " + secret}, {"api-key": secret}):
                answer = await self.call("GET", "/v1/models", key=None, headers=header)
                self.refused(answer, 401, "invalid_api_key")
        self.assertFalse(self.vllm.hits)

    async def test_the_model_key_does_not_open_the_demo(self):
        for header in ("bearer", "api-key"):
            for method, path in (("GET", "/api/admin/state"), ("GET", "/api/status"), ("GET", "/api/results"),
                                 ("POST", "/api/chat"), ("GET", "/api/admin/events")):
                answer = await self.call(method, path, header=header, json=USER if method == "POST" else None)
                self.assertIn(answer.status, (401, 403, 404), (method, path, answer.status))
                self.assertNotIn("choices", answer.text)
        for path in ("/api/login", "/api/admin/login"):
            answer = await self.call("POST", path, key=None, json={"password": KEY})
            self.assertEqual(answer.status, 401, path)
        async with self.client().get("/api/me", headers={"Authorization": "Bearer " + KEY}) as response:
            self.assertEqual(await response.json(), {"demo": False, "admin": False, "open": False})

    async def test_the_key_alone_decides(self):
        demo = await self.login()
        async with demo.get("/v1/models", headers={"Authorization": "Bearer " + KEY}) as response:
            self.assertEqual(response.status, 200)  # a demo cookie neither helps nor hurts


class SurfaceTests(ApiCase):
    async def test_the_model_list_is_static_and_needs_no_gpu(self):
        answer = await self.call("GET", "/v1/models")
        self.assertEqual(answer.status, 200)
        body = answer.json()
        self.assertEqual((body["object"], [(m["id"], m["object"], m["owned_by"]) for m in body["data"]]),
                         ("list", [("ax-k2-fp8", "model", "skt")]))
        self.assertIsInstance(body["data"][0]["created"], int)
        await self.start_link()  # a ready GPU changes nothing and is not asked
        self.assertEqual((await self.call("GET", "/v1/models")).json(), body)
        self.assertEqual((await self.call("GET", "/v1/models?api-version=2024-10-21")).json(), body)
        self.assertFalse(self.vllm.hits)

    async def test_one_model_by_any_of_its_names(self):
        for name in ("ax-k2-fp8", "AX-K2-FP8", "skt/A.X-K2", "skt/a.x-k2"):
            answer = await self.call("GET", "/v1/models/" + name)
            self.assertEqual((answer.status, answer.json()["id"]), (200, "ax-k2-fp8"), name)
        for name in ("ax-k2-nvfp4", "skt/A.X-K2-NVFP4", "gpt-4o", "ax-k2-fp8/extra", "x" * 300):
            self.refused(await self.call("GET", "/v1/models/" + name), 404, "model_not_found",
                         "invalid_request_error", "model")

    async def test_health_is_a_separate_route(self):
        answer = await self.call("GET", "/v1/health")
        self.assertEqual(answer.status, 503)
        self.assertEqual(answer.json(), {"status": "unavailable", "ready": False, "state": "off",
                                         "profile": "fp8", "model": "ax-k2-fp8"})
        self.assertEqual(answer.headers["Retry-After"], "30")
        self.assertEqual((await self.call("GET", "/v1/models")).status, 200)  # the list does not depend on it
        await self.start_link()
        answer = await self.call("GET", "/v1/health")
        self.assertEqual(answer.status, 200)
        self.assertEqual(answer.json(), {"status": "ok", "ready": True, "state": "ready", "profile": "fp8",
                                         "model": "ax-k2-fp8"})
        self.assertNotIn("Retry-After", answer.headers)

    async def test_every_answer_is_correlated_and_not_cached(self):
        for method, path, key, status in (
                ("GET", "/v1/models", KEY, 200), ("GET", "/v1/models", None, 401), ("GET", "/v1/models", "bad", 401),
                ("GET", "/v1/health", KEY, 503), ("GET", "/v1/nope", KEY, 404), ("POST", "/v1/models", KEY, 405),
                ("GET", "/v1/chat/completions", KEY, 405), ("POST", "/v1/chat/completions", KEY, 400)):
            answer = await self.call(method, path, key=key)
            self.assertEqual(answer.status, status, (method, path))
            self.assertRegex(answer.headers["X-Request-Id"], r"^req_[0-9a-f]{32}$")
            self.assertEqual(answer.headers["X-Axk2-Profile"], "fp8")
            self.assertEqual(answer.headers["Cache-Control"], "no-store")
        self.refused(await self.call("GET", "/v1/nope"), 404, "not_found")
        wrong_method = await self.call("POST", "/v1/models")
        self.refused(wrong_method, 405, "method_not_allowed")
        self.assertIn("GET", wrong_method.headers["Allow"])

    async def test_a_safe_request_id_is_kept_and_an_unsafe_one_replaced(self):
        for given in ("trace-1.2_3:4", "A" * 128, "3f2504e0-4f89-11d3-9a0c-0305e82c3301"):
            answer = await self.call("GET", "/v1/models", headers={"X-Request-Id": given})
            self.assertEqual(answer.headers["X-Request-Id"], given)
            answer = await self.call("GET", "/v1/models", key=None, headers={"X-Request-Id": given})
            self.assertEqual(answer.headers["X-Request-Id"], given)  # also on an error
        for given in ("A" * 129, "has space", "semi;colon", "<script>", "a/b", "a=b"):
            answer = await self.call("GET", "/v1/models", headers={"X-Request-Id": given})
            self.assertRegex(answer.headers["X-Request-Id"], r"^req_[0-9a-f]{32}$", given)

    async def test_unknown_urls_and_methods_are_openai_errors(self):
        for path in ("/v1", "/v1/", "/v1/embeddings", "/v1/completions", "/v1/models/x/y/z"):
            answer = await self.call("GET", path)
            self.assertIn(answer.status, (404, 307, 405), path)
            if answer.status == 404:
                self.assertIn(answer.error["code"], ("not_found", "model_not_found"))
        for method in ("PUT", "DELETE", "PATCH"):
            self.refused(await self.call(method, "/v1/chat/completions", json=ASK), 405, "method_not_allowed")

    async def test_the_api_does_not_disturb_the_demo_routes(self):
        demo = await self.login()
        async with demo.get("/api/me") as response:
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertNotIn("X-Axk2-Profile", response.headers)
        async with self.client().get("/") as response:
            self.assertEqual(response.status, 200)
            self.assertNotIn("X-Axk2-Profile", response.headers)


class Nvfp4Tests(ApiCase):
    root_url = "/models/nvfp4/v1"

    async def test_the_second_profile_has_its_own_names_and_routes(self):
        answer = await self.call("GET", self.root_url + "/models")
        self.assertEqual(answer.status, 200)
        self.assertEqual([m["id"] for m in answer.json()["data"]], ["ax-k2-nvfp4"])
        self.assertEqual(answer.headers["X-Axk2-Profile"], "nvfp4")
        for name in ("ax-k2-nvfp4", "skt/A.X-K2-NVFP4"):
            answer = await self.call("GET", self.root_url + "/models/" + name)
            self.assertEqual((answer.status, answer.json()["id"]), (200, "ax-k2-nvfp4"))
        for name in ("ax-k2-fp8", "skt/A.X-K2"):
            self.refused(await self.call("GET", self.root_url + "/models/" + name), 404, "model_not_found")
        self.refused(await self.call("GET", self.root_url + "/models", key=None), 401, "missing_api_key")
        self.refused(await self.chat(ASK, path=self.root_url + "/chat/completions"), 404, "model_not_found",
                     param="model")
        self.refused(await self.chat(ASK_NVFP4), 404, "model_not_found", param="model")  # the root is FP8 only
        self.assertFalse(self.vllm.hits)

    async def test_the_profiles_are_ready_independently(self):
        await self.start_link()  # only the FP8 link is up
        self.assertEqual((await self.call("GET", "/v1/health")).status, 200)
        answer = await self.call("GET", self.root_url + "/health")
        self.assertEqual((answer.status, answer.json()["ready"], answer.json()["profile"]), (503, False, "nvfp4"))
        answer = await self.chat(ASK_NVFP4, path=self.root_url + "/chat/completions")
        self.refused(answer, 503, "backend_unavailable", "server_error")
        self.assertIn("Retry-After", answer.headers)
        self.assertFalse(self.vllm.hits)  # the FP8 link was not used for it

    async def test_a_ready_nvfp4_link_serves_only_the_nvfp4_routes(self):
        child = await self.start_child_link()
        await self.until(child.state.is_ready)
        answer = await self.call("GET", self.root_url + "/health")
        self.assertEqual((answer.status, answer.json()["ready"], answer.json()["state"]), (200, True, "ready"))
        self.assertEqual((await self.call("GET", "/v1/health")).status, 503)  # FP8 has no link
        self.vllm.script.append(Reply(*say("안녕", "하세요")))
        answer = await self.chat(ASK_NVFP4, path=self.root_url + "/chat/completions")
        self.assertEqual(answer.status, 200, answer.text)
        self.assertEqual(answer.headers["X-Axk2-Profile"], "nvfp4")
        self.assertEqual(answer.json()["choices"][0]["message"]["content"], "안녕하세요")
        self.assertEqual(self.vllm.requests[-1]["model"], "axk2")
        self.refused(await self.chat(ASK), 503, "backend_unavailable")  # FP8: no link, not served by this one
        self.assertEqual(len(self.vllm.requests), 1)
        self.assertEqual((self.model_api.gate.active, child.state.model_api.gate.active), (0, 0))

    async def not_ready_with(self, **facts):
        child = await self.start_child_link(**facts)
        await asyncio.sleep(0.5)  # a few status messages arrive; the link itself is healthy
        self.assertIsNotNone(child.state.hub.ready_link())
        self.assertFalse(child.state.is_ready())
        answer = await self.call("GET", self.root_url + "/health")
        self.assertEqual((answer.status, answer.json()["ready"]), (503, False))
        self.refused(await self.chat(ASK_NVFP4, path=self.root_url + "/chat/completions"), 503,
                     "backend_unavailable")
        self.assertFalse(self.vllm.hits)

    async def test_nvfp4_is_not_ready_for_another_build(self):
        await self.not_ready_with(source_sha="f" * 40)

    async def test_nvfp4_is_not_ready_with_another_topology(self):
        await self.not_ready_with(tp=4)


if __name__ == "__main__":
    unittest.main()
