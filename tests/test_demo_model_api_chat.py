"""What a chat call does, on loopback: the real app, the real backend_link.Link and a scripted fake vLLM. The
request vLLM gets (built by the server from an allow-list), the answer in both forms (one chat.completion, or
vLLM's own SSE chunks), tool calls and their round trip, the requests a Foundry agent sends (also the one an APIM
trace showed) and what is refused before the GPU sees it. Failures, limits and the log are in
test_demo_model_api_failures.py."""
import asyncio
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
try:
    import test_demo_model_api as units
    import test_demo_model_api_server as support
finally:
    del sys.path[:1]

ASK, TOOL, Reply, call, say, sse = support.ASK, units.TOOL, support.Reply, support.call, support.say, units.sse
JSON = {"Content-Type": "application/json"}
UPSTREAM_KEYS = {"model", "messages", "stream", "stream_options", "chat_template_kwargs", "max_tokens", "temperature",
                 "top_p"}
WEATHER_CALL = {"id": "chatcmpl-tool-1", "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city": "Seoul"}'}}


def chunks(text):
    """The JSON chunks of an SSE body and whether `data: [DONE]` ends it."""
    payloads = [line[6:] for line in text.split("\n") if line.startswith("data: ")]
    return [json.loads(item) for item in payloads if item != "[DONE]"], payloads[-1:] == ["[DONE]"]


def content_of(items):
    return "".join(choice["delta"].get("content") or "" for item in items for choice in item.get("choices", ()))


def without_usage(blocks):
    return "".join(block for block in blocks if block != sse(units.USAGE))


class LinkedCase(support.ApiCase):
    """The API with the FP8 link up; each test scripts what the fake vLLM answers."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.start_link()

    def script(self, *replies):
        self.vllm.script.extend(replies)

    def upstream(self):
        """The one request that reached vLLM."""
        self.assertEqual(len(self.vllm.requests), 1)
        return self.vllm.requests[0]


class PlainAnswerTests(LinkedCase):
    async def test_a_non_stream_answer_is_one_chat_completion(self):
        self.script(Reply(*say("안녕하세요", ", 반갑습니다")))
        answer = await self.chat()
        self.assertEqual(answer.status, 200)
        self.assertEqual(answer.headers["Content-Type"].split(";")[0], "application/json")
        self.assertEqual(answer.json(), {
            "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "axk2",
            "choices": [{"index": 0, "logprobs": None, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "안녕하세요, 반갑습니다"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}})

    async def test_the_upstream_request_is_built_by_the_server(self):
        self.script(Reply(*say("ok")))
        await self.chat(dict(ASK, temperature=0.2, top_p=0.5, max_tokens=100, stop="END", seed=7))
        self.assertEqual(self.upstream(), {
            "model": "axk2", "messages": [{"role": "user", "content": "안녕"}], "stream": True,
            "stream_options": {"include_usage": True}, "chat_template_kwargs": {"enable_thinking": False},
            "max_tokens": 100, "temperature": 0.2, "top_p": 0.5, "stop": ["END"], "seed": 7})

    async def test_the_defaults_and_the_server_cap_for_the_token_limit(self):
        cases = ((ASK, 4096), (dict(ASK, max_tokens=10**9), 8192), (dict(ASK, max_completion_tokens=77), 77),
                 (dict(ASK, max_tokens=5, max_completion_tokens=77), 77), (dict(ASK, max_tokens=5.0), 5))
        for payload, wanted in cases:
            with self.subTest(payload=payload):
                self.vllm.requests.clear()
                self.script(Reply(*say("ok")))
                self.assertEqual((await self.chat(payload)).status, 200)
                body = self.upstream()
                self.assertEqual((body["max_tokens"], body["temperature"], body["top_p"]), (wanted, 0.6, 0.95))

    async def test_vllm_extensions_cannot_change_the_server_settings(self):
        hostile = dict(ASK, chat_template_kwargs={"enable_thinking": True}, guided_json={"type": "object"},
                       guided_regex=".*", priority=-100, echo=True, best_of=8, use_beam_search=True, top_k=1,
                       min_p=0.5, repetition_penalty=2, ignore_eos=True, skip_special_tokens=False,
                       add_generation_prompt=False, continue_final_message=True, extra_body={"x": 1},
                       stream_options={"include_usage": False, "continuous_usage_stats": True})
        self.script(Reply(*say("ok")))
        self.assertEqual((await self.chat(hostile)).status, 200)
        body = self.upstream()
        self.assertEqual(set(body), UPSTREAM_KEYS)
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(body["stream_options"], {"include_usage": True})

    async def test_every_name_of_the_model_works(self):
        for name in ("ax-k2-fp8", "AX-K2-FP8", " ax-k2-fp8 ", "skt/A.X-K2", "SKT/a.x-k2"):
            with self.subTest(name=name):
                self.vllm.requests.clear()
                self.script(Reply(*say("ok")))
                answer = await self.chat(dict(ASK, model=name))
                self.assertEqual(answer.status, 200, answer.text)
                self.assertEqual(self.upstream()["model"], "axk2")

    async def test_reasoning_never_reaches_a_non_stream_caller(self):
        self.script(Reply(*say("답", reasoning=("생각 중", " …"))))
        answer = await self.chat()
        self.assertEqual(answer.json()["choices"][0]["message"], {"role": "assistant", "content": "답"})
        self.assertNotIn("reasoning", answer.text)
        self.assertNotIn("생각", answer.text.encode("utf-8").decode("unicode_escape", "ignore") + answer.text)

    async def test_a_length_stop_is_reported_as_is(self):
        self.script(Reply(*say("잘린", finish="length")))
        self.assertEqual((await self.chat()).json()["choices"][0]["finish_reason"], "length")


class ToolTests(LinkedCase):
    async def test_a_tool_call_is_one_message_with_json_string_arguments(self):
        self.script(Reply(*call()))
        answer = await self.chat(dict(ASK, tools=[TOOL]))
        self.assertEqual(answer.status, 200)
        body = answer.json()
        self.assertEqual(body["choices"], [{"index": 0, "logprobs": None, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [WEATHER_CALL]}}])
        self.assertIsInstance(body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"], str)
        self.assertEqual(body["usage"], {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14})
        upstream = self.upstream()
        self.assertEqual((upstream["tools"], upstream["tool_choice"]), ([TOOL], "auto"))
        self.assertNotIn("parallel_tool_calls", upstream)

    async def test_tool_choice_and_parallel_calls_are_passed_on(self):
        self.script(Reply(*call()))
        forced = {"type": "function", "function": {"name": "get_weather"}}
        await self.chat(dict(ASK, tools=[TOOL], tool_choice=forced, parallel_tool_calls=False))
        upstream = self.upstream()
        self.assertEqual(upstream["tool_choice"], forced)
        self.assertIs(upstream["parallel_tool_calls"], False)

    async def test_tool_settings_without_tools_are_ignored(self):
        self.script(Reply(*say("ok")))
        answer = await self.chat(dict(ASK, tools=[], tool_choice="auto", parallel_tool_calls=True))
        self.assertEqual(answer.status, 200)
        self.assertFalse({"tools", "tool_choice", "parallel_tool_calls"} & set(self.upstream()))

    async def test_a_tool_result_gets_the_final_answer(self):
        self.script(Reply(*call()), Reply(*say("서울은 맑고 21도입니다.")))
        first = await self.chat(dict(ASK, tools=[TOOL]))
        message = first.json()["choices"][0]["message"]
        result = {"role": "tool", "tool_call_id": message["tool_calls"][0]["id"], "content": '{"temp": 21}'}
        second = await self.chat(dict(ASK, tools=[TOOL], messages=ASK["messages"] + [message, result]))
        self.assertEqual(second.status, 200)
        choice = second.json()["choices"][0]
        self.assertEqual((choice["finish_reason"], choice["message"]["content"]), ("stop", "서울은 맑고 21도입니다."))
        self.assertNotIn("tool_calls", choice["message"])
        self.assertEqual(self.vllm.requests[1]["messages"], [
            {"role": "user", "content": "안녕"},
            {"role": "assistant", "content": "", "tool_calls": [WEATHER_CALL]},
            {"role": "tool", "tool_call_id": "chatcmpl-tool-1", "content": '{"temp": 21}'}])

    async def test_tool_call_deltas_are_merged_per_index(self):
        items = [units.chunk({"role": "assistant", "content": ""}),
                 units.chunk({"tool_calls": [{"index": 0, "id": "call-a", "type": "function",
                                              "function": {"name": "get_weather", "arguments": ""}}]}),
                 units.chunk({"tool_calls": [{"index": 1, "id": "call-b", "type": "function",
                                              "function": {"name": "get_weather", "arguments": ""}}]}),
                 units.chunk(units.calls(1, arguments='{"city": "Bu')),
                 units.chunk(units.calls(0, arguments='{"city": "Seoul"}')),
                 units.chunk(units.calls(1, arguments='san"}')),
                 units.chunk({}, "tool_calls"), units.USAGE]
        self.script(Reply(*[sse(item) for item in items], sse("[DONE]")))
        answer = await self.chat(dict(ASK, tools=[TOOL]))
        calls = answer.json()["choices"][0]["message"]["tool_calls"]
        self.assertEqual([(item["id"], item["function"]["arguments"]) for item in calls],
                         [("call-a", '{"city": "Seoul"}'), ("call-b", '{"city": "Busan"}')])
        for item in calls:
            json.loads(item["function"]["arguments"])

    async def test_history_with_loose_tool_calls_reaches_vllm_in_the_standard_form(self):
        history = [{"role": "user", "content": "서울"},
                   {"role": "assistant", "content": None, "tool_calls": [
                       {"id": "call_1", "type": "function", "function": {"name": "get_weather",
                                                                         "arguments": {"city": "Seoul"}}},
                       {"id": "call_2", "function": {"name": "get_time"}}]},
                   {"role": "tool", "tool_call_id": "call_1", "content": [{"type": "text", "text": "맑음"}]},
                   {"role": "tool", "tool_call_id": "call_2", "content": "10시"}]
        self.script(Reply(*say("ok")))
        self.assertEqual((await self.chat(dict(ASK, messages=history))).status, 200)
        sent = self.upstream()["messages"]
        self.assertEqual(sent[1], {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Seoul"}'}},
            {"id": "call_2", "type": "function", "function": {"name": "get_time", "arguments": "{}"}}]})
        self.assertEqual(sent[2:], [{"role": "tool", "tool_call_id": "call_1", "content": "맑음"},
                                    {"role": "tool", "tool_call_id": "call_2", "content": "10시"}])


class StreamTests(LinkedCase):
    async def test_the_stream_is_vllms_own_chunks_and_done(self):
        blocks = say("안", "녕")
        self.script(Reply(*blocks))
        answer = await self.chat(dict(ASK, stream=True))
        self.assertEqual(answer.status, 200)
        self.assertTrue(answer.headers["Content-Type"].startswith("text/event-stream"))
        self.assertEqual((answer.headers["Cache-Control"], answer.headers["X-Accel-Buffering"]), ("no-store", "no"))
        self.assertEqual(answer.headers["X-Axk2-Profile"], "fp8")
        self.assertTrue(answer.headers["X-Request-Id"])
        self.assertEqual(answer.text, without_usage(blocks))  # unchanged, the usage chunk not asked for left out
        self.assertTrue(answer.text.endswith("data: [DONE]\n\n"))
        self.assertNotIn("event:", answer.text)
        self.assertEqual(self.upstream()["stream_options"], {"include_usage": True})

    async def test_the_usage_chunk_is_sent_only_when_asked_for(self):
        for options, wanted in ((None, False), ({}, False), ({"include_usage": False}, False),
                                ({"include_usage": True}, True)):
            with self.subTest(options=options):
                blocks = say("a", "b")
                self.script(Reply(*blocks))
                payload = dict(ASK, stream=True) if options is None else dict(ASK, stream=True, stream_options=options)
                answer = await self.chat(payload)
                self.assertEqual(answer.text, "".join(blocks) if wanted else without_usage(blocks))
                items, done = chunks(answer.text)
                self.assertTrue(done)
                self.assertEqual(any(item.get("usage") for item in items), wanted)
                self.assertEqual(content_of(items), "ab")

    async def test_a_streamed_tool_call_arrives_unchanged(self):
        blocks = call()
        self.script(Reply(*blocks))
        answer = await self.chat(dict(ASK, tools=[TOOL], stream=True, stream_options={"include_usage": True}))
        self.assertEqual(answer.text, "".join(blocks))
        items, done = chunks(answer.text)
        self.assertTrue(done)
        self.assertEqual(items[-2]["choices"][0]["finish_reason"], "tool_calls")

    async def test_reasoning_is_removed_from_the_stream(self):
        self.script(Reply(*say("답", reasoning=("생각 중", " …"))))
        answer = await self.chat(dict(ASK, stream=True))
        items, done = chunks(answer.text)
        self.assertTrue(done)
        self.assertEqual(content_of(items), "답")
        self.assertNotIn("reasoning", answer.text)
        self.assertNotIn("생각", json.dumps(items, ensure_ascii=False))

    async def test_chunks_split_anywhere_are_put_together_again(self):
        blocks = say("하나", "둘", "셋")
        text = "".join(blocks)
        pieces = []
        for start in range(0, len(text), 23):
            pieces += [text[start:start + 23], 0.005]
        self.script(Reply(*pieces))
        answer = await self.chat(dict(ASK, stream=True))
        self.assertEqual(answer.text, without_usage(blocks))

    async def test_a_missing_done_is_added(self):
        self.script(Reply(*say("끝", done=False)))
        answer = await self.chat(dict(ASK, stream=True))
        self.assertEqual(answer.status, 200)
        self.assertTrue(answer.text.endswith('"finish_reason": "stop"}]}\n\ndata: [DONE]\n\n'), answer.text[-120:])
        self.assertEqual(answer.text.count("[DONE]"), 1)

    async def test_a_slow_first_token_gets_keepalive_comments_only(self):
        blocks = say("늦은 답")
        self.script(Reply(*blocks, delay=0.6))
        with mock.patch.object(support.api.ModelAPI, "keepalive", 0.1):
            answer = await self.chat(dict(ASK, stream=True))
        self.assertEqual(answer.status, 200)
        self.assertTrue(answer.text.startswith(": keepalive\n\n"))
        self.assertGreaterEqual(answer.text.count(": keepalive\n\n"), 3)
        self.assertEqual(answer.text.replace(": keepalive\n\n", ""), without_usage(blocks))
        self.assertNotIn("event:", answer.text)

    async def test_a_pause_between_chunks_gets_keepalive_comments_too(self):
        blocks = say("앞", "뒤")
        self.script(Reply(*blocks[:2], 0.5, *blocks[2:]))
        with mock.patch.object(support.api.ModelAPI, "keepalive", 0.1):
            answer = await self.chat(dict(ASK, stream=True))
        self.assertGreaterEqual(answer.text.count(": keepalive\n\n"), 3)
        self.assertEqual(answer.text.replace(": keepalive\n\n", ""), without_usage(blocks))

    async def test_a_late_error_head_becomes_an_error_in_the_stream(self):
        self.script(Reply(status=400, text='{"message": "bad things"}', delay=0.5))
        with mock.patch.object(support.api.ModelAPI, "keepalive", 0.1):
            answer = await self.chat(dict(ASK, stream=True))
        self.assertEqual(answer.status, 200)
        items, done = chunks(answer.text.replace(": keepalive\n\n", ""))
        self.assertTrue(done)
        self.assertEqual(items[-1]["error"]["code"], "invalid_request")


class FoundryTests(LinkedCase):
    """What a Foundry agent (model.format OpenAI, no model in the URL) may send, and what it needs back."""
    PATH = "/v1/chat/completions?api-version=2025-01-01-preview"
    REQUEST = {
        "model": "ax-k2-fp8",
        "messages": [{"role": "developer", "content": [{"type": "text", "text": "당신은 도우미입니다."}]},
                     {"role": "user", "content": [{"type": "text", "text": "서울"}, {"type": "text", "text": "날씨?"}]}],
        "tools": [TOOL], "tool_choice": "auto", "parallel_tool_calls": True, "reasoning_effort": "medium",
        "response_format": {"type": "text"}, "max_completion_tokens": 512, "temperature": 1, "top_p": 1,
        "logprobs": False, "top_logprobs": 0, "stream": False, "store": False, "metadata": {"agent": "demo"},
        "user": "agent-1", "service_tier": "auto", "prompt_cache_key": "k", "n": 1}

    async def test_an_agent_request_with_all_the_openai_extras(self):
        self.script(Reply(*call()))
        answer = await self.chat(self.REQUEST, path=self.PATH)
        self.assertEqual(answer.status, 200, answer.text)
        self.assertEqual(answer.json()["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(self.upstream(), {
            "model": "axk2", "stream": True, "stream_options": {"include_usage": True},
            "chat_template_kwargs": {"enable_thinking": False}, "max_tokens": 512, "temperature": 1, "top_p": 1,
            "messages": [{"role": "system", "content": "당신은 도우미입니다."},
                         {"role": "user", "content": "서울\n날씨?"}],
            "tools": [TOOL], "tool_choice": "auto", "parallel_tool_calls": True})

    async def test_an_agent_stream_request(self):
        blocks = call()
        self.script(Reply(*blocks))
        payload = dict(self.REQUEST, stream=True, stream_options={"include_usage": True})
        answer = await self.chat(payload, path=self.PATH)
        self.assertEqual(answer.status, 200)
        self.assertEqual(answer.text, "".join(blocks))

    async def test_the_models_route_and_the_query_string_are_tolerated(self):
        for path in ("/v1/models?api-version=2025-01-01-preview", "/v1/models?foo=bar&x"):
            answer = await self.call("GET", path)
            self.assertEqual((answer.status, answer.json()["data"][0]["id"]), (200, "ax-k2-fp8"), path)

    async def test_the_body_is_read_whatever_its_content_type(self):
        raw = json.dumps(ASK).encode("utf-8")

        async def pieces():
            yield raw[:10]
            yield raw[10:]

        sent = (dict(data=raw), dict(data=raw, headers={"Content-Type": "text/plain"}),
                dict(data=raw, headers=JSON), dict(data=pieces(), headers=JSON))  # the last one is chunked
        for kwargs in sent:
            with self.subTest(kwargs=sorted(kwargs)):
                self.script(Reply(*say("ok")))
                answer = await self.call("POST", "/v1/chat/completions", **kwargs)
                self.assertEqual(answer.status, 200, answer.text)

    async def test_the_second_turn_of_an_agent_after_a_tool_result(self):
        history = self.REQUEST["messages"] + [
            {"role": "assistant", "content": None, "tool_calls": [WEATHER_CALL]},
            {"role": "tool", "tool_call_id": "chatcmpl-tool-1", "content": '{"temp": 21}'}]
        self.script(Reply(*say("21도입니다.")))
        answer = await self.chat(dict(self.REQUEST, messages=history, stream=True), path=self.PATH)
        items, done = chunks(answer.text)
        self.assertTrue(done)
        self.assertEqual(content_of(items), "21도입니다.")


OBSERVED_FIELDS = {"model", "messages", "stream", "max_completion_tokens", "stream_options", "temperature", "top_p"}


def observed(**fields):
    """The model request of one Foundry agent turn as an APIM trace showed it: these top-level fields and no other,
    a system and a user message, no tools, stream on, max_completion_tokens (not max_tokens), no query string. The
    values here are made up; `fields` replace some of them."""
    request = {"model": "ax-k2-fp8",
               "messages": [{"role": "system", "content": "당신은 간결하게 답하는 도우미입니다."},
                            {"role": "user", "content": "서울은 어느 나라의 수도인가요?"}],
               "stream": True, "max_completion_tokens": 256, "stream_options": {"include_usage": True},
               "temperature": 1, "top_p": 1}
    request.update(fields)
    return request


class FoundryWire(LinkedCase):
    """The calls come from a client that, like the one in the trace, adds no User-Agent, Accept or
    Accept-Encoding; `wire` records the names of the headers that went out with each request."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.wire = []

        async def record(session, context, params):
            self.wire.append((params.method, str(params.url), {name.lower() for name in params.headers}))

        trace = aiohttp.TraceConfig()
        trace.on_request_headers_sent.append(record)
        self.bare = aiohttp.ClientSession(self.base, trace_configs=[trace],
                                          skip_auto_headers=("User-Agent", "Accept", "Accept-Encoding"))
        self.clients.append(self.bare)

    async def foundry(self, request=None, path="/v1/chat/completions"):
        """The call as the gateway forwards it: the Bearer key, a JSON body and nothing else."""
        sent = {"Authorization": "Bearer " + support.KEY}
        async with self.bare.post(path, json=observed() if request is None else request, headers=sent) as response:
            return support.Answer(response.status, response.headers, await response.read())


class ObservedFoundryRequestTests(FoundryWire):
    """The observed request is served on the normal path for every value its fields may take: nothing in it is
    a 400, and a max_completion_tokens above the server's cap is cut down to the cap (see the cap tests below)."""

    async def test_the_observed_request_is_streamed_back_as_vllms_own_chunks(self):
        self.assertEqual(set(observed()), OBSERVED_FIELDS)
        blocks = say("서울", "입니다.")
        self.script(Reply(*blocks))
        answer = await self.foundry()
        self.assertEqual(answer.status, 200, answer.text)
        self.assertTrue(answer.headers["Content-Type"].startswith("text/event-stream"))
        self.assertEqual((answer.headers["X-Axk2-Profile"], answer.headers["Cache-Control"]), ("fp8", "no-store"))
        self.assertTrue(answer.headers["X-Request-Id"])
        self.assertEqual(answer.text, "".join(blocks))  # the usage chunk was asked for, so it is there
        self.assertTrue(answer.text.endswith("data: [DONE]\n\n"))
        self.assertNotIn("event:", answer.text)
        self.assertEqual(content_of(chunks(answer.text)[0]), "서울입니다.")
        self.assertEqual(self.upstream(), {
            "model": "axk2", "stream": True, "stream_options": {"include_usage": True},
            "chat_template_kwargs": {"enable_thinking": False}, "max_tokens": 256, "temperature": 1, "top_p": 1,
            "messages": [{"role": "system", "content": "당신은 간결하게 답하는 도우미입니다."},
                         {"role": "user", "content": "서울은 어느 나라의 수도인가요?"}]})
        ((method, url, names),) = self.wire
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/v1/chat/completions"), url)  # no query string
        self.assertFalse({"user-agent", "accept", "accept-encoding"} & names, names)

    async def test_the_same_request_with_text_parts_for_the_content(self):
        def parts(text):
            return [{"type": "text", "text": text}]

        self.script(Reply(*say("ok")))
        answer = await self.foundry(observed(messages=[{"role": "system", "content": parts("규칙")},
                                                       {"role": "user", "content": parts("질문")}]))
        self.assertEqual(answer.status, 200, answer.text)
        self.assertEqual(self.upstream()["messages"], [{"role": "system", "content": "규칙"},
                                                       {"role": "user", "content": "질문"}])

    async def test_every_form_of_stream_options_is_accepted_and_include_usage_decides_the_usage_chunk(self):
        forms = (({"include_usage": True}, True), ({}, False), ({"include_obfuscation": False}, False),
                 ({"include_usage": True, "include_obfuscation": False}, True),
                 ({"include_usage": False, "include_obfuscation": True}, False), (None, False))
        for options, usage in forms:
            with self.subTest(stream_options=options):
                self.vllm.requests.clear()
                blocks = say("a", "b")
                self.script(Reply(*blocks))
                answer = await self.foundry(observed(stream_options=options))
                self.assertEqual(answer.status, 200, answer.text)
                self.assertEqual(answer.text, "".join(blocks) if usage else without_usage(blocks))
                items, done = chunks(answer.text)
                self.assertTrue(done)
                self.assertEqual((any(item.get("usage") for item in items), content_of(items)), (usage, "ab"))
                self.assertEqual(self.upstream()["stream_options"], {"include_usage": True})  # always, and only that

    async def test_temperature_and_top_p_as_whole_numbers_decimals_and_null(self):
        cases = (((1, 1), (1, 1)), ((1.0, 1.0), (1, 1)), ((0.7, 0.9), (0.7, 0.9)), ((2, 1), (2, 1)),
                 ((0, 0.5), (0, 0.5)), ((0.01, 0.0001), (0.01, 0.0001)), ((None, None), (0.6, 0.95)),
                 ((None, 0.5), (0.6, 0.5)), ((0.2, None), (0.2, 0.95)), ((0, 0), (0, 1e-6)))  # top_p 0 is made > 0
        for (temperature, top_p), wanted in cases:
            with self.subTest(temperature=temperature, top_p=top_p):
                self.vllm.requests.clear()
                self.script(Reply(*say("ok")))
                answer = await self.foundry(observed(temperature=temperature, top_p=top_p))
                self.assertEqual(answer.status, 200, answer.text)
                body = self.upstream()
                self.assertEqual((body["temperature"], body["top_p"]), wanted)

    async def test_max_completion_tokens_is_cut_down_to_the_cap_and_never_refused(self):
        cases = ((256, 256), (256.0, 256), (1, 1), (8192, 8192), (8193, 8192), (10**6, 8192), (2**40, 8192),
                 (None, 4096))
        for sent, wanted in cases:
            with self.subTest(max_completion_tokens=sent):
                self.vllm.requests.clear()
                self.script(Reply(*say("ok")))
                answer = await self.foundry(observed(max_completion_tokens=sent))
                self.assertEqual(answer.status, 200, answer.text)
                body = self.upstream()
                self.assertEqual(body["max_tokens"], wanted)
                self.assertNotIn("max_completion_tokens", body)  # vLLM gets the name it knows

    async def test_a_limit_that_cannot_be_a_token_count_is_still_a_400(self):
        for sent in (0, -1, 2.5, "256", True):
            with self.subTest(max_completion_tokens=sent):
                answer = await self.foundry(observed(max_completion_tokens=sent))
                self.refused(answer, 400, "invalid_value", "invalid_request_error", param="max_completion_tokens")
        self.assertEqual(self.vllm.hits["chat"], 0)


class ObservedFoundryCapTests(FoundryWire):
    """The cap that max_completion_tokens is cut down to is the server setting, not a fixed number."""
    options = {"max_tokens": 1000, "default_max_tokens": 300}

    async def test_the_cap_and_the_default_follow_the_settings(self):
        for sent, wanted in ((256, 256), (1000, 1000), (1001, 1000), (10**6, 1000), (None, 300)):
            with self.subTest(max_completion_tokens=sent):
                self.vllm.requests.clear()
                self.script(Reply(*say("ok")))
                answer = await self.foundry(observed(max_completion_tokens=sent))
                self.assertEqual(answer.status, 200, answer.text)
                self.assertEqual(self.upstream()["max_tokens"], wanted)


class ObservedFoundryRequestWithoutALinkTests(support.ApiCase):
    """No GPU link: the observed request is not refused for its shape. It is a plain JSON 503 at once (also with
    stream on, no event stream), which the gateway or the SDK may retry."""

    async def test_it_is_an_immediate_retryable_503(self):
        started = time.monotonic()
        answer = await self.chat(observed())
        self.assertLess(time.monotonic() - started, 3)
        self.refused(answer, 503, "backend_unavailable", "server_error")
        self.assertEqual(answer.headers["Retry-After"], "30")
        self.assertNotIn("data:", answer.text)
        self.assertEqual((self.vllm.hits["chat"], self.model_api.gate.active), (0, 0))


class RefusalTests(LinkedCase):
    async def test_a_body_that_is_not_a_json_object(self):
        for raw in (b"", b"{nope", b"[1, 2]", b'"text"', b'{"model": "ax-k2-fp8", "temperature": NaN}'):
            with self.subTest(raw=raw):
                answer = await self.call("POST", "/v1/chat/completions", data=raw, headers=JSON)
                self.refused(answer, 400, "invalid_json", "invalid_request_error")
        self.assertEqual(self.vllm.hits["chat"], 0)

    async def test_invalid_and_unsupported_requests_never_reach_the_gpu(self):
        text, image = {"role": "user", "content": "x"}, {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://example.invalid/a.png"}}]}
        cases = (
            ({"messages": ASK["messages"]}, 400, "missing_required_parameter", "model"),
            (dict(ASK, model=7), 400, "invalid_value", "model"),
            (dict(ASK, model="gpt-4o"), 404, "model_not_found", "model"),
            (dict(ASK, model="ax-k2-nvfp4"), 404, "model_not_found", "model"),
            ({"model": "ax-k2-fp8"}, 400, "missing_required_parameter", "messages"),
            (dict(ASK, messages=[]), 400, "missing_required_parameter", "messages"),
            (dict(ASK, messages=["hi"]), 400, "invalid_value", "messages[0]"),
            (dict(ASK, messages=[{"role": "robot", "content": "x"}]), 400, "invalid_value", "messages[0].role"),
            (dict(ASK, messages=[image]), 400, "unsupported_value", "messages[0].content[0]"),
            (dict(ASK, messages=[text, {"role": "tool", "content": "x"}]), 400, "invalid_value",
             "messages[1].tool_call_id"),
            (dict(ASK, n=2), 400, "unsupported_value", "n"),
            (dict(ASK, logprobs=True), 400, "unsupported_value", "logprobs"),
            (dict(ASK, logprobs=False, top_logprobs=1), 400, "unsupported_value", "top_logprobs"),
            (dict(ASK, response_format={"type": "json_object"}), 400, "unsupported_value", "response_format"),
            (dict(ASK, tools=[{"type": "web_search"}]), 400, "unsupported_value", "tools[0]"),
            (dict(ASK, tools=[TOOL], tool_choice="always"), 400, "invalid_value", "tool_choice"),
            (dict(ASK, stream="yes"), 400, "invalid_value", "stream"),
            (dict(ASK, temperature=5), 400, "invalid_value", "temperature"),
            (dict(ASK, max_tokens=0), 400, "invalid_value", "max_tokens"))
        for payload, status, code, param in cases:
            with self.subTest(code=code, param=param):
                self.refused(await self.chat(payload), status, code, param=param)
        self.assertEqual((self.vllm.hits["chat"], self.vllm.requests), (0, []))
        self.assertEqual(self.model_api.gate.active, 0)

    async def test_a_request_is_refused_even_while_the_gpu_is_busy(self):
        self.script(Reply(*say("a"), ends="hold"))
        held = asyncio.create_task(self.chat(dict(ASK, stream=True)))
        await self.until(lambda: self.vllm.hits["chat"] == 1)
        self.refused(await self.chat(dict(ASK, n=3)), 400, "unsupported_value", param="n")
        held.cancel()
        await self.until(self.vllm.disconnected.is_set)


class LimitTests(LinkedCase):
    options = {"max_messages": 3, "max_tools": 1, "max_schema_bytes": 1024, "max_body_bytes": 4096}

    async def test_message_tool_and_schema_limits(self):
        other = dict(TOOL, function=dict(TOOL["function"], name="other"))
        schema = {"type": "object", "properties": {"q": {"type": "string", "description": "d" * 2000}}}
        huge = dict(TOOL, function=dict(TOOL["function"], parameters=schema))
        cases = ((dict(ASK, messages=ASK["messages"] * 4), "messages"), (dict(ASK, tools=[TOOL, other]), "tools"),
                 (dict(ASK, tools=[huge]), "tools"))
        for payload, param in cases:
            with self.subTest(param=param):
                self.refused(await self.chat(payload), 400, "invalid_value", param=param)
        self.script(Reply(*say("ok")))  # the limits are inclusive
        answer = await self.chat(dict(ASK, messages=ASK["messages"] * 3, tools=[TOOL]))
        self.assertEqual(answer.status, 200, answer.text)

    async def test_a_body_over_the_limit_is_refused_with_413(self):
        raw = json.dumps(dict(ASK, messages=[{"role": "user", "content": "x" * 5000}])).encode("utf-8")

        async def pieces():
            for start in range(0, len(raw), 1500):
                yield raw[start:start + 1500]

        for kwargs in (dict(data=raw), dict(data=pieces())):  # Content-Length known, and a chunked upload
            with self.subTest(chunked=not isinstance(kwargs["data"], bytes)):
                answer = await self.call("POST", "/v1/chat/completions", headers=JSON, **kwargs)
                self.refused(answer, 413, "request_too_large", "invalid_request_error")
        self.assertEqual(self.vllm.hits["chat"], 0)


if __name__ == "__main__":
    unittest.main()
