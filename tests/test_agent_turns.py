"""Same-workspace native MAF history, trusted per-turn policy and real lifecycle boundaries."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from tests.test_demo_agent import Config, FixtureHub, FixtureLink, Gate, ROOT, Workspace, agent_response, create_app
from tests.pdf_samples import text_pdf
from tests.test_web_iq import provider_fixture
from agent import parse_tool_arguments, requested_tool, retain_history, turn_policy


class TextLink(FixtureLink):
    def __init__(self, text, hold_after=False):
        super().__init__()
        self.text, self.hold_after = text, hold_after

    def send(self, message):
        if message["type"] == "cancel":
            self.cancelled.append(message["id"])
            return
        self.requests.append(message["body"])
        queue = self.streams[message["id"]]
        queue.put_nowait({"type": "head", "status": 200})
        queue.put_nowait({"type": "data", "chunk": "data: " + json.dumps({"choices": [
            {"delta": {"reasoning_content": "actual fixture reasoning", "content": self.text},
             "finish_reason": None if self.hold_after else "stop"}]}) + "\n\n"})
        if not self.hold_after:
            queue.put_nowait({"type": "end"})


class RawArgumentsLink(FixtureLink):
    """Round n calls the tool with exactly the n-th argument text a model produced, valid JSON or not."""

    def __init__(self, name, *texts):
        super().__init__(name)
        self.texts = texts

    def send(self, message):
        if message["type"] == "cancel":
            self.cancelled.append(message["id"])
            return
        self.requests.append(message["body"])
        queue = self.streams[message["id"]]
        queue.put_nowait({"type": "head", "status": 200})
        if len(self.requests) <= len(self.texts):
            text = self.texts[len(self.requests) - 1]
            half = len(text) // 2
            choices = [
                {"delta": {"tool_calls": [{"index": 0, "id": f"call_{len(self.requests)}", "function": {
                    "name": self.name, "arguments": text[:half]}}]}},
                {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": text[half:]}}]},
                 "finish_reason": "tool_calls"},
            ]
        else:
            choices = [{"delta": {"content": "Final A.X answer"}, "finish_reason": "stop"}]
        for choice in choices:
            queue.put_nowait({"type": "data", "chunk": "data: " + json.dumps({"choices": [choice]}) + "\n\n"})
        queue.put_nowait({"type": "end"})


async def run(space, link, text, tools=True, **kwargs):
    space.busy = True
    response = agent_response(FixtureHub(link), Gate(1, 4),
                              {"model": "axk2", "messages": [{"role": "user", "content": text}]},
                              space, tools, **kwargs)
    return b"".join([chunk async for chunk in response.body_iterator]).decode()


def sse_events(output):
    events = []
    for block in output.split("\n\n"):
        name = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name:
            events.append((name, data))
    return events


class TurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_calculator_then_followup_reuses_actual_results_and_reasoning(self):
        space = Workspace()
        first = FixtureLink("calculator", {"expression": "2+3"})
        await run(space, first, "계산기로 2+3")
        next_link = FixtureLink("calculator", {"expression": "5+1"})
        output = await run(space, next_link, "그 결과에 1을 더해 줘")
        initial = next_link.requests[0]["messages"]
        old_results = [m for m in initial if m["role"] == "tool"]
        self.assertTrue(any('"result": "5"' in m["content"] for m in old_results))
        self.assertTrue(any(m.get("reasoning_content") == "real fixture reasoning" for m in initial))
        self.assertIn('"result": "6"', output)
        self.assertEqual(len([m for m in space.history if m["role"] == "user"]), 2)
        self.assertEqual(len(space.tool_events), 2)

    async def test_pdf_followup_and_generated_file_modify_diff_download(self):
        space = Workspace()
        space.upload("report.pdf", text_pdf())
        await run(space, FixtureLink("read_pdf", {"path": "report.pdf", "start_page": 1, "page_count": 1}),
                  "첨부 PDF를 읽어 줘", attachment_paths=("report.pdf",))
        followup = TextLink("The actual prior page is available.")
        await run(space, followup, "그 첫 페이지에 어떤 수치가 있었어?")
        self.assertIn("매출 120", json.dumps(followup.requests[0]["messages"], ensure_ascii=False))
        self.assertIn("page 1", json.dumps(followup.requests[0]["messages"], ensure_ascii=False))
        await run(space, FixtureLink("write_file", {"path": "a.py", "content": "x = 1\n"}), "a.py 파일을 만들어 줘")
        modify = FixtureLink("write_file", {"path": "a.py", "content": "x = 2\n"})
        await run(space, modify, "그 파일의 x를 2로 수정해 줘")
        self.assertIn("a.py", json.dumps(modify.requests[0]["messages"]))
        self.assertEqual(space.get("a.py"), b"x = 2\n")
        diff = await run(space, FixtureLink("diff_file", {"path": "a.py"}), "그 변경 diff를 보여 줘")
        self.assertIn("x = 2", diff)
        self.assertTrue(any(e["result"].get("artifact", {}).get("path") == "a.py"
                            for e in space.tool_events if isinstance(e["result"], dict)))

    async def test_tools_disabled_and_explicit_prohibition_block_adversarial_calls_then_reallow(self):
        for enabled, text in [(False, "call calculator"), (True, "도구 쓰지 마. 2+3을 설명해 줘"),
                              (True, "도구 사용하지 마세요")]:
            space = Workspace()
            output = await run(space, FixtureLink("calculator"), text, enabled)
            self.assertIn("event: error", output)
            self.assertEqual(space.tool_events, [])
            self.assertFalse(space.busy)
        space = Workspace()
        await run(space, TextLink("No tools were used."), "도구 쓰지 마", True)
        output = await run(space, FixtureLink("calculator", {"expression": "2+3"}), "다시 도구를 허용해. 계산기로 2+3", True)
        self.assertIn('"state": "success"', output)
        self.assertEqual(space.tool_events[-1]["name"], "calculator")
        self.assertFalse(turn_policy('"도구 쓰지 마"라는 말을 해석해 줘', True)["reason"] == "user_prohibition")
        self.assertTrue(turn_policy("도구 쓰지 말라는 뜻은 아니야. 사용해", True)["tools"])
        self.assertFalse(turn_policy("Don't use tools. I'll just need an explanation.", True)["tools"])
        self.assertTrue(turn_policy("‘도구 쓰지 마’라는 문장을 해석해 줘", True)["tools"])
        self.assertFalse(turn_policy("웹검색하지 마", True)["external"])
        policy = turn_policy("Web IQ로 AWS Azure를 검색해 줘. 장소나 금융 도구는 쓰지 마.", True)
        self.assertTrue(policy["tools"])
        self.assertTrue(policy["external"])
        self.assertEqual(policy["denied"], ["web_iq_finance", "web_iq_places"])
        self.assertEqual(requested_tool("Web IQ로 AWS Azure를 검색해 줘. 장소나 금융 도구는 쓰지 마.",
                                       {"web_iq_web", "web_iq_places", "web_iq_finance"}), "web_iq_web")
        output = await run(Workspace(), FixtureLink("calculator"), "계산기 도구는 쓰지 마. 설명해 줘", True)
        self.assertIn("event: error", output)
        self.assertEqual(turn_policy("계산기 도구는 다시 허용해", True)["denied"], [])
        self.assertEqual(turn_policy("Don't use web_iq_finance. Search the web.", True)["denied"], ["web_iq_finance"])

    async def test_search_prohibition_correct_vertical_and_actual_external_execution(self):
        self.assertEqual(requested_tool("Web IQ로 최신 AWS Azure 한국 리전과 비용을 검색해 줘",
                                       {"web_iq_web", "web_iq_places", "web_iq_finance"}), "web_iq_web")
        self.assertIsNone(requested_tool("파이썬 이진 탐색을 설명해 줘", {"write_file", "calculator"}))
        async with provider_fixture() as (provider, calls):
            await provider.prepare()
            # This custom provider advertises q rather than query, as discovered.
            output = await run(Workspace(), FixtureLink("web_iq_search", {"q": "public AWS Azure"}),
                               "검색 없이 답해. AWS Azure를 비교해 줘", True, web_iq=provider)
            self.assertIn("event: error", output)
            self.assertEqual(calls, [])
            await run(Workspace(), FixtureLink("web_iq_search", {"q": "public AWS Azure"}),
                      "Web IQ로 public AWS Azure 검색해 줘", True, web_iq=provider)
            self.assertEqual(calls, [{"q": "public AWS Azure", "count": 2}])

    async def test_premature_wait_promise_is_bounded_and_never_background_progress(self):
        link = TextLink("잠시만 기다려 주세요. 지금 검색하겠습니다.")
        output = await run(Workspace(), link, "비교해 줘", False)
        self.assertIn("event: notice", output)
        self.assertIn('"tool_invocations": 0', output)
        self.assertEqual(len(link.requests), 1)
        link = TextLink("Please wait, I will search.")
        space = Workspace()
        output = await run(space, link, "Web IQ로 검색해 줘", True)
        self.assertEqual(len(link.requests), 2)
        self.assertIn("event: notice", output)
        self.assertEqual(space.tool_events, [])
        self.assertFalse(space.busy)

    async def test_cancel_reasoning_queue_and_after_completed_tool_retain_truthful_state(self):
        for link in (FixtureLink(hold=True), TextLink("", hold_after=True)):
            space, gate = Workspace(), Gate(1, 2)
            response = agent_response(FixtureHub(link), gate, {"messages": [{"role": "user", "content": "hold"}]},
                                      space, True)
            async def consume():
                async for _ in response.body_iterator:
                    pass
            task = asyncio.create_task(consume())
            for _ in range(100):
                if link.requests:
                    break
                await asyncio.sleep(.005)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(gate.active, 0)
            self.assertEqual(len(link.cancelled), 1)
            self.assertIn("cancelled", json.dumps(space.history))
            self.assertFalse(space.busy)

        class AfterResult(FixtureLink):
            def send(self, message):
                if len(self.requests) == 1:
                    self.hold = True
                super().send(message)
        space, link = Workspace(), AfterResult("write_file", {"path": "kept.py", "content": "x=1"})
        response = agent_response(FixtureHub(link), Gate(1, 2),
                                  {"messages": [{"role": "user", "content": "save"}]}, space, True)
        async def consume_after():
            async for _ in response.body_iterator:
                pass
        task = asyncio.create_task(consume_after())
        for _ in range(100):
            if len(link.requests) == 2:
                break
            await asyncio.sleep(.005)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(space.get("kept.py"), b"x=1")
        self.assertEqual(space.tool_events[0]["state"], "success")
        self.assertIn("kept.py", json.dumps(space.history))

    async def test_queued_and_pdf_tool_cancellation_never_marks_read_completed(self):
        gate, link, space = Gate(1, 2), FixtureLink(), Workspace()
        occupied = gate.join()
        response = agent_response(FixtureHub(link), gate, {"messages": [{"role": "user", "content": "queued"}]}, space, True)
        async def consume(stream):
            async for _ in stream.body_iterator:
                pass
        task = asyncio.create_task(consume(response))
        for _ in range(100):
            if gate.waiting:
                break
            await asyncio.sleep(.005)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(gate.waiting)
        self.assertEqual(link.requests, [])
        gate.leave(occupied)
        self.assertEqual(gate.active, 0)

        from agent_framework import tool
        entered = asyncio.Event()
        @tool
        async def read_pdf(path: str, start_page: int = 1, page_count: int = 1):
            entered.set()
            await asyncio.Event().wait()
        space = Workspace()
        space.upload("a.pdf", text_pdf())
        with patch.object(space, "tools", return_value=[read_pdf]):
            response = agent_response(FixtureHub(FixtureLink()), Gate(1, 2),
                                      {"messages": [{"role": "user", "content": "read PDF"}]}, space, True,
                                      attachment_paths=["a.pdf"])
            task = asyncio.create_task(consume(response))
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertNotIn("a.pdf", space.pdf_checked)
        self.assertEqual(space.pdf_outcomes["a.pdf"]["state"], "cancelled")
        self.assertFalse(space.busy)

    async def test_pdf_preflight_works_when_model_only_returns_prose_and_only_new_files(self):
        space = Workspace()
        for path in ("a.pdf", "b.pdf", "c.pdf"):
            space.upload(path, text_pdf())
        link = TextLink("Actual PDF text is now available.")
        output = await run(space, link, "PDF를 요약해 줘", attachment_paths=["a.pdf", "b.pdf", "c.pdf"])
        self.assertEqual([e["name"] for e in space.tool_events], ["read_pdf", "read_pdf", "read_pdf"])
        self.assertEqual(output.count('"state": "success"'), 3)
        self.assertIn("매출 120", output)
        self.assertIn("page 1", json.dumps(link.requests[0]["messages"]))
        before = len(space.tool_events)
        space.upload("new.pdf", text_pdf())
        await run(space, TextLink("new PDF"), "새 PDF도 읽어 줘", attachment_paths=["a.pdf", "new.pdf"])
        self.assertEqual(len(space.tool_events) - before, 1)
        self.assertEqual(space.tool_events[-1]["arguments"]["path"], "new.pdf")
        before = len(space.tool_events)
        space.upload("blocked.pdf", text_pdf())
        await run(space, TextLink("No file was read."), "도구 쓰지 마", attachment_paths=["blocked.pdf"])
        self.assertEqual(len(space.tool_events), before)
        self.assertNotIn("blocked.pdf", space.pdf_checked)

    async def test_close_invalidates_artifacts_history_and_token(self):
        with tempfile.TemporaryDirectory() as folder:
            app = create_app(Config(data=Path(folder), aml_dir=ROOT / "aml", session_secret="test", open_demo=True),
                             start_supervisor=False)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                token = (await client.post("/api/workspace")).json()["token"]
                headers = {"X-AX-Workspace": token}
                await client.post("/api/workspace/upload?name=a.txt", headers=headers, content=b"private")
                response = await client.post("/api/workspace/close", headers=headers)
                self.assertEqual(response.status_code, 200)
                denied = await client.get("/api/workspace/download?path=a.txt", headers=headers)
                self.assertEqual(denied.status_code, 410)
                other = (await client.post("/api/workspace")).json()["token"]
                denied = await client.get("/api/workspace/download?path=a.txt", headers={"X-AX-Workspace": other})
                self.assertEqual(denied.status_code, 404)

    def test_bounded_memory_and_client_epoch_terminal_contract(self):
        space = Workspace()
        history = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "x" * (2 * 1024 * 1024)},
                   {"role": "user", "content": "new"}, {"role": "assistant", "content": "retained"}]
        self.assertTrue(retain_history(space, history))
        self.assertEqual(space.history, history[-2:])
        js = (ROOT / "demo/frontend/static/chat.js").read_text()
        for fragment in ('turnEpoch !== state.epoch', 'releaseWorkspace("close", state.workspace, state)', 'releaseWorkspace("cancel")',
                         'cancelled: "중단됨"', 'view.status.remove()', 'state.busy = false'):
            self.assertIn(fragment, js)
        self.assertNotIn("localStorage", js)


class ToolArgumentTests(unittest.IsolatedAsyncioTestCase):
    """What a model may put in a tool call's arguments never ends the turn by itself."""

    def test_arguments_are_classified_like_model_api_plus_whitespace(self):
        for raw in (None, "", " ", "\n\t "):
            self.assertEqual(parse_tool_arguments(raw), ({}, None))
        self.assertEqual(parse_tool_arguments("{}"), ({}, None))
        self.assertEqual(parse_tool_arguments(' {"a": [1, 2]}\n'), ({"a": [1, 2]}, None))
        for raw in ("{broken", "not json at all", '{"a": 1} trailing', "[" * 100_000):
            self.assertEqual(parse_tool_arguments(raw), (None, "not valid JSON"))
        for raw in ("[1, 2]", "42", '"text"', "null", "true"):
            self.assertEqual(parse_tool_arguments(raw), (None, "not a JSON object"))

    async def test_blank_arguments_run_a_tool_that_needs_none(self):
        for raw in ("", " ", "\n\t"):
            space, link = Workspace(), RawArgumentsLink("get_current_time", raw)
            output = await run(space, link, "지금 몇 시야?")
            events = sse_events(output)
            self.assertNotIn("error", [name for name, _ in events])
            self.assertEqual([e["name"] for e in space.tool_events], ["get_current_time"])
            self.assertEqual(space.tool_events[0]["state"], "success")
            self.assertEqual(space.tool_events[0]["arguments"], {})
            replayed = next(m for m in link.requests[1]["messages"] if m.get("tool_calls"))
            self.assertEqual(replayed["tool_calls"][0]["function"]["arguments"], "{}")
            self.assertIn("Final A.X answer", output)

    async def test_blank_arguments_for_a_tool_with_required_ones_are_a_tool_error(self):
        space, link = Workspace(), RawArgumentsLink("calculator", "")
        output = await run(space, link, "2+3을 알려 줘")
        events = sse_events(output)
        self.assertNotIn("error", [name for name, _ in events])
        self.assertEqual(space.tool_events[0]["state"], "error")
        self.assertEqual(len(link.requests), 2)
        self.assertIn("Final A.X answer", output)

    async def test_arguments_that_are_not_a_json_object_come_back_as_a_tool_error(self):
        for raw in ("{broken", "not json at all", "[1, 2]", "42", '"text"', "null", "[" * 100_000):
            space, link = Workspace(), RawArgumentsLink("get_current_time", raw)
            with self.assertLogs("agent", level="WARNING"):
                output = await run(space, link, "지금 몇 시야?")
            events = sse_events(output)
            self.assertNotIn("error", [name for name, _ in events], raw[:20])
            actions = [data for name, data in events if name == "action"]
            self.assertEqual([a["state"] for a in actions], ["pending", "error"])
            self.assertEqual(actions[-1]["arguments"], {})
            self.assertIn("The tool was not run", actions[-1]["result"]["error"])
            self.assertEqual([e["state"] for e in space.tool_events], ["error"])
            self.assertEqual(len(link.requests), 2)
            replay = link.requests[1]["messages"]
            call = next(m for m in replay if m.get("tool_calls"))["tool_calls"][0]
            self.assertEqual(call["function"]["arguments"], "{}")
            result = next(m for m in replay if m["role"] == "tool")
            self.assertEqual(result["tool_call_id"], call["id"])
            self.assertIn("The tool was not run", result["content"])
            self.assertIn("JSON object", result["content"])
            self.assertIn("Final A.X answer", output)

    async def test_the_model_can_call_again_and_the_requested_tool_stays_required_until_it_ran(self):
        space = Workspace()
        link = RawArgumentsLink("calculator", "{expression: 2+3", '{"expression": "2+3"}')
        output = await run(space, link, "계산기로 2+3")
        self.assertNotIn("error", [name for name, _ in sse_events(output)])
        self.assertEqual([e["state"] for e in space.tool_events], ["error", "success"])
        self.assertEqual(len(link.requests), 3)
        self.assertEqual(link.requests[0]["tool_choice"], link.requests[1]["tool_choice"])
        self.assertNotEqual(link.requests[1]["tool_choice"], link.requests[2].get("tool_choice"))
        self.assertIn('"result": "5"', output)
        self.assertIn("Final A.X answer", output)

    async def test_a_valid_object_with_padding_runs_as_before(self):
        space, link = Workspace(), RawArgumentsLink("calculator", ' {"expression":  "2+3"}\n')
        output = await run(space, link, "2+3을 계산해 줘")
        self.assertEqual(space.tool_events[0]["state"], "success")
        self.assertEqual(space.tool_events[0]["arguments"], {"expression": "2+3"})
        kept = next(m for m in space.history if m.get("tool_calls"))
        self.assertEqual(json.loads(kept["tool_calls"][0]["function"]["arguments"]), {"expression": "2+3"})
        replayed = next(m for m in link.requests[1]["messages"] if m.get("tool_calls"))
        self.assertEqual(json.loads(replayed["tool_calls"][0]["function"]["arguments"]), {"expression": "2+3"})
        self.assertNotIn("The tool was not run", output)
        self.assertIn("Final A.X answer", output)

    async def test_unusable_arguments_do_not_bypass_the_turn_policy(self):
        space = Workspace()
        with self.assertLogs("agent", level="WARNING"):
            output = await run(space, RawArgumentsLink("calculator", "{broken"), "계산기 도구는 쓰지 마. 설명해 줘")
        self.assertIn("event: error", output)
        self.assertEqual(space.tool_events, [])
        self.assertFalse(space.busy)
        space = Workspace()
        with self.assertLogs("agent", level="WARNING"):
            output = await run(space, RawArgumentsLink("not_a_tool", "{broken"), "아무 도구나 써 줘")
        self.assertIn("event: error", output)
        self.assertEqual(space.tool_events, [])

    async def test_logged_warning_has_sizes_only_and_history_stays_replayable(self):
        space, link = Workspace(), RawArgumentsLink("get_current_time", '{"private": "SENTINEL-TEXT"')
        with self.assertLogs("agent", level="WARNING") as logged:
            await run(space, link, "지금 몇 시야?")
        line = "\n".join(logged.output)
        self.assertIn("get_current_time", line)
        self.assertIn("not valid JSON", line)
        self.assertIn(str(len('{"private": "SENTINEL-TEXT"')), line)
        self.assertNotIn("SENTINEL-TEXT", line)
        for message in space.history:
            for call in message.get("tool_calls") or []:
                self.assertIsInstance(json.loads(call["function"]["arguments"]), dict)
        followup = TextLink("The earlier call is visible.")
        output = await run(space, followup, "방금 어떻게 됐어?")
        self.assertNotIn("event: error", output)
        carried = followup.requests[0]["messages"]
        self.assertIn("The tool was not run", json.dumps(carried, ensure_ascii=False))
        self.assertEqual([m for m in carried if m.get("tool_calls")][0]["tool_calls"][0]["function"]["arguments"], "{}")
