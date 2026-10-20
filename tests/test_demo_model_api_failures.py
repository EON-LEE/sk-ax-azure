"""Failures and protection of the model-only API, on loopback like its sibling test files (the fixtures come from
test_demo_model_api_server.py and test_demo_model_api_chat.py): the GPU link missing or lost, vLLM refusing or
answering badly, the time limit, the server's own errors, concurrency and rate limits, a caller that leaves while
the GPU is working, and what the one-line log holds and never holds. Keys, addresses and names are fakes."""
import asyncio
import contextlib
import json
import logging
import re
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
try:
    import test_demo_model_api as units
    import test_demo_model_api_chat as chat_tests
    import test_demo_model_api_server as support
finally:
    del sys.path[:1]

ASK, KEY, OTHER, JSON = support.ASK, support.KEY, support.OTHER, chat_tests.JSON
Reply, say = support.Reply, support.say
STREAM_ASK = dict(ASK, stream=True)
BOTH = (ASK, STREAM_ASK)
SECRET = "internal-detail-7f3a91"
TRACE = ('Traceback (most recent call last): File "/srv/vllm/engine.py", line 88, in step: '
         "RuntimeError: CUDA error on 192.0.2.7")
TRACE_JSON = json.dumps({"error": {"message": TRACE, "type": "InternalServerError", "code": 500}})
LEAKS = (SECRET, "Traceback", "/srv/vllm", "engine.py", "CUDA", "192.0.2.7")
LOG_KEYS = {"ts", "event", "request_id", "profile", "route", "stream", "status", "prompt_tokens", "completion_tokens",
            "ttft_ms", "queue_ms", "total_ms", "finish_reason", "error"}
ANSWERED_KEYS = LOG_KEYS | {"tool_calls"}  # the line of a request whose answer from vLLM was read


def held():
    """An answer that has begun and then sends only comments until its caller goes away."""
    return Reply(*say("x")[:2], ends="hold")


def leaked(text):
    """What only an internal error would show: a traceback, a path, an address, an exception class."""
    return [word for word in LEAKS if word in text] + re.findall(r"[A-Za-z]Error\b", text)


def mode(payload):
    return "stream" if payload.get("stream") else "plain"


class FailureCase(chat_tests.LinkedCase):
    """The FP8 link is up (LinkedCase); a test scripts what the fake vLLM does."""

    async def idle(self):
        """Nothing is left running or waiting in this API's gate."""
        gate = self.model_api.gate
        await self.until(lambda: gate.active == 0 and not gate.waiting)

    def quiet_fake_vllm(self):
        """A fake vLLM that wakes up after its caller has been cancelled makes aiohttp log it; that is not under test."""
        server_log = logging.getLogger("aiohttp.server")
        server_log.disabled = True
        self.addCleanup(setattr, server_log, "disabled", False)

    async def logged(self, count):
        """The log lines so far, once at least `count` are there (a line is written after the response is sent)."""
        await self.until(lambda: len(self.logs()) >= count)
        return self.logs()

    async def drop(self, task):
        """Cancel a call as a vanished caller does and wait until it is over."""
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def event_error(self, answer, code, kind="server_error"):
        """`answer` is a stream that had begun (HTTP 200) and ends with one error event and [DONE]; returns the
        chunks before the error."""
        self.assertEqual(answer.status, 200, answer.text)
        self.assertEqual(answer.headers["Content-Type"].split(";")[0], "text/event-stream")
        items, done = chat_tests.chunks(answer.text)
        self.assertTrue(done, answer.text)
        self.assertEqual(answer.text.count("[DONE]"), 1)
        error = items[-1]["error"]
        self.assertEqual(set(error), {"message", "type", "param", "code"}, error)
        self.assertEqual((error["code"], error["type"]), (code, kind))
        self.assertTrue(error["message"])
        self.assertNotIn("event:", answer.text)
        self.assertEqual(leaked(answer.text), [])
        return items[:-1]

    async def after_the_start(self, reply, status, code, retry_after=None):
        """A failure after vLLM has begun to answer: an HTTP error for a plain call, an error event for a stream
        (its 200 has been sent). Returns the plain call's error and the stream's chunks before its error."""
        self.script(reply, reply)
        plain, streamed = await self.chat(), await self.chat(STREAM_ASK)
        error = self.refused(plain, status, code, "server_error")
        if retry_after is not None:
            self.assertEqual(plain.headers["Retry-After"], retry_after)
        before = self.event_error(streamed, code)
        self.assertEqual(leaked(plain.text), [])
        await self.idle()
        return error, before


class NoLinkTests(support.ApiCase):
    """No GPU link at all (ApiCase starts none)."""

    async def test_a_chat_call_is_a_retryable_503_in_both_forms(self):
        for payload in BOTH:
            with self.subTest(mode=mode(payload)):
                answer = await self.chat(payload)
                self.refused(answer, 503, "backend_unavailable", "server_error")
                self.assertEqual(answer.headers["Retry-After"], "30")
        self.assertEqual((self.vllm.hits["chat"], self.model_api.gate.active), (0, 0))


class LinkLossTests(FailureCase):
    async def test_a_plain_call_is_a_503_and_so_is_the_next_one(self):
        self.script(held())
        pending = asyncio.create_task(self.chat())
        await self.until(lambda: self.vllm.hits["chat"] == 1)
        await self.stop_link()
        answer = await asyncio.wait_for(pending, 10)
        self.refused(answer, 503, "backend_unavailable", "server_error")
        self.assertEqual(answer.headers["Retry-After"], "5")
        await self.until(lambda: self.app.state.hub.ready_link() is None)
        answer = await self.chat()
        self.refused(answer, 503, "backend_unavailable", "server_error")
        self.assertEqual(answer.headers["Retry-After"], "30")
        await self.idle()

    async def test_a_stream_ends_with_an_error_event(self):
        self.script(held())
        async with self.plain.post("/v1/chat/completions", json=STREAM_ASK,
                                   headers={"Authorization": "Bearer " + KEY}) as response:
            first = await asyncio.wait_for(response.content.readline(), 10)  # the answer has begun: HTTP 200 is sent
            self.assertTrue(first.startswith(b"data:"), first)
            await self.stop_link()
            rest = await asyncio.wait_for(response.read(), 10)
            answer = support.Answer(response.status, response.headers, first + rest)
        self.assertEqual(chat_tests.content_of(self.event_error(answer, "backend_unavailable")), "x")
        await self.idle()

    async def test_a_call_that_has_not_begun_is_a_plain_503_also_for_a_stream(self):
        self.script(Reply(*say("late"), delay=1.0))
        self.quiet_fake_vllm()
        pending = asyncio.create_task(self.chat(STREAM_ASK))
        await self.until(lambda: self.vllm.hits["chat"] == 1)
        await self.stop_link()
        answer = await asyncio.wait_for(pending, 10)
        self.refused(answer, 503, "backend_unavailable", "server_error")
        self.assertEqual(answer.headers["Retry-After"], "5")
        await self.idle()


class DeadVllmTests(FailureCase):
    async def test_a_dead_vllm_behind_a_live_link_is_a_retryable_503(self):
        await self.runners.pop(0).cleanup()  # vLLM is gone, the link is still connected
        for payload in BOTH:
            with self.subTest(mode=mode(payload)):
                answer = await self.chat(payload)
                self.refused(answer, 503, "backend_unavailable", "server_error")
                # 5: the link's request failed; 30: the link's status already says "unhealthy"
                self.assertIn(answer.headers["Retry-After"], ("5", "30"))
                self.assertEqual(leaked(answer.text), [])
        await self.idle()


class UpstreamRefusalTests(FailureCase):
    """vLLM answers with an error status: only validation problems keep vLLM's own words."""

    async def test_a_context_that_is_too_long_is_a_400_with_its_own_code(self):
        too_long = json.dumps({"error": {"message": "This model's maximum context length is 131072 tokens. However, "
                                                    "you requested 150000 tokens in the messages.",
                                         "type": "BadRequestError", "code": 400}})
        for payload in BOTH:
            with self.subTest(mode=mode(payload)):
                self.script(Reply(status=400, text=too_long))
                answer = await self.chat(payload)
                error = self.refused(answer, 400, "context_length_exceeded", "invalid_request_error", "messages")
                self.assertIn("maximum context length", error["message"])
                self.assertEqual(leaked(answer.text), [])
        await self.idle()

    async def test_other_validation_errors_keep_vllms_message(self):
        cases = ((400, '{"error": {"message": "max_tokens must be at least 1, got -5."}}'),
                 (422, '{"message": "max_tokens must be at least 1, got -5."}'))
        for payload in BOTH:
            for status, text in cases:
                with self.subTest(mode=mode(payload), status=status):
                    self.script(Reply(status=status, text=text))
                    error = self.refused(await self.chat(payload), 400, "invalid_request", "invalid_request_error")
                    self.assertEqual(error["message"], "max_tokens must be at least 1, got -5.")
                    self.assertIsNone(error["param"])
        await self.idle()

    async def test_a_busy_vllm_is_a_retryable_503_without_its_text(self):
        for payload in BOTH:
            for status in (429, 503):
                with self.subTest(mode=mode(payload), status=status):
                    self.script(Reply(status=status, text=TRACE_JSON))
                    answer = await self.chat(payload)
                    self.refused(answer, 503, "backend_unavailable", "server_error")
                    self.assertEqual(answer.headers["Retry-After"], "5")
                    self.assertEqual(leaked(answer.text), [])
        await self.idle()

    async def test_a_failing_vllm_is_a_502_and_never_the_callers_own_401_or_404(self):
        html = "<html><body><h1>502 Bad Gateway</h1>proxy at 192.0.2.7</body></html>"
        cases = ((401, '{"error": "Unauthorized"}'), (404, TRACE_JSON), (500, TRACE_JSON), (502, html))
        for payload in BOTH:
            for status, text in cases:
                with self.subTest(mode=mode(payload), status=status):
                    self.script(Reply(status=status, text=text))
                    answer = await self.chat(payload)
                    error = self.refused(answer, 502, "upstream_error", "server_error")
                    self.assertEqual(error["message"], "The model backend failed to process the request.")
                    self.assertEqual(leaked(answer.text), [])
        self.assertEqual(self.vllm.hits["chat"], 8)
        await self.idle()


class BrokenAnswerTests(FailureCase):
    """vLLM's answer starts well and then breaks."""

    async def test_a_connection_cut_in_the_middle(self):
        await self.after_the_start(Reply(*say("abc")[:2], ends="cut"), 503, "backend_unavailable", retry_after="5")

    async def test_an_answer_that_ends_without_a_finish(self):
        error, before = await self.after_the_start(Reply(*say("abc")[:2]), 502, "upstream_error")
        self.assertIn("ended the response early", error["message"])
        self.assertEqual(chat_tests.content_of(before), "abc")  # what arrived was passed on

    async def test_a_chunk_that_is_not_json(self):
        error, before = await self.after_the_start(Reply("data: {not json\n\n"), 502, "upstream_error")
        self.assertEqual(before, [])
        self.assertNotIn("not json", error["message"])

    async def test_an_error_chunk_from_vllm_is_reported_without_its_text(self):
        reported = units.sse({"error": {"message": TRACE, "type": "InternalServerError", "code": 500}})
        error, _ = await self.after_the_start(Reply(reported), 502, "upstream_error")
        self.assertIn("reported an error", error["message"])


class DeadlineTests(FailureCase):
    options = {"timeout": 1.0}

    async def test_a_generation_that_does_not_finish_is_a_504(self):
        self.script(held())
        started = time.monotonic()
        answer = await self.chat()
        self.assertGreaterEqual(time.monotonic() - started, 0.9)
        self.refused(answer, 504, "timeout", "server_error")
        await asyncio.wait_for(self.vllm.disconnected.wait(), 10)  # the GPU work was cancelled
        await self.idle()

    async def test_a_stream_that_does_not_finish_ends_with_a_timeout_event(self):
        self.script(held())
        before = self.event_error(await self.chat(STREAM_ASK), "timeout")
        self.assertEqual(chat_tests.content_of(before), "x")  # what arrived before the limit was passed on
        await asyncio.wait_for(self.vllm.disconnected.wait(), 10)
        await self.idle()

    async def test_no_answer_at_all_is_a_504_also_for_a_stream(self):
        self.quiet_fake_vllm()
        for payload in BOTH:
            with self.subTest(mode=mode(payload)):
                self.script(Reply(*say("late"), delay=1.3))
                self.refused(await self.chat(payload), 504, "timeout", "server_error")
                await self.idle()


class InternalErrorTests(FailureCase):
    async def test_a_server_bug_is_a_plain_500_without_details(self):
        with mock.patch.object(support.api, "build_request", side_effect=RuntimeError(SECRET)):
            answer = await self.chat(headers={"X-Request-Id": "trace-9"})
        self.refused(answer, 500, "internal_error", "server_error")
        self.assertEqual(answer.headers["X-Request-Id"], "trace-9")
        self.assertEqual(answer.headers["X-Axk2-Profile"], "fp8")
        self.assertEqual(answer.headers["Cache-Control"], "no-store")
        (record,) = await self.logged(1)
        self.assertEqual((record["request_id"], record["status"], record["error"]), ("trace-9", 500, "internal_error"))
        self.assertEqual(leaked(answer.text), [])
        self.assertNotIn(SECRET, sys.stdout.getvalue())
        self.assertEqual(self.vllm.hits["chat"], 0)

    async def test_a_failure_while_the_answer_is_read(self):
        self.script(Reply(*say("ok")), Reply(*say("ok")))
        with mock.patch.object(support.api.Events, "feed", side_effect=RuntimeError(SECRET)):
            plain, streamed = await self.chat(), await self.chat(STREAM_ASK)
        self.refused(plain, 500, "internal_error", "server_error")
        self.assertEqual(self.event_error(streamed, "internal_error"), [])
        self.assertEqual(leaked(plain.text), [])
        self.assertNotIn(SECRET, sys.stdout.getvalue())
        await self.idle()

    async def test_the_outer_frame_answers_when_everything_inside_fails(self):
        async def broken(scope, receive, send):
            raise RuntimeError(SECRET)

        sent = []

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "POST", "path": "/chat/completions",
                 "headers": [(b"x-request-id", b"abc-123")]}
        await support.api.Frame(broken, "fp8")(scope, None, send)
        start, body = sent
        headers = dict(start["headers"])
        self.assertEqual(start["status"], 500)
        self.assertEqual((headers[b"x-request-id"], headers[b"x-axk2-profile"], headers[b"cache-control"]),
                         (b"abc-123", b"fp8", b"no-store"))
        error = json.loads(body["body"])["error"]
        self.assertEqual((error["code"], error["type"]), ("internal_error", "server_error"))
        record = self.logs()[-1]
        self.assertEqual((record["request_id"], record["profile"], record["status"], record["error"]),
                         ("abc-123", "fp8", 500, "internal_error"))
        self.assertEqual(leaked(body["body"].decode("utf-8")), [])
        self.assertNotIn(SECRET, sys.stdout.getvalue())


class SlotTests(FailureCase):
    options = {"max_active": 1, "max_queue": 1}

    async def test_one_call_runs_one_waits_and_the_next_is_refused(self):
        gate = self.model_api.gate
        self.script(held(), Reply(*say("second")))
        running = asyncio.create_task(self.chat(STREAM_ASK))
        await self.until(lambda: self.vllm.hits["chat"] == 1)
        waiting = asyncio.create_task(self.chat())
        await self.until(lambda: len(gate.waiting) == 1)
        third = await self.chat()
        self.refused(third, 429, "capacity_exceeded", "rate_limit_error")
        self.assertEqual(third.headers["Retry-After"], "5")
        self.assertEqual(self.vllm.hits["chat"], 1)  # the refused call never reached the GPU
        await self.drop(running)  # a vanished caller gives its place to the one that waits
        answer = await asyncio.wait_for(waiting, 10)
        self.assertEqual(answer.json()["choices"][0]["message"]["content"], "second")
        await asyncio.wait_for(self.vllm.disconnected.wait(), 10)
        await self.idle()


class FullGateTests(FailureCase):
    options = {"max_active": 2, "max_queue": 0}

    async def test_without_a_queue_a_third_call_is_refused_at_once(self):
        self.script(held(), held())
        running = [asyncio.create_task(self.chat(STREAM_ASK)) for _ in range(2)]
        await self.until(lambda: self.vllm.hits["chat"] == 2)
        started = time.monotonic()
        answer = await self.chat()
        self.assertLess(time.monotonic() - started, 3)
        self.refused(answer, 429, "capacity_exceeded", "rate_limit_error")
        self.assertEqual((answer.headers["Retry-After"], self.vllm.hits["chat"]), ("5", 2))
        for task in running:
            await self.drop(task)
        await self.idle()


class QueueTimeoutTests(FailureCase):
    options = {"max_active": 1, "max_queue": 1, "queue_timeout": 0.6}

    async def test_a_waiting_call_gives_up_after_the_queue_timeout(self):
        gate = self.model_api.gate
        self.script(held())
        running = asyncio.create_task(self.chat(STREAM_ASK))
        await self.until(lambda: self.vllm.hits["chat"] == 1)
        started = time.monotonic()
        waiting = asyncio.create_task(self.chat())
        await self.until(lambda: len(gate.waiting) == 1)
        answer = await asyncio.wait_for(waiting, 10)
        self.assertGreaterEqual(time.monotonic() - started, 0.5)
        self.refused(answer, 429, "capacity_exceeded", "rate_limit_error")
        self.assertEqual(answer.headers["Retry-After"], "5")
        self.assertEqual((gate.active, len(gate.waiting), self.vllm.hits["chat"]), (1, 0, 1))
        await self.drop(running)
        await self.idle()


class RateTests(FailureCase):
    options = {"rate_per_minute": 3}

    async def test_chat_calls_are_limited_per_minute_and_nothing_else_is(self):
        self.script(Reply(*say("a")), Reply(*say("b")))
        for _ in range(4):  # the other routes do not use the budget
            self.assertEqual((await self.call("GET", "/v1/models")).status, 200)
            self.assertEqual((await self.call("GET", "/v1/health")).status, 200)
        self.refused(await self.chat(key=OTHER), 401, "invalid_api_key")  # nor does a refused key
        self.assertEqual((await self.chat()).status, 200)
        self.assertEqual((await self.chat()).status, 200)
        self.refused(await self.chat(dict(ASK, n=2)), 400, "unsupported_value", param="n")  # a refused body does
        limited = await self.chat()
        self.refused(limited, 429, "rate_limit_exceeded", "rate_limit_error")
        self.assertTrue(1 <= int(limited.headers["Retry-After"]) <= 60, limited.headers["Retry-After"])
        self.assertEqual(self.vllm.hits["chat"], 2)  # the limited call never reached the GPU
        self.assertEqual((await self.call("GET", "/v1/models")).status, 200)


class WindowTests(unittest.TestCase):
    def api(self, rate):
        return support.api.ModelAPI(support.settings(KEY, rate_per_minute=rate), profile="fp8", hub=None,
                                    is_ready=lambda: True, power=lambda: "ready", limiter=None, client_ip=None)

    def test_the_window_is_a_sliding_minute(self):
        api, now = self.api(2), time.monotonic()
        api.recent.extend([now - 61, now - 59])  # the first has left the window, the second leaves in a second
        api.limit_rate()
        with self.assertRaises(support.api.ApiError) as raised:
            api.limit_rate()
        self.assertEqual((raised.exception.status, raised.exception.code), (429, "rate_limit_exceeded"))
        self.assertEqual(raised.exception.headers["Retry-After"], "1")

    def test_a_limited_call_does_not_extend_the_window(self):
        api = self.api(1)
        api.limit_rate()
        for _ in range(3):
            with self.assertRaises(support.api.ApiError):
                api.limit_rate()
        self.assertEqual(len(api.recent), 1)


class DisconnectTests(FailureCase):
    async def test_a_caller_that_leaves_a_plain_call_cancels_the_gpu_work(self):
        self.script(held())
        pending = asyncio.create_task(self.chat())
        await self.until(lambda: self.vllm.hits["chat"] == 1)
        await self.drop(pending)
        await asyncio.wait_for(self.vllm.disconnected.wait(), 15)  # noticed within about a second
        await self.idle()
        (record,) = await self.logged(1)
        self.assertEqual((record["status"], record["stream"], record["error"]), (499, False, "client_gone"))

    async def test_a_caller_that_leaves_a_stream_cancels_the_gpu_work(self):
        self.script(held())
        response = await self.plain.post("/v1/chat/completions", json=STREAM_ASK,
                                         headers={"Authorization": "Bearer " + KEY})
        self.assertEqual(response.status, 200)
        await response.content.readline()  # the first event has arrived
        response.close()
        await asyncio.wait_for(self.vllm.disconnected.wait(), 15)
        await self.idle()
        (record,) = await self.logged(1)
        self.assertEqual((record["status"], record["stream"], record["error"]), (200, True, "client_gone"))


class DemoGateTests(FailureCase):
    max_active, max_queue = 1, 0  # the demo chat's own gate

    async def test_the_demo_gate_and_the_model_gate_are_separate(self):
        demo = self.app.state.gate
        ticket = demo.join()  # the demo's gate is full now
        try:
            with self.assertRaises(support.api.Full):
                demo.join()
            self.script(Reply(*say("ok")), held())
            self.assertEqual((await self.chat()).status, 200)  # the model API has places of its own
            running = asyncio.create_task(self.chat(STREAM_ASK))
            await self.until(lambda: self.model_api.gate.active == 1)
            self.assertEqual((demo.active, len(demo.waiting), self.model_api.gate.active), (1, 0, 1))
            await self.drop(running)
            await self.idle()
        finally:
            demo.leave(ticket)
        self.assertEqual(demo.active, 0)


class RequestBodyTests(FailureCase):
    async def test_a_body_that_does_not_arrive_in_time_is_a_408(self):
        async def stalled():
            yield b'{"model": "ax-k2-fp8", '
            await asyncio.sleep(2)
            yield b'"messages": []}'

        with mock.patch.object(support.api.ModelAPI, "read_timeout", 0.3):
            answer = await self.call("POST", "/v1/chat/completions", data=stalled(), headers=JSON)
        self.refused(answer, 408, "request_timeout", "invalid_request_error")
        self.assertEqual((self.vllm.hits["chat"], self.model_api.gate.active), (0, 0))


class LogTests(FailureCase):
    PROMPT, REPLY = "고객-비밀-질문-7731", "비밀-응답-4412"

    async def test_a_call_leaves_one_line_with_counts_and_timings_only(self):
        self.script(Reply(*say(self.REPLY)))
        ask = dict(ASK, messages=[{"role": "user", "content": self.PROMPT}])
        answer = await self.chat(ask, headers={"X-Request-Id": "trace-1"})
        self.assertEqual(answer.status, 200)
        (record,) = await self.logged(1)
        self.assertEqual(set(record), ANSWERED_KEYS)
        facts = ("event", "request_id", "profile", "route", "stream", "status", "prompt_tokens", "completion_tokens",
                 "finish_reason", "error")
        self.assertEqual({key: record[key] for key in facts},
                         {"event": "model_api", "request_id": "trace-1", "profile": "fp8", "route": "chat",
                          "stream": False, "status": 200, "prompt_tokens": 11, "completion_tokens": 3,
                          "finish_reason": "stop", "error": None})
        self.assertEqual(answer.headers["X-Request-Id"], "trace-1")
        for key in ("ttft_ms", "queue_ms", "total_ms"):
            self.assertIsInstance(record[key], int, key)
        self.assertGreaterEqual(record["total_ms"], record["ttft_ms"])

    async def test_a_stream_logs_its_counts_although_the_usage_event_is_not_sent(self):
        self.script(Reply(*say("a", "b")))
        answer = await self.chat(STREAM_ASK)
        self.assertEqual(chat_tests.content_of(chat_tests.chunks(answer.text)[0]), "ab")
        (record,) = await self.logged(1)
        self.assertEqual(set(record), ANSWERED_KEYS)
        self.assertEqual((record["stream"], record["status"], record["prompt_tokens"], record["completion_tokens"],
                          record["finish_reason"], record["error"]), (True, 200, 11, 3, "stop", None))
        self.assertIsInstance(record["ttft_ms"], int)

    async def test_refusals_and_other_routes_are_logged_with_their_code(self):
        malformed = b'{"messages": ' + self.PROMPT.encode("utf-8") + b"}"
        answers = [await self.chat(key=OTHER), await self.chat(key=None), await self.chat(dict(ASK, model="gpt-4o")),
                   await self.chat(dict(ASK, n=2)),
                   await self.call("POST", "/v1/chat/completions", data=malformed, headers=JSON),
                   await self.call("GET", "/v1/models"), await self.call("GET", "/v1/nothing")]
        records = await self.logged(len(answers))
        self.assertEqual([(r["route"], r["status"], r["error"]) for r in records], [
            ("chat", 401, "invalid_api_key"), ("chat", 401, "missing_api_key"), ("chat", 404, "model_not_found"),
            ("chat", 400, "unsupported_value"), ("chat", 400, "invalid_json"), ("models", 200, None),
            ("other", 404, "not_found")])
        for answer, record in zip(answers, records):
            self.assertEqual(set(record), LOG_KEYS)
            self.assertEqual(record["request_id"], answer.headers["X-Request-Id"])
            self.assertEqual((record["prompt_tokens"], record["completion_tokens"], record["finish_reason"]),
                             (None, None, None))
        self.assertNotIn(self.PROMPT, answers[4].text)  # a body that is refused is not echoed

    async def test_only_the_names_of_dropped_fields_are_logged(self):
        self.script(Reply(*say("ok")))
        extras = dict(store=True, metadata={"tag": self.PROMPT}, user="visitor-1", api_key=KEY)
        self.assertEqual((await self.chat(dict(ASK, **extras))).status, 200)
        (record,) = await self.logged(1)
        self.assertEqual(record["dropped_fields"], ["api_key", "metadata", "store", "user"])
        self.assertEqual(set(record), ANSWERED_KEYS | {"dropped_fields"})
        self.assertFalse({"api_key", "metadata", "store", "user"} & set(self.upstream()))
        for value in (KEY, self.PROMPT, "visitor-1"):
            self.assertNotIn(value, sys.stdout.getvalue())

    async def test_no_secret_reaches_a_response_or_the_log(self):
        self.script(Reply(*say(self.REPLY)), Reply(*say(self.REPLY)), Reply(status=500, text=TRACE_JSON))
        ask = dict(ASK, messages=[{"role": "user", "content": self.PROMPT}])
        answers = [await self.chat(ask), await self.chat(dict(ask, stream=True)), await self.chat(ask),
                   await self.chat(ask, key=OTHER), await self.chat(ask, key=KEY[:-1] + "!", header="api-key"),
                   await self.chat(ask, key=None), await self.call("GET", "/v1/health", key=OTHER)]
        with mock.patch.object(support.api, "build_request", side_effect=RuntimeError(SECRET)):
            answers.append(await self.chat(ask))
        self.assertEqual([answer.status for answer in answers], [200, 200, 502, 401, 401, 401, 401, 500])
        await self.logged(len(answers))
        shown = "\n".join(answer.text + repr(sorted(answer.headers.items())) for answer in answers)
        logged = sys.stdout.getvalue()
        digest = support.api.key_digest(KEY)
        for secret in (KEY, OTHER, KEY[:-1] + "!", digest):
            self.assertNotIn(secret, shown)
            self.assertNotIn(secret, logged)
        for content in (self.PROMPT, self.REPLY):  # content may be answered, but is never logged
            self.assertNotIn(content, logged)
        for word in ("Bearer", "Authorization", "api-key", "Traceback", SECRET):
            self.assertNotIn(word, logged)
        self.assertEqual(leaked("\n".join(answer.text for answer in answers[2:])), [])


if __name__ == "__main__":
    unittest.main()
