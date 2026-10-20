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
from types import SimpleNamespace
from unittest.mock import patch

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


# the chat buttons, in the order of the start screen; contract-review and code-fix-diff need an attachment and
# axk2-public-research waits for a live re-check, so none of the three is a button
BUTTONS = ("time-zones", "plan-fee", "ai-chip-news", "chip-price-ratio",
           "foundry-byom-steps", "pangyo-lunch-plan", "csv-chart", "monthly-sales-report")


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
