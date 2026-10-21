"""What the demo-example verifier reports when a run goes wrong, against the fake demo of test_demo_check_examples.

Every test changes one thing in an otherwise good turn (or in the fake's routes) and checks which part of the record
reacts: `failed` holds what breaks a pass criterion, `flags` holds what only looks wrong, and the stop rules decide when
the rest of the plan is skipped.
"""
from __future__ import annotations

import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import test_demo_check_examples as base
from tests.test_demo_check_examples import BY_ID, DOCS, NEWS, calc, call, catalog, check, final, found, item, page, step

AGENT, WORKSPACE = "/api/agent", "/api/workspace"
# what the server sends when the last round holds nothing but tool-call markup (agent.TOOL_LIMIT_NOTICE)
LIMIT_NOTICE = "도구 사용 한도에 도달해 남은 단계를 실행하지 못했습니다. 지금까지 확인한 결과는 위 도구 카드에 있습니다."


class FailureCase(base.DemoCase):
    def failed(self, name, *argv):
        return self.one(name, *argv)[1]["failed"]

    def answer_with(self, name, text):
        self.script(name)["rounds"][-1]["text"] = text

    def reply(self, route, status, payload, profile="fp8", method="POST"):
        self.demo.overrides[(profile, method, route)] = lambda hit: (status, payload)


class TurnFailureTests(FailureCase):
    def test_a_tool_that_reports_an_error_fails_the_run(self):
        self.script("ai-chip-news")["rounds"][2] = step(
            call("preview_html", {"path": "briefing.html"}, state="error",
                 result={"status": "error", "error": "boom"}))
        outcome, rec = self.one("ai-chip-news")
        self.assertEqual(outcome.code, 1)
        self.assertEqual(rec["failed"], {"tool_errors": "1: preview_html (boom)",
                                         "tools": "not run (in order): preview_html",
                                         "artifacts": "no preview artifact"})
        self.assertEqual(rec["tools"][-1], {"name": "preview_html", "state": "error", "ms": 120, "error": "boom"})
        self.assertEqual(outcome.summary["totals"],
                         {"passed": 0, "failed": 1, "skipped": 0, "http_5xx": 0, "web_iq_calls": 1})

    def test_a_step_that_never_ran_is_named(self):
        del self.script("ai-chip-news")["rounds"][2]
        self.assertEqual(self.failed("ai-chip-news"),
                         {"tools": "not run (in order): preview_html", "artifacts": "no preview artifact"})

    def test_a_wrong_first_tool_is_named(self):
        rounds = self.script("ai-chip-news")["rounds"]
        searched = rounds[0]["calls"][0]
        rounds[0] = step(call("web_iq_web", searched["arguments"], result=searched["result"]))
        self.assertEqual(self.failed("ai-chip-news"),
                         {"first_tool": "expected web_iq_news, got web_iq_web",
                          "tools": "not run (in order): web_iq_news"})

    def test_steps_in_the_wrong_order_are_named(self):
        rounds = self.script("contract-review")["rounds"]
        rounds[1], rounds[2] = rounds[2], rounds[1]
        self.assertEqual(self.failed("contract-review"), {"tools": "not run (in order): write_file"})

    def test_a_tool_called_with_the_wrong_arguments_is_named(self):
        zone = {"timezone": "Asia/Seoul", "time": "fixture"}
        self.script("time-zones")["rounds"][0] = step(
            *(call("get_current_time", {"timezone": "Asia/Seoul"}, result=zone) for _ in range(3)))
        self.assertEqual(self.failed("time-zones"),
                         {"tools": "get_current_time was not called with /America/New_York/; "
                                   "get_current_time was not called with /Europe/London/"})

    def test_a_turn_without_answer_text_fails(self):
        rounds = self.script("plan-fee")["rounds"]
        rounds[0]["text"], rounds[-1]["text"] = "계산해 보겠습니다.", ""
        self.assertEqual(self.failed("plan-fee"), {"answer": "the turn ended without answer text"})

    def test_an_answer_that_holds_tool_call_markup_fails(self):
        for text in ('<tool_call>{"name": "calculator", "arguments": {"expression": "1+1"}}</tool_call>',
                     '할인 후 총액은 1,242,000원입니다.\n<tool_call>\n{"name": "calc', "끝났습니다.</tool_call>",
                     "< TOOL_CALL >{}", "<tool_call"):
            self.answer_with("plan-fee", text)
            with self.subTest(text=text):
                self.assertEqual(self.failed("plan-fee"), {"answer": "the answer holds tool-call markup"})

    def test_words_that_only_resemble_the_tag_are_no_markup(self):
        for text in ("할인 후 총액은 1,242,000원이고 414,000원 적습니다. tool_call은 호출 형식의 이름입니다.",
                     "1,242,000원, 414,000원. <tool_calling> 같은 글자는 표식이 아닙니다."):
            self.answer_with("plan-fee", text)
            with self.subTest(text=text):
                self.assertEqual(self.failed("plan-fee"), {})

    def test_the_tool_limit_notice_as_the_answer_fails(self):
        self.answer_with("plan-fee", LIMIT_NOTICE)
        self.assertEqual(self.failed("plan-fee"),
                         {"answer": "the answer is the server's tool-limit notice: the model wrote none"})
        self.answer_with("plan-fee", "할인 후 총액은 1,242,000원이고 414,000원 적습니다. 참고: " + LIMIT_NOTICE)
        self.assertEqual(self.failed("plan-fee"), {})

    def test_a_turn_that_ends_in_another_state_fails(self):
        self.script("plan-fee")["end_state"] = "cancelled"
        _, rec = self.one("plan-fee")
        self.assertEqual((rec["state"], rec["failed"]), ("ended cancelled", {"completed": "ended cancelled"}))

    def test_an_error_event_ends_the_run_as_an_error(self):
        self.script("plan-fee")["error"] = "model_failed"
        outcome, rec = self.one("plan-fee")
        self.assertEqual(outcome.code, 1)
        self.assertEqual(rec["state"], "error model_failed")
        self.assertEqual(rec["failed"], {"completed": "error model_failed"})
        self.assertEqual(rec["error"], {"code": "model_failed", "message": "the turn failed"})
        self.assertEqual(self.demo.closed, self.demo.created)

    def test_a_turn_that_never_ends_is_cancelled_at_the_deadline(self):
        self.script("plan-fee")["hang"] = True
        _, rec = self.one("plan-fee", "--timeout", "1")
        self.assertEqual((rec["state"], rec["failed"]), ("deadline", {"completed": "deadline"}))
        self.assertLess(rec["wall_seconds"], 3)
        self.assertEqual(self.demo.cancelled, ["tok001"])
        self.assertEqual(self.demo.closed, ["tok001"])

    def test_event_lines_that_make_no_sense_are_ignored(self):
        self.script("plan-fee")["raw"] = ["data: {not json\n\n", "event: action\ndata: [1, 2]\n\n",
                                          "event: metrics\ndata: null\n\n", ": a comment\n\n", "data:\n\n",
                                          "event: unknown\ndata: {}\n\n"]
        outcome, rec = self.one("plan-fee")
        self.assertEqual((outcome.code, rec["ok"], rec["failed"], rec["flags"]), (0, True, {}, {}))


class TimeLimitTests(FailureCase):
    def test_a_turn_over_the_limit_fails_and_one_under_it_passes(self):
        script = self.script("plan-fee")
        for seconds, failed in ((46.5, {"time": "46.5 s is over 45 s"}), (44.9, {}), (45.0, {})):
            script["seconds"] = seconds
            with self.subTest(seconds=seconds):
                self.assertEqual(self.failed("plan-fee"), failed)

    def test_the_limit_can_be_changed(self):
        self.assertEqual(self.failed("plan-fee", "--max-seconds", "5"), {"time": "7.35 s is over 5 s"})
        self.script("plan-fee")["seconds"] = 80
        self.assertEqual(self.failed("plan-fee", "--max-seconds", "90"), {})


class SourceFailureTests(FailureCase):
    def test_a_link_no_tool_returned_is_named(self):
        self.script("ai-chip-news")["rounds"][-1]["text"] += " https://invented.example.com/x"
        self.assertEqual(self.failed("ai-chip-news"),
                         {"sources": "links that no tool returned: invented.example.com/x"})

    def test_a_link_invented_in_a_written_file_is_named(self):
        plan = self.script("pangyo-lunch-plan")["rounds"][2]["calls"][0]["arguments"]
        plan["content"] += "- https://invented.example.com/y\n"
        self.assertEqual(self.failed("pangyo-lunch-plan"),
                         {"sources": "links that no tool returned: invented.example.com/y"})

    def test_too_few_sources_are_counted(self):
        self.answer_with("ai-chip-news", "기사 2건의 출처입니다. %sa1 %sa2" % (NEWS, NEWS))
        self.assertEqual(self.failed("ai-chip-news"), {"sources": "2 of 3 source links come from tool results"})

    def test_an_answer_without_links_has_no_sources(self):
        self.answer_with("ai-chip-news", "출처는 생략합니다.")
        self.assertEqual(self.failed("ai-chip-news"), {"sources": "0 of 3 source links come from tool results"})

    def test_a_source_on_the_wrong_host_is_named(self):
        elsewhere = DOCS + "docs/byom-fixture"
        rounds = self.script("foundry-byom-steps")["rounds"]
        rounds[0] = step(call("web_iq_web", {"query": "Foundry BYOM"},
                              result=found("web", item("web", "BYOM 연결", elsewhere))))
        rounds[1] = step(call("web_iq_browse", {"url": elsewhere}, result=page(elsewhere)))
        rounds[2] = final("1. 모델을 등록합니다.\n\n출처: " + elsewhere)
        self.assertEqual(self.failed("foundry-byom-steps"),
                         {"sources": "no source on " + BY_ID["foundry-byom-steps"].hosts[0]})

    def test_a_shortened_link_counts_but_a_bare_host_does_not(self):
        deep = DOCS + "axk2/intro"
        rounds = self.script("axk2-public-research")["rounds"]
        rounds[0] = step(call("web_iq_web", {"query": "A.X K2"}, result=found("web", item("web", "A.X K2", deep))))
        rounds[1] = step(call("web_iq_browse", {"url": deep}, result=page(deep)))
        for cited, failed in ((DOCS + "axk2", {}), (DOCS + "axk2/intro", {}), (DOCS + "axk", {"sources": (
                "0 of 1 source links come from tool results; links that no tool returned: docs.example.com/axk")}),
                              (DOCS, {"sources": "0 of 1 source links come from tool results; "
                                                 "links that no tool returned: docs.example.com"})):
            rounds[2] = final("요약입니다. 출처: " + cited)
            with self.subTest(cited=cited):
                self.assertEqual(self.failed("axk2-public-research"), failed)

    def test_the_page_a_tool_was_asked_to_read_counts_as_returned(self):
        read = DOCS + "axk2/read"
        rounds = self.script("axk2-public-research")["rounds"]
        rounds[0] = step(call("web_iq_web", {"query": "A.X K2"}, result=found("web", item("web", "A.X K2", DOCS))))
        rounds[1] = step(call("web_iq_browse", {"url": read}, result=page(read)))
        rounds[2] = final("요약입니다. 출처: " + read)
        self.assertEqual(self.failed("axk2-public-research"), {})


class LinkFormatTests(FailureCase):
    def cited(self, text):
        self.answer_with("ai-chip-news", text)
        _, rec = self.one("ai-chip-news")
        return rec

    def test_links_in_markdown_and_prose_are_read(self):
        a1, a2, a3 = (NEWS + "a%d" % n for n in (1, 2, 3))
        cases = {
            "bold with a particle": f"**{a1}**에서 {a2} 그리고 {a3}.",
            "particle only": f"{a1}에서 {a2}에서 {a3}에서",
            "markdown links": f"[기사 1]({a1}) [기사 2]({a2}) [기사 3]({a3})",
            "angle brackets": f"<{a1}> <{a2}> <{a3}>",
            "trailing punctuation": f"{a1}, {a2}; {a3}!",
            "escaped line breaks": f"{a1}\\n{a2}\\n{a3}",
            "query, fragment and final slash": f"{a1}?ref=x {a2}#top {a3}/",
            "host in capitals": a1.replace("news", "NEWS") + f" {a2} {a3}",
            "table cells": f"| 기사 1 | {a1} |\n| 기사 2 | {a2} |\n| 기사 3 | {a3} |",
        }
        for name, text in cases.items():
            with self.subTest(name):
                rec = self.cited(text)
                self.assertEqual((rec["failed"], rec["source_hosts"]), ({}, ["news.example.com"]))

    def test_one_page_cited_in_several_forms_is_one_source(self):
        a1 = NEWS + "a1"
        rec = self.cited(f"**{a1}**에서 {a1}에서 [기사]({a1}) {a1}?x=1 {a1}/")
        self.assertEqual(rec["failed"], {"sources": "1 of 3 source links come from tool results"})
        self.assertEqual(rec["source_hosts"], ["news.example.com"])

    def test_a_link_that_differs_in_the_path_is_not_a_source(self):
        rec = self.cited("출처: %sa1 %sa2 %sa3x" % (NEWS, NEWS, NEWS))
        self.assertEqual(rec["failed"], {"sources": "2 of 3 source links come from tool results; "
                                                    "links that no tool returned: news.example.com/a3x"})


class FlagTests(FailureCase):
    """What looks wrong but does not break a pass criterion."""

    def flags(self, name, *argv):
        outcome, rec = self.one(name, *argv)
        self.assertEqual((outcome.code, rec["ok"], rec["failed"]), (0, True, {}), outcome.err)
        return rec["flags"]

    def test_extra_model_calls_are_flagged(self):
        rounds = self.script("plan-fee")["rounds"]
        rounds[:0] = [step(calc("1+1", "2")), step(calc("2+2", "4")), step(calc("3+3", "6"))]
        self.assertEqual(self.flags("plan-fee"), {"calls": "5 model calls, 2 planned"})

    def test_an_answer_without_the_numbers_is_flagged(self):
        self.answer_with("plan-fee", "계산을 마쳤습니다.")
        flags = self.flags("plan-fee")
        self.assertEqual(set(flags), {"contains answer", "shown"})
        self.assertIn("no match for", flags["contains answer"])

    def test_a_calculation_with_an_invented_number_is_flagged(self):
        self.script("chip-price-ratio")["rounds"][2] = step(calc("210000/70001", "3"))
        self.assertEqual(self.flags("chip-price-ratio"),
                         {"inputs": "calculator numbers not from the prompt or a tool result: 70001"})

    def test_a_file_that_was_not_rewritten_is_flagged(self):
        original = catalog.discount_py().decode("utf-8")
        self.script("code-fix-diff")["rounds"][1] = step(call("write_file", {"path": "discount.py",
                                                                              "content": original}))
        flags = self.flags("code-fix-diff")
        self.assertEqual(flags["changes"], "discount.py was not rewritten")
        self.assertIn("result diff_file", flags)
        self.assertIn("contains discount.py", flags)

    def test_python_that_does_not_parse_is_flagged_and_never_run(self):
        self.script("code-fix-diff")["rounds"][1] = step(
            call("write_file", {"path": "discount.py", "content": "import os\nos.remove('x'\n"}))
        self.assertEqual(self.flags("code-fix-diff")["python discount.py"], "SyntaxError")

    def test_a_file_that_misses_the_expected_text_is_flagged(self):
        briefing = self.script("ai-chip-news")["rounds"][1]["calls"][0]["arguments"]
        briefing["content"] = "<h1>AI 반도체 뉴스</h1>"
        self.assertEqual(self.flags("ai-chip-news"), {"contains briefing.html": r"no match for /(?:<tr\b[\s\S]*?){3}/"})

    def test_reasoning_and_notices_are_flagged(self):
        script = self.script("plan-fee")
        script["rounds"][0]["reasoning"] = "생각 중"
        script["notices"] = ["no_execution"]
        self.assertEqual(self.flags("plan-fee"),
                         {"reasoning": "reasoning text arrived although thinking was off", "notice": "no_execution"})

    def test_code_without_a_function_is_flagged(self):
        self.answer_with("code-baseline", "함수는 sum으로 충분합니다.")
        self.assertEqual(set(self.flags("code-baseline")), {"contains answer"})

    def test_tool_call_markup_in_an_earlier_round_is_flagged(self):
        self.script("plan-fee")["rounds"][0]["text"] = '<tool_call>{"name": "calculator"}</tool_call>'
        self.assertEqual(self.flags("plan-fee"), {"markup": "tool-call markup in the text of round 1"})

    def test_markup_the_server_cut_from_the_answer_is_flagged(self):
        self.script("plan-fee")["rounds"][-1]["markup_removed"] = True
        self.assertEqual(self.flags("plan-fee"),
                         {"markup removed": "the server cut tool-call markup from the answer of round 2"})


class HttpFailureTests(FailureCase):
    def test_http_500_counts_as_a_server_error(self):
        self.reply(AGENT, 500, {"error": "upstream", "message": "x"})
        outcome, rec = self.one("plan-fee")
        self.assertEqual(rec["state"], "http 500 upstream")
        self.assertEqual(rec["failed"], {"completed": "http 500 upstream", "http_5xx": "1"})
        self.assertEqual((outcome.code, rec["http_5xx"], rec["http"]["agent"]), (1, 1, 500))
        self.assertEqual(self.demo.closed, self.demo.created)

    def test_a_proxy_page_instead_of_json_is_read(self):
        self.reply(AGENT, 502, b"<html>Bad gateway</html>")
        _, rec = self.one("plan-fee")
        self.assertEqual(rec["failed"], {"completed": "http 502", "http_5xx": "1"})

    def test_a_refusal_before_the_stream_names_its_code(self):
        self.reply(AGENT, 409, {"error": "workspace_busy", "message": "x"})
        _, rec = self.one("plan-fee")
        self.assertEqual(rec["failed"], {"completed": "http 409 workspace_busy"})

    def test_a_preview_that_does_not_open_fails_the_files_check(self):
        self.reply("/api/workspace/preview", 400, {"error": "preview"}, method="GET")
        _, rec = self.one("ai-chip-news")
        self.assertEqual(rec["failed"], {"files": "briefing.html does not open in the preview (HTTP 400, 20 B)"})
        (entry,) = rec["files"]
        self.assertEqual((entry["download"], entry["preview"], entry["preview_status"], entry["preview_bytes"]),
                         (200, False, 400, 20))

    def test_a_preview_without_any_content_tag_names_its_status_and_length(self):
        for body in (b"", b"no markup here"):
            self.reply("/api/workspace/preview", 200, body, method="GET")
            with self.subTest(length=len(body)):
                _, rec = self.one("ai-chip-news")
                self.assertEqual(rec["failed"], {"files": "briefing.html does not open in the preview "
                                                          f"(HTTP 200, {len(body)} B, no content tag)"})
                (entry,) = rec["files"]
                self.assertEqual((entry["preview"], entry["preview_status"], entry["preview_bytes"]),
                                 (False, 200, len(body)))

    def test_a_file_that_cannot_be_downloaded_fails_the_files_check(self):
        self.reply("/api/workspace/download", 404, {"error": "not_found"}, method="GET")
        outcome, rec = self.one("ai-chip-news")
        self.assertIn("briefing.html was not written", rec["failed"]["files"])
        self.assertEqual(rec["files"], [{"name": "briefing.html", "download": 404, "bytes": None}])
        self.assertEqual(rec["flags"]["contains briefing.html"], "file was not written")

    def test_an_upload_that_fails_stops_before_the_turn(self):
        self.reply("/api/workspace/upload", 413, {"error": "too_large"})
        outcome, rec = self.one("contract-review")
        self.assertEqual(rec["state"], "upload http 413 too_large")
        self.assertEqual(rec["failed"], {"completed": "upload http 413 too_large"})
        self.assertNotIn(("fp8", "POST", AGENT), self.demo.requests)
        self.assertEqual(self.demo.closed, ["tok001"])

    def test_an_upload_that_names_no_file_stops_before_the_turn(self):
        self.reply("/api/workspace/upload", 200, {"files": [], "bytes": 0})
        _, rec = self.one("code-fix-diff")
        self.assertEqual(rec["state"], "upload http 200")
        self.assertNotIn(("fp8", "POST", AGENT), self.demo.requests)

    def test_a_workspace_that_cannot_be_created_ends_the_run(self):
        self.reply(WORKSPACE, 503, {"error": "workspace_capacity"})
        outcome, rec = self.one("plan-fee")
        self.assertEqual(rec["failed"], {"completed": "http 503 workspace_capacity", "http_5xx": "1"})
        self.assertEqual((self.demo.closed, rec["http"]), ([], {"workspace": 503}))


class FailureEvidenceTests(FailureCase):
    """What the record keeps about a failed call: enough to tell the cause, never the address that was asked for."""
    ASKED = "https://docs.example.com/rejected-page-7"
    REFUSED = "Web IQ unavailable (unsafe_browse_url); no successful search result is available."
    PIECES = ("rejected-page-7", "secret-param", "install-frag", "not-a-query")

    def reject(self, arguments, error=None):
        rounds = self.script("axk2-public-research")["rounds"]
        rounds[1] = step(call("web_iq_browse", arguments, state="error", result={"error": error or self.REFUSED}))
        return self.one("axk2-public-research")[1]

    def test_a_rejected_browse_keeps_only_whether_its_address_had_a_query_or_fragment(self):
        cases = {self.ASKED: (False, False),
                 self.ASKED + "?secret-param=1": (True, False),
                 self.ASKED + "#install-frag": (False, True),
                 self.ASKED + "?secret-param=1#install-frag": (True, True),
                 self.ASKED + "#install-frag?not-a-query": (False, True),
                 self.ASKED + "?": (True, False)}
        for url, (query, fragment) in cases.items():
            with self.subTest(url=url):
                rec = self.reject({"url": url})
                self.assertEqual(rec["tools"][1], {"name": "web_iq_browse", "state": "error", "ms": 120,
                                                   "error": self.REFUSED,
                                                   "url_shape": {"query": query, "fragment": fragment}})
                self.assertIn("tool_errors", rec["failed"])
                text = json.dumps(rec, ensure_ascii=False)
                for piece in self.PIECES:
                    self.assertNotIn(piece, text)

    def test_a_rejected_browse_without_an_address_has_no_shape(self):
        for arguments in ({}, {"url": 5}, {"url": None}, {"query": self.ASKED + "?secret-param=1"}):
            with self.subTest(arguments=arguments):
                rec = self.reject(arguments)
                self.assertIsNone(rec["tools"][1]["url_shape"])
                text = json.dumps(rec, ensure_ascii=False)
                for piece in self.PIECES:
                    self.assertNotIn(piece, text)

    def test_only_a_failed_browse_has_a_shape(self):
        _, rec = self.one("axk2-public-research")
        self.assertTrue(all("url_shape" not in tool for tool in rec["tools"]), rec["tools"])
        rounds = self.script("ai-chip-news")["rounds"]
        rounds[0] = step(call("web_iq_news", {"url": self.ASKED + "?secret-param=1"}, state="error",
                              result={"error": "boom"}))
        _, rec = self.one("ai-chip-news")
        self.assertEqual(rec["tools"][0], {"name": "web_iq_news", "state": "error", "ms": 120, "error": "boom"})

    def test_a_web_address_quoted_by_a_tool_error_is_not_kept(self):
        quoted = "not valid JSON, starting " + json.dumps('{"url": "' + self.ASKED + '?secret-param=1')
        rec = self.reject({"url": self.ASKED}, error=quoted)
        self.assertEqual(rec["tools"][1]["error"], 'not valid JSON, starting "{\\"url\\": \\"<url>"')
        self.assertEqual(rec["failed"]["tool_errors"],
                         '1: web_iq_browse (not valid JSON, starting "{\\"url\\": \\"<url>")')
        text = json.dumps(rec, ensure_ascii=False)
        for piece in self.PIECES:
            self.assertNotIn(piece, text)


class StopRuleTests(FailureCase):
    def test_a_profile_that_is_not_open_is_skipped_while_the_other_runs(self):
        self.demo.me["nvfp4"] = {"open": False}
        outcome = self.go("--only", "plan-fee", "--runs", "fp8=1,nvfp4=2")
        reason = "the demo is not in open mode and this tool does not log in"
        self.assertEqual(outcome.code, 1)
        self.assertEqual([(r["profile"], r["run"], r.get("skipped")) for r in outcome.runs()],
                         [("fp8", 1, None), ("nvfp4", 1, reason), ("nvfp4", 2, reason)])
        self.assertEqual(outcome.summary["profiles"]["nvfp4"], {"web_iq": "unknown", "problem": reason})
        self.assertEqual(outcome.sleeps, [])
        self.assertEqual(self.demo.created, ["tok001"])

    def test_a_profile_whose_me_route_fails_is_skipped(self):
        self.reply("/api/me", 500, {}, method="GET")
        outcome = self.go("--only", "plan-fee", "--runs", "fp8=1")
        self.assertEqual([r.get("skipped") for r in outcome.runs()], ["api/me http 500"])
        self.assertEqual((outcome.code, self.demo.created), (1, []))

    def test_examples_that_need_web_iq_are_skipped_when_it_is_unavailable(self):
        self.demo.web_iq["fp8"] = "unavailable"
        outcome = self.go("--only", "ai-chip-news,plan-fee", "--runs", "fp8=1")
        skipped, passed = outcome.runs()
        self.assertEqual(skipped["skipped"], "Web IQ is unavailable on this profile")
        self.assertTrue(passed["ok"])
        self.assertEqual((outcome.code, outcome.sleeps, self.demo.created), (1, [], ["tok001"]))
        self.assertEqual(outcome.summary["profiles"]["fp8"], {"web_iq": "unavailable", "problem": None})

    def test_a_503_skips_the_later_runs_of_that_profile_only(self):
        self.reply(WORKSPACE, 503, {"error": "workspace_capacity"}, profile="nvfp4")
        outcome = self.go("--only", "plan-fee", "--runs", "fp8=1,nvfp4=2")
        first, second, third = outcome.runs()
        self.assertTrue(first["ok"])
        self.assertEqual(second["failed"], {"completed": "http 503 workspace_capacity", "http_5xx": "1"})
        self.assertEqual(third["skipped"], "the profile answered HTTP 503")
        self.assertEqual(outcome.summary["totals"],
                         {"passed": 1, "failed": 1, "skipped": 1, "http_5xx": 1, "web_iq_calls": 0})
        self.assertEqual(outcome.sleeps, [10.0])

    def test_a_refusal_to_serve_stops_the_whole_plan(self):
        for status, code in ((401, "unauthorized"), (403, "forbidden"), (429, "rate_limited")):
            with self.subTest(status=status):
                self.demo.overrides.clear()
                self.reply(WORKSPACE, status, {"error": code})
                outcome = self.go("--only", "plan-fee", "--runs", "fp8=3")
                first, *later = outcome.runs()
                self.assertEqual(first["state"], f"http {status} {code}")
                self.assertEqual((first["stop"], first["http_5xx"]), (status, 0))
                self.assertEqual({r["skipped"] for r in later}, {f"stopped after HTTP {status}"})
                self.assertEqual((len(later), outcome.sleeps, outcome.code), (2, [], 1))

    def test_a_refusal_in_the_agent_route_stops_the_plan_too(self):
        self.reply(AGENT, 429, {"error": "rate_limited"})
        outcome = self.go("--only", "plan-fee,code-baseline", "--runs", "fp8=1")
        first, second = outcome.runs()
        self.assertEqual((first["stop"], second["skipped"]), (429, "stopped after HTTP 429"))
        self.assertEqual(self.demo.closed, self.demo.created)

    def test_three_server_errors_in_a_row_stop_the_plan(self):
        self.reply(AGENT, 500, {"error": "upstream"})
        outcome = self.go("--only", "plan-fee", "--runs", "fp8=5")
        runs = outcome.runs()
        self.assertEqual([bool(r.get("skipped")) for r in runs], [False, False, False, True, True])
        self.assertEqual({r["skipped"] for r in runs[3:]}, {"stopped after 3 runs in a row with HTTP 5xx"})
        self.assertEqual(outcome.summary["totals"],
                         {"passed": 0, "failed": 3, "skipped": 2, "http_5xx": 3, "web_iq_calls": 0})
        self.assertEqual(outcome.sleeps, [10.0, 10.0])

    def test_a_success_between_server_errors_resets_the_count(self):
        self.demo.overrides[("fp8", "POST", AGENT)] = lambda hit: (500, {"error": "upstream"}) if hit != 3 else None
        outcome = self.go("--only", "plan-fee", "--runs", "fp8=5")
        runs = outcome.runs()
        self.assertEqual([r["ok"] for r in runs], [False, False, True, False, False])
        self.assertFalse(any(r.get("skipped") for r in runs))
        self.assertEqual(outcome.summary["totals"]["http_5xx"], 4)

    def test_ctrl_c_between_runs_skips_the_rest(self):
        def interrupt(seconds):
            raise KeyboardInterrupt

        outcome = self.go("--only", "plan-fee,code-baseline", "--runs", "fp8=1", sleep=interrupt)
        first, second = outcome.runs()
        self.assertEqual((outcome.code, outcome.summary["interrupted"], first["ok"]), (1, True, True))
        self.assertEqual(second, {"id": "code-baseline", "profile": "fp8", "run": 1, "ok": False,
                                  "skipped": "interrupted"})
        self.assertEqual(self.demo.closed, ["tok001"])

    def test_ctrl_c_during_a_run_still_closes_its_workspace(self):
        with mock.patch.object(check.Server, "stream", side_effect=KeyboardInterrupt):
            outcome = self.go("--only", "plan-fee", "--runs", "fp8=2")
        self.assertEqual((outcome.code, outcome.summary["interrupted"]), (1, True))
        self.assertEqual([r.get("skipped") for r in outcome.runs()], ["interrupted", "interrupted"])
        self.assertEqual((self.demo.created, self.demo.closed), (["tok001"], ["tok001"]))

    def test_a_server_that_does_not_answer_skips_every_run(self):
        with socket.socket() as closed:
            closed.bind(("127.0.0.1", 0))
            url = "http://127.0.0.1:%d" % closed.getsockname()[1]
        outcome = self.go("--only", "plan-fee", "--runs", "fp8=2", "--timeout", "5", url=url)
        reasons = {r["skipped"] for r in outcome.runs()}
        self.assertEqual(len(reasons), 1)
        self.assertRegex(reasons.pop(), r"^api/me transport \w+Error$")
        self.assertEqual((outcome.code, outcome.summary["totals"]["skipped"]), (1, 2))
        self.assertEqual(outcome.sleeps, [10.0, 10.0])


class OutputFileTests(FailureCase):
    def test_an_unwritable_output_file_is_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = str(Path(folder, "missing", "result"))
            for flag in ("--out", "--table"):
                with self.subTest(flag=flag):
                    outcome = self.go("--only", "plan-fee", "--runs", "fp8=1", flag, missing)
                    self.assertEqual(outcome.code, 2)
                    self.assertIn(f"the {flag} file cannot be written", outcome.err)
                    self.assertNotIn(folder, outcome.err)

    def test_a_failed_run_keeps_exit_code_1_when_the_file_cannot_be_written(self):
        self.script("plan-fee")["seconds"] = 99
        with tempfile.TemporaryDirectory() as folder:
            outcome = self.go("--only", "plan-fee", "--runs", "fp8=1", "--out", str(Path(folder, "no", "r.json")))
        self.assertEqual(outcome.code, 1)


class PlanTests(FailureCase):
    def test_without_go_nothing_is_sent(self):
        outcome = self.cli("--only", "plan-fee,ai-chip-news")
        summary = outcome.summary
        self.assertEqual((outcome.code, self.demo.requests, summary["go"]), (0, [], False))
        self.assertIn("nothing was sent", outcome.err)
        calls = BY_ID["plan-fee"].calls + BY_ID["ai-chip-news"].calls
        self.assertEqual((summary["planned_runs"], summary["planned_model_calls"]), (6, 3 * calls))
        self.assertEqual([(p["id"], p["profile"], p["run"], p["external"]) for p in summary["plan"]],
                         [("plan-fee", "fp8", 1, False), ("plan-fee", "fp8", 2, False),
                          ("plan-fee", "nvfp4", 1, False), ("ai-chip-news", "fp8", 1, True),
                          ("ai-chip-news", "fp8", 2, True), ("ai-chip-news", "nvfp4", 1, True)])
        self.assertNotIn("runs", summary)

    def test_the_default_plan_covers_every_example_three_times(self):
        summary = self.cli().summary
        self.assertEqual(summary["planned_runs"], 3 * len(catalog.EXAMPLES))
        self.assertEqual(summary["planned_model_calls"], 3 * sum(e.calls for e in catalog.EXAMPLES))
        self.assertEqual((summary["gap_seconds"], summary["max_seconds"]), (10.0, 45.0))

    def test_the_base_url_option_wins_over_the_environment(self):
        outcome = self.cli("--go", "--base-url", self.demo.url, "--only", "plan-fee", "--runs", "fp8=1",
                           url="http://127.0.0.1:1")
        self.assertEqual((outcome.code, [r["ok"] for r in outcome.runs()]), (0, [True]))


if __name__ == "__main__":
    unittest.main()
