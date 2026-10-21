"""The candidate agent examples: wording, synthetic files and the tool the real agent loop forces in round 1.

Nothing here reaches a model, a hub or a web app. The agent loop runs against the reverse-link fixture of the other
agent tests; Web IQ is the in-process fake whose tool list has the same shape as the real one.
"""
import ast
import json
import re
import sys
import unittest
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tests.test_demo_agent import ROOT, Workspace
from tests.test_agent_turns import TextLink, run
from tests.test_web_iq import VERTICALS, WebIQ
from tests.test_web_iq_verticals import contracts
from agent import LOG as AGENT_LOG, MODEL_CALLS, TOOL_ROUNDS, turn_policy

sys.path[:0] = [str(ROOT / "demo" / "frontend"), str(ROOT / "demo" / "tools")]
import agentic_examples as catalog
from pdf_documents import read_pdf
del sys.path[:2]

EXAMPLES = catalog.EXAMPLES


# the chat buttons, in the order of the start screen; contract-review and code-fix-diff need an attachment, and
# code-baseline (no tools) and ai-chip-news (its last live check failed in the file steps) stay in the catalog only
BUTTONS = ("time-zones", "plan-fee", "axk2-public-research", "chip-price-ratio",
           "foundry-byom-steps", "pangyo-lunch-plan", "csv-chart", "monthly-sales-report")


def have_time_zones():
    try:
        return all(ZoneInfo(name) for name in ("Asia/Seoul", "America/New_York", "Europe/London"))
    except ZoneInfoNotFoundError:
        return False


def chat_buttons():
    js = (ROOT / "demo" / "frontend" / "static" / "chat.js").read_text(encoding="utf-8")
    block = js.split("const EXAMPLES = [", 1)[1].split("];", 1)[0]
    return re.findall(r'\{ label: "([^"]+)", text: "([^"]+)", tools: true \}', block)


def tool_names():
    return {tool.name for tool in Workspace().tools()} | {"web_iq_" + name for name in VERTICALS}


def web_iq_provider():
    class Session:
        async def list_tools(self, cursor=None):
            return SimpleNamespace(tools=contracts(), nextCursor=None)

    @asynccontextmanager
    async def session():
        yield Session()

    return WebIQ({"AXK2_WEBIQ_API_KEY": "fixture", "AXK2_WEBIQ_ACCESS_APPROVED": "1"}, session_factory=session)


def events(output):
    for block in output.split("\n\n"):
        name, data = "message", []
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data.append(line[6:])
        if data:
            yield name, json.loads("\n".join(data))


def corrected_discount():
    return (catalog.discount_py().decode("utf-8")
            .replace("total += price * rate", "total += price * (1 - rate)")
            .replace("    return sum(values) / len(values)", "    return sum(values) / len(values) if values else 0"))


class CatalogTests(unittest.TestCase):
    def test_twelve_distinct_examples_with_two_or_three_quick_ones(self):
        self.assertEqual(len(EXAMPLES), 12)
        self.assertEqual(len({e.id for e in EXAMPLES}), 12)
        self.assertEqual(len({e.label for e in EXAMPLES}), 12)
        groups = Counter(e.group for e in EXAMPLES)
        self.assertEqual(set(groups), {"quick", "agent"})
        self.assertIn(groups["quick"], (2, 3))
        for example in EXAMPLES:
            self.assertRegex(example.id, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

    def test_prompts_are_korean_plain_text_that_fits_a_string_literal(self):
        for example in EXAMPLES:
            with self.subTest(example=example.id):
                self.assertRegex(example.prompt, "[가-힣]")
                self.assertLessEqual(len(example.prompt), 400)
                for forbidden in ('"', "\\", "\n", "\r", "http", "://", "@"):
                    self.assertNotIn(forbidden, example.prompt)
                self.assertEqual(example.prompt, example.prompt.strip())

    def test_planned_calls_and_every_pattern(self):
        for example in EXAMPLES:
            with self.subTest(example=example.id):
                self.assertTrue(1 <= example.calls <= MODEL_CALLS)
                for _, pattern in (*example.args, *example.results, *example.contains):
                    re.compile(pattern)
                # the last model call of a turn has no tools: a plan needs at most TOOL_ROUNDS tool rounds
                self.assertLessEqual(len(example.tools), TOOL_ROUNDS * 4)
                self.assertTrue(example.tools or example.calls == 1)

    def test_tools_are_real_tool_names(self):
        real = tool_names()
        for example in EXAMPLES:
            with self.subTest(example=example.id):
                named = {*example.tools, *(t for group in example.any_of for t in group),
                         *(t for t, _ in example.args), *(t for t, _ in example.results)}
                self.assertFalse(named - real, named - real)
                self.assertTrue(example.forced is None or example.forced in real)
                if example.forced:
                    self.assertEqual(example.tools[0], example.forced)

    def test_check_fields_agree_with_each_other(self):
        for example in EXAMPLES:
            with self.subTest(example=example.id):
                sources = {"answer", *example.files, *([example.changes] if example.changes else [])}
                for source, _ in example.contains:
                    self.assertIn(source, sources)
                self.assertEqual(len(set(example.files)), len(example.files))
                for name in example.files:
                    self.assertRegex(name, r"^[\w.-]+\.(?:csv|html|md|txt|json|py)$")
                if example.preview:
                    self.assertIn(example.preview, example.files)
                    self.assertTrue(example.preview.endswith(".html"))
                self.assertEqual("preview" in example.artifacts, bool(example.preview))
                self.assertTrue(set(example.artifacts) <= {"preview", "chart"})
                if example.upload:
                    self.assertIn(example.upload, catalog.FIXTURES)
                self.assertIn(example.changes, ("", example.upload))
                uses_web_iq = any(t.startswith("web_iq_") for t in example.tools)
                self.assertEqual(example.external, uses_web_iq)
                if example.sources or example.hosts:
                    self.assertTrue(example.external)
                for host in example.hosts:
                    self.assertRegex(host, r"^[a-z0-9-]+(?:\.[a-z0-9-]+)+$")
                if example.upload:
                    self.assertFalse(example.external, "a search that follows an upload must name its own terms")
        self.assertEqual(set(catalog.FIXTURES), {e.upload for e in EXAMPLES if e.upload})

    def test_fixture_builders_are_deterministic_bytes(self):
        for name, build in catalog.FIXTURES.items():
            with self.subTest(name=name):
                self.assertIsInstance(build(), bytes)
                self.assertEqual(build(), build())


class ChatButtonTests(unittest.TestCase):
    def test_every_button_sends_the_catalog_prompt_word_for_word(self):
        by_id = {e.id: e for e in EXAMPLES}
        self.assertEqual(chat_buttons(), [(by_id[name].label, by_id[name].prompt) for name in BUTTONS])

    def test_a_button_needs_nothing_the_visitor_does_not_have(self):
        by_id = {e.id: e for e in EXAMPLES}
        self.assertEqual(len(set(BUTTONS)), len(BUTTONS))
        for name in BUTTONS:
            with self.subTest(example=name):
                self.assertEqual(by_id[name].upload, "")
        self.assertEqual(Counter(by_id[name].group for name in BUTTONS), {"quick": 2, "agent": 6})
        self.assertEqual(sum(by_id[name].external for name in BUTTONS), 4)

    def test_the_public_research_example_is_the_third_button_and_ai_chip_news_is_catalog_only(self):
        by_id = {e.id: e for e in EXAMPLES}
        buttons = chat_buttons()
        research, news = by_id["axk2-public-research"], by_id["ai-chip-news"]
        self.assertEqual(buttons[2], (research.label, research.prompt))
        self.assertNotIn(news.label, [label for label, _ in buttons])
        self.assertNotIn(news.prompt, [text for _, text in buttons])
        # the note keeps the last live result, so the example does not return to the buttons without a new check
        for fact in ("버튼에서 뺌", "1/3", "단일 표본", "write_file"):
            self.assertIn(fact, news.risk)


class TimeZoneExampleTests(unittest.TestCase):
    """The first button asks for each city's local time and the UTC offset the tool returned, and for no difference.

    A live check of the earlier wording had the model write a wrong subtraction (single sample), so the prompt leaves
    nothing to subtract: the reader compares the three lines.
    """

    def setUp(self):
        self.example = next(e for e in EXAMPLES if e.id == "time-zones")

    def test_it_still_reads_the_clock_of_all_three_cities_with_the_tool(self):
        example = self.example
        self.assertIsNone(example.forced)
        self.assertEqual(example.tools, ("get_current_time",) * 3)
        self.assertEqual(example.args,
                         (("get_current_time", "America/New_York"), ("get_current_time", "Europe/London")))
        self.assertIn("get_current_time", example.prompt)
        for zone in ("Asia/Seoul", "America/New_York", "Europe/London"):
            self.assertIn(zone, example.prompt)

    def test_it_asks_for_one_line_per_city_with_the_local_time_and_the_offset_the_tool_returned(self):
        prompt = self.example.prompt
        self.assertIn("도시마다 한 줄씩 현지 시각과 도구가 돌려준 UTC 오프셋만 적어 줘.", prompt)
        for word in ("회의", "제안", "덜 불편"):
            self.assertNotIn(word, prompt)

    def test_it_asks_for_no_difference_between_cities_so_the_model_subtracts_nothing(self):
        prompt = self.example.prompt
        self.assertIn("도시 간 시차는 적지 마.", prompt)
        for word in ("몇 시간", "빠른지", "차이로", "계산해"):
            self.assertNotIn(word, prompt)

    def test_the_prompt_fixes_no_number_because_summer_time_moves_the_offsets(self):
        # London ends summer time on 2026-10-25 and New York on 2026-11-01: no hour or offset is written
        self.assertNotRegex(self.example.prompt, r"\d")

    @unittest.skipUnless(have_time_zones(), "this host has no IANA time zone data")
    def test_the_clock_tool_returns_local_times_that_carry_the_utc_offset(self):
        tool = next(tool for tool in Workspace().tools() if tool.name == "get_current_time")
        texts = {zone: json.loads(tool.func(zone))["time"]
                 for zone in ("Asia/Seoul", "America/New_York", "Europe/London")}
        for zone, text in texts.items():
            # the offset is part of the returned text, so the answer can copy it instead of computing it
            self.assertRegex(text, r"[+-]\d\d:\d\d$", zone)
        clocks = {zone: datetime.fromisoformat(text) for zone, text in texts.items()}
        seoul = clocks["Asia/Seoul"]
        self.assertEqual(seoul.utcoffset(), timedelta(hours=9))
        for zone in ("America/New_York", "Europe/London"):
            clock = clocks[zone]
            self.assertIsNotNone(clock.utcoffset(), zone)
            ahead = seoul.utcoffset() - clock.utcoffset()
            # the written local times differ by the same amount (the three calls are a few milliseconds apart)
            written = seoul.replace(tzinfo=None) - clock.replace(tzinfo=None)
            self.assertLess(abs(written - ahead), timedelta(seconds=5), zone)


class PangyoLunchPlanExampleTests(unittest.TestCase):
    """The lunch-plan button takes the date from the clock tool, leaves the weekday out and copies the addresses.

    A live check of the earlier wording showed 'today' written as two different weekdays in one answer and the same
    building number under different road names (single sample).
    """

    def setUp(self):
        self.example = next(e for e in EXAMPLES if e.id == "pangyo-lunch-plan")

    def test_it_keeps_the_forced_place_search_the_tool_flow_and_the_file_check(self):
        example = self.example
        self.assertEqual(example.forced, "web_iq_places")
        self.assertEqual(example.tools, ("web_iq_places", "get_current_time", "write_file"))
        self.assertEqual(example.files, ("lunch_plan.md",))
        self.assertEqual(example.contains, (("lunch_plan.md", r"12\s*[:시]\s*30"),))
        self.assertTrue(example.external)

    def test_the_date_is_the_value_the_clock_tool_returned_and_no_weekday_is_written(self):
        prompt = self.example.prompt
        self.assertIn("get_current_time으로 오늘 날짜를 확인해", prompt)
        self.assertIn("날짜는 도구가 돌려준 값을 그대로 쓰고 요일은 적지 마.", prompt)
        self.assertIn("도구가 요일을 돌려줬다면 그 값만 그대로 써.", prompt)
        # neither a weekday name nor a calendar year is fixed in the prompt: the date comes from the tool
        self.assertNotRegex(prompt, r"[월화수목금토일]요일")
        self.assertNotRegex(prompt, r"\d{4}")

    def test_the_address_is_copied_from_the_search_result_together_with_name_and_source(self):
        prompt = self.example.prompt
        self.assertIn("식당 이름·주소·출처 URL을 함께 넣고", prompt)
        self.assertIn("주소는 검색 결과의 주소 문자열을 그대로 옮겨 줘.", prompt)

    @unittest.skipUnless(have_time_zones(), "this host has no IANA time zone data")
    def test_the_clock_tool_returns_no_weekday_so_the_answer_has_none_to_copy(self):
        tool = next(tool for tool in Workspace().tools() if tool.name == "get_current_time")
        result = json.loads(tool.func("Asia/Seoul"))
        # if the tool ever returns a weekday, the prompt's 'use the returned value' sentence starts to matter
        self.assertEqual(sorted(result), ["time", "timezone"])
        self.assertRegex(result["time"], r"^\d{4}-\d\d-\d\dT")
        self.assertNotRegex(json.dumps(result, ensure_ascii=False), r"[월화수목금토일]요일|Mon|Tue|Wed|Thu|Fri|Sat|Sun")


class CsvChartExampleTests(unittest.TestCase):
    """The chart button shows the chart through the tool card only and keeps SVG text out of the answer.

    A live check of the earlier wording had the final answer carry the SVG text in two of three runs (single sample).
    """

    def setUp(self):
        self.example = next(e for e in EXAMPLES if e.id == "csv-chart")

    def test_it_keeps_the_forced_file_write_the_chart_tool_and_the_answer_checks(self):
        example = self.example
        self.assertEqual(example.forced, "write_file")
        self.assertEqual(example.tools, ("write_file", "chart_table"))
        self.assertEqual(example.files, ("traffic.csv",))
        self.assertEqual(example.artifacts, ("chart",))
        self.assertEqual([pattern for source, pattern in example.contains if source == "answer"],
                         ["2720", "453", "가상|시연용|예시"])

    def test_the_prompt_sends_the_chart_to_the_tool_card_and_forbids_svg_text_in_the_answer(self):
        prompt = self.example.prompt
        self.assertIn("traffic.csv 파일에 저장하고", prompt)
        self.assertIn("막대 차트로 보여 줘.", prompt)
        self.assertIn("차트는 chart_table 도구 결과 카드에서 볼 수 있으니 답변에는 SVG 원문이나 코드를 붙이지 마.", prompt)
        self.assertIn("시연용 가상 데이터라고 표시해 줘.", prompt)


class FixtureTests(unittest.TestCase):
    def test_contract_pdf_is_a_small_three_page_pdf_the_server_can_read(self):
        data = catalog.contract_pdf()
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertLess(len(data), 20_000)
        first = read_pdf(data, "contract.pdf", 1, 1)
        self.assertEqual((first["status"], first["total_pages"], first["next_page"]), ("text_extracted", 3, 2))
        rest = read_pdf(data, "contract.pdf", 2, 2)
        self.assertEqual([page["page"] for page in rest["pages"]], [2, 3])
        self.assertIsNone(rest["next_page"])

    def test_clauses_sit_on_the_pages_the_examples_ask_for(self):
        data = catalog.contract_pdf()
        pages = {page["page"]: re.sub(r"\s+", "", page["text"])
                 for page in read_pdf(data, "contract.pdf", 1, 3)["pages"]}
        self.assertEqual(sorted(pages), [1, 2, 3])
        self.assertIn("시연용가상", pages[1])
        for text in ("제1조", "제2조", "제3조", "330000원", "24개월"):
            self.assertIn(text, pages[1])
        for text in ("제4조", "제5조", "위약금=남은계약개월수×월이용료×30%"):
            self.assertIn(text, pages[2])
        for text in ("제6조", "제7조"):
            self.assertIn(text, pages[3])

    def test_the_penalty_after_twelve_months_is_the_amount_the_checks_expect(self):
        example = next(e for e in EXAMPLES if e.id == "contract-review")
        penalty = (24 - 12) * 330000 * 30 // 100
        self.assertEqual(penalty, 1188000)
        for source, pattern in example.contains:
            if "1188000" in pattern:
                self.assertRegex(str(penalty), pattern)

    def test_discount_script_has_the_bugs_the_example_is_about(self):
        source = catalog.discount_py().decode("utf-8")
        ast.parse(source)
        scope = {}
        exec(compile(source, "discount.py", "exec"), scope)
        self.assertNotEqual(scope["discounted_total"]([1000, 2000], 0.1), 2700)
        with self.assertRaises(ZeroDivisionError):
            scope["average"]([])

    def test_the_expected_fix_passes_the_catalog_content_patterns(self):
        fixed = corrected_discount()
        scope = {}
        exec(compile(fixed, "discount.py", "exec"), scope)
        self.assertAlmostEqual(scope["discounted_total"]([1000, 2000], 0.1), 2700)
        self.assertEqual(scope["average"]([]), 0)
        example = next(e for e in EXAMPLES if e.id == "code-fix-diff")
        self.assertNotEqual(fixed, catalog.discount_py().decode("utf-8"))
        patterns = [p for name, p in example.contains if name == "discount.py"]
        self.assertEqual(len(patterns), 2)
        for pattern in patterns:
            self.assertRegex(fixed, pattern)
            self.assertNotRegex(catalog.discount_py().decode("utf-8"), pattern)


class PolicyTests(unittest.TestCase):
    def test_no_prompt_turns_tools_or_web_search_off(self):
        for example in EXAMPLES:
            with self.subTest(example=example.id):
                self.assertEqual(turn_policy(example.prompt, True),
                                 {"tools": True, "external": True, "denied": [], "reason": "enabled"})


class FirstRoundTests(unittest.IsolatedAsyncioTestCase):
    async def test_round_one_tool_choice_is_the_forced_tool_of_the_catalog(self):
        provider = web_iq_provider()
        await provider.prepare()
        for example in EXAMPLES:
            with self.subTest(example=example.id):
                space, paths = Workspace(), ()
                if example.upload:
                    space.upload(example.upload, catalog.FIXTURES[example.upload]())
                    paths = (example.upload,)
                link = TextLink("fixture answer")
                # a forced tool that the fixture never calls makes the turn fail, which the agent logs with a traceback
                with patch.object(AGENT_LOG, "disabled", True):
                    await run(space, link, example.prompt, attachment_paths=paths, web_iq=provider)
                want = {"type": "function", "function": {"name": example.forced}} if example.forced else "auto"
                self.assertEqual(link.requests[0]["tool_choice"], want)
                names = {tool["function"]["name"] for tool in link.requests[0]["tools"]}
                self.assertLessEqual({*example.tools, *([example.forced] if example.forced else [])}, names)

    async def test_the_contract_example_reads_page_one_before_the_model_is_called(self):
        provider = web_iq_provider()
        space = Workspace()
        space.upload("contract.pdf", catalog.contract_pdf())
        link = TextLink("fixture answer")
        example = next(e for e in EXAMPLES if e.id == "contract-review")
        output = await run(space, link, example.prompt, attachment_paths=("contract.pdf",), web_iq=provider)
        actions = [data for name, data in events(output) if name == "action" and data["state"] == "success"]
        self.assertEqual([a["name"] for a in actions], ["read_pdf"])
        self.assertEqual(actions[0]["arguments"], {"path": "contract.pdf", "start_page": 1, "page_count": 1})
        self.assertEqual(actions[0]["result"]["next_page"], 2)
        first_round = next(data for name, data in events(output) if name == "round")
        self.assertEqual(first_round, {"index": 1})
        self.assertEqual(len(link.requests), 1)


if __name__ == "__main__":
    unittest.main()
