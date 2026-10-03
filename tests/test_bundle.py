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

    def test_aml_bundle(self):
        for path in (ROOT / "aml").rglob("*.py"):
            with self.subTest(path=path.name):
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for path in [*(ROOT / "aml").glob("*.sh"), *(ROOT / "aml" / "src").glob("*.sh")]:
            with self.subTest(path=path.name):
                self.assertNotIn(b"\r\n", path.read_bytes(), "shell scripts must keep LF line endings")
        manifest = json.loads((ROOT / "aml" / "src" / "overlay_manifest.json").read_text(encoding="utf-8"))
        statuses = [item["status"] for item in manifest["files"]]
        self.assertEqual((statuses.count("modified"), statuses.count("added")), (21, 4))
        for item in manifest["files"]:
            self.assertTrue(item["path"].startswith("vllm/") and item["path"].endswith(".py"))
            self.assertEqual(item["base_sha256"] is None, item["status"] == "added")

    def test_dsa_port_manifest_and_diff_applier(self):
        sys.path.insert(0, str(ROOT / "aml" / "src"))
        try:
            import apply_overlay
        finally:
            sys.path.pop(0)
        port = json.loads((ROOT / "aml" / "src" / "dsa_port.json").read_text(encoding="utf-8"))
        self.assertEqual(port["pr_commit"], "3740c02bb1223d37823593664ae3eafa397b9937")
        self.assertEqual(len(port["new_files"]), 5)
        self.assertEqual(
            sorted(port["patched_files"]),
            sorted(["vllm/model_executor/layers/sparse_attn_indexer.py", "vllm/platforms/cuda.py",
                    "vllm/v1/attention/backends/mla/indexer.py", "vllm/v1/attention/backends/registry.py",
                    "vllm/v1/attention/backends/mla/triton_mla_sparse.py"]))
        for path, item in port["patched_files"].items():
            with self.subTest(path=path):
                self.assertRegex(item["before_sha256"], r"^[0-9a-f]{64}$")
                self.assertRegex(item["after_sha256"], r"^[0-9a-f]{64}$")
                self.assertIn("\n@@ ", item["diff"])
        guard = port["patched_files"]["vllm/v1/attention/backends/mla/triton_mla_sparse.py"]["diff"]
        self.assertIn("return capability.major not in (9, 10)", guard)
        diff = "--- a/x\n+++ b/x\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"
        self.assertEqual(apply_overlay.apply_unified_diff("a\nb\nc\nd\n", diff), "a\nB\nc\nd\n")
        with self.assertRaises(ValueError):
            apply_overlay.apply_unified_diff("a\nb\nc\na\nb\nc\n", diff)

    def test_rendered_payload_round_trip(self):
        import base64
        import io
        import tarfile
        sys.path.insert(0, str(ROOT / "aml"))
        try:
            import render_job
        finally:
            sys.path.pop(0)
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(render_job.payload())), mode="r:gz") as tar:
            names = set(tar.getnames())
            entry = tar.extractfile("entry.sh").read()
        self.assertTrue({"entry.sh", "apply_overlay.py", "overlay_manifest.json", "dsa_port.json",
                         "client_tests.py", "build_smoke_checkpoint.py", "verify_suites.py", "ib_probe.py"} <= names)
        self.assertNotIn(b"\r\n", entry)
        self.assertLess(len(render_job.payload()), 100_000, "payload travels in one environment variable")

    def test_verification_plan(self):
        import re
        sys.path.insert(0, str(ROOT / "aml" / "src"))
        try:
            import verify_suites
        finally:
            sys.path.pop(0)
        self.assertEqual(sorted(verify_suites.DOC_FIG7), [1024, 2048, 4096, 8192, 16384, 32768, 65536, 120000])
        self.assertEqual(sorted(verify_suites.DOC_FIG8_TOTAL), sorted(verify_suites.DOC_FIG7))
        for isl, (total, output) in verify_suites.DOC_FIG7.items():
            self.assertGreater(total, output)
            self.assertGreater(verify_suites.DOC_FIG8_TOTAL[isl], total, "EAGLE3 raises throughput in Fig. 8")
        job = (ROOT / "aml" / "jobs" / "verify-remaining-nd96-hub.yml").read_text(encoding="utf-8")
        plan = re.search(r'PHASE_PLAN: "([^"]+)"', job).group(1).split()
        entry = (ROOT / "aml" / "src" / "entry.sh").read_text(encoding="utf-8")
        handled = set(re.findall(r"^\s+([a-z0-9-]+)\) model=", entry, re.M))
        self.assertTrue(set(plan) <= handled, set(plan) - handled)
        self.assertIn("--enable-auto-tool-choice", entry)
        self.assertNotIn("NCCL_IB_DISABLE", job, "the IB probe decides the transport")
        diff = json.loads((ROOT / "aml" / "src" / "dsa_port.json").read_text(encoding="utf-8"))[
            "patched_files"]["vllm/v1/attention/backends/mla/triton_mla_sparse.py"]["diff"]
        self.assertIn("+    def supports_batch_invariance(cls) -> bool:", diff)
        self.assertIn("num_kv_splits=1 if envs.VLLM_BATCH_INVARIANT else None", diff)

    def test_new_evidence(self):
        def read(name):
            return json.loads((ROOT / "evidence" / name).read_text(encoding="utf-8"))

        dense = read("axk2-dense-equivalence-cpu.json")
        self.assertTrue(dense["passed"])
        self.assertEqual(dense["index_topk"], 8)
        snapshot = json.dumps(read("azure-capacity-snapshot.json"))
        for part in snapshot.split("/subscriptions/")[1:]:
            self.assertTrue(part.startswith("00000000-0000-0000-0000-000000000000"))
        fork = read("skt-vllm-fork-review.json")
        self.assertEqual(fork["compare"], {"ahead_by": 29, "behind_by": 0, "files_changed": 47})

    def test_a100_run_evidence(self):
        def read(name):
            return json.loads((ROOT / "evidence" / name).read_text(encoding="utf-8"))

        full = read("a100-full-model-tp8-pp2-results.json")
        summary = full["full_model_tests"]["summary"]
        self.assertEqual((summary["functional_passed"], summary["functional_total"]), (6, 6))
        self.assertTrue(summary["reasoning_passed"])
        self.assertEqual(summary["arithmetic"], "14/20")
        self.assertFalse(summary["deterministic"], "keep the observed non-determinism on record")
        self.assertEqual([b["completed"] for b in full["full_model_tests"]["benchmarks"]], [6, 32, 96])
        self.assertEqual(len(full["overlay"]["patches"]), 2)
        self.assertTrue(any("TRITON_MLA" in line for line in full["kernels_and_memory_from_vllm_log"]))
        comparison = read("a100-2layer-vs-official.json")["comparison"]
        for section in ("vllm_single_node_vs_official", "vllm_multi_node_vs_official"):
            for prompt in comparison[section].values():
                self.assertGreater(prompt["within_index_topk"]["chosen_logprob"]["pearson"], 0.9999)
        attempts = read("a100-deployment-attempts.json")
        self.assertIn("deleted", attempts["cleanup"])
        for name in ("a100-full-model-tp8-pp2-results.json", "a100-2layer-vs-official.json", "a100-deployment-attempts.json"):
            text = (ROOT / "evidence" / name).read_text(encoding="utf-8")
            self.assertNotIn("onmicrosoft", text)
            self.assertNotRegex(text, r"/subscriptions/(?!0{8}-)")

    def test_native_dsa_evidence(self):
        def read(name):
            return json.loads((ROOT / "evidence" / name).read_text(encoding="utf-8"))

        two = read("a100-native-dsa-2layer-vs-official.json")
        self.assertEqual(two["kernel_tests_on_a100"], ["94 passed in 108.43s (0:01:48)"])
        summary = two["summary_vs_official"]
        native = summary["node0.smoke-native-tp8"]
        dense = summary["node0.smoke-dense-tp8"]
        self.assertGreater(native["beyond_index_topk"]["positions"], 7000)
        self.assertLess(native["beyond_index_topk"]["mean_abs_diff"], dense["beyond_index_topk"]["mean_abs_diff"])
        self.assertAlmostEqual(native["within_index_topk"]["mean_abs_diff"],
                               dense["within_index_topk"]["mean_abs_diff"], places=4)
        pair = two["pairs"]["node0.smoke-dense-tp8 vs node0.smoke-native-tp8"]
        self.assertGreater(pair["beyond_index_topk"]["mean_abs_diff"], 2 * pair["within_index_topk"]["mean_abs_diff"])
        self.assertTrue(any("TRITON_MLA_SPARSE" in line
                            for line in two["backend_selection_from_vllm_logs"]["smoke-native-tp8-pp2"]))
        full = read("a100-native-vs-dense-full-model.json")
        self.assertEqual(sorted(full["modes"]), ["dense", "native"])
        for mode, data in full["modes"].items():
            with self.subTest(mode=mode):
                self.assertEqual((data["summary"]["functional_passed"], data["summary"]["needle_found"]), (6, "12/12"))
                self.assertTrue(all(b["returncode"] == 0 for b in data["benchmarks"]))
        self.assertTrue(any("TRITON_MLA_SPARSE" in line
                            for line in full["backend_selection_from_vllm_logs"]["full-native-tp8-pp2"]))
        self.assertFalse(any("TRITON_MLA_SPARSE" in line
                             for line in full["backend_selection_from_vllm_logs"]["full-dense-tp8-pp2"]))
        for name in ("a100-native-dsa-2layer-vs-official.json", "a100-native-vs-dense-full-model.json",
                     "a100-native-dsa-deployment-log.json"):
            text = (ROOT / "evidence" / name).read_text(encoding="utf-8")
            self.assertNotIn("onmicrosoft", text)
            self.assertNotIn("b0af194e", text)


if __name__ == "__main__":
    unittest.main()
