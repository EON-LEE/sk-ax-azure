"""Tool names the model writes with an "mcp_" more or less than the caller declared, on the model-only API
(demo/frontend/model_api.py): the rule (declared_name), the SSE events it rewrites and the bytes it leaves alone, the
non-stream answer put together from them and the log line, which holds lengths and never a name; then the same on
loopback with the real app (the fixtures come from test_demo_model_api_failures.py). All names are made-up
placeholders; SEARCH and SEARCH_LOST are as long (49 and 45 characters) as the pair a model was seen to mix up."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
try:
    import test_demo_model_api as units
    import test_demo_model_api_chat as chat_tests
    import test_demo_model_api_failures as failures
    import test_demo_model_api_server as support
finally:
    del sys.path[:1]

api, ASK, Reply, ANSWERED_KEYS = units.api, support.ASK, support.Reply, failures.ANSWERED_KEYS
SEARCH = "mcp___mcp_example_service___generic_search_lookup"  # a declared name, 49 characters
SEARCH_LOST = "mcp___example_service___generic_search_lookup"  # without one "mcp_", 45 characters
SEARCH_EXTRA = "mcp_mcp___example_service___generic_search_lookup"  # one "mcp_" more than SEARCH_LOST
NOTES, NOTES_LOST = "mcp___mcp_notes___note_read", "mcp___notes___note_read"
ARGS = json.dumps({"query": '서울 "날씨"\n\\ unique-arg-4417'}, ensure_ascii=False)
GOLDEN = ('data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1,"model":"axk2",'
          '"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call-1","type":"function",'
          '"function":{"name":"NAME","arguments":"{\\"query\\": \\"서울\\"}"}}]},'
          '"logprobs":null,"finish_reason":null}]}\n\n')


def golden(name):
    """One event as vLLM writes it (compact JSON, `logprobs` and all) for a call of the tool `name`."""
    return GOLDEN.replace("NAME", name)


def compact(blocks):
    """`blocks` (events as units.sse writes them) in the compact JSON that vLLM writes."""
    return [block if block == "data: [DONE]\n\n" else
            "data: " + json.dumps(json.loads(block[6:]), ensure_ascii=False, separators=(",", ":")) + "\n\n"
            for block in blocks]


def wire(*items):
    """The events of a stream of `items` (chunks, or "[DONE]"), one string each."""
    return compact([units.sse(item) for item in items])


def hermes(name, arguments=ARGS, call_id="chatcmpl-tool-1"):
    """The events vLLM streams for one tool call: the id and the whole name first, then the arguments."""
    return compact(support.call(name, arguments, call_id))


PREAMBLE = ("Let me look that", " up for you now")  # 31 characters the model wrote before its call


def with_preamble(name, arguments=ARGS, call_id="chatcmpl-tool-1"):
    """hermes(name), with the text written before the call in two deltas between the role chunk and the call."""
    blocks = hermes(name, arguments, call_id)
    return blocks[:1] + wire(*[units.chunk({"content": piece}) for piece in PREAMBLE]) + blocks[1:]


def parallel(first, second, third):
    """The events for three calls named so: the first two begin in one chunk, the arguments arrive interleaved."""
    def start(index, name, arguments=""):
        return {"index": index, "id": "call-" + "abc"[index], "type": "function",
                "function": {"name": name, "arguments": arguments}}

    return wire(units.chunk({"role": "assistant", "content": ""}),
                units.chunk({"tool_calls": [start(0, first), start(1, second)]}),
                units.chunk(units.calls(1, arguments='{"city": "Busan"}')),
                units.chunk({"tool_calls": [start(2, third, "{}")]}),
                units.chunk(units.calls(0, arguments='{"query": "azure"}')),
                units.chunk({}, "tool_calls"), units.USAGE, "[DONE]")


def named(text, old, new):
    """`text` with the tool name `old` written as `new` wherever compact JSON has it as a "name"."""
    return text.replace(f'"name":"{old}"', f'"name":"{new}"')


def declare(*names):
    """The `tools` of a request that declares these functions."""
    return [{"type": "function", "function": {
        "name": name, "description": "A fake tool.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}} for name in names]


def run(text, tools, keep_usage=True, size=None):
    """(bytes sent on, parsed chunks, Events, Context) like test_demo_model_api.run, for a request with `tools`."""
    ctx = api.Context("rid", "fp8")
    parser = api.Events(ctx, tools)
    pieces = [text] if size is None else [text[i:i + size] for i in range(0, len(text), size)]
    sent, parsed = [], []
    for piece in pieces:
        for raw, found in parser.feed(piece, keep_usage):
            if raw is not None:
                sent.append(raw)
            if found is not None:
                parsed.append(found)
    return b"".join(sent), parsed, parser, ctx


def synthesize(text, tools):
    """The non-stream answer for `text`, put together the way ModelAPI.collect does."""
    parser, synth = api.Events(api.Context("rid", "fp8"), tools), api.Synth()
    for _, parsed in parser.feed(text, True):
        if parsed is not None:
            synth.add(parsed)
    return synth.completion(parser, "ax-k2-fp8")


def names_in(items):
    """The tool names in parsed stream chunks, in order."""
    return [call["function"]["name"] for item in items for choice in item["choices"]
            for call in choice["delta"].get("tool_calls", ()) if call["function"].get("name")]


def sent_calls(parsed):
    return [call for found in parsed for choice in found["choices"] for call in choice["delta"].get("tool_calls", ())]


class RuleTests(unittest.TestCase):
    def check(self, *cases):
        for name, declared, expected in cases:
            with self.subTest(name=name, declared=sorted(declared)):
                self.assertEqual(api.declared_name(name, declared), expected)

    def test_a_declared_name_stays(self):
        self.check((SEARCH, {SEARCH}, SEARCH), (SEARCH_LOST, {SEARCH_LOST}, SEARCH_LOST),
                   (SEARCH, {SEARCH, SEARCH_LOST}, SEARCH), (SEARCH_LOST, {SEARCH, SEARCH_LOST}, SEARCH_LOST))

    def test_an_mcp_part_too_few_or_too_many_is_put_right(self):
        self.check((SEARCH_LOST, {SEARCH}, SEARCH), (SEARCH, {SEARCH_LOST}, SEARCH_LOST),
                   (SEARCH_LOST, {SEARCH, NOTES, "get_weather"}, SEARCH),
                   ("mcp_get_weather", {"get_weather"}, "get_weather"),
                   ("get_weather", {"mcp_get_weather"}, "mcp_get_weather"),
                   ("mcp_mcp_get_weather", {"get_weather"}, "get_weather"),
                   ("get_weather", {"mcp_mcp_get_weather"}, "mcp_mcp_get_weather"))

    def test_the_key_ignores_where_the_mcp_parts_sit(self):
        self.check(("get_mcp_weather", {"get_weather"}, "get_weather"),
                   ("mcp_get_weather", {"get_mcp_weather"}, "get_mcp_weather"))

    def test_zero_or_several_declared_names_with_the_key_change_nothing(self):
        self.check((SEARCH_EXTRA, {SEARCH, SEARCH_LOST}, SEARCH_EXTRA),
                   (SEARCH_EXTRA, {SEARCH, SEARCH_LOST, "get_weather"}, SEARCH_EXTRA),
                   (SEARCH_LOST, set(), SEARCH_LOST), (SEARCH_LOST, (), SEARCH_LOST),
                   (SEARCH_LOST, {NOTES, "get_weather"}, SEARCH_LOST))

    def test_an_unrelated_name_stays(self):
        for name in ("get_time", "mcp___example_service___generic_fetch_lookup", SEARCH_LOST.upper(),
                     "mcp____example_service___generic_search_lookup", "example_service___generic_search_lookup"):
            self.check((name, {SEARCH}, name))


class SpecTests(unittest.TestCase):
    def test_the_declared_names_are_those_of_the_request(self):
        self.assertEqual(units.build(units.ask(tools=declare(SEARCH, NOTES))).tools, {SEARCH, NOTES})
        self.assertEqual(units.build(units.ask()).tools, frozenset())
        self.assertEqual(units.build(units.ask(tools=[])).tools, frozenset())


class RenameTests(unittest.TestCase):
    def test_a_declared_name_passes_through_byte_for_byte(self):
        text = "".join(hermes(SEARCH))
        sent, _, _, ctx = run(text, [SEARCH, NOTES])
        self.assertEqual(sent, text.encode("utf-8"))
        self.assertEqual(list(ctx.repairs), [])

    def test_the_wire_bytes_change_in_the_name_only(self):
        sent, parsed, _, ctx = run(golden(SEARCH_LOST), [SEARCH])
        self.assertEqual(sent, golden(SEARCH).encode("utf-8"))
        self.assertEqual(sent_calls(parsed)[0]["function"], {"name": SEARCH, "arguments": '{"query": "서울"}'})
        self.assertEqual(list(ctx.repairs), [{"emitted_len": 45, "declared_len": 49}])
        self.assertEqual(run(golden(SEARCH), [SEARCH])[0], golden(SEARCH).encode("utf-8"))

    def test_a_stream_changes_one_event_and_no_other_byte(self):
        text = "".join(hermes(SEARCH_LOST))
        sent = run(text, [SEARCH])[0]
        self.assertEqual(sent, named(text, SEARCH_LOST, SEARCH).encode("utf-8"))
        before, after = text.split("\n\n"), sent.decode("utf-8").split("\n\n")
        self.assertEqual(len(before), len(after))
        self.assertEqual([i for i, pair in enumerate(zip(before, after)) if pair[0] != pair[1]], [1])

    def test_a_name_is_put_right_in_both_directions(self):
        for emitted, declared in ((SEARCH_LOST, SEARCH), (SEARCH, SEARCH_LOST)):
            with self.subTest(emitted=len(emitted), declared=len(declared)):
                text = "".join(hermes(emitted))
                sent, _, _, ctx = run(text, [declared])
                self.assertEqual(sent, named(text, emitted, declared).encode("utf-8"))
                self.assertEqual(list(ctx.repairs), [{"emitted_len": len(emitted), "declared_len": len(declared)}])

    def test_split_anywhere_gives_the_same_bytes(self):
        text = "".join(hermes(SEARCH_LOST))
        whole = run(text, [SEARCH])[0]
        for size in (1, 7, 64, 1000):
            sent, _, _, ctx = run(text, [SEARCH], size=size)
            self.assertEqual(sent, whole, size)
            self.assertEqual(len(ctx.repairs), 1)

    def test_arguments_ids_and_order_are_untouched(self):
        arguments = json.dumps({"query": '서울 "날씨"\n\\ 끝', "note": SEARCH_LOST}, ensure_ascii=False)
        text = "".join(hermes(SEARCH_LOST, arguments, "chatcmpl-tool-9"))
        sent, parsed, _, _ = run(text, [SEARCH])
        self.assertEqual(sent, named(text, SEARCH_LOST, SEARCH).encode("utf-8"))
        calls = sent_calls(parsed)
        self.assertEqual("".join(call["function"].get("arguments", "") for call in calls), arguments)
        self.assertEqual([(call["index"], call.get("id")) for call in calls],
                         [(0, "chatcmpl-tool-9"), (0, None), (0, None)])
        self.assertIn(SEARCH_LOST, calls[1]["function"]["arguments"] + calls[2]["function"]["arguments"])

    def test_several_calls_in_several_chunks_are_put_right_one_by_one(self):
        text = "".join(parallel(SEARCH_LOST, "get_weather", NOTES_LOST))
        tools = [SEARCH, NOTES, "get_weather"]
        sent, parsed, _, ctx = run(text, tools)
        self.assertEqual(sent, named(named(text, SEARCH_LOST, SEARCH), NOTES_LOST, NOTES).encode("utf-8"))
        self.assertEqual(names_in(parsed), [SEARCH, "get_weather", NOTES])
        self.assertEqual(list(ctx.repairs), [{"emitted_len": len(SEARCH_LOST), "declared_len": len(SEARCH)},
                                             {"emitted_len": len(NOTES_LOST), "declared_len": len(NOTES)}])
        message = synthesize(text, tools)["choices"][0]["message"]
        self.assertEqual([(call["id"], call["function"]["name"], call["function"]["arguments"])
                          for call in message["tool_calls"]],
                         [("call-a", SEARCH, '{"query": "azure"}'), ("call-b", "get_weather", '{"city": "Busan"}'),
                          ("call-c", NOTES, "{}")])

    def test_a_name_repeated_in_every_chunk_is_put_right_in_each(self):
        items = [units.chunk(units.calls(0, name=SEARCH_LOST, arguments=piece)) for piece in ('{"query":', ' "x"}')]
        text = "".join(wire(*items, units.chunk({}, "tool_calls")))
        sent, _, _, ctx = run(text, [SEARCH])
        self.assertEqual(sent, named(text, SEARCH_LOST, SEARCH).encode("utf-8"))
        self.assertEqual(len(ctx.repairs), 2)
        function = synthesize(text, [SEARCH])["choices"][0]["message"]["tool_calls"][0]["function"]
        self.assertEqual(function, {"name": SEARCH, "arguments": '{"query": "x"}'})

    def test_the_non_stream_answer_has_the_declared_name(self):
        text = "".join(hermes(SEARCH_LOST))
        choice = synthesize(text, [SEARCH])["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertEqual(choice["message"]["tool_calls"], [
            {"id": "chatcmpl-tool-1", "type": "function", "function": {"name": SEARCH, "arguments": ARGS}}])
        unchanged = synthesize(text, [])["choices"][0]["message"]["tool_calls"]
        self.assertEqual(unchanged[0]["function"]["name"], SEARCH_LOST)

    def test_text_before_the_call_is_passed_on_and_the_call_is_put_right(self):
        text = "".join(with_preamble(SEARCH_LOST))
        sent, _, _, ctx = run(text, [SEARCH])
        self.assertEqual(sent, named(text, SEARCH_LOST, SEARCH).encode("utf-8"))
        before, after = text.split("\n\n"), sent.decode("utf-8").split("\n\n")
        self.assertEqual([i for i, pair in enumerate(zip(before, after)) if pair[0] != pair[1]], [3])
        self.assertEqual(list(ctx.repairs), [{"emitted_len": 45, "declared_len": 49}])
        choice = synthesize(text, [SEARCH])["choices"][0]
        self.assertEqual((choice["finish_reason"], choice["message"]["content"]), ("tool_calls", "".join(PREAMBLE)))
        self.assertEqual(choice["message"]["tool_calls"], [
            {"id": "chatcmpl-tool-1", "type": "function", "function": {"name": SEARCH, "arguments": ARGS}}])

    def test_a_name_written_in_the_text_is_not_touched(self):
        blocks = with_preamble(SEARCH_LOST)
        blocks[1] = wire(units.chunk({"content": f"Calling {SEARCH_LOST}"}))[0]
        text = "".join(blocks)
        sent = run(text, [SEARCH])[0]
        self.assertEqual(sent, named(text, SEARCH_LOST, SEARCH).encode("utf-8"))
        self.assertIn(f'"content":"Calling {SEARCH_LOST}"', sent.decode("utf-8"))

    def test_without_declared_tools_nothing_changes(self):
        text = "".join(hermes(SEARCH_LOST))
        for tools in ((), [], set()):
            sent, _, _, ctx = run(text, tools)
            self.assertEqual(sent, text.encode("utf-8"))
            self.assertEqual(list(ctx.repairs), [])
        sent, _, _, ctx = units.run(text)  # an Events without the argument, as the other tests build it
        self.assertEqual(sent, text.encode("utf-8"))
        self.assertEqual(list(ctx.repairs), [])

    def test_an_unrelated_or_an_ambiguous_name_stays(self):
        for emitted, tools in (("get_time", [SEARCH]), (SEARCH_LOST, [NOTES, "get_weather"]),
                               (SEARCH_EXTRA, [SEARCH, SEARCH_LOST]), (SEARCH_EXTRA, [SEARCH, SEARCH_LOST, NOTES])):
            with self.subTest(emitted=emitted, tools=tools):
                text = "".join(hermes(emitted))
                sent, _, _, ctx = run(text, tools)
                self.assertEqual(sent, text.encode("utf-8"))
                self.assertEqual(list(ctx.repairs), [])

    def test_reasoning_is_still_removed_and_a_renamed_call_is_kept(self):
        def start(index, name):
            return {"index": index, "id": f"call-{index}", "type": "function",
                    "function": {"name": name, "arguments": ""}}

        text = "".join(wire(units.chunk({"reasoning_content": "hmm"}),
                            units.chunk({"reasoning_content": "hmm", "tool_calls": [start(0, SEARCH_LOST)]}),
                            units.chunk({"reasoning": "hmm", "tool_calls": [start(1, "get_weather")]}),
                            units.chunk({}, "tool_calls"), "[DONE]"))
        sent, _, _, ctx = run(text, [SEARCH, "get_weather"])
        self.assertNotIn(b"reasoning", sent)
        blocks = [block for block in sent.decode("utf-8").split("\n\n") if block]
        self.assertEqual(len(blocks), 4)  # the reasoning-only chunk is gone, the two calls, the finish chunk, [DONE]
        self.assertEqual(json.loads(blocks[0][6:])["choices"][0]["delta"], {"tool_calls": [start(0, SEARCH)]})
        self.assertEqual(json.loads(blocks[1][6:])["choices"][0]["delta"], {"tool_calls": [start(1, "get_weather")]})
        self.assertEqual(blocks[3], "data: [DONE]")
        self.assertEqual(list(ctx.repairs), [{"emitted_len": 45, "declared_len": 49}])

    def test_name_pieces_are_left_as_they_come(self):
        # The hermes parser sends a name whole; a server that split names would get its pieces compared one by one.
        for pieces, whole in ((["mcp___mcp_exam", "ple_service___generic_search_", "lookup"], SEARCH),
                              (["mcp___exam", "ple_service___generic_search_lookup"], SEARCH_LOST)):
            with self.subTest(whole=len(whole)):
                first = {"index": 0, "id": "call-a", "type": "function",
                         "function": {"name": pieces[0], "arguments": ""}}
                items = [units.chunk({"tool_calls": [first]})]
                items += [units.chunk(units.calls(0, name=piece)) for piece in pieces[1:]]
                text = "".join(wire(*items, units.chunk({}, "tool_calls")))
                sent, _, _, ctx = run(text, [SEARCH])
                self.assertEqual(sent, text.encode("utf-8"))
                self.assertEqual(list(ctx.repairs), [])
                function = synthesize(text, [SEARCH])["choices"][0]["message"]["tool_calls"][0]["function"]
                self.assertEqual(function["name"], whole)

    def test_odd_tool_calls_are_passed_on_as_they_are(self):
        odd = [None, "text", {"index": 0, "function": {"name": SEARCH_LOST}}, [], [None, 5, "x", [], {}],
               [{"function": None}, {"function": "x"}, {"function": {}}, {"function": {"name": None}},
                {"function": {"name": 7}}, {"function": {"name": ""}}, {"function": {"name": [SEARCH_LOST]}}]]
        for shape in odd:
            with self.subTest(shape=shape):
                text = units.sse(units.chunk({"tool_calls": shape}))
                sent, _, _, ctx = run(text, [SEARCH])
                self.assertEqual(sent, text.encode("utf-8"))
                self.assertEqual(list(ctx.repairs), [])
        mixed = units.sse(units.chunk({"tool_calls": [5, {"index": 1, "function": {"name": SEARCH_LOST}}]}))
        _, parsed, _, ctx = run(mixed, [SEARCH])
        self.assertEqual(sent_calls(parsed), [5, {"index": 1, "function": {"name": SEARCH}}])
        self.assertEqual(len(ctx.repairs), 1)

    def test_the_usage_chunk_is_still_dropped_unless_asked_for(self):
        blocks = hermes(SEARCH_LOST)
        sent, _, parser, ctx = run("".join(blocks), [SEARCH], keep_usage=False)
        expected = "".join(named(block, SEARCH_LOST, SEARCH) for block in blocks if '"usage"' not in block)
        self.assertEqual(sent, expected.encode("utf-8"))
        self.assertEqual((parser.usage, ctx.prompt_tokens, ctx.completion_tokens), (units.USAGE["usage"], 11, 3))


class LogRecordTests(unittest.TestCase):
    def logged(self, ctx):
        with self.assertLogs("axk2.model_api", "INFO") as logs:
            ctx.log()
        line = logs.records[0].getMessage()
        return line, json.loads(line)

    def test_only_lengths_are_recorded(self):
        _, _, _, ctx = run("".join(hermes(SEARCH_LOST)), [SEARCH])
        line, record = self.logged(ctx)
        self.assertEqual(record["tool_name_repairs"], [{"emitted_len": 45, "declared_len": 49}])
        self.assertEqual(set(record), ANSWERED_KEYS | {"tool_name_repairs"})
        for secret in (SEARCH, SEARCH_LOST, "unique-arg-4417"):
            self.assertNotIn(secret, line)

    def test_a_call_without_a_repair_leaves_no_key(self):
        _, _, _, ctx = run("".join(hermes(SEARCH)), [SEARCH])
        self.assertEqual(set(self.logged(ctx)[1]), ANSWERED_KEYS)

    def test_a_long_run_of_repairs_keeps_the_line_short(self):
        calls = [{"index": i, "id": f"call-{i}", "type": "function",
                  "function": {"name": SEARCH_LOST, "arguments": ARGS}} for i in range(10)]
        text = "".join(wire(*[units.chunk({"tool_calls": [call]}) for call in calls]))
        _, _, _, ctx = run(text, [SEARCH])
        self.assertEqual(self.logged(ctx)[1]["tool_name_repairs"], [{"emitted_len": 45, "declared_len": 49}] * 8)


class ToolNameApiTests(failures.FailureCase):
    async def ask(self, blocks, declared=(SEARCH,), stream=False):
        """One call that vLLM answers with `blocks`; the request declares `declared` (no `tools` when empty)."""
        self.script(Reply(*blocks))
        payload = dict(ASK, stream=True, stream_options={"include_usage": True}) if stream else dict(ASK)
        if declared:
            payload["tools"] = declare(*declared)
        answer = await self.chat(payload)
        self.assertEqual(answer.status, 200, answer.text)
        return answer

    async def test_a_non_stream_answer_has_the_declared_name(self):
        answer = await self.ask(hermes(SEARCH_LOST), [SEARCH, NOTES])
        choice = answer.json()["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertEqual(choice["message"]["tool_calls"], [
            {"id": "chatcmpl-tool-1", "type": "function", "function": {"name": SEARCH, "arguments": ARGS}}])
        self.assertEqual([tool["function"]["name"] for tool in self.upstream()["tools"]], [SEARCH, NOTES])

    async def test_a_stream_has_the_declared_name_and_no_other_change(self):
        blocks = hermes(SEARCH_LOST)
        answer = await self.ask(blocks, [SEARCH], stream=True)
        self.assertEqual(answer.text, named("".join(blocks), SEARCH_LOST, SEARCH))
        items, done = chat_tests.chunks(answer.text)
        self.assertTrue(done)
        self.assertEqual(names_in(items), [SEARCH])

    async def test_several_calls_are_put_right_one_by_one(self):
        blocks = parallel(SEARCH_LOST, "get_weather", NOTES_LOST)
        declared = [SEARCH, NOTES, "get_weather"]
        answer = await self.ask(blocks, declared)
        calls = answer.json()["choices"][0]["message"]["tool_calls"]
        self.assertEqual([(call["id"], call["function"]["name"]) for call in calls],
                         [("call-a", SEARCH), ("call-b", "get_weather"), ("call-c", NOTES)])
        answer = await self.ask(blocks, declared, stream=True)
        self.assertEqual(names_in(chat_tests.chunks(answer.text)[0]), [SEARCH, "get_weather", NOTES])

    async def test_text_before_the_call_arrives_and_the_call_has_the_declared_name(self):
        blocks = with_preamble(SEARCH_LOST)
        message = (await self.ask(blocks, [SEARCH])).json()["choices"][0]["message"]
        self.assertEqual(message["content"], "".join(PREAMBLE))
        self.assertEqual([call["function"] for call in message["tool_calls"]], [{"name": SEARCH, "arguments": ARGS}])
        answer = await self.ask(blocks, [SEARCH], stream=True)
        self.assertEqual(answer.text, named("".join(blocks), SEARCH_LOST, SEARCH))
        items, done = chat_tests.chunks(answer.text)
        self.assertTrue(done)
        self.assertEqual((chat_tests.content_of(items), names_in(items)), ("".join(PREAMBLE), [SEARCH]))

    async def test_names_that_need_no_change_arrive_unchanged(self):
        cases = ((SEARCH, [SEARCH]), (SEARCH_LOST, [SEARCH, SEARCH_LOST]), ("get_time", [SEARCH]),
                 (SEARCH_EXTRA, [SEARCH, SEARCH_LOST]), (SEARCH_LOST, []))
        for emitted, declared in cases:
            blocks = hermes(emitted)
            with self.subTest(emitted=len(emitted), declared=len(declared)):
                answer = await self.ask(blocks, declared)
                self.assertEqual(answer.json()["choices"][0]["message"]["tool_calls"][0]["function"]["name"], emitted)
                answer = await self.ask(blocks, declared, stream=True)
                self.assertEqual(answer.text, "".join(blocks))
        records = await self.logged(len(cases) * 2)
        self.assertTrue(all(set(record) == ANSWERED_KEYS for record in records), records)

    async def test_a_name_that_is_not_a_string_arrives_as_it_was_sent(self):
        start = {"index": 0, "id": "call-1", "type": "function", "function": {"name": 7, "arguments": "{}"}}
        blocks = wire(units.chunk({"tool_calls": [start]}), units.chunk({}, "tool_calls"), "[DONE]")
        answer = await self.ask(blocks, [SEARCH], stream=True)
        self.assertEqual(answer.text, "".join(blocks))
        self.assertEqual(set((await self.logged(1))[0]), ANSWERED_KEYS)

    async def test_the_log_has_lengths_only(self):
        for stream in (False, True):
            await self.ask(hermes(SEARCH_LOST), [SEARCH], stream=stream)
        records = await self.logged(2)
        for record in records:
            self.assertEqual(set(record), ANSWERED_KEYS | {"tool_name_repairs"})
            self.assertEqual(record["tool_name_repairs"], [{"emitted_len": 45, "declared_len": 49}])
        logged = sys.stdout.getvalue()
        for secret in (SEARCH, SEARCH_LOST, "unique-arg-4417"):
            self.assertNotIn(secret, logged)


if __name__ == "__main__":
    unittest.main()
