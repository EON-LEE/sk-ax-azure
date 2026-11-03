"""What the model API's log line says of the tool calls in an answer (`Watch` in demo/frontend/model_api.py): how many
calls were parsed; for each name its length, whether it was declared, whether it was replaced, how many "mcp_" parts
it has, whether it starts with "functions." and whether it has a dot; a finish reason of "tool_calls" with no call; a
hermes tag left in the text. The line holds counts, lengths and flags, never a name, an argument or a word of the
text, and the answer is the same with or without the watching. First on streams fed to Events, then on loopback with
the real app (the fixtures come from test_demo_model_api_tool_names.py). All names are made-up placeholders."""
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
    import test_demo_model_api_tool_names as names
finally:
    del sys.path[:1]

api, ASK, Reply = units.api, support.ASK, support.Reply
SEARCH, SEARCH_LOST, NOTES, NOTES_LOST = names.SEARCH, names.SEARCH_LOST, names.NOTES, names.NOTES_LOST
FUNCTIONS = "functions.example_service.generic_search_lookup"  # 47 characters: a prefix and two dots
TAG, END = "<tool_call>", "</tool_call>"
ONE_REPAIR = [{"emitted_len": 45, "declared_len": 49}]  # SEARCH_LOST replaced by SEARCH


def facts(length, declared=False, repaired=False, mcp_parts=0, functions_prefix=False, has_dot=False):
    """What the log says of one tool name."""
    return {"len": length, "declared": declared, "repaired": repaired, "mcp_parts": mcp_parts,
            "functions_prefix": functions_prefix, "has_dot": has_dot}


def summary(*calls, count=None, finish_without_calls=False, marker_in_text=False):
    """The log's `tool_calls` for calls that `facts` describes, in index order."""
    return {"count": len(calls) if count is None else count, "names": list(calls),
            "finish_without_calls": finish_without_calls, "marker_in_text": marker_in_text}


EXACT = facts(49, declared=True, mcp_parts=2)  # SEARCH, where SEARCH is declared
NEAR = facts(45, repaired=True, mcp_parts=1)  # SEARCH_LOST, where SEARCH is declared
PREFIXED = facts(47, functions_prefix=True, has_dot=True)  # FUNCTIONS


def start(name, index=0, call_id="call-a", arguments=""):
    """A delta that begins tool call `index` with its id and the whole name."""
    return {"tool_calls": [{"index": index, "id": call_id, "type": "function",
                            "function": {"name": name, "arguments": arguments}}]}


def stream(*deltas, finish="stop"):
    """The events of an answer made of `deltas`: a role chunk first, then a chunk with the finish reason (none when
    `finish` is None), the usage chunk and [DONE]."""
    items = [units.chunk({"role": "assistant", "content": ""}), *[units.chunk(delta) for delta in deltas]]
    if finish:
        items.append(units.chunk({}, finish))
    return "".join(names.wire(*items, units.USAGE, "[DONE]"))


def leaves(value):
    """The values at the ends of nested dicts and lists."""
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        return [leaf for item in value for leaf in leaves(item)]
    return [value]


class WatchTests(unittest.TestCase):
    def watched(self, text, tools=(SEARCH,), size=None):
        """(bytes sent on, the log record, the log line) of the stream `text` for a request that declares `tools`."""
        sent, _, _, ctx = names.run(text, tools, size=size)
        with self.assertLogs("axk2.model_api", "INFO") as logs:
            ctx.log()
        line = logs.records[0].getMessage()
        return sent, json.loads(line), line

    def seen(self, text, tools=(SEARCH,), size=None):
        """The `tool_calls` of the log record."""
        return self.watched(text, tools, size)[1]["tool_calls"]

    def test_a_watch_that_saw_nothing_has_an_empty_summary(self):
        self.assertEqual(api.Watch(frozenset()).summary(None), summary())
        self.assertEqual(api.Watch(frozenset()).summary("tool_calls"), summary(finish_without_calls=True))

    def test_a_declared_name_is_listed_as_declared_and_nothing_changes(self):
        text = "".join(names.hermes(SEARCH))
        sent, record, _ = self.watched(text)
        self.assertEqual(record["tool_calls"], summary(EXACT))
        self.assertNotIn("tool_name_repairs", record)
        self.assertEqual(sent, text.encode("utf-8"))

    def test_a_name_that_differs_by_mcp_parts_is_listed_as_replaced_and_as_the_model_wrote_it(self):
        cases = ((SEARCH_LOST, SEARCH, NEAR, 49), (SEARCH, SEARCH_LOST, facts(49, repaired=True, mcp_parts=2), 45))
        for emitted, declared, expected, declared_len in cases:
            with self.subTest(emitted=len(emitted)):
                text = "".join(names.hermes(emitted))
                sent, record, _ = self.watched(text, [declared])
                self.assertEqual(record["tool_calls"], summary(expected))
                self.assertEqual(record["tool_name_repairs"],
                                 [{"emitted_len": len(emitted), "declared_len": declared_len}])
                self.assertEqual(sent, names.named(text, emitted, declared).encode("utf-8"))

    def test_a_functions_prefix_is_noted_and_the_name_is_left_as_it_is(self):
        cases = ((FUNCTIONS, PREFIXED),
                 ("functions." + SEARCH, facts(59, mcp_parts=2, functions_prefix=True, has_dot=True)))
        for emitted, expected in cases:
            with self.subTest(emitted=len(emitted)):
                text = "".join(names.hermes(emitted))
                sent, record, _ = self.watched(text)
                self.assertEqual(record["tool_calls"], summary(expected))
                self.assertNotIn("tool_name_repairs", record)
                self.assertEqual(sent, text.encode("utf-8"))

    def test_a_dot_without_the_prefix_is_noted_alone(self):
        text = stream(start("example_service.generic_search_lookup"), finish="tool_calls")
        self.assertEqual(self.seen(text), summary(facts(37, has_dot=True)))

    def test_a_name_that_is_not_close_to_a_declared_one_is_listed_as_it_is(self):
        cases = (("get_time", [SEARCH], facts(8)), ("get_weather", ["get_weather"], facts(11, declared=True)))
        for emitted, declared, expected in cases:
            with self.subTest(emitted=emitted):
                text = "".join(names.hermes(emitted))
                sent, record, _ = self.watched(text, declared)
                self.assertEqual(record["tool_calls"], summary(expected))
                self.assertNotIn("tool_name_repairs", record)
                self.assertEqual(sent, text.encode("utf-8"))

    def test_without_tools_no_name_is_declared_or_replaced(self):
        text = "".join(names.hermes(SEARCH_LOST))
        for tools in ((), []):
            with self.subTest(tools=tools):
                sent, record, _ = self.watched(text, tools)
                self.assertEqual(record["tool_calls"], summary(facts(45, mcp_parts=1)))
                self.assertNotIn("tool_name_repairs", record)
                self.assertEqual(sent, text.encode("utf-8"))

    def test_several_calls_are_listed_one_by_one(self):
        text = "".join(names.parallel(SEARCH_LOST, "get_weather", NOTES_LOST))
        sent, record, _ = self.watched(text, [SEARCH, NOTES, "get_weather"])
        self.assertEqual(record["tool_calls"],
                         summary(NEAR, facts(11, declared=True), facts(23, repaired=True, mcp_parts=1)))
        self.assertEqual(record["tool_name_repairs"], ONE_REPAIR + [{"emitted_len": 23, "declared_len": 27}])
        wanted = names.named(names.named(text, SEARCH_LOST, SEARCH), NOTES_LOST, NOTES)
        self.assertEqual(sent, wanted.encode("utf-8"))

    def test_the_names_are_listed_in_index_order_not_in_the_order_they_arrive(self):
        text = stream(start("get_c", 2, "call-c"), start("get_a", 0, "call-a"), start("get_bb", 1, "call-b"),
                      finish="tool_calls")
        self.assertEqual(self.seen(text, ()), summary(facts(5), facts(6), facts(5)))

    def test_at_most_eight_names_are_listed_and_all_calls_are_counted(self):
        text = stream(*[start(f"tool_{i}", i, f"call-{i}") for i in range(10)], finish="tool_calls")
        seen = self.seen(text, ())
        self.assertEqual((seen["count"], len(seen["names"])), (10, 8))

    def test_a_call_without_a_name_is_counted_with_a_length_of_zero(self):
        bare = {"tool_calls": [{"index": 0, "id": "call-a", "type": "function", "function": {"arguments": "{}"}}]}
        self.assertEqual(self.seen(stream(bare, finish="tool_calls"), ()), summary(facts(0)))

    def test_a_name_in_pieces_is_put_together_and_is_not_replaced(self):
        def pieces(name):
            return stream(start(name[:20]), units.calls(0, name=name[20:]), units.calls(0, arguments="{}"),
                          finish="tool_calls")

        sent, record, _ = self.watched(pieces(SEARCH))
        self.assertEqual((record["tool_calls"], sent), (summary(EXACT), pieces(SEARCH).encode("utf-8")))
        sent, record, _ = self.watched(pieces(SEARCH_LOST))
        self.assertEqual((record["tool_calls"], sent),
                         (summary(facts(45, mcp_parts=1)), pieces(SEARCH_LOST).encode("utf-8")))
        self.assertNotIn("tool_name_repairs", record)

    def test_a_name_sent_again_for_the_same_call_counts_once(self):
        text = stream(start(SEARCH_LOST), units.calls(0, name=SEARCH_LOST, arguments="{}"), finish="tool_calls")
        sent, record, _ = self.watched(text)
        self.assertEqual(record["tool_calls"], summary(NEAR))
        self.assertEqual(sent, names.named(text, SEARCH_LOST, SEARCH).encode("utf-8"))

    def test_the_count_and_the_lengths_agree_with_the_non_stream_answer(self):
        streams = (names.hermes(SEARCH), names.hermes("get_time"), names.with_preamble("get_time"),
                   names.parallel("get_a", "get_bb", "get_ccc"))
        for blocks in streams:
            text = "".join(blocks)
            message = names.synthesize(text, ())["choices"][0]["message"]
            wanted = [call["function"]["name"] for call in message.get("tool_calls", ())]
            seen = self.seen(text, ())
            with self.subTest(calls=len(wanted)):
                self.assertEqual((seen["count"], [one["len"] for one in seen["names"]]),
                                 (len(wanted), [len(name) for name in wanted]))

    def test_odd_shapes_do_not_break_the_watching(self):
        cases = (({"tool_calls": 5}, 0), ({"tool_calls": "x"}, 0), ({"tool_calls": {"index": 0}}, 0),
                 ({"tool_calls": [None, 5, "x", []]}, 0), ({"tool_calls": [{"index": "0", "function": 5}]}, 1),
                 ({"tool_calls": [{"index": True, "function": {"name": 7}}]}, 1),
                 ({"content": 5}, 0), ({"content": None}, 0), ({"content": [TAG]}, 0), ({"content": {"x": TAG}}, 0))
        for delta, count in cases:
            with self.subTest(delta=delta):
                text = stream(delta)
                sent, record, _ = self.watched(text)
                self.assertEqual(record["tool_calls"], summary(*[facts(0)] * count))
                self.assertEqual(sent, text.encode("utf-8"))

    def test_a_finish_of_tool_calls_with_no_call_is_flagged_and_nothing_else_is(self):
        cases = (("tool_calls", 0, True), ("stop", 0, False), ("length", 0, False), (None, 0, False),
                 ("tool_calls", 1, False), ("stop", 1, False))
        for finish, count, flagged in cases:
            with self.subTest(finish=finish, count=count):
                text = stream(*[start("get_time")] * count, finish=finish)
                _, record, _ = self.watched(text, ())
                self.assertEqual(record["finish_reason"], finish)
                self.assertEqual(record["tool_calls"], summary(*[facts(8)] * count, finish_without_calls=flagged))

    def test_a_tag_left_in_the_text_is_flagged(self):
        for text in ("Sure. " + TAG + '{"name": "get_time"}' + END, "stray " + END, TAG):
            with self.subTest(text=len(text)):
                self.assertEqual(self.seen(stream({"content": text})), summary(marker_in_text=True))

    def test_a_tag_cut_in_two_deltas_is_found_at_every_cut(self):
        for tag in (TAG, END):
            for cut in range(1, len(tag)):
                with self.subTest(tag=tag, cut=cut):
                    text = stream({"content": "ok " + tag[:cut]}, {"content": tag[cut:] + " done"})
                    self.assertTrue(self.seen(text)["marker_in_text"])
        one_by_one = stream(*[{"content": char} for char in "see " + TAG + " now"])
        self.assertTrue(self.seen(one_by_one)["marker_in_text"])

    def test_text_that_only_looks_like_a_tag_is_not_flagged(self):
        for text in ("call the tool_call function", "<tool_call", "<tool_calls>", "</tool_call", "tool_call>",
                     "< tool_call >"):
            with self.subTest(text=text):
                self.assertFalse(self.seen(stream({"content": text}))["marker_in_text"])
        halves = stream({"content": "<tool"}, {"content": "_call"}, {"content": " x"})
        self.assertFalse(self.seen(halves)["marker_in_text"])

    def test_a_tag_outside_the_text_is_not_flagged(self):
        deltas = ({"reasoning_content": TAG}, {"reasoning": END}, start(TAG), units.calls(0, arguments=TAG))
        for delta in deltas:
            with self.subTest(delta=sorted(delta)):
                self.assertFalse(self.seen(stream(delta))["marker_in_text"])

    def test_a_tag_and_a_call_are_both_noted(self):
        text = stream({"content": "Sure. " + TAG}, start(SEARCH), finish="tool_calls")
        self.assertEqual(self.seen(text), summary(EXACT, marker_in_text=True))

    def test_the_record_does_not_depend_on_how_the_text_is_split(self):
        text = stream({"content": "Look: " + TAG}, start(SEARCH_LOST), units.calls(0, arguments="{}"),
                      finish="tool_calls")
        sent, record, _ = self.watched(text)
        self.assertEqual(record["tool_calls"], summary(NEAR, marker_in_text=True))
        for size in (1, 7, 64):
            with self.subTest(size=size):
                again_sent, again, _ = self.watched(text, size=size)
                self.assertEqual((again_sent, again["tool_calls"]), (sent, record["tool_calls"]))

    def test_the_line_holds_no_name_no_argument_and_no_text(self):
        texts = ("".join(names.with_preamble(SEARCH_LOST)),
                 stream({"content": "unique-text-5521 " + TAG}, start(FUNCTIONS, arguments='{"q": "unique-arg-4417"}'),
                        finish="tool_calls"))
        hidden = (SEARCH, SEARCH_LOST, FUNCTIONS, "example_service", "generic_search", "unique-arg-4417",
                  "unique-text-5521", TAG, *names.PREAMBLE)
        for text in texts:
            with self.subTest(text=len(text)):
                _, record, line = self.watched(text)
                for piece in hidden:
                    self.assertNotIn(piece, line)
                self.assertTrue(all(type(leaf) in (int, bool) for leaf in leaves(record["tool_calls"])))

    def test_the_answer_is_the_same_without_the_watching(self):
        declared = [SEARCH, NOTES, "get_weather"]
        texts = ("".join(names.hermes(SEARCH_LOST)), "".join(names.with_preamble(SEARCH)),
                 "".join(names.parallel(SEARCH_LOST, "get_weather", NOTES_LOST)),
                 stream({"content": TAG}, finish="tool_calls"))
        for text in texts:
            with self.subTest(size=len(text)):
                watched = names.run(text, declared)
                with mock.patch.object(api.Watch, "see", lambda watch, delta: None):
                    unwatched = names.run(text, declared)
                self.assertEqual(watched[:2], unwatched[:2])
                self.assertEqual(list(watched[3].repairs), list(unwatched[3].repairs))


class ToolLogApiTests(failures.FailureCase):
    async def both(self, blocks, declared=(SEARCH,)):
        """(plain answer, streamed answer, their two log records) for one request that vLLM answers with `blocks`
        each time; the request declares `declared` (no `tools` when empty)."""
        before = len(self.logs())
        self.script(Reply(*blocks), Reply(*blocks))
        payload = dict(ASK, tools=names.declare(*declared)) if declared else dict(ASK)
        plain = await self.chat(payload)
        streamed = await self.chat(dict(payload, stream=True, stream_options={"include_usage": True}))
        self.assertEqual((plain.status, streamed.status), (200, 200), (plain.text, streamed.text))
        records = (await self.logged(before + 2))[before:]
        self.assertEqual([record["stream"] for record in records], [False, True])
        return plain, streamed, records

    @staticmethod
    def called(plain):
        """The names of the tool calls in a plain answer."""
        message = plain.json()["choices"][0]["message"]
        return [call["function"]["name"] for call in message.get("tool_calls", ())]

    async def test_an_exact_name_is_logged_in_a_plain_and_a_streamed_answer(self):
        blocks = names.hermes(SEARCH)
        plain, streamed, records = await self.both(blocks)
        for record in records:
            self.assertEqual(record["tool_calls"], summary(EXACT))
            self.assertNotIn("tool_name_repairs", record)
        self.assertEqual(self.called(plain), [SEARCH])
        self.assertEqual(streamed.text, "".join(blocks))

    async def test_a_name_put_right_is_logged_as_the_model_wrote_it(self):
        plain, streamed, records = await self.both(names.hermes(SEARCH_LOST))
        for record in records:
            self.assertEqual(record["tool_calls"], summary(NEAR))
            self.assertEqual(record["tool_name_repairs"], ONE_REPAIR)
        self.assertEqual(self.called(plain), [SEARCH])
        self.assertEqual(names.names_in(chat_tests.chunks(streamed.text)[0]), [SEARCH])

    async def test_a_functions_prefix_is_logged_and_the_name_is_left_as_it_is(self):
        blocks = names.hermes(FUNCTIONS)
        plain, streamed, records = await self.both(blocks)
        for record in records:
            self.assertEqual(record["tool_calls"], summary(PREFIXED))
            self.assertNotIn("tool_name_repairs", record)
        self.assertEqual(self.called(plain), [FUNCTIONS])
        self.assertEqual(streamed.text, "".join(blocks))

    async def test_a_name_that_is_not_declared_is_logged_as_such(self):
        blocks = names.hermes(SEARCH_LOST)
        plain, streamed, records = await self.both(blocks, declared=())
        for record in records:
            self.assertEqual(record["tool_calls"], summary(facts(45, mcp_parts=1)))
            self.assertNotIn("tool_name_repairs", record)
        self.assertEqual(self.called(plain), [SEARCH_LOST])
        self.assertEqual(streamed.text, "".join(blocks))

    async def test_a_finish_of_tool_calls_with_no_call_is_logged_and_the_answer_is_left_as_it_is(self):
        plain, streamed, records = await self.both(support.say(finish="tool_calls"))
        for record in records:
            self.assertEqual(record["finish_reason"], "tool_calls")
            self.assertEqual(record["tool_calls"], summary(finish_without_calls=True))
        choice = plain.json()["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertNotIn("tool_calls", choice["message"])
        items, done = chat_tests.chunks(streamed.text)
        self.assertEqual((done, names.names_in(items)), (True, []))

    async def test_a_tag_in_the_text_is_logged_and_the_text_is_left_as_it_is(self):
        text = TAG + '{"name": "get_time", "arguments": {}}' + END
        plain, streamed, records = await self.both(support.say("Sure. ", text))
        for record in records:
            self.assertEqual(record["tool_calls"], summary(marker_in_text=True))
        self.assertEqual(plain.json()["choices"][0]["message"]["content"], "Sure. " + text)
        self.assertEqual(chat_tests.content_of(chat_tests.chunks(streamed.text)[0]), "Sure. " + text)

    async def test_an_answer_with_text_only_logs_nothing_found(self):
        _, _, records = await self.both(support.say("Hello."), declared=())
        for record in records:
            self.assertEqual(record["tool_calls"], summary())

    async def test_the_log_holds_no_name_no_argument_and_no_text(self):
        await self.both(names.with_preamble(SEARCH_LOST))
        await self.both(names.hermes(FUNCTIONS), declared=())
        await self.both(support.say("unique-text-5521 ", TAG), declared=())
        logged = sys.stdout.getvalue()
        for piece in (SEARCH, SEARCH_LOST, FUNCTIONS, "example_service", "unique-arg-4417", "unique-text-5521",
                      names.PREAMBLE[0], TAG):
            self.assertNotIn(piece, logged)


if __name__ == "__main__":
    unittest.main()
