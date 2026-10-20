"""Tool-round budget and the last model call, which gets no tools: closing note, held-back answer, markup removal.

Model replies are scripted fixtures. They put hermes <tool_call> markup into plain answer text, as live runs showed in
the call that sends no tools (cause not verified); they say nothing about how the live model behaves."""
import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "demo" / "frontend"))
from agent import (AXClient, LAST_ROUND_NOTE, MODEL_CALLS, TOOL_LIMIT_NOTICE, TOOL_ROUNDS, agent_response,
                   strip_tool_markup, without_content)
from hub import Gate
from workspace import Workspace
sys.path.pop(0)
from tests.test_demo_agent import FixtureHub
from tests.test_web_search_latency import ScriptedLink, answer_round, sse_events

PROMPT = "계산기로 2+2 계산해줘"
MARKUP = '<tool_call>\n{"name": "bash", "arguments": {"command": "ls"}}\n</tool_call>'
USAGE = {"prompt_tokens": 100, "completion_tokens": 20}


def calc_round(number, count=1):
    """One scripted tool round with `count` parallel calculator calls and ids that stay unique across rounds."""
    calls = [{"index": i, "id": f"call_{number}_{i}", "function": {
        "name": "calculator", "arguments": json.dumps({"expression": f"{number}+{i}"})}} for i in range(count)]
    return [{"delta": {"tool_calls": calls}, "finish_reason": "tool_calls"}]


def last_round(*texts, reasoning=None):
    """The scripted last model call: reasoning first when given, one content chunk per text, then the finish chunk."""
    choices = [{"delta": {"reasoning_content": reasoning}}] if reasoning else []
    return choices + [{"delta": {"content": text}} for text in texts] + [{"delta": {}, "finish_reason": "stop"}]


class FailingLink(ScriptedLink):
    """The link breaks in the middle of the last scripted round instead of ending its stream."""
    def send(self, message):
        if message["type"] == "cancel" or len(self.requests) < len(self.rounds) - 1:
            return super().send(message)
        index = len(self.requests)
        self.requests.append(message["body"])
        queue = self.streams[message["id"]]
        queue.put_nowait({"type": "head", "status": 200})
        for choice in self.rounds[index]:
            queue.put_nowait({"type": "data", "chunk": "data: " + json.dumps({"choices": [choice]}) + "\n\n"})
        queue.put_nowait({"type": "error", "message": "link failed"})


async def run_turn(link, workspace=None, text=PROMPT, use_tools=True):
    workspace = Workspace() if workspace is None else workspace
    response = agent_response(FixtureHub(link), Gate(1, 2), {"messages": [{"role": "user", "content": text}],
                                                            "model": "axk2"}, workspace, use_tools)
    return sse_events(b"".join([chunk async for chunk in response.body_iterator]).decode())


def rounds_of(events):
    """The message chunks the browser got, one list per model call."""
    found = []
    for name, data in events:
        if name == "round":
            found.append([])
        elif name == "message":
            found[-1].append(data)
    return found


def shown(chunks):
    return "".join((choice.get("delta") or {}).get("content") or ""
                   for chunk in chunks for choice in chunk.get("choices") or [])


def metrics_of(events):
    return next(data for name, data in events if name == "metrics")


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    def test_the_notes_are_the_texts_the_coordinator_asked_for(self):
        self.assertEqual(LAST_ROUND_NOTE, "도구를 더 쓸 수 없음. 지금까지 결과로 답하고, 끝내지 못한 단계는 그렇다고 밝힐 것. "
                                          "도구 호출 형식을 출력하지 말 것.")
        self.assertEqual(TOOL_LIMIT_NOTICE, "도구 사용 한도에 도달해 남은 단계를 실행하지 못했습니다. "
                                            "지금까지 확인한 결과는 위 도구 카드에 있습니다.")

    def test_one_constant_sets_the_budget_and_the_other_limits_stay(self):
        self.assertEqual((TOOL_ROUNDS, MODEL_CALLS), (6, 7))
        client = AXClient(FixtureHub(ScriptedLink([])), Gate(1, 2), {"messages": [{"role": "user", "content": "x"}]},
                          None)
        configuration = client.function_invocation_configuration
        self.assertEqual(configuration["max_iterations"], MODEL_CALLS)
        self.assertEqual((configuration["max_function_calls"], configuration["max_duration_seconds"]), (24, 900))

    async def test_six_tool_rounds_then_one_call_without_tools(self):
        link = ScriptedLink([calc_round(n) for n in range(1, TOOL_ROUNDS + 1)] + [answer_round("끝")])
        events = await run_turn(link)
        self.assertEqual(len(link.requests), MODEL_CALLS)
        for body in link.requests[:TOOL_ROUNDS]:
            self.assertIn("tools", body)
            self.assertNotIn(LAST_ROUND_NOTE, json.dumps(body, ensure_ascii=False))
        last = link.requests[-1]
        self.assertNotIn("tools", last)
        self.assertNotIn("tool_choice", last)
        self.assertEqual(last["messages"][-1], {"role": "system", "content": LAST_ROUND_NOTE})
        self.assertEqual(sum(m["content"] == LAST_ROUND_NOTE for m in last["messages"] if m["role"] == "system"), 1)
        self.assertEqual(metrics_of(events)["model_calls"], MODEL_CALLS)
        self.assertEqual(shown(rounds_of(events)[-1]), "끝")
        self.assertNotIn("error", [name for name, _ in events])

    async def test_a_turn_that_answers_early_never_gets_the_note(self):
        link = ScriptedLink([calc_round(1), answer_round("4입니다")])
        events = await run_turn(link)
        self.assertEqual(len(link.requests), 2)
        for body in link.requests:
            self.assertIn("tools", body)
            self.assertNotIn(LAST_ROUND_NOTE, json.dumps(body, ensure_ascii=False))
        self.assertEqual(rounds_of(events)[-1], [{"choices": [{"delta": {"content": "4입니다"}, "finish_reason": "stop"}]}])

    async def test_the_function_call_cap_also_ends_in_a_round_with_the_note(self):
        link = ScriptedLink([calc_round(n, 8) for n in range(1, 4)] + [answer_round("끝")])
        events = await run_turn(link)
        self.assertEqual(len(link.requests), 4)
        self.assertNotIn("tools", link.requests[-1])
        self.assertEqual(link.requests[-1]["messages"][-1], {"role": "system", "content": LAST_ROUND_NOTE})
        self.assertEqual(shown(rounds_of(events)[-1]), "끝")

    async def test_a_model_that_still_calls_a_tool_in_the_last_round_ends_the_turn_with_an_error(self):
        link = ScriptedLink([calc_round(n) for n in range(1, MODEL_CALLS + 1)] + [answer_round("never requested")])
        events = await run_turn(link)
        self.assertEqual(len(link.requests), MODEL_CALLS)
        self.assertIn("round limit", next(data for name, data in events if name == "error")["message"])

    async def test_a_turn_without_tools_gets_neither_note_nor_buffering(self):
        for text, use_tools in (("안녕", False), ("도구는 쓰지 마. 안녕", True)):
            with self.subTest(text=text, use_tools=use_tools):
                link = ScriptedLink([[{"delta": {"content": "안"}}, {"delta": {"content": "녕"}, "finish_reason": "stop"}]])
                events = await run_turn(link, text=text, use_tools=use_tools)
                self.assertNotIn("tools", link.requests[0])
                self.assertNotIn(LAST_ROUND_NOTE, json.dumps(link.requests[0], ensure_ascii=False))
                self.assertEqual([shown([chunk]) for chunk in rounds_of(events)[0]], ["안", "녕"])


class LastRoundTests(unittest.IsolatedAsyncioTestCase):
    async def finish(self, final, workspace=None, link_class=ScriptedLink):
        rounds = [calc_round(n) for n in range(1, TOOL_ROUNDS + 1)] + [final]
        link = link_class(rounds, [None] * TOOL_ROUNDS + [USAGE])
        return link, await run_turn(link, workspace)

    async def test_the_answer_is_sent_once_between_the_reasoning_and_the_closing_chunks(self):
        _, events = await self.finish(last_round("첫째 ", "둘째", reasoning="정리 중"))
        self.assertEqual(rounds_of(events)[-1], [
            {"choices": [{"delta": {"reasoning_content": "정리 중"}}]},
            {"choices": [{"index": 0, "delta": {"content": "첫째 둘째"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": USAGE}])

    async def test_markup_is_removed_before_anything_is_sent(self):
        cases = {
            "block after the answer": (["결과는 4입니다.\n", MARKUP], "결과는 4입니다.", True),
            "tags split across chunks": (["설명\n<tool_", 'call>{"name": "bash"}</tool', "_call>\n끝"], "설명\n\n끝", True),
            "two blocks": (["A\n", MARKUP, "\nB\n", MARKUP], "A\n\nB", True),
            "unclosed tag": (["앞부분 답변\n<tool_call>\n", '{"name": "bash"'], "앞부분 답변", True),
            "markup only": ([MARKUP], TOOL_LIMIT_NOTICE, True),
            "unclosed markup only": (["<tool_call>", '{"name"'], TOOL_LIMIT_NOTICE, True),
            "no text at all": ([], TOOL_LIMIT_NOTICE, False),
            "blank text": (["  \n"], TOOL_LIMIT_NOTICE, False),
            "plain answer": (["표는 ", "<table>입니다."], "표는 <table>입니다.", False),
        }
        for name, (texts, expected, removed) in cases.items():
            with self.subTest(name):
                _, events = await self.finish(last_round(*texts))
                chunks = rounds_of(events)[-1]
                self.assertEqual(shown(chunks), expected)
                self.assertNotIn("<tool_call", json.dumps(chunks, ensure_ascii=False))
                self.assertEqual(sum(1 for chunk in chunks if shown([chunk])), 1)
                self.assertEqual(chunks[-2:], [{"choices": [{"delta": {}, "finish_reason": "stop"}]},
                                               {"choices": [], "usage": USAGE}])
                self.assertEqual(metrics_of(events)["rounds"][-1]["markup_removed"], removed)
                self.assertNotIn("notice", [event for event, _ in events])  # the notice is no "promised action"

    async def test_the_chat_keeps_and_replays_the_cleaned_answer(self):
        space = Workspace()
        await self.finish(last_round("결과는 4입니다.\n", MARKUP), space)
        kept = [row["content"] for row in space.history if row["role"] == "assistant" and row.get("content")]
        self.assertEqual(kept[-1], "결과는 4입니다.")
        self.assertNotIn("<tool_call", json.dumps(space.history, ensure_ascii=False))
        second = ScriptedLink([answer_round("네")])
        await run_turn(second, space, "방금 답을 한 줄로 줄여줘")
        replayed = json.dumps(second.requests[0], ensure_ascii=False)
        self.assertNotIn("<tool_call", replayed)
        self.assertNotIn(LAST_ROUND_NOTE, replayed)
        self.assertIn("결과는 4입니다.", replayed)

    async def test_the_notice_is_what_the_chat_keeps_when_nothing_is_left(self):
        space = Workspace()
        await self.finish(last_round(MARKUP), space)
        self.assertEqual([row["content"] for row in space.history if row["role"] == "assistant"][-1],
                         TOOL_LIMIT_NOTICE)

    async def test_metrics_flag_only_the_round_that_lost_markup_and_keep_the_old_fields(self):
        _, events = await self.finish(last_round("끝\n", MARKUP))
        metrics = metrics_of(events)
        self.assertEqual([entry["markup_removed"] for entry in metrics["rounds"]], [False] * TOOL_ROUNDS + [True])
        self.assertEqual((metrics["model_calls"], metrics["rounds"][-1]["output_tokens"]), (MODEL_CALLS, 20))
        self.assertGreaterEqual(metrics["ttft_seconds"], 0)
        for entry in metrics["rounds"]:
            self.assertLessEqual({"index", "input_tokens", "output_tokens", "first_token_seconds", "seconds",
                                  "cached_tokens"}, set(entry))
        _, plain = await self.finish(last_round("끝"))
        self.assertEqual([entry["markup_removed"] for entry in metrics_of(plain)["rounds"]], [False] * MODEL_CALLS)

    async def test_earlier_rounds_stream_exactly_as_before(self):
        first = [{"delta": {"content": "계산"}}, {"delta": {"content": "합니다"}}] + calc_round(1)
        events = await run_turn(ScriptedLink([first, answer_round("끝")]))
        self.assertEqual(rounds_of(events)[0], [{"choices": [choice]} for choice in first])
        self.assertEqual([entry["markup_removed"] for entry in metrics_of(events)["rounds"]], [False, False])

    async def test_a_link_failure_keeps_only_the_cleaned_part_of_the_partial_answer(self):
        cases = {"text then unclosed markup": (["앞부분\n<tool_call>\n", '{"name"'], "앞부분"),
                 "unclosed markup only": (["<tool_call>", '{"name"'], None)}
        for name, (texts, kept) in cases.items():
            with self.subTest(name):
                space = Workspace()
                _, events = await self.finish(last_round(*texts)[:-1], space, FailingLink)
                self.assertIn("error", [event for event, _ in events])
                self.assertEqual(rounds_of(events)[-1], [])
                history = json.dumps(space.history, ensure_ascii=False)
                self.assertNotIn("<tool_call", history)
                self.assertEqual("앞부분" in history, kept is not None)


class MarkupHelperTests(unittest.TestCase):
    def test_text_without_markup_comes_back_byte_identical(self):
        for text in ("", "plain", "  keep  \n\n\n\nspacing \n", "<tool_calls>list</tool_calls>",
                     "<tool_call_id>7</tool_call_id>", "a < b and c > d", "tool_call without brackets"):
            with self.subTest(text=text):
                self.assertEqual(strip_tool_markup(text), text)

    def test_blocks_unclosed_tags_and_stray_closers_are_removed(self):
        cases = [
            ('a\n<tool_call>{"x": 1}</tool_call>\nb', "a\n\nb"),
            ("<tool_call>{}</tool_call><tool_call>{}</tool_call>", ""),
            ('keep\n<tool_call>\n{"name": "bash"', "keep"),
            ("<TOOL_CALL>x</Tool_Call> after", "after"),
            ("< tool_call >x</ tool_call >y", "y"),
            ("x </tool_call> y", "x  y"),
            ("a\n\n<tool_call>{}</tool_call>\n\n\nb", "a\n\nb"),
            ("<tool_call>{}</tool_call>", ""),
            ("<tool_call>", ""),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(strip_tool_markup(text), expected)

    def test_without_content_drops_only_the_text_and_never_touches_the_chunk(self):
        content_only = {"choices": [{"index": 0, "delta": {"content": "x"}}]}
        with_finish = {"id": "c1", "choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]}
        reasoning = {"choices": [{"delta": {"content": "x", "reasoning_content": "r", "role": "assistant"}}]}
        usage = {"choices": [], "usage": USAGE}
        empty = {"choices": [{"delta": {}}]}
        before = copy.deepcopy([content_only, with_finish, reasoning, usage, empty])
        self.assertIsNone(without_content(content_only))
        self.assertEqual(without_content(with_finish),
                         {"id": "c1", "choices": [{"delta": {}, "finish_reason": "stop"}]})
        self.assertEqual(without_content(reasoning),
                         {"choices": [{"delta": {"reasoning_content": "r", "role": "assistant"}}]})
        self.assertEqual(without_content(usage), usage)
        self.assertIsNone(without_content(empty))
        self.assertEqual([content_only, with_finish, reasoning, usage, empty], before)


if __name__ == "__main__":
    unittest.main()
