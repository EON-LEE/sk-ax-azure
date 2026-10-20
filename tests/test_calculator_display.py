"""Calculator display: a result that is not an integer comes with a decimal approximation, the exact fraction stays.

The model replies are scripted fixtures; they say nothing about how the live model uses the approximation."""
import json
import sys
import unittest
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "demo" / "frontend"))
from workspace import Workspace, approximation, calculate
sys.path.pop(0)
from tests.test_web_search_latency import ScriptedLink, answer_round, tool_round, turn


class ApproximationTests(unittest.TestCase):
    def test_non_integers_get_the_fraction_and_four_decimals(self):
        cases = {
            "125/3": "125/3 (≈ 41.6667)",
            "-125/3": "-125/3 (≈ -41.6667)",
            "1/3": "1/3 (≈ 0.3333)",
            "2/3": "2/3 (≈ 0.6667)",
            "0.1+0.2": "3/10 (≈ 0.3)",
            "1/8": "1/8 (≈ 0.125)",
            "12345679/100": "12345679/100 (≈ 123456.79)",
            "99999/100000": "99999/100000 (≈ 1)",
        }
        for expression, expected in cases.items():
            with self.subTest(expression=expression):
                self.assertEqual(calculate(expression, approximate=True), expected)

    def test_integers_and_the_default_stay_exactly_as_they_were(self):
        for expression, expected in {"2^64-1": "18446744073709551615", "10/2": "5", "-4/2": "-2", "0": "0",
                                     "7/2*100": "350"}.items():
            with self.subTest(expression=expression):
                self.assertEqual(calculate(expression), expected)
                self.assertEqual(calculate(expression, approximate=True), expected)
        for expression, expected in {"125/3": "125/3", "0.1+0.2": "3/10", "1/30": "1/30"}.items():
            with self.subTest(expression=expression):
                self.assertEqual(calculate(expression), expected)

    def test_rounding_is_half_away_from_zero_and_uses_no_floats(self):
        self.assertEqual(approximation(Fraction(2469, 20000)), "0.1235")  # 0.12345
        self.assertEqual(approximation(Fraction(-2469, 20000)), "-0.1235")
        self.assertEqual(approximation(Fraction(2468, 20000)), "0.1234")
        self.assertEqual(approximation(Fraction(10 ** 20 + 1, 3)), "33333333333333333333.6667")  # no float can hold this

    def test_small_values_keep_four_significant_digits(self):
        self.assertEqual(approximation(Fraction(1, 30)), "0.03333")
        self.assertEqual(approximation(Fraction(1, 30000)), "0.00003333")
        self.assertEqual(approximation(Fraction(-7, 300)), "-0.02333")
        self.assertEqual(approximation(Fraction(1, 10 ** 31)), "0." + "0" * 30 + "1")

    def test_values_outside_the_display_range_show_only_the_fraction(self):
        self.assertIsNone(approximation(Fraction(1, 10 ** 32)))
        self.assertIsNone(approximation(Fraction(2 * 10 ** 30 + 1, 2)))
        self.assertEqual(approximation(Fraction(2 * 10 ** 29 + 1, 2)), "1" + "0" * 29 + ".5")
        self.assertEqual(calculate("1/10^32", approximate=True), "1/" + "1" + "0" * 32)
        self.assertEqual(calculate("10^30+1/2", approximate=True), "2" + "0" * 29 + "1/2")

    def test_failures_stay_failures(self):
        for expression in ("__import__('os')", "2^100000", "1/0", "True+1"):
            with self.subTest(expression=expression), self.assertRaises((ValueError, ZeroDivisionError)):
                calculate(expression, approximate=True)


class CalculatorToolTests(unittest.IsolatedAsyncioTestCase):
    async def calculate(self, expression):
        link = ScriptedLink([tool_round("calculator", {"expression": expression}), answer_round("약 41.7입니다.")])
        _, events = await turn(link, Workspace(), text="계산기로 계산해줘: " + expression)
        action = next(data for name, data in events if name == "action" and data["state"] == "success")
        return action, link.requests

    async def test_the_card_and_the_model_get_the_fraction_with_its_approximation(self):
        action, requests = await self.calculate("125/3")
        self.assertEqual(action["result"], {"expression": "125/3", "result": "125/3 (≈ 41.6667)"})
        read = [message["content"] for message in requests[1]["messages"] if message["role"] == "tool"]
        self.assertEqual(len(read), 1)
        self.assertIn("125/3 (≈ 41.6667)", read[0])
        self.assertNotIn("\\u2248", read[0])
        self.assertEqual(json.loads(read[0])["result"], "125/3 (≈ 41.6667)")

    async def test_an_integer_result_is_unchanged(self):
        action, requests = await self.calculate("(69000*24)*3/4 - 100000")
        self.assertEqual(action["result"], {"expression": "(69000*24)*3/4 - 100000", "result": "1142000"})
        read = [message["content"] for message in requests[1]["messages"] if message["role"] == "tool"]
        self.assertEqual(json.loads(read[0]), {"expression": "(69000*24)*3/4 - 100000", "result": "1142000"})

    def test_the_tool_description_says_the_approximation_is_for_display_only(self):
        tool = next(tool for tool in Workspace().tools() if tool.name == "calculator")
        description = tool.to_json_schema_spec()["function"]["description"]
        self.assertIn("근사는 표시용", description)
        self.assertIn("Exact rational arithmetic", description)


if __name__ == "__main__":
    unittest.main()
