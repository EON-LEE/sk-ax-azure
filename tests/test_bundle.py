import ast
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class BundleTests(unittest.TestCase):
    def test_python_and_json_parse(self):
        for path in (ROOT / "experiments").glob("*.py"):
            with self.subTest(path=path.name):
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for path in (ROOT / "evidence").glob("*.json"):
            with self.subTest(path=path.name):
                json.loads(path.read_text(encoding="utf-8"))

    def test_cloud_scripts_are_disabled_by_default(self):
        environment = os.environ.copy()
        for name in ("AXK2_ENABLE_LEGACY_CLOUD", "AZURE_SUBSCRIPTION_ID", "AZURE_CLI"):
            environment.pop(name, None)
        scripts = [
            "axk2_dist_remote.py", "axk2_remote_experiment.py", "run_axk2_remote.py",
            "provision_a100_smoke.py", "provision_axk2_distributed.py",
            "cleanup_axk2_distributed.py", "request_sweden_spot_quota.py",
        ]
        for name in scripts:
            with self.subTest(script=name):
                result = subprocess.run(
                    [sys.executable, str(ROOT / "experiments" / name)],
                    env=environment, capture_output=True, text=True, timeout=10,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Historical cloud scripts are disabled", result.stderr)

    def test_evidence_preserves_scope_and_failures(self):
        def read(name):
            return json.loads((ROOT / "evidence" / name).read_text(encoding="utf-8"))

        distributed = read("dist-rank1-result.json")
        self.assertEqual(len(distributed["cases"]), 7)
        for case in distributed["cases"]:
            self.assertTrue(case["distributed_vs_serial_prefill"]["passed"])
            self.assertEqual(case["distributed_vs_serial_decode"]["max_absolute_error"], 0)
        cpu = read("axk2-request-pipeline-cpu-result.json")
        self.assertIn("CPU only", cpu["scope"])
        self.assertTrue(cpu["greedy_token_ids_equal_official_serial_model"])
        self.assertFalse(read("axk2-chunking-diagnostic.json")["official_full_vs_chunked_passed_at_1e4"])
        self.assertEqual(
            read("quota-support-escalation.json")["subscription_id"],
            "00000000-0000-0000-0000-000000000000",
        )


if __name__ == "__main__":
    unittest.main()
