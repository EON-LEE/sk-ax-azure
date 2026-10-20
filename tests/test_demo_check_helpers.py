"""The helpers and the command line of the demo-example verifier, with no server involved.

Typed values that must never come back in an error message (a URL with credentials, a path, a secret-looking word)
use words like zzz-typed so that a leak shows up as a plain substring check.
"""
from __future__ import annotations

import ast
import io
import json
import tempfile
import time
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

from tests.test_demo_check_examples import BY_ID, catalog, check


def turn_with(*calls):
    """A turn whose tool calls (name, arguments, result) all succeeded."""
    turn = check.Turn()
    for position, (name, arguments, result) in enumerate(calls, 1):
        turn.feed("action", json.dumps({"id": f"c{position}", "name": name, "arguments": arguments,
                                        "state": "success", "result": result}))
    return turn


class NumberTests(unittest.TestCase):
    def test_thousands_separators_go_only_between_groups_of_three(self):
        self.assertEqual(check.plain("1,188,000원 12,345 1,2,3 12,3456 3,14"), "1188000원 12345 1,2,3 12,3456 3,14")

    def test_numbers_are_exact_fractions(self):
        self.assertEqual(check.numbers("1,188,000원, 41.7%, 5/12, 합계 2720."),
                         {Fraction(1188000), Fraction("41.7"), Fraction(5), Fraction(12), Fraction(2720)})

    def test_the_last_calculator_result_must_be_shown_rounded_or_as_a_percent(self):
        shown = check.calculation_shown
        turn = turn_with(("calculator", {"expression": "5/12"}, {"result": "5/12"}))
        for text in ("약 41.7%입니다", "약 42%", "0.4167", "5/12는 0.42"):
            with self.subTest(text=text):
                self.assertTrue(shown(turn, [text]))
        self.assertFalse(shown(turn, ["약 40%입니다"]))
        self.assertTrue(shown(turn, ["약 40%입니다", "파일에는 41.7%"]))

    def test_only_the_last_calculator_counts(self):
        turn = turn_with(("calculator", {"expression": "5/12"}, {"result": "5/12"}),
                         ("calculator", {"expression": "3+4"}, {"result": "7"}))
        self.assertFalse(check.calculation_shown(turn, ["약 41.7%"]))
        self.assertTrue(check.calculation_shown(turn, ["합계 7"]))

    def test_a_result_that_cannot_be_compared_is_not_held_against_the_answer(self):
        self.assertTrue(check.calculation_shown(turn_with(("get_current_time", {}, {"time": "fixture"})), ["x"]))
        for result in ({"result": "abc"}, {"result": "1/0"}, {}, None, "5/12"):
            with self.subTest(result=result):
                self.assertTrue(check.calculation_shown(turn_with(("calculator", {}, result)), ["x"]))

    def test_calculator_numbers_must_come_from_the_prompt_or_a_tool_result(self):
        example = BY_ID["chip-price-ratio"]
        read = [("web_iq_finance", {"query": "000660"}, {"items": [{"snippet": "종가 210,000"}]}),
                ("web_iq_finance", {"query": "005930"}, {"items": [{"snippet": "종가 70,000"}]})]
        calculator = lambda expression: ("calculator", {"expression": expression}, {"result": "3"})  # noqa: E731
        stray = check.stray_inputs
        self.assertEqual(stray(example, turn_with(*read, calculator("210000/70000"))), [])
        self.assertEqual(stray(example, turn_with(*read, calculator("210000/70000*100"))), [])
        self.assertEqual(stray(example, turn_with(*read, calculator("210000/70001"))), ["70001"])
        self.assertEqual(stray(example, turn_with(calculator("210000/70000"), *read)), ["210000", "70000"])
        self.assertEqual(stray(example, turn_with(*read, calculator("660+5930"))), [])


class LinkHelperTests(unittest.TestCase):
    def test_a_link_is_compared_as_host_and_path(self):
        for url, expected in (("https://Docs.Example.com/a/b/", "docs.example.com/a/b"),
                              ("http://docs.example.com/a/b?x=1#top", "docs.example.com/a/b"),
                              ("https://docs.example.com/a%20b", "docs.example.com/a b"),
                              ("https://docs.example.com/a.", "docs.example.com/a"),
                              ("https://docs.example.com/**", "docs.example.com"),
                              ("https://docs.example.com", "docs.example.com"),
                              ("https://docs.example.com:8443/a", "docs.example.com/a"),
                              ("http://[bad", "")):
            with self.subTest(url=url):
                self.assertEqual(check.norm(url), expected)

    def test_links_are_found_in_prose_and_markdown(self):
        text = '[a](https://docs.example.com/a) <https://docs.example.com/b>, https://docs.example.com/c. ' \
               '"https://docs.example.com/d" 그리고 http://docs.example.com/e?x=1'
        self.assertEqual(check.links_in(text), {f"docs.example.com/{name}" for name in "abcde"})
        self.assertEqual(check.links_in("링크 없음 http:// https"), set())

    def test_a_cited_link_must_come_from_a_tool(self):
        known = {"docs.example.com/abc/d", "docs.example.com/c"}
        came = check.came_from_tools
        self.assertEqual(came("docs.example.com/c", known), "docs.example.com/c")
        self.assertEqual(came("docs.example.com/abc", known), "docs.example.com/abc")
        self.assertEqual(came("docs.example.com/c에서", known), "docs.example.com/c")
        self.assertEqual(came("docs.example.com/c**에서", known), "docs.example.com/c")
        for wrong in ("", "docs.example.com", "docs.example.com/x", "docs.example.com/ab", "other.example.com/c",
                      "docs.example.com/c/d"):
            with self.subTest(link=wrong):
                self.assertEqual(came(wrong, known), "")

    def test_a_link_with_korean_letters_in_its_path_matches_as_it_is(self):
        known = {"ko.example.com/wiki/삼성전자"}
        self.assertEqual(check.came_from_tools("ko.example.com/wiki/삼성전자", known), "ko.example.com/wiki/삼성전자")

    def test_the_links_the_tools_returned(self):
        turn = turn_with(("web_iq_news", {}, {"items": [{"url": "https://docs.example.com/a?x=1", "snippet":
                                                          "see https://docs.example.com/b."}]}),
                         ("web_iq_browse", {"url": "https://docs.example.com/read?x=1"}, {"text": "page"}),
                         ("write_file", {"path": "a.md", "content": "https://docs.example.com/mine"},
                          {"file": "a.md"}))
        self.assertEqual(check.known_links(turn),
                         {"docs.example.com/a", "docs.example.com/b", "docs.example.com/read"})

    def test_links_from_a_failed_tool_do_not_count(self):
        turn = check.Turn()
        turn.feed("action", json.dumps({"id": "a", "name": "web_iq_web", "state": "error",
                                        "result": {"items": [{"url": "https://docs.example.com/a"}]}}))
        self.assertEqual(check.known_links(turn), set())

    def test_links_in_the_answer_and_in_text_files_are_cited(self):
        files = {"p.md": "https://docs.example.com/b", "d.py": "# https://docs.example.com/c",
                 "p.HTML": "https://docs.example.com/d", "b.pdf": "https://docs.example.com/e"}
        self.assertEqual(check.cited_links("답 https://docs.example.com/a", files),
                         {f"docs.example.com/{name}" for name in "abd"})


class TextHelperTests(unittest.TestCase):
    def test_a_diff_keeps_its_line_breaks_and_a_result_is_json(self):
        self.assertEqual(check.result_text("--- a\n+++ b\n"), "--- a\n+++ b\n")
        self.assertEqual(check.result_text({"가": 1}), '{"가": 1}')

    def test_control_characters_and_length(self):
        self.assertEqual(check.tidy("a\x00b\nc\x7f"), "a b c")
        self.assertEqual(check.tidy(None), "")
        self.assertEqual(len(check.tidy("x" * 500)), 128)
        self.assertEqual(check.tidy("  y  ", 1), "y")

    def test_cell_and_strings(self):
        self.assertEqual([check.cell(value) for value in (None, "", 0, "x", 7.5)], ["-", "-", "0", "x", "7.5"])
        value = {"a": ["x", {"b": "y"}], "c": 5, "d": ("z",), "e": None}
        self.assertEqual(sorted(check.strings(value)), ["x", "y", "z"])

    def test_what_a_failed_tool_said(self):
        text = check.failure_text
        self.assertEqual(text({"result": {"error": "boom", "message": "m"}}), "boom")
        self.assertEqual(text({"result": {"message": "m"}}), "m")
        self.assertEqual(text({"result": {"status": "partial"}}), "partial")
        self.assertEqual(text({"result": {"x": 1}}), '{"x": 1}')
        self.assertEqual(text({"result": "plain text"}), "plain text")
        self.assertEqual(text({"state": "cancelled"}), "cancelled")

    def test_what_a_refusal_said(self):
        reply = check.Reply()
        reply.error = "ConnectionRefusedError"
        self.assertEqual(check.refusal(reply), "transport ConnectionRefusedError")
        reply.error, reply.status, reply.body = None, 503, b'{"error": "workspace_capacity"}'
        self.assertEqual(check.refusal(reply), "http 503 workspace_capacity")
        reply.status, reply.body = 502, b"<html>Bad gateway</html>"
        self.assertEqual(check.refusal(reply), "http 502")
        self.assertEqual(reply.json(), {})
        reply.body = b"[1]"
        self.assertEqual(reply.json(), {})
        reply.body = b'{"error": "x"}'
        self.assertEqual(reply.json(), {"error": "x"})


class EventStreamTests(unittest.TestCase):
    def read(self, text, deadline=None):
        body = io.BytesIO(text if isinstance(text, bytes) else text.encode("utf-8"))
        return list(check.events(body, time.monotonic() + 60 if deadline is None else deadline))

    def test_events_end_at_a_blank_line(self):
        pairs = self.read('event: round\ndata: {"index": 1}\n\ndata: {"a": 1}\n\n: keep-alive\n\n'
                          'event: end\ndata: {}\n\n')
        self.assertEqual(pairs, [("round", '{"index": 1}'), ("message", '{"a": 1}'), ("end", "{}")])

    def test_data_lines_are_joined_and_a_last_event_without_a_blank_line_is_kept(self):
        self.assertEqual(self.read("data: a\ndata:b\n\nevent: end\ndata: {}"), [("message", "a\nb"), ("end", "{}")])

    def test_crlf_line_ends_and_broken_bytes(self):
        self.assertEqual(self.read("event: end\r\ndata: {}\r\n\r\n"), [("end", "{}")])
        ((name, data),) = self.read(b"data: \xff{}\n\n")
        self.assertEqual((name, data), ("message", "\ufffd{}"))

    def test_a_deadline_ends_the_wait(self):
        with self.assertRaises(check.Stalled) as caught:
            self.read("data: a\n\n", deadline=time.monotonic() - 1)
        self.assertEqual(str(caught.exception), "deadline")

    def test_an_oversized_line_is_refused(self):
        with mock.patch.object(check, "MAX_LINE", 16), self.assertRaises(check.Stalled) as caught:
            self.read("data: " + "x" * 40 + "\n\n")
        self.assertEqual(str(caught.exception), "oversized line")


class TurnFeedTests(unittest.TestCase):
    def feed(self, *events):
        turn = check.Turn()
        for name, data in events:
            turn.feed(name, data if isinstance(data, str) else json.dumps(data))
        return turn

    @staticmethod
    def text(content):
        return "message", {"choices": [{"delta": {"content": content}}]}

    def test_lines_that_are_not_events_are_ignored(self):
        turn = self.feed(("round", "{not json"), ("action", "[1, 2]"), ("metrics", "null"), ("end", '"text"'),
                         ("message", ""), ("unknown", {}), ("message", {"choices": []}),
                         ("message", {"choices": [None]}), ("message", {"usage": {"total_tokens": 3}}),
                         ("message", {"choices": [{"delta": {"content": 5}}]}))
        self.assertEqual((turn.round, turn.texts, turn.actions, turn.metrics, turn.end, turn.reasoning),
                         (0, {}, {}, None, None, False))
        self.assertEqual(turn.answer(), "")

    def test_the_answer_is_the_text_of_the_last_round(self):
        turn = self.feed(("round", {"index": 1}), self.text("계산해 "), self.text("보겠습니다."),
                         ("round", {"index": 2}), self.text(" 답: "), self.text("42 "))
        self.assertEqual(turn.texts, {1: "계산해 보겠습니다.", 2: " 답: 42 "})
        self.assertEqual(turn.answer(), "답: 42")

    def test_a_round_event_without_an_index_counts_on(self):
        turn = self.feed(("round", {}), ("round", {"index": "x"}), ("round", {"index": 7}), ("round", {}))
        self.assertEqual(turn.round, 8)

    def test_reasoning_is_noticed_and_is_not_part_of_the_answer(self):
        turn = self.feed(("round", {"index": 1}),
                         ("message", {"choices": [{"delta": {"reasoning_content": "생각"}}]}), self.text("답"))
        self.assertEqual((turn.reasoning, turn.answer()), (True, "답"))

    def test_actions_merge_by_id_in_the_order_they_first_appeared(self):
        turn = self.feed(("action", {"id": "c1", "name": "calculator", "state": "pending"}),
                         ("action", {"id": "c2", "name": "get_current_time", "state": "running"}),
                         ("action", {"id": "c1", "state": "success", "result": {"result": "3"}}),
                         ("action", {"name": "no_id"}), ("action", {"name": "no_id_either"}))
        self.assertEqual(list(turn.actions), ["c1", "c2", 2, 3])
        self.assertEqual(turn.actions["c1"], {"id": "c1", "name": "calculator", "state": "success",
                                              "result": {"result": "3"}})
        self.assertEqual([a["id"] for a in turn.ok()], ["c1"])
        self.assertEqual(len(turn.ok("calculator")), 1)
        self.assertEqual(turn.ok("get_current_time"), [])

    def test_notices_are_kept_short(self):
        turn = self.feed(("notice", {"code": "no_execution", "message": "m"}), ("notice", {"code": "x" * 100}))
        self.assertEqual(turn.notices, ["no_execution", "x" * 40])

    def test_policy_metrics_end_and_error_are_kept_as_they_came(self):
        turn = self.feed(("policy", {"a": 1}), ("metrics", {"state": "ok"}), ("end", {"state": "completed"}),
                         ("error", {"code": "model_failed"}))
        self.assertEqual((turn.policy, turn.metrics, turn.end, turn.error),
                         ({"a": 1}, {"state": "ok"}, {"state": "completed"}, {"code": "model_failed"}))


class ReportTests(unittest.TestCase):
    def test_the_table_has_one_row_per_run_and_never_breaks_a_row(self):
        run = {"id": "plan-fee", "profile": "fp8", "run": 1, "ok": True, "failed": {}, "flags": {"a|b": "x|y"},
               "tools": [{"name": "calculator"}, {"name": None}], "web_iq_calls": 0,
               "files": [{"name": "a.md"}, {"name": "b.md"}], "total_seconds": 7.35, "ttft_seconds": 3.1,
               "input_tokens": 100, "output_tokens": 20, "model_calls": 2, "source_hosts": ["docs.example.com"]}
        stopped = {"id": "ai-chip-news", "profile": "nvfp4", "run": 2, "ok": False, "skipped": "stopped | after 429"}
        lines = check.render_table([run, stopped]).splitlines()
        self.assertEqual(len(lines), 4)
        rows = [[cell.strip() for cell in line.split("|")[1:-1]] for line in lines]
        self.assertEqual({len(row) for row in rows}, {14})
        self.assertEqual(rows[0][:4], ["example", "model", "run", "result"])
        self.assertEqual(set(rows[1]), {"---"})
        self.assertEqual(rows[2], ["plan-fee", "FP8", "1", "PASS", "7.35", "3.1", "100/20", "2", "calculator>?", "0",
                                   "a.md,b.md", "docs.example.com", "", "a/b: x/y"])
        self.assertEqual(rows[3][:4], ["ai-chip-news", "NVFP4", "2", "skipped"])
        self.assertEqual((rows[3][12], rows[3][13]), ("stopped / after 429", ""))

    def test_a_failed_run_lists_what_failed(self):
        run = {"id": "plan-fee", "profile": "fp8", "run": 1, "ok": False, "failed": {"time": "46.5 s", "tools": "x"},
               "flags": {}, "tools": [], "web_iq_calls": 0, "files": [], "wall_seconds": 46.5}
        (row,) = [[c.strip() for c in line.split("|")[1:-1]] for line in check.render_table([run]).splitlines()[2:]]
        self.assertEqual((row[3], row[4], row[8], row[10], row[12]), ("FAIL", "46.5", "-", "-", "time: 46.5 s; tools: x"))

    def test_progress_lines(self):
        run = {"id": "plan-fee", "profile": "fp8", "run": 1, "ok": True, "failed": {}, "total_seconds": 7.35,
               "model_calls": 2, "tools": [{"name": "calculator"}]}
        lines = io.StringIO()
        check.progress_line(1, 3, run, lines)
        check.progress_line(2, 3, {**run, "ok": False, "failed": {"time": "x", "sources": "y"},
                                   "total_seconds": None, "wall_seconds": 61.2, "tools": [{"name": None}]}, lines)
        check.progress_line(3, 3, {"id": "ai-chip-news", "profile": "nvfp4", "run": 2, "ok": False,
                                   "skipped": "stopped after HTTP 429"}, lines)
        self.assertEqual(lines.getvalue().splitlines(), [
            "[1/3] FP8 plan-fee #1: PASS 7.35s calls 2 tools calculator",
            "[2/3] FP8 plan-fee #1: FAIL time,sources 61.2s calls 2 tools ?",
            "[3/3] NVFP4 ai-chip-news #2: skipped (stopped after HTTP 429)"])


class ArgumentTests(unittest.TestCase):
    def test_runs(self):
        self.assertEqual(check.parse_runs("fp8=2,nvfp4=1"), {"fp8": 2, "nvfp4": 1})
        self.assertEqual(check.parse_runs("nvfp4=3,fp8=0"), {"fp8": 0, "nvfp4": 3})
        self.assertEqual(check.parse_runs("nvfp4=5"), {"nvfp4": 5})
        for bad in ("fp8", "fp8=", "fp8=-1", "fp8=6", "fp8=1,fp8=2", "bogus=1", "fp8 =1", "fp8=1,", ""):
            with self.subTest(runs=bad), self.assertRaisesRegex(check.Usage, "--runs must look like"):
                check.parse_runs(bad)
        with self.assertRaisesRegex(check.Usage, "--runs selects no run"):
            check.parse_runs("fp8=0,nvfp4=0")

    def test_only_keeps_the_given_order_without_duplicates(self):
        everything = [example.id for example in catalog.EXAMPLES]
        self.assertEqual([e.id for e in check.pick_examples(None)], everything)
        self.assertEqual([e.id for e in check.pick_examples([])], everything)
        picked = check.pick_examples(["plan-fee, time-zones", "plan-fee", "code-baseline"])
        self.assertEqual([e.id for e in picked], ["plan-fee", "time-zones", "code-baseline"])
        for bad in (["plan-fee,nope"], [""], ["plan-fee,"]):
            with self.subTest(only=bad), self.assertRaisesRegex(check.Usage, "--only names an unknown example"):
                check.pick_examples(bad)

    def test_a_target_is_the_root_of_a_site(self):
        for url, expected in (("https://demo.example.com", ("https", "demo.example.com", 443)),
                              ("  https://demo.example.com/  ", ("https", "demo.example.com", 443)),
                              ("https://demo.example.com:8443", ("https", "demo.example.com", 8443)),
                              ("http://127.0.0.1:8080", ("http", "127.0.0.1", 8080)),
                              ("http://localhost", ("http", "localhost", 80)),
                              ("http://[::1]:9000", ("http", "::1", 9000))):
            with self.subTest(url=url):
                target = check.parse_target(url)
                self.assertEqual((target.scheme, target.host, target.port), expected)

    def test_a_bad_target_is_refused_without_repeating_it(self):
        for url in ("", "ftp://demo.example.com", "demo.example.com", "https://",
                    "https://user:zzz-typed@demo.example.com", "https://demo.example.com/zzz-typed",
                    "https://demo.example.com/?q=zzz-typed", "https://demo.example.com/#zzz-typed",
                    "http://demo.example.com", "https://demo.example.com:99999", "https://[zzz-typed"):
            with self.subTest(url=url), self.assertRaises(check.Usage) as caught:
                check.parse_target(url)
            self.assertNotIn("zzz-typed", str(caught.exception))
            self.assertNotIn("demo.example.com", str(caught.exception))


class CommandLineTests(unittest.TestCase):
    def main(self, *argv, environ=None):
        out, err = io.StringIO(), io.StringIO()
        code = check.main(list(argv), environ=environ or {}, stdout=out, stderr=err, sleep=self.no_sleep)
        return code, out.getvalue(), err.getvalue()

    def no_sleep(self, seconds):
        self.fail("nothing is sent here, so nothing waits")

    def test_a_usage_error_is_exit_2_with_an_empty_stdout_and_no_echo_of_the_typed_value(self):
        limits = "--max-seconds must be 1 to 900 and --timeout 1 to 3600"
        for argv, typed, message in (
                (["--gap", "4.375"], "4.375", "--gap must be between 10 and 3600 seconds"),
                (["--gap", "zzz-typed"], "zzz-typed", "argument --gap is invalid"),
                (["--gap", "nan"], None, "--gap must be between"),
                (["--gap", "inf"], None, "--gap must be between"),
                (["--gap", "3601"], None, "--gap must be between"),
                (["--runs", "zzz-typed=1"], "zzz-typed", "--runs must look like"),
                (["--runs", "fp8=0,nvfp4=0"], None, "--runs selects no run"),
                (["--only", "zzz-typed"], "zzz-typed", "--only names an unknown example"),
                (["--max-seconds", "0.123"], "0.123", limits),
                (["--max-seconds", "901"], "901", limits),
                (["--max-seconds", "nan"], None, limits),
                (["--timeout", "0.0042"], "0.0042", limits),
                (["--token", "zzz-typed"], "zzz-typed", "unrecognized or malformed arguments"),
                (["zzz-typed"], "zzz-typed", "unrecognized or malformed arguments"),
                (["--go"], None, "--base-url or AXK2_DEMO_URL"),
                (["--go", "--base-url", "https://user:zzz-typed@demo.example.com"], "zzz-typed",
                 "no credentials, path, query or fragment"),
                (["--base-url", "https://demo.example.com/zzz-typed"], "zzz-typed", "must be the site root"),
                (["--base-url", "http://demo.example.com"], "demo.example.com", "plain http is only allowed"),
                (["--base-url", "http://127.0.0.1:99999"], "99999", "not a valid URL")):
            with self.subTest(argv=argv):
                code, out, err = self.main(*argv)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(message, err)
                if typed:
                    self.assertNotIn(typed, err)

    def test_a_malformed_url_in_the_environment_is_refused_even_without_go(self):
        code, out, err = self.main(environ={"AXK2_DEMO_URL": "ftp://demo.example.com"})
        self.assertEqual((code, out), (2, ""))
        self.assertNotIn("demo.example.com", err)

    def test_the_base_url_option_wins_over_the_environment(self):
        # the loopback address in the environment would be accepted; the option is a plain-http public host
        code, out, err = self.main("--go", "--base-url", "http://demo.example.com",
                                   environ={"AXK2_DEMO_URL": "http://127.0.0.1:1"})
        self.assertEqual(code, 2)
        self.assertIn("plain http is only allowed", err)

    def test_a_plan_needs_no_url(self):
        code, out, err = self.main("--only", "plan-fee")
        self.assertEqual(code, 0)
        self.assertIn("nothing was sent", err)
        summary = json.loads(out)
        self.assertEqual((summary["go"], summary["planned_runs"]), (False, 3))
        self.assertNotIn("started_at", summary)

    def test_the_examples_are_listed(self):
        code, out, err = self.main("--list")
        lines = out.splitlines()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([line.split("\t")[0] for line in lines], [e.id for e in catalog.EXAMPLES])
        self.assertTrue(all(line.count("\t") == 2 for line in lines))

    def test_the_fixtures_are_written(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder, "new", "fixtures")
            code, out, err = self.main("--write-fixtures", str(target))
            pdf, script = (target / "contract.pdf").read_bytes(), (target / "discount.py").read_text("utf-8")
        self.assertEqual((code, out.split(), err), (0, ["contract.pdf", "discount.py"], ""))
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(pdf, catalog.contract_pdf())
        ast.parse(script)

    def test_fixtures_that_cannot_be_written_are_a_usage_error(self):
        with tempfile.TemporaryDirectory() as folder:
            taken = Path(folder, "taken")
            taken.write_text("x", "utf-8")
            code, out, err = self.main("--write-fixtures", str(taken))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("files cannot be written", err)
        self.assertNotIn(folder, err)


if __name__ == "__main__":
    unittest.main()
