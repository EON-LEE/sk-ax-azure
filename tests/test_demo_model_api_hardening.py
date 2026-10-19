"""Requests that are valid JSON and still must never reach the GPU link: text with an unpaired UTF-16 surrogate
(the escape \\ud800 decodes to a str that UTF-8 cannot carry, so the hub's writer would fail, close the one GPU link
and fail every request in flight) and nesting deeper than the JSON encoder can write. Such a request gets a plain
HTTP 400 (a 5xx would be retried by Foundry and the SDKs), before it takes a slot or writes to the link and whatever
its `stream` flag says. On loopback like the sibling files, with a spy on the hub's write side."""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
try:
    import test_demo_model_api as units
    import test_demo_model_api_chat as chat_tests
    import test_demo_model_api_failures as failures
    import test_demo_model_api_server as support
finally:
    del sys.path[:1]

api, ASK, JSON, STREAM_ASK = support.api, support.ASK, chat_tests.JSON, failures.STREAM_ASK
Reply, say = support.Reply, support.say
LONE = "\ud800"  # an unpaired high surrogate; aiohttp's json= sends it as the valid JSON escape \ud800
CALL = chat_tests.WEATHER_CALL
ASKED = {"role": "user", "content": "서울 날씨?"}
CALLED = {"role": "assistant", "content": None, "tool_calls": [CALL]}
RESULT = {"role": "tool", "tool_call_id": CALL["id"], "content": '{"temp": 21}'}
ARGUMENTS_AT = "messages[1].tool_calls[0].function.arguments"
FINE = "\U0001f600 한글 \u0000 \ufffe\uffff \u2028 \U0010ffff"  # unusual and valid
PAIRED = '{"city": "\\ud83d\\ude00", "nul": "\\u0000"}'  # arguments text whose escapes do pair up
ESCAPED = ('{"city": "\\ud800"}', '"\\udc00"', '["x", "\\ud83d"]', '{"\\ud800": 1}', '"\\ud83d\\u0041"',
           '{"a": {"b": ["\\ude00"]}}')  # arguments text that decodes to a lone surrogate
SENTINEL = "@@deep@@"


def tool(**function):
    """The test tool with some parts of its function replaced."""
    return dict(units.TOOL, function=dict(units.TOOL["function"], **function))


def history(*messages):
    return dict(ASK, messages=list(messages))


def with_arguments(arguments):
    """The tool round trip in which the assistant called the tool with these arguments."""
    called = dict(CALLED, tool_calls=[dict(CALL, function=dict(CALL["function"], arguments=arguments))])
    return history(ASKED, called, RESULT)


def schema(**details):
    return {"type": "object", "properties": {"city": dict({"type": "string"}, **details)}}


def streamed(payload):
    return dict(payload, stream=True)


def raw_request(where, deep, stream=False):
    """JSON text of a request that carries the nested text `deep` as a dropped field, as the arguments object or
    as the arguments string."""
    payload = dict(ASK, metadata=SENTINEL) if where == "dropped field" else with_arguments(SENTINEL)
    nested = json.dumps(deep) if where == "arguments string" else deep
    return json.dumps(dict(payload, stream=stream)).replace(json.dumps(SENTINEL), nested)


# one unpaired surrogate each, by where it sits in a request
PLACEMENTS = {
    "user message": history({"role": "user", "content": "a" + LONE + "b"}),
    "text part": history({"role": "user", "content": [{"type": "text", "text": LONE}]}),
    "system message": history({"role": "system", "content": LONE}, ASKED),
    "developer message": history({"role": "developer", "content": LONE}, ASKED),
    "assistant message": history(ASKED, {"role": "assistant", "content": LONE}, ASKED),
    "tool result": history(ASKED, CALLED, dict(RESULT, content=LONE)),
    "arguments text": with_arguments('{"city": "%s"}' % LONE),
    "arguments object": with_arguments({"city": LONE}),
    "stop": dict(ASK, stop=LONE),
    "stop list": dict(ASK, stop=["END", LONE]),
    "tool name": dict(ASK, tools=[tool(name="get_" + LONE)]),
    "tool description": dict(ASK, tools=[tool(description=LONE)]),
    "schema description": dict(ASK, tools=[tool(parameters=schema(description=LONE))]),
    "schema property name": dict(ASK, tools=[tool(parameters={"type": "object",
                                                              "properties": {LONE: {"type": "string"}}})]),
    "tool choice": dict(ASK, tools=[units.TOOL], tool_choice={"type": "function", "function": {"name": LONE}}),
    "model": dict(ASK, model=LONE),
    "dropped field": dict(ASK, metadata={"run": LONE}),
    "dropped field key": dict(ASK, metadata={LONE: 1}),
    "field name": dict(ASK, **{LONE: 1}),
}


class EncodableTests(unittest.TestCase):
    def test_text_that_utf8_can_carry(self):
        for value in ("", "a", "한글", "\U0001f600", "\u0000", "\ufffe\uffff", "\u2028", FINE,
                      [1, 2.5, None, True], {"k": ["\U0010ffff", {"j": "x"}]}):
            self.assertTrue(api.encodable(value), value)

    def test_an_unpaired_surrogate_anywhere_is_not_encodable(self):
        # in Python two escapes written one after the other are two surrogates, not one character
        for value in (LONE, "\udc00", "a" + LONE + "b", "\ud83dx", "\ude00\ud83d", "\ud83d\ude00", [LONE], {"k": LONE},
                      {LONE: 1}, {"k": [{"j": [LONE]}]}):
            self.assertFalse(api.encodable(value), value)

    def test_nesting_beyond_the_encoders_depth_is_not_encodable(self):
        self.assertTrue(api.encodable(units.nested(100)))
        self.assertFalse(api.encodable(units.nested(100_000)))


class ParseTests(unittest.TestCase):
    def refuse(self, call, status, code, param=None):
        with self.assertRaises(api.ApiError) as caught:
            call()
        error = caught.exception
        self.assertEqual((error.status, error.code), (status, code), error.message)
        if param is not None:
            self.assertEqual(error.param, param)

    def test_text_that_only_looks_like_trouble_parses(self):
        raw = b'{"a": "\\ud83d\\ude00", "b": "\\u0000", "c": "' + "한".encode("utf-8") + b'", "d": "\\ufffe"}'
        self.assertEqual(api.parse_json(raw), {"a": "\U0001f600", "b": "\x00", "c": "한", "d": "\ufffe"})
        calls = [dict(CALL, function=dict(CALL["function"], arguments=PAIRED))]
        self.assertEqual(api.clean_calls(calls, "messages[1]")[0]["function"]["arguments"], PAIRED)

    def test_a_re_encoding_that_fails_is_a_400_whatever_it_raises(self):
        # the depth the encoder can write may differ from the one the parser read, and text may be unencodable
        for error in (RecursionError("depth"), ValueError("range"),
                      UnicodeEncodeError("utf-8", LONE, 0, 1, "surrogates not allowed")):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(api.json, "dumps", side_effect=error):
                    self.refuse(lambda: api.parse_json(b'{"model": "ax-k2-fp8"}'), 400, "invalid_json")
                    self.refuse(lambda: api.clean_calls([CALL], "messages[1]"), 400, "invalid_value", ARGUMENTS_AT)

    def test_arguments_text_that_decodes_to_a_lone_surrogate_or_cannot_be_read_is_a_400(self):
        for arguments in ESCAPED + ("[" * 100_000, "{nope", "\ud83d" + "x"):
            with self.subTest(arguments=arguments[:30]):
                calls = [dict(CALL, function=dict(CALL["function"], arguments=arguments))]
                self.refuse(lambda: api.clean_calls(calls, "messages[1]"), 400, "invalid_value", ARGUMENTS_AT)

    def test_what_is_accepted_can_always_be_written_and_the_rest_is_a_400(self):
        for where in ("dropped field", "arguments object", "arguments string"):
            for levels in (1, 50, 500, 2900, 2990, 2995, 2996, 2997, 2998, 3000, 10_000, 100_000):
                with self.subTest(where=where, levels=levels):
                    raw = raw_request(where, "[" * levels + "]" * levels).encode("utf-8")
                    try:
                        spec = api.build_request(api.parse_json(raw), units.SETTINGS, "fp8")
                    except api.ApiError as refused:
                        self.assertEqual(refused.status, 400)
                        continue
                    message = {"type": "req", "id": "r", "path": api.UPSTREAM_PATH, "body": spec.body}
                    sent = json.dumps(message, ensure_ascii=False).encode("utf-8")  # the hub's writer
                    self.assertEqual(json.loads(sent), message)  # the link's reader


class GuardedCase(failures.FailureCase):
    """The FP8 link is up and everything the API writes towards it is recorded."""
    options = {"rate_per_minute": 10_000}  # a refused body counts against the per-minute budget as well

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.conn, self.written = self.app.state.hub.ready_link(), []
        send = self.conn.send

        def record(message):
            if message.get("path") == api.UPSTREAM_PATH or message.get("type") == "cancel":
                self.written.append(message)  # the model list the hub asks for on its own is not a chat call
            send(message)

        self.conn.send = record
        self.addCleanup(self.conn.__dict__.pop, "send", None)

    def untouched(self):
        """Nothing was written towards the GPU, no slot is held, and the link is the same open, ready one."""
        self.assertEqual((self.written, self.vllm.hits["chat"]), ([], 0))
        for gate in (self.model_api.gate, self.app.state.gate):
            self.assertEqual(gate.active, 0)
            self.assertFalse(gate.waiting)
        self.assertFalse(self.conn.closed)
        self.assertIs(self.app.state.hub.ready_link(), self.conn)


class UnpairedSurrogateTests(GuardedCase):
    async def test_every_place_in_a_request_is_checked_in_both_forms(self):
        for place, payload in PLACEMENTS.items():
            for form in (payload, streamed(payload)):
                with self.subTest(place=place, stream=bool(form.get("stream"))):
                    answer = await self.chat(form)
                    self.refused(answer, 400, "invalid_json", "invalid_request_error")
                    self.assertNotIn("data:", answer.text)  # not a single byte of a stream
                    self.assertNotIn("ud800", answer.text)
                    self.untouched()

    async def test_a_surrogate_escaped_inside_the_arguments_text_is_refused_too(self):
        # the arguments are JSON text: the escape is only decoded where the server reads that text
        for arguments in ESCAPED:
            for form in (with_arguments(arguments), streamed(with_arguments(arguments))):
                with self.subTest(arguments=arguments, stream=bool(form.get("stream"))):
                    answer = await self.chat(form)
                    self.refused(answer, 400, "invalid_value", "invalid_request_error", param=ARGUMENTS_AT)
                    self.assertNotIn("data:", answer.text)
                    self.untouched()

    async def test_a_surrogate_that_arrives_as_bytes_is_refused_too(self):
        head, tail = b'{"model": "ax-k2-fp8", "stream": true, "messages": [{"role": "user", "content": "x', b'y"}]}'
        text = (head + b"?" + tail).decode("utf-8")
        bodies = {"escape": head + b"\\ud800" + tail, "utf-8 bytes of the surrogate": head + b"\xed\xa0\x80" + tail,
                  "utf-16": text.replace("?", LONE).encode("utf-16-le", "surrogatepass"),
                  "utf-32": text.replace("?", LONE).encode("utf-32-le", "surrogatepass")}
        for name, raw in bodies.items():
            with self.subTest(body=name):
                answer = await self.call("POST", "/v1/chat/completions", data=raw, headers=JSON)
                self.refused(answer, 400, "invalid_json", "invalid_request_error")
                self.assertNotIn("data:", answer.text)
                self.untouched()

    async def test_valid_but_unusual_text_is_served_as_it_is(self):
        called = dict(CALLED, tool_calls=[dict(CALL, function=dict(CALL["function"], arguments=PAIRED))])
        payload = dict(history({"role": "user", "content": FINE}, called, dict(RESULT, content=FINE)),
                       tools=[tool(description=FINE)], stop=[FINE])
        for stream in (False, True):
            body = dict(payload, stream=stream)
            sends = {"escaped": dict(json=body),
                     "utf-8": dict(data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=JSON)}
            for how, kwargs in sends.items():
                with self.subTest(how=how, stream=stream):
                    self.vllm.requests.clear()
                    self.written.clear()
                    self.script(Reply(*say("ok")))
                    answer = await self.call("POST", "/v1/chat/completions", **kwargs)
                    self.assertEqual(answer.status, 200, answer.text)
                    sent = self.upstream()
                    self.assertEqual([message["content"] for message in sent["messages"]], [FINE, "", FINE])
                    self.assertEqual(sent["messages"][1]["tool_calls"][0]["function"]["arguments"], PAIRED)
                    self.assertEqual((sent["tools"][0]["function"]["description"], sent["stop"]), (FINE, [FINE]))
                    (written,) = self.written  # the spy sees a normal call
                    json.dumps(written, ensure_ascii=False).encode("utf-8")  # as the hub's writer does

    async def test_a_refusal_is_logged_with_its_code_and_without_its_text(self):
        await self.chat(PLACEMENTS["user message"])
        await self.chat(streamed(with_arguments(ESCAPED[0])))
        first, second = await self.logged(2)
        for record, error in ((first, "invalid_json"), (second, "invalid_value")):
            self.assertEqual(set(record), failures.LOG_KEYS)
            self.assertEqual((record["route"], record["status"], record["error"]), ("chat", 400, error))
        self.assertNotIn("ud800", sys.stdout.getvalue())


class InFlightTests(GuardedCase):
    options = {"rate_per_minute": 10_000, "max_active": 1, "max_queue": 0}

    async def test_a_refusal_needs_no_slot_and_does_not_hurt_the_call_in_flight(self):
        self.script(Reply(*say("a"), ends="hold"))
        running = asyncio.create_task(self.chat(STREAM_ASK))
        await self.until(lambda: self.vllm.hits["chat"] == 1)
        self.refused(await self.chat(), 429, "capacity_exceeded", "rate_limit_error")  # the only slot is taken
        written = list(self.written)
        refusals = ((PLACEMENTS["user message"], "invalid_json"),
                    (streamed(PLACEMENTS["tool result"]), "invalid_json"),
                    (with_arguments(ESCAPED[0]), "invalid_value"),
                    (streamed(with_arguments(ESCAPED[3])), "invalid_value"))
        for payload, code in refusals:
            self.refused(await self.chat(payload), 400, code, "invalid_request_error")
        self.assertEqual((self.written, self.vllm.hits["chat"]), (written, 1))
        self.assertFalse(running.done())
        self.assertFalse(self.conn.closed)
        self.assertIs(self.app.state.hub.ready_link(), self.conn)
        await self.drop(running)
        await self.until(self.vllm.disconnected.is_set)
        await self.idle()


class DeepJsonTests(GuardedCase):
    async def test_nesting_that_cannot_be_written_is_a_400_in_both_forms(self):
        deep = "[" * 100_000 + "]" * 100_000
        cases = (("dropped field", "invalid_json", None), ("arguments object", "invalid_json", None),
                 ("arguments string", "invalid_value", ARGUMENTS_AT))
        for where, code, param in cases:
            for stream in (False, True):
                with self.subTest(where=where, stream=stream):
                    raw = raw_request(where, deep, stream).encode("utf-8")
                    answer = await self.call("POST", "/v1/chat/completions", data=raw, headers=JSON)
                    self.refused(answer, 400, code, "invalid_request_error", param=param)
                    self.assertNotIn("data:", answer.text)
                    self.untouched()


if __name__ == "__main__":
    unittest.main()
