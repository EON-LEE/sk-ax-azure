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
from agent import requested_tool, retain_history, turn_policy


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


async def run(space, link, text, tools=True, **kwargs):
    space.busy = True
    response = agent_response(FixtureHub(link), Gate(1, 4),
                              {"model": "axk2", "messages": [{"role": "user", "content": text}]},
                              space, tools, **kwargs)
    return b"".join([chunk async for chunk in response.body_iterator]).decode()


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
        for fragment in ('turnEpoch !== state.epoch', 'releaseWorkspace("close")', 'releaseWorkspace("cancel")',
                         'cancelled: "중단됨"', 'view.status.remove()', 'state.busy = false'):
            self.assertIn(fragment, js)
        self.assertNotIn("localStorage", js)
