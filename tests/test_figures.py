import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "docs" / "report"

import sys
sys.path.insert(0, str(REPORT))
try:
    import make_figures
finally:
    sys.path.pop(0)


class FigureDataTests(unittest.TestCase):
    def test_load_throughput_series(self):
        data = make_figures.load_throughput()
        series = data["series"]
        self.assertEqual(len(series["b200_fp8_k2"]["points"]), 8)
        self.assertEqual(len(series["a100_fp8_native"]["points"]), 8)
        self.assertEqual(len(series["a100_fp8_dense"]["points"]), 8)
        self.assertEqual(len(series["a100_nvfp4"]["points"]), 6)
        native = {p["input_tokens"]: p for p in series["a100_fp8_native"]["points"]}
        dense = {p["input_tokens"]: p for p in series["a100_fp8_dense"]["points"]}
        nvfp4 = {p["input_tokens"]: p for p in series["a100_nvfp4"]["points"]}
        b200 = {p["input_tokens"]: p for p in series["b200_fp8_k2"]["points"]}
        self.assertAlmostEqual(native[1024]["total_token_throughput"], 814.5934910317782)
        self.assertAlmostEqual(dense[8192]["total_token_throughput"], 3070.3062463058586)
        self.assertAlmostEqual(nvfp4[32768]["total_token_throughput"], 1674.0)
        self.assertEqual(b200[120000]["total_tok_s"], 12300.0)
        self.assertAlmostEqual(native[8192]["total_token_throughput"] / b200[8192]["total_tok_s"], 0.5050629, places=5)
        self.assertAlmostEqual((nvfp4[1024]["total_token_throughput"] / 8) / (native[1024]["total_token_throughput"] / 16), 2.2708, places=3)

    def test_published_json_shape(self):
        payload = json.loads((REPORT / "skt_published.json").read_text(encoding="utf-8"))
        self.assertIn("series", payload)
        required = {"source", "hardware", "gpu_count", "conditions", "points"}
        for name, item in payload["series"].items():
            with self.subTest(name=name):
                self.assertTrue(required <= set(item))
                self.assertGreater(len(item["source"]), 10)
                self.assertIsInstance(item["gpu_count"], int)
                self.assertTrue(item["points"])
                for point in item["points"]:
                    self.assertIn("input_tokens", point)
                    self.assertIn("total_tok_s", point)
                    self.assertIn("output_tok_s", point)
        fig9 = payload["series"]["b200_nvfp4_k2_fig9"]
        self.assertEqual(fig9["gpu_count"], 4)
        self.assertIn("SERVING_DESIGN", fig9["conditions"])
        self.assertEqual([p["total_tok_s"] for p in fig9["points"][:3]], [2300, 3300, 4800])

    def test_render_outputs_when_matplotlib_available(self):
        if importlib.util.find_spec("matplotlib") is None:
            self.skipTest("matplotlib is not importable")
        base = REPORT / "figures"
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="_test_figures_", dir=base) as tmp:
            out = Path(tmp)
            make_figures.render_all(out)
            expected = {
                "fig7-overlay.png",
                "fig9-overlay.png",
                "per-gpu-throughput.png",
                "cost-per-1m-output-tokens.png",
                "throughput.json",
                "throughput-tables.md",
            }
            self.assertEqual(expected, {p.name for p in out.iterdir()})
            for name in expected:
                path = out / name
                self.assertGreater(path.stat().st_size, 100)
            for name in [n for n in expected if n.endswith(".png")]:
                self.assertLess((out / name).stat().st_size, 400_000)

class BenchmarkTests(unittest.TestCase):
    def evidence(self, folder):
        path = Path(folder) / "a100-benchmarks.json"
        path.write_text(json.dumps({"run": "ev-test", "summary": {
            "aime": {"score": 0.9, "ci95_half": 0.05, "n_items": 30, "repeats": 8},
            "kobalt": {"score": 0.7, "ci95_half": 0.03, "n_items": 700, "repeats": 1},
            "niah": {"score": 1.0, "ci95_half": None, "grid": {"8192": {"0.5": True}}}}}), encoding="utf-8")
        return path

    def test_rows_merge_published_and_measured(self):
        with tempfile.TemporaryDirectory() as tmp:
            bench = make_figures.load_benchmarks(self.evidence(tmp))
        rows = {r["benchmark"]: r for r in bench["rows"]}
        self.assertEqual(rows["AIME26"]["a100"], 90.0)
        self.assertEqual(rows["AIME26"]["a100_ci95"], 5.0)
        self.assertEqual(rows["AIME26"]["skt"], 97.1)
        self.assertIsNone(rows["CLIcK"]["a100"])
        self.assertIsNone(rows["GPQA Diamond"]["a100"])
        self.assertEqual(bench["niah"]["a100"], 100.0)
        self.assertEqual(bench["run"], "ev-test")

    def test_table_and_missing_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            bench = make_figures.load_benchmarks(self.evidence(tmp))
            make_figures.write_benchmark_table(bench, Path(tmp))
            table = (Path(tmp) / "benchmark-table.md").read_text(encoding="utf-8")
            empty = make_figures.load_benchmarks(Path(tmp) / "absent.json")
        self.assertIn("| Math | AIME26 | 97.1 | **90.0** ± 5.0 |", table)
        self.assertIn("| Korean | CLIcK | 91.6 | pending |", table)
        self.assertIn("| Science & Knowledge | GPQA Diamond | 85.6 | – (gated) |", table)
        self.assertIn("| Long context | NIAH | 100 | **100.0** |", table)
        self.assertTrue(all(r["a100"] is None for r in empty["rows"]))
        self.assertIsNone(empty["run"])

    def test_partial_run_and_niah_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a100-benchmarks.json"
            path.write_text(json.dumps({"run": "ev-part", "summary": {
                "aime": {"score": 1.0, "ci95_half": 0.0, "n_items": 10, "items_expected": 30, "repeats": 2,
                         "n_generations": 10, "generations_expected": 60},
                "click": {"score": 0.855, "ci95_half": 0.061, "n_items": 131, "items_expected": 200, "repeats": 1,
                          "n_generations": 131, "generations_expected": 200}}}), encoding="utf-8")
            verification = Path(tmp) / "verification.json"
            verification.write_text(json.dumps({"per_phase": {"native-pp2": {"niah": {
                "hits": 9, "total": 9, "by_length": {"32768": {}, "131072": {}, "262144": {}}}}}}), encoding="utf-8")
            bench = make_figures.load_benchmarks(path, niah_fallback=verification)
            make_figures.write_benchmark_table(bench, Path(tmp))
            table = (Path(tmp) / "benchmark-table.md").read_text(encoding="utf-8")
            without = make_figures.load_benchmarks(path)
        self.assertIn("| Math | AIME26 | 97.1 | **100.0** (n=10/60 gen) |", table)
        self.assertIn("| Korean | CLIcK | 91.6 | **85.5** ± 6.1 (n=131/200) |", table)
        self.assertIn("| Long context | NIAH | 100 | **100.0** (9/9, 32K, 128K, 256K) |", table)
        self.assertIn("stopped early", table)
        self.assertIn("earlier verification run", table)
        self.assertIsNone(without["niah"]["a100"])

    def test_render_benchmarks(self):
        if importlib.util.find_spec("matplotlib") is None:
            self.skipTest("matplotlib is not importable")
        with tempfile.TemporaryDirectory() as tmp:
            bench = make_figures.load_benchmarks(self.evidence(tmp))
            self.assertTrue(make_figures.render_benchmarks(bench, Path(tmp)))
            self.assertGreater((Path(tmp) / "benchmarks-vs-published.png").stat().st_size, 10_000)
            self.assertFalse(make_figures.render_benchmarks(make_figures.load_benchmarks(Path(tmp) / "x.json"), Path(tmp)))

if __name__ == "__main__":
    unittest.main()
