"""demo/tools/model_api_smoke.py: its command line, its checks against the real app on loopback (scripted fake
vLLM, both profiles) and against a fake gateway that behaves like APIM: the key is taken only from `api-key`,
a refusal has the gateway's own 401 body and every answer has the gateway's own x-request-id."""
import asyncio
import io
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "tests", ROOT / "demo" / "tools"):
    sys.path.insert(0, str(path))
try:
    import model_api_smoke as smoke
    import test_demo_model_api_chat as chat_tests
    import test_demo_model_api_server as support
finally:
    del sys.path[:2]

KEY, Reply, say, call = support.KEY, support.Reply, support.say, support.call
ORDER = ["models", "chat", "stream", "tool", "tool_result", "auth"]
SECRETS = ("SECRETPW", "SECRETQS", "SECRETKEYVALUE")


class Outcome:
    """What one run of smoke.main left behind."""

    def __init__(self, code, out, err):
        self.code, self.out, self.err = code, out, err
        self.summary = json.loads(out) if out.strip() else None

    def check(self, name):
        return self.summary["checks"][name]


def main_once(argv, env=None, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    code = smoke.main(list(argv), environ=env or {}, stdin=io.StringIO(stdin), stdout=out, stderr=err)
    return Outcome(code, out.getvalue(), err.getvalue())


async def run_async(*argv, key=KEY, stdin=""):
    env = {"AXK2_MODEL_API_KEY": key} if key else {}
    return await asyncio.to_thread(main_once, argv, env, stdin)


class CommandLineTests(unittest.TestCase):
    def test_bad_usage_exits_2_and_never_echoes_what_was_typed(self):
        good = ["--base-url", "https://host.example/v1"]
        env = {"AXK2_MODEL_API_KEY": "abcdefgh12345678"}
        cases = [
            ([], env),
            (["--base-url", "ftp://host.example/v1"], env),
            (["--base-url", "https://user:SECRETPW@host.example/v1"], env),
            (["--base-url", "https://host.example/v1?api-key=SECRETQS"], env),
            (["--base-url", "https://host.example/v1#SECRETQS"], env),
            (["--base-url", "http://host.example/v1"], env),
            (["--base-url", "https://"], env),
            (good + ["--only", "bogus"], env),
            (good + ["--only", ","], env),
            (good + ["--max-tokens", "0"], env),
            (good + ["--max-tokens", "5000"], env),
            (good + ["--max-tokens", "SECRETKEYVALUE"], env),
            (good + ["--timeout", "0"], env),
            (good + ["--model", "bad model"], env),
            (good + ["--auth-header", "basic"], env),
            (good + ["--key", "SECRETKEYVALUE"], env),
            (good + ["--key-file", "x", "--key-stdin"], env),
            (good + ["--key-file", str(ROOT / "no" / "such" / "file")], env),
            (good, {}),
            (good, {"AXK2_MODEL_API_KEY": "short"}),
            (good, {"AXK2_MODEL_API_KEY": "has space in it 1234"}),
        ]
        for argv, environ in cases:
            with self.subTest(argv=[item for item in argv if item not in SECRETS]):
                outcome = main_once(argv, environ)
                self.assertEqual(outcome.code, 2, outcome.err)
                self.assertEqual(outcome.out, "")
                self.assertIn("error", outcome.err)
                for secret in SECRETS:
                    self.assertNotIn(secret, outcome.err)

    def test_the_key_comes_from_a_file_stdin_or_the_environment(self):
        parser = smoke.build_parser(io.StringIO())
        with tempfile.TemporaryDirectory() as folder:
            keyfile = Path(folder) / "key.txt"
            keyfile.write_bytes(b"\xef\xbb\xbf  file-key-0123456789  \r\nsecond line is ignored\r\n")
            cases = [
                (["--key-file", str(keyfile)], {}, "", "file-key-0123456789"),
                (["--key-stdin"], {}, "stdin-key-0123456789\nmore\n", "stdin-key-0123456789"),
                ([], {"AXK2_MODEL_API_KEY": "env-key-0123456789"}, "", "env-key-0123456789"),
                (["--key-env", "OTHER_NAME"], {"OTHER_NAME": "named-key-0123456789"}, "", "named-key-0123456789"),
            ]
            for extra, environ, stdin, wanted in cases:
                with self.subTest(extra=extra):
                    args = parser.parse_args(["--base-url", "https://host.example/v1"] + extra)
                    self.assertEqual(smoke.read_key(args, environ, io.StringIO(stdin)), wanted)

    def test_the_target_is_split_into_what_the_requests_need(self):
        cases = [
            ("https://host.example/v1", ("https", "host.example", 443, "/v1"), "https://host.example/v1"),
            ("https://host.example/models/nvfp4/v1/", ("https", "host.example", 443, "/models/nvfp4/v1"),
             "https://host.example/models/nvfp4/v1"),
            ("https://host.example", ("https", "host.example", 443, ""), "https://host.example"),
            ("http://127.0.0.1:8080/v1", ("http", "127.0.0.1", 8080, "/v1"), "http://127.0.0.1:8080/v1"),
            ("http://localhost/v1", ("http", "localhost", 80, "/v1"), "http://localhost/v1"),
            ("http://[::1]:9/v1", ("http", "::1", 9, "/v1"), "http://[::1]:9/v1"),
        ]
        for url, parts, label in cases:
            with self.subTest(url=url):
                target = smoke.parse_target(url)
                self.assertEqual((target.scheme, target.host, target.port, target.path), parts)
                self.assertEqual(target.label, label)

    def test_checks_are_selected_in_the_fixed_order(self):
        self.assertEqual(smoke.pick_checks(None), ORDER)
        self.assertEqual(smoke.pick_checks(["auth,chat"]), ["chat", "auth"])
        self.assertEqual(smoke.pick_checks(["tool_result", "MODELS", "models"]), ["models", "tool_result"])
        with self.assertRaises(smoke.Usage):
            smoke.pick_checks(["chat,nope"])


class StreamReaderTests(unittest.TestCase):
    """What the stream reader makes of SSE lines (the lines of a real answer are in the other classes)."""

    def feed(self, lines, gap=0.1):
        stream = smoke.Stream(0.0)
        for index, line in enumerate(lines):
            stream.take(line, gap * (index + 1))
        return stream

    def data(self, delta=None, finish=None, **extra):
        choice = {"index": 0, "delta": delta if delta is not None else {}, "finish_reason": finish}
        return "data: " + json.dumps(dict({"id": "c", "choices": [choice]}, **extra))

    def test_each_kind_of_line_is_counted(self):
        stream = self.feed([
            ": keepalive", self.data({"role": "assistant", "content": ""}), self.data({"content": "a"}),
            self.data({"content": "b"}), "event: message", "id: 7", "data: not json", "data: [1]",
            self.data({}, "stop"), 'data: {"choices": [], "usage": {"completion_tokens": 2}}', "data: [DONE]",
            "data: late"])
        self.assertEqual((stream.comment_lines, stream.event_lines, stream.other_lines), (1, 1, 1))
        self.assertEqual((stream.chunks, stream.content_chunks, stream.bad_json), (5, 2, 2))
        self.assertEqual((stream.done, stream.after_done, stream.finish_reason), (True, 1, "stop"))
        self.assertEqual("".join(stream.text), "ab")
        self.assertEqual(stream.usage, {"completion_tokens": 2})
        self.assertAlmostEqual(stream.spread(), 0.8)
        self.assertAlmostEqual(stream.max_gap, 0.5)

    def test_reasoning_tool_deltas_and_error_objects_are_noticed(self):
        stream = self.feed([self.data({"reasoning_content": "hm"}), self.data({"tool_calls": [{"index": 0}]}),
                            'data: {"error": {"code": "timeout", "message": "x"}}'])
        self.assertTrue(stream.reasoning)
        self.assertTrue(stream.tool_deltas)
        self.assertEqual(stream.errors, ["timeout"])

    def test_the_time_per_output_token_comes_from_the_content_chunks(self):
        stream = smoke.Stream(0.0)
        stream.take(self.data({"content": "a"}), 1.0)
        stream.take(self.data({"content": "b"}), 1.5)
        stream.take(self.data({"content": "c"}), 2.0)
        stream.take('data: {"choices": [], "usage": {"completion_tokens": 3}}', 2.1)
        self.assertAlmostEqual(stream.tpot_ms(), 500.0)
        self.assertEqual(smoke.Stream(0.0).tpot_ms(), None)

    def test_a_burst_of_chunks_looks_buffered_and_a_trickle_does_not(self):
        lines = [self.data({"content": str(n)}) for n in range(10)]
        self.assertEqual(self.feed(lines, gap=0.001).buffering(), "likely")
        self.assertEqual(self.feed(lines, gap=0.1).buffering(), "unlikely")
        self.assertEqual(self.feed(lines, gap=0.01).buffering(), "inconclusive")
        self.assertEqual(self.feed(lines[:4], gap=0.001).buffering(), "inconclusive")
        self.assertEqual(smoke.Stream(0.0).buffering(), "inconclusive")


class AgainstTheApiTests(chat_tests.LinkedCase):
    """The tool against the real app: the checks pass, and what they send is what the contract promises."""

    def four(self, tool=None):
        self.script(Reply(*say("Paris.")), Reply(*say("1 2 3", " 4 5")), Reply(*(tool or call())),
                    Reply(*say("It is 21 C and clear in Seoul.")))

    def url(self, path="/v1"):
        return self.base + path

    async def test_every_check_passes_and_at_most_four_calls_reach_the_gpu(self):
        self.four()
        outcome = await run_async("--base-url", self.url())
        self.assertEqual(outcome.code, 0, outcome.out)
        summary = outcome.summary
        self.assertEqual(list(summary["checks"]), ORDER)
        self.assertTrue(summary["ok"] and all(entry["ok"] for entry in summary["checks"].values()), outcome.out)
        self.assertEqual((summary["admissions"], summary["tool"], summary["auth_header"], summary["model"],
                          summary["max_tokens"]), (4, "model_api_smoke", "bearer", "ax-k2-fp8", 64))
        self.assertEqual(summary["target"], self.url())
        self.assertEqual(len(self.vllm.requests), 4)
        self.assertEqual(self.vllm.hits["models"], 0)

    async def test_the_requests_are_small_sequential_and_without_thinking(self):
        self.four()
        await run_async("--base-url", self.url(), "--max-tokens", "48")
        bodies = self.vllm.requests
        self.assertEqual([body["max_tokens"] for body in bodies], [48] * 4)
        self.assertTrue(all(body["chat_template_kwargs"] == {"enable_thinking": False} for body in bodies))
        self.assertEqual([bool(body.get("tools")) for body in bodies], [False, False, True, True])
        self.assertEqual(bodies[2]["tool_choice"], "auto")
        history = bodies[3]["messages"]
        self.assertEqual([message["role"] for message in history], ["user", "assistant", "tool"])
        self.assertEqual(history[1]["tool_calls"][0]["id"], "chatcmpl-tool-1")
        self.assertEqual(history[2]["tool_call_id"], "chatcmpl-tool-1")

    async def test_the_tool_result_check_replays_the_models_own_tool_call(self):
        self.four(call(arguments='{"city": "Busan"}', call_id="chatcmpl-tool-77"))
        outcome = await run_async("--base-url", self.url())
        self.assertEqual(outcome.check("tool")["tool_calls"],
                         [{"id": "chatcmpl-tool-77", "name": "get_weather", "arguments": '{"city": "Busan"}'}])
        self.assertEqual(outcome.check("tool")["finish_reason"], "tool_calls")
        self.assertEqual(outcome.check("tool_result")["history"], "model_call")
        self.assertTrue(outcome.check("tool_result")["mentions_result"])
        self.assertEqual(self.vllm.requests[3]["messages"][1]["tool_calls"][0]["function"]["arguments"],
                         '{"city": "Busan"}')

    async def test_every_check_records_the_request_id_of_the_answer(self):
        self.four()
        outcome = await run_async("--base-url", self.url())
        for name in ORDER[:-1]:
            entry = outcome.check(name)
            with self.subTest(check=name):
                self.assertEqual(entry["status"], 200)
                self.assertRegex(entry["sent_request_id"], r"^smoke-[0-9a-f]{8}-" + name + "$")
                self.assertEqual(entry["x_request_id"], entry["sent_request_id"])
                self.assertTrue(entry["request_id_echoed"])
                self.assertEqual(entry["served_profile"], "fp8")
        auth = outcome.check("auth")
        self.assertEqual(auth["x_request_id"], auth["no_key"]["x_request_id"])
        for probe in ("no_key", "wrong_key"):
            self.assertEqual(auth[probe]["status"], 401)
            self.assertEqual(auth[probe]["x_request_id"], auth[probe]["sent_request_id"])

    async def test_the_stream_check_keeps_the_timing_evidence(self):
        self.four()
        stream = (await run_async("--base-url", self.url())).check("stream")
        self.assertEqual((stream["done"], stream["event_lines"], stream["other_lines"], stream["finish_reason"]),
                         (True, 0, 0, "stop"))
        self.assertEqual((stream["chunks"], stream["content_chunks"]), (5, 2))
        self.assertEqual(stream["usage"], {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14})
        self.assertEqual(stream["content_type"].split(";")[0], "text/event-stream")
        self.assertEqual(stream["answer_preview"], "1 2 3 4 5")
        for field in ("connect_s", "headers_s", "first_chunk_s", "ttft_s", "first_to_last_chunk_s",
                      "max_chunk_gap_s", "total_s", "tpot_ms", "buffering", "comment_lines"):
            self.assertIn(field, stream)
        self.assertLessEqual(stream["first_chunk_s"], stream["total_s"])
        self.assertLessEqual(stream["first_chunk_s"], stream["ttft_s"])
        self.assertEqual(stream["buffering"], "inconclusive")

    async def test_the_api_key_header_style_works_against_the_app_too(self):
        self.four()
        outcome = await run_async("--base-url", self.url(), "--auth-header", "api-key")
        self.assertEqual(outcome.code, 0, outcome.out)
        self.assertEqual(outcome.summary["auth_header"], "api-key")

    async def test_a_wrong_key_stops_the_run_before_the_gpu(self):
        outcome = await run_async("--base-url", self.url(), key="a-wrong-key-0123456789")
        self.assertEqual(outcome.code, 1)
        self.assertFalse(outcome.summary["ok"])
        models = outcome.check("models")
        self.assertEqual((models["status"], models["error_code"], models["ok"]), (401, "invalid_api_key", False))
        for name in ORDER[1:]:
            self.assertEqual(outcome.check(name), {"ok": False, "skipped": "stopped after models: HTTP 401"})
        self.assertEqual((outcome.summary["admissions"], len(self.vllm.requests)), (0, 0))

    async def test_the_auth_check_alone_needs_no_key(self):
        outcome = await run_async("--base-url", self.url(), "--only", "auth", key=None)
        self.assertEqual(outcome.code, 0, outcome.out)
        self.assertEqual(list(outcome.summary["checks"]), ["auth"])
        self.assertEqual(outcome.summary["admissions"], 0)
        self.assertEqual(len(self.vllm.requests), 0)
        auth = outcome.check("auth")
        self.assertEqual((auth["no_key"]["status"], auth["wrong_key"]["status"], auth["problems"]), (401, 401, []))

    async def test_a_model_that_answers_in_text_fails_the_tool_check_and_the_run_goes_on(self):
        self.script(Reply(*say("Paris.")), Reply(*say("1 2 3")), Reply(*say("It is sunny.")),
                    Reply(*say("It is 21 C and clear in Seoul.")))
        outcome = await run_async("--base-url", self.url())
        self.assertEqual(outcome.code, 1)
        tool = outcome.check("tool")
        self.assertFalse(tool["ok"])
        self.assertEqual(tool["problems"][0], "the model answered in text instead of calling the tool")
        self.assertEqual(outcome.check("tool_result")["history"], "synthetic")
        self.assertTrue(outcome.check("tool_result")["ok"])
        self.assertEqual(self.vllm.requests[3]["messages"][1]["tool_calls"][0]["id"], "call_smoke_1")

    async def test_the_tool_check_says_why_a_tool_call_is_not_usable(self):
        cases = [
            (say("It", finish="length"), "the output limit cut the reply before a tool call (raise --max-tokens)"),
            (call(arguments='{"city": '), "function.arguments is not valid JSON"),
            (call(arguments='["Seoul"]'), "function.arguments is not a JSON object"),
            (call(name="delete_everything"), "a tool call names a function that was not offered"),
        ]
        for blocks, problem in cases:
            with self.subTest(problem=problem):
                self.script(Reply(*blocks))
                outcome = await run_async("--base-url", self.url(), "--only", "tool")
                self.assertEqual(outcome.code, 1)
                self.assertIn(problem, outcome.check("tool")["problems"])

    async def test_a_length_stop_passes_and_an_empty_answer_fails(self):
        self.script(Reply(*say("Par", "is", finish="length")))
        outcome = await run_async("--base-url", self.url(), "--only", "chat")
        self.assertEqual(outcome.code, 0, outcome.out)
        self.assertEqual(outcome.check("chat")["finish_reason"], "length")
        self.script(Reply(*say("")))
        outcome = await run_async("--base-url", self.url(), "--only", "chat")
        self.assertEqual(outcome.code, 1)
        self.assertEqual(outcome.check("chat")["problems"], ["the answer text is empty"])

    async def test_an_unavailable_backend_stops_the_run_with_the_retry_hint(self):
        await self.stop_link()
        await self.until(lambda: self.app.state.hub.ready_link() is None)
        outcome = await run_async("--base-url", self.url())
        self.assertEqual(outcome.code, 1)
        chat = outcome.check("chat")
        self.assertEqual((chat["status"], chat["error_code"], chat["retry_after"]), (503, "backend_unavailable", "30"))
        self.assertTrue(outcome.check("models")["ok"])
        self.assertEqual(outcome.check("stream"), {"ok": False, "skipped": "stopped after chat: HTTP 503"})
        self.assertEqual(outcome.summary["admissions"], 1)

    async def test_the_summary_can_be_kept_in_a_file_and_never_holds_the_key(self):
        self.four()
        with tempfile.TemporaryDirectory() as folder:
            keyfile, out = Path(folder) / "key.txt", Path(folder) / "evidence.json"
            keyfile.write_text(KEY + "\n", encoding="utf-8")
            outcome = await asyncio.to_thread(
                main_once, ["--base-url", self.url(), "--key-file", str(keyfile), "--out", str(out)])
            self.assertEqual(outcome.code, 0, outcome.out)
            kept = out.read_text(encoding="utf-8")
        self.assertEqual(kept, outcome.out)
        self.assertEqual(json.loads(kept)["run_id"], outcome.summary["run_id"])
        for text in (kept, outcome.out, outcome.err):
            self.assertNotIn(KEY, text)
            self.assertNotIn("Bearer", text)

    async def test_the_second_profile_is_reached_through_its_own_base_url(self):
        await self.start_child_link()
        self.four()
        outcome = await run_async("--base-url", self.url("/models/nvfp4/v1"), "--model", "ax-k2-nvfp4")
        self.assertEqual(outcome.code, 0, outcome.out)
        self.assertEqual(outcome.summary["target"], self.url("/models/nvfp4/v1"))
        for name in ORDER[:-1]:
            self.assertEqual(outcome.check(name)["served_profile"], "nvfp4")
        self.assertEqual(outcome.check("models")["model_ids"], ["ax-k2-nvfp4"])

    async def test_the_model_name_must_be_the_listed_one(self):
        outcome = await run_async("--base-url", self.url(), "--model", "ax-k2-nvfp4", "--only", "models")
        self.assertEqual(outcome.code, 1)
        self.assertEqual(outcome.check("models")["model_ids"], ["ax-k2-fp8"])
        self.assertIn("the requested model is not in the list (use the canonical name)",
                      outcome.check("models")["problems"])


def completion(message, finish="stop"):
    return {"id": "chatcmpl-gw", "object": "chat.completion", "created": 1, "model": "ax-k2-fp8",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}}


class Gateway:
    """The APIM front the smoke tool must also work against."""
    KEY = "gateway-subscription-key-0123456789"

    def __init__(self, *, gap=0.03, tokens=12, buffered=False, stall=0.0, defect=None):
        self.gap, self.tokens, self.buffered, self.stall, self.defect = gap, tokens, buffered, stall, defect
        self.seen, self.count = [], 0

    async def start(self):
        app = web.Application()
        app.router.add_get("/gw/v1/models", self.models)
        app.router.add_post("/gw/v1/chat/completions", self.chat)
        self.runner = web.AppRunner(app, shutdown_timeout=1)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        self.url = "http://127.0.0.1:%d/gw/v1" % self.runner.addresses[0][1]

    async def stop(self):
        await self.runner.cleanup()

    def note(self, request):
        self.count += 1
        self.seen.append({"path": request.path, "authorization": request.headers.get("Authorization"),
                          "api_key": request.headers.get("api-key"), "request_id": request.headers.get("X-Request-Id")})
        return {"x-request-id": "apim-%d" % self.count}

    def refuse(self, headers):
        body = {"statusCode": 401, "message": "Access denied due to missing or invalid subscription key."}
        return web.json_response(body, status=401, headers=headers)

    async def models(self, request):
        headers = self.note(request)
        if request.headers.get("api-key") != self.KEY:
            return self.refuse(headers)
        card = {"id": "ax-k2-fp8", "object": "model", "created": 1, "owned_by": "skt"}
        return web.json_response({"object": "list", "data": [card]}, headers=headers)

    async def chat(self, request):
        headers = self.note(request)
        if request.headers.get("api-key") != self.KEY:
            return self.refuse(headers)
        body = await request.json()
        if self.stall:
            await asyncio.sleep(self.stall)
        if body.get("stream"):
            return await self.stream(request, headers)
        roles = [message.get("role") for message in body["messages"]]
        if body.get("tools") and "tool" not in roles:
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_gw_1", "type": "function",
                 "function": {"name": "get_weather", "arguments": '{"city": "Seoul"}'}}]}
            return web.json_response(completion(message, "tool_calls"), headers=headers)
        text = "It is 21 C and clear in Seoul." if "tool" in roles else "Paris."
        return web.json_response(completion({"role": "assistant", "content": text}), headers=headers)

    async def stream(self, request, headers):
        response = web.StreamResponse(headers=dict(headers, **{"Content-Type": "text/event-stream"}))
        await response.prepare(request)
        blocks = say(*("%d " % number for number in range(self.tokens)), done=self.defect != "no_done")
        if self.defect == "event":
            blocks = ["event: message\n" + block for block in blocks]
        if self.buffered:
            await asyncio.sleep(self.gap * len(blocks))
            await response.write("".join(blocks).encode("utf-8"))
        else:
            for block in blocks:
                await response.write(block.encode("utf-8"))
                await asyncio.sleep(self.gap)
        await response.write_eof()
        return response


class AgainstAGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def start(self, **options):
        self.gateway = Gateway(**options)
        await self.gateway.start()
        self.addAsyncCleanup(self.gateway.stop)

    async def smoke(self, *argv, key=Gateway.KEY):
        return await run_async("--base-url", self.gateway.url, *argv, key=key)

    async def test_the_api_key_style_sends_the_key_only_in_the_api_key_header(self):
        await self.start()
        outcome = await self.smoke("--auth-header", "api-key")
        self.assertEqual(outcome.code, 0, outcome.out)
        self.assertEqual(outcome.summary["admissions"], 4)
        sent = [item for item in self.gateway.seen if item["api_key"] is not None]
        self.assertEqual(len(sent), 6)
        self.assertEqual({item["api_key"] for item in sent if not item["api_key"].startswith("wrong-")},
                         {Gateway.KEY})
        self.assertEqual([item["authorization"] for item in self.gateway.seen], [None] * len(self.gateway.seen))
        keyless = [item for item in self.gateway.seen if item["api_key"] is None]
        self.assertEqual(len(keyless), 1)
        self.assertEqual(keyless[0]["request_id"], outcome.check("auth")["no_key"]["sent_request_id"])

    async def test_the_bearer_style_sends_only_an_authorization_header(self):
        await self.start()
        outcome = await self.smoke("--only", "models")
        self.assertEqual(outcome.code, 1)
        self.assertEqual(self.gateway.seen[0]["authorization"], "Bearer " + Gateway.KEY)
        self.assertIsNone(self.gateway.seen[0]["api_key"])
        self.assertEqual(outcome.check("models")["status"], 401)

    async def test_the_gateways_own_401_body_is_enough_for_the_auth_check(self):
        await self.start()
        outcome = await self.smoke("--auth-header", "api-key", "--only", "auth", key=None)
        self.assertEqual(outcome.code, 0, outcome.out)
        auth = outcome.check("auth")
        self.assertEqual((auth["no_key"]["status"], auth["wrong_key"]["status"], auth["problems"]), (401, 401, []))
        self.assertEqual(self.gateway.count, 2)
        self.assertNotIn("subscription key", outcome.out)

    async def test_the_gateways_request_id_is_recorded_next_to_the_one_that_was_sent(self):
        await self.start()
        outcome = await self.smoke("--auth-header", "api-key")
        for name in ORDER[:-1]:
            entry = outcome.check(name)
            with self.subTest(check=name):
                self.assertRegex(entry["x_request_id"], r"^apim-\d+$")
                self.assertRegex(entry["sent_request_id"], r"^smoke-[0-9a-f]{8}-")
                self.assertFalse(entry["request_id_echoed"])
        self.assertRegex(outcome.check("auth")["no_key"]["x_request_id"], r"^apim-\d+$")

    async def test_a_trickling_stream_shows_that_the_gateway_does_not_buffer(self):
        await self.start()
        stream = (await self.smoke("--auth-header", "api-key")).check("stream")
        self.assertTrue(stream["ok"], stream)
        self.assertEqual((stream["chunks"], stream["content_chunks"], stream["done"]), (15, 12, True))
        self.assertGreaterEqual(stream["first_to_last_chunk_s"], 0.25)
        self.assertLess(stream["first_chunk_s"], stream["total_s"] - 0.25)
        self.assertEqual(stream["buffering"], "unlikely")
        self.assertIsNotNone(stream["tpot_ms"])

    async def test_a_buffering_gateway_is_visible_in_the_same_numbers(self):
        await self.start(buffered=True)
        stream = (await self.smoke("--auth-header", "api-key", "--only", "stream")).check("stream")
        self.assertTrue(stream["ok"], stream)
        self.assertLess(stream["first_to_last_chunk_s"], 0.05)
        self.assertGreaterEqual(stream["first_chunk_s"], 0.3)
        self.assertEqual(stream["buffering"], "likely")

    async def test_a_stream_that_breaks_the_protocol_is_named(self):
        for defect, problem in (("event", "the stream contains event: lines"),
                                ("no_done", "the stream did not end with data: [DONE]")):
            with self.subTest(defect=defect):
                await self.start(defect=defect, gap=0.0)
                outcome = await self.smoke("--auth-header", "api-key", "--only", "stream")
                self.assertEqual(outcome.code, 1)
                self.assertIn(problem, outcome.check("stream")["problems"])

    async def test_a_gateway_that_does_not_answer_ends_the_run_with_a_timeout(self):
        await self.start(stall=5.0)
        outcome = await self.smoke("--auth-header", "api-key", "--only", "chat,tool", "--timeout", "1")
        self.assertEqual(outcome.code, 1)
        self.assertEqual(outcome.check("chat")["error"], "TimeoutError")
        self.assertEqual(outcome.check("chat")["problems"], ["request failed: TimeoutError"])
        self.assertEqual(outcome.check("tool"), {"ok": False, "skipped": "stopped after chat: TimeoutError"})

    async def test_nothing_listening_is_a_failed_run_not_a_crash(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        outcome = await run_async("--base-url", "http://127.0.0.1:%d/v1" % port, "--only", "models,chat",
                                  "--timeout", "5", key=Gateway.KEY)
        self.assertEqual(outcome.code, 1)
        models = outcome.check("models")
        self.assertIsNone(models["status"])
        self.assertTrue(models["error"])
        self.assertTrue(outcome.check("chat")["skipped"].startswith("stopped after models: "))
        self.assertEqual(outcome.summary["admissions"], 0)

    async def test_no_output_holds_the_key_whatever_happens(self):
        await self.start()
        for argv in (("--auth-header", "api-key"), ("--only", "models"), ("--auth-header", "api-key", "--only", "auth")):
            with self.subTest(argv=argv):
                outcome = await self.smoke(*argv)
                for text in (outcome.out, outcome.err):
                    self.assertNotIn(Gateway.KEY, text)


if __name__ == "__main__":
    unittest.main()
