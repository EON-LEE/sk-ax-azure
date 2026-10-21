"""The demo-example verifier against the real demo app on loopback (the fp8 profile), with a scripted GPU link.

The fake demo of test_demo_check_examples answers with what a tool would return. Here the real app answers: its routes,
its workspace, its real tools and its real attachment and PDF handling. Only the model is scripted: each test replays
the tool calls of the ideal turn of one example (`ideal()` of that module), and the verifier has to pass the turn on what
the real tools returned. What this adds over the fake demo:

* the verifier's HTTP and event-stream handling against uvicorn (HTTP/1.1, chunked streaming);
* the first tool the server forces on the model (`tool_choice`) for each prompt, which decides the order of a plan;
* the upload and attachment handling of the contract PDF and the code file, with the page-1 pre-read of the PDF;
* the real results of the calculator (exact fractions), analyze_table, chart_table, read_pdf and diff_file;
* the download and the sanitised preview of the files a turn writes.

Web IQ is not configured in the app, so its examples are skipped, as the verifier skips them on any demo without it.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import uvicorn

from tests import test_demo_check_examples as base
from tests.test_demo_check_examples import PROMPTS, catalog, check

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "demo" / "frontend"))
try:
    import agent  # noqa: F401  (the app imports it per request, from sys.modules once it is loaded here)
    from app import Config, create_app
finally:
    sys.path.pop(0)


def have_time_zones():
    try:
        for name in ("Asia/Seoul", "America/New_York", "Europe/London"):
            ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return False
    return True


def forced(name):
    return {"type": "function", "function": {"name": name}}


class ScriptLink:
    """The GPU link of the app (the interface of FixtureLink in test_demo_agent) playing the rounds of one script."""

    def __init__(self, script):
        self.rounds = script["rounds"]
        self.streams, self.requests, self.cancelled = {}, [], []

    def send(self, message):
        if message["type"] == "cancel":
            self.cancelled.append(message["id"])
            return
        number = len(self.requests)
        self.requests.append(message["body"])
        queue = self.streams[message["id"]]
        queue.put_nowait({"type": "head", "status": 200})
        turn = self.rounds[number]
        choices = [{"delta": {"content": turn["text"]}}] if turn["text"] else []
        for index, call in enumerate(turn["calls"]):
            piece = {"index": index, "id": f"call_{number}_{index}", "function": {
                "name": call["name"], "arguments": json.dumps(call["arguments"], ensure_ascii=False)}}
            choices.append({"delta": {"tool_calls": [piece]}})
        choices[-1]["finish_reason"] = "tool_calls" if turn["calls"] else "stop"
        chunks = [{"choices": [choice]} for choice in choices]
        chunks.append({"choices": [], "usage": {"prompt_tokens": 16001 + number, "completion_tokens": 40,
                                                "prompt_tokens_details": {"cached_tokens": 0}}})
        for chunk in chunks:
            queue.put_nowait({"type": "data", "chunk": "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"})
        queue.put_nowait({"type": "end"})


class AppCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 10
        self.scripts, self.link = base.ideal(), None
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        app = create_app(Config(data=Path(self.tmp.name), aml_dir=ROOT / "aml", session_secret="s" * 32,
                                open_demo=True), start_supervisor=False)
        app.state.hub.ready_link = lambda: self.link
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.url = "http://127.0.0.1:%d" % sock.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="on"))
        self.server.capture_signals = contextlib.nullcontext
        self.serving = asyncio.create_task(self.server.serve(sockets=[sock]))
        while not self.server.started:
            await asyncio.sleep(0.02)

    async def asyncTearDown(self):
        try:
            self.server.should_exit = True
            await asyncio.wait_for(self.serving, 10)
        finally:
            self.tmp.cleanup()

    async def run_cli(self, name, *argv):
        """One run of one example against the app: (the whole outcome, the run record)."""
        out, err, sleeps = io.StringIO(), io.StringIO(), []
        code = await asyncio.to_thread(
            check.main, ["--go", "--only", name, "--runs", "fp8=1", *argv], environ={"AXK2_DEMO_URL": self.url},
            stdout=out, stderr=err, sleep=sleeps.append)
        outcome = base.Outcome(code, out.getvalue(), err.getvalue(), sleeps)
        return outcome, outcome.run(name)

    async def verify(self, name, *argv, script=None):
        """run_cli with the model playing the ideal turn of the example (or the given script)."""
        self.link = ScriptLink(script or self.scripts[PROMPTS[name]])
        return await self.run_cli(name, *argv)

    def assert_passes(self, outcome, rec):
        self.assertEqual(outcome.code, 0, outcome.err)
        self.assertEqual((rec["ok"], rec["failed"], rec["flags"], rec["state"]), (True, {}, {}, "completed"),
                         outcome.err)
        self.assertEqual((rec["http_5xx"], rec["stop"], rec["notices"]), (0, None, []))
        self.assertEqual(rec["http"]["close"], 200)
        self.assertEqual((len(self.link.requests), rec["model_calls"], len(rec["rounds"])),
                         (len(self.link.rounds),) * 3)
        self.assertEqual([r["input_tokens"] for r in rec["rounds"]],
                         [16001 + n for n in range(len(self.link.rounds))])
        self.assertEqual(self.link.cancelled, [])

    def tool_results(self, request):
        """What the model was given as tool results in its n-th request (0-based)."""
        return [m["content"] for m in self.link.requests[request]["messages"] if m["role"] == "tool"]

    def results_of(self, request, key):
        found = []
        for text in self.tool_results(request):
            with contextlib.suppress(ValueError):
                value = json.loads(text)
                if isinstance(value, dict) and key in value:
                    found.append(value[key])
        return found

    def tools_of(self, name):
        return [c["name"] for r in self.scripts[PROMPTS[name]]["rounds"] for c in r["calls"]]


class RealToolTests(AppCase):
    async def test_calculator_example_forces_the_calculator_and_gets_exact_results(self):
        outcome, rec = await self.verify("plan-fee")
        self.assert_passes(outcome, rec)
        self.assertEqual(self.link.requests[0]["tool_choice"], forced("calculator"))
        self.assertEqual([t["name"] for t in rec["tools"]], ["calculator", "calculator"])
        self.assertEqual(self.results_of(1, "result"), ["1242000", "414000"])
        self.assertEqual(rec["files"], [])

    async def test_the_no_tool_example_is_left_to_the_model(self):
        outcome, rec = await self.verify("code-baseline")
        self.assert_passes(outcome, rec)
        self.assertEqual(self.link.requests[0]["tool_choice"], "auto")
        self.assertGreater(len(self.link.requests[0]["tools"]), 5)
        self.assertEqual((rec["tools"], rec["first_tool"], rec["model_calls"]), ([], None, 1))

    async def test_sales_report_is_written_downloaded_and_previewed(self):
        outcome, rec = await self.verify("monthly-sales-report")
        self.assert_passes(outcome, rec)
        self.assertEqual(self.link.requests[0]["tool_choice"], forced("write_file"))
        self.assertEqual([t["name"] for t in rec["tools"]], self.tools_of("monthly-sales-report"))
        self.assertEqual({t["state"] for t in rec["tools"]}, {"success"})
        self.assertEqual(sorted(self.results_of(1, "result")), ["5/12 (≈ 0.4167)", "8660"])
        self.assertEqual([(f["name"], f["download"], f.get("preview")) for f in rec["files"]],
                         [("sales.csv", 200, None), ("report.html", 200, True)])

    async def test_csv_chart_uses_the_real_table_tools_on_the_written_file(self):
        outcome, rec = await self.verify("csv-chart")
        self.assert_passes(outcome, rec)
        self.assertEqual(self.link.requests[0]["tool_choice"], forced("write_file"))
        self.assertEqual(self.results_of(2, "result"), ["2720", "1360/3"])
        (chart,) = (a for a in self.results_of(3, "artifact") if a["kind"] == "chart")
        self.assertEqual(chart["svg"].count("<rect"), 6)
        self.assertEqual([(f["name"], f["download"]) for f in rec["files"]], [("traffic.csv", 200)])

    async def test_contract_review_pre_reads_page_one_then_reads_the_rest(self):
        outcome, rec = await self.verify("contract-review")
        self.assert_passes(outcome, rec)
        self.assertEqual(self.link.requests[0]["tool_choice"], "auto")
        self.assertEqual([t["name"] for t in rec["tools"]], ["read_pdf", "read_pdf", "calculator", "write_file"])
        self.assertEqual(rec["first_tool"], "read_pdf")
        (first,) = (json.loads(text) for text in self.tool_results(0))
        self.assertEqual((first["total_pages"], [p["page"] for p in first["pages"]]), (3, [1]))
        self.assertIn("제1조", first["pages"][0]["text"])
        rest = [json.loads(text) for text in self.tool_results(1)][1]
        self.assertEqual([p["page"] for p in rest["pages"]], [2, 3])
        text = " ".join(p["text"] for p in rest["pages"])
        for article in ("제4조", "제5조", "제6조", "제7조"):
            self.assertIn(article, text)
        self.assertEqual(self.results_of(2, "result"), ["1188000"])
        self.assertEqual([(f["name"], f["download"], f.get("preview")) for f in rec["files"]],
                         [("contract_check.md", 200, None)])

    async def test_code_fix_reads_the_upload_first_and_diffs_against_it(self):
        outcome, rec = await self.verify("code-fix-diff")
        self.assert_passes(outcome, rec)
        self.assertEqual(self.link.requests[0]["tool_choice"], forced("read_file"))
        self.assertEqual([t["name"] for t in rec["tools"]], ["read_file", "write_file", "diff_file"])
        (diff,) = self.tool_results(3)[2:]
        self.assertIn("-        total += price * rate", diff)
        self.assertIn("+        total += price * (1 - rate)", diff)
        self.assertIn("+    if not values:", diff)
        self.assertEqual([(f["name"], f["download"]) for f in rec["files"]], [("discount.py", 200)])

    @unittest.skipUnless(have_time_zones(), "this host has no IANA time zone data")
    async def test_time_zone_example_gets_three_real_clocks(self):
        outcome, rec = await self.verify("time-zones")
        self.assert_passes(outcome, rec)
        self.assertEqual([t["name"] for t in rec["tools"]], ["get_current_time"] * 3)
        self.assertEqual(sorted(self.results_of(1, "timezone")), ["America/New_York", "Asia/Seoul", "Europe/London"])


class RealFailureTests(AppCase):
    async def test_a_forced_tool_the_model_never_calls_ends_in_an_error_after_one_repair_retry(self):
        promise = {"text": "계산하겠습니다.", "calls": []}
        with self.assertLogs("agent", "ERROR") as logs:
            outcome, rec = await self.verify("plan-fee", script={"rounds": [promise, promise]})
        self.assertIn("A.X MAF turn failed", logs.output[0])
        self.assertEqual(outcome.code, 1)
        self.assertEqual((rec["state"], rec["failed"]), ("error agent", {"completed": "error agent"}))
        self.assertEqual((rec["notices"], rec["tools"], rec["error"]["code"]), (["no_execution"] * 2, [], "agent"))
        self.assertIn("실제로 실행되지 않았", rec["error"]["message"])
        self.assertEqual([r["tool_choice"] for r in self.link.requests], [forced("calculator")] * 2)
        self.assertIn("emitted no tool call", json.dumps(self.link.requests[1]["messages"][-1]))
        self.assertEqual((rec["http"]["agent"], rec["http_5xx"], rec["http"]["close"]), (200, 0, 200))

    async def test_a_tool_error_of_the_real_calculator_fails_the_run(self):
        script = {"rounds": [{"text": "", "calls": [{"name": "calculator", "arguments": {"expression": "69000*24*"}}]},
                             {"text": "계산하지 못했습니다.", "calls": []}]}
        with self.assertLogs("agent_framework", "WARNING") as logs:
            outcome, rec = await self.verify("plan-fee", script=script)
        self.assertTrue(any("calculator" in line for line in logs.output), logs.output)
        self.assertEqual(outcome.code, 1)
        self.assertEqual(rec["state"], "completed")
        self.assertEqual([(t["name"], t["state"]) for t in rec["tools"]], [("calculator", "error")])
        self.assertTrue(rec["failed"]["tool_errors"].startswith("1: calculator ("), rec["failed"])
        self.assertEqual(len(self.link.requests), 2)

    async def test_a_link_that_is_not_ready_is_a_refused_run_and_marks_the_profile(self):
        self.link = None
        outcome, rec = await self.run_cli("plan-fee")
        self.assertEqual(outcome.code, 1)
        self.assertEqual((rec["state"], rec["http"]["agent"], rec["http_5xx"]), ("http 503 not_ready", 503, 1))
        self.assertEqual(set(rec["failed"]), {"completed", "http_5xx"})
        self.assertEqual(rec["http"]["close"], 200)
        self.assertEqual(outcome.summary["profiles"],
                         {"fp8": {"web_iq": "unknown", "problem": "the profile answered HTTP 503"}})


class LastRoundTests(AppCase):
    """The tool-round limit of the real agent loop, as the verifier sees it: a model that never stops calling tools."""

    @staticmethod
    def script_of(last):
        keep = {"text": "", "calls": [{"name": "calculator", "arguments": {"expression": "1+1"}}]}
        return {"rounds": [keep] * agent.TOOL_ROUNDS + [{"text": last, "calls": []}]}

    def assert_closing_round(self):
        requests = self.link.requests
        self.assertEqual(len(requests), agent.MODEL_CALLS)
        self.assertTrue(all("tools" in request for request in requests[:-1]))
        self.assertNotIn("tools", requests[-1])
        self.assertEqual(requests[-1]["messages"][-1], {"role": "system", "content": agent.LAST_ROUND_NOTE})
        self.assertFalse(any(m.get("content") == agent.LAST_ROUND_NOTE for m in requests[-2]["messages"]))

    async def test_markup_only_in_the_closing_round_becomes_the_notice_and_fails_the_answer_check(self):
        marker = '<tool_call>{"name": "calculator", "arguments": {"expression": "2+2"}}</tool_call>'
        outcome, rec = await self.verify("plan-fee", script=self.script_of(marker))
        self.assert_closing_round()
        self.assertTrue(agent.TOOL_LIMIT_NOTICE.startswith(check.LIMIT_NOTICE))
        self.assertEqual((outcome.code, rec["state"], rec["model_calls"]), (1, "completed", agent.MODEL_CALLS))
        self.assertEqual(rec["failed"]["answer"], "the answer is the server's tool-limit notice: the model wrote none")
        self.assertEqual(rec["flags"]["markup removed"],
                         "the server cut tool-call markup from the answer of round %d" % agent.MODEL_CALLS)
        self.assertEqual(rec["flags"]["calls"], "%d model calls, 2 planned" % agent.MODEL_CALLS)
        self.assertNotIn("markup", rec["flags"])
        self.assertEqual([r["markup_removed"] for r in rec["rounds"]], [False] * agent.TOOL_ROUNDS + [True])

    async def test_a_written_answer_in_the_closing_round_passes_the_answer_check(self):
        answer = "할인 후 총액은 1,242,000원이고 414,000원 적습니다. 남은 단계는 실행하지 못했습니다."
        outcome, rec = await self.verify("plan-fee", script=self.script_of(answer))
        self.assert_closing_round()
        self.assertEqual((rec["state"], rec["model_calls"]), ("completed", agent.MODEL_CALLS))
        self.assertNotIn("answer", rec["failed"])
        self.assertNotIn("markup removed", rec["flags"])
        self.assertEqual([r["markup_removed"] for r in rec["rounds"]], [False] * agent.MODEL_CALLS)


class ExternalExampleTests(AppCase):
    async def test_web_iq_examples_are_skipped_when_the_app_has_no_web_iq(self):
        external = [e.id for e in catalog.EXAMPLES if e.external]
        self.assertGreaterEqual(len(external), 4)
        outcome, rec = await self.verify(external[0])
        self.assertEqual(outcome.code, 1)
        self.assertEqual(rec["skipped"], "Web IQ is unavailable on this profile")
        self.assertEqual(outcome.summary["profiles"], {"fp8": {"web_iq": "unavailable", "problem": None}})
        self.assertEqual(outcome.summary["totals"],
                         {"passed": 0, "failed": 0, "skipped": 1, "http_5xx": 0, "web_iq_calls": 0})
        self.assertEqual(self.link.requests, [])
        self.assertIn("skipped", outcome.err)


if __name__ == "__main__":
    unittest.main()
