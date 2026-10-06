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


if __name__ == "__main__":
    unittest.main()
