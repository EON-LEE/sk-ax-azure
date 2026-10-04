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
                         "nvfp4_sm80_port.json", "stage_weights.py", "client_tests.py",
                         "build_smoke_checkpoint.py", "verify_suites.py", "ib_probe.py"} <= names)
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
        for suites in re.findall(r"suites=([a-z0-9,]+)", entry):
            with self.subTest(suites=suites):
                self.assertTrue(set(suites.split(",")) <= set(verify_suites.SUITES), suites)
        self.assertIn("--enable-auto-tool-choice", entry)
        self.assertNotIn("NCCL_IB_DISABLE", job, "the IB probe decides the transport")
        diff = json.loads((ROOT / "aml" / "src" / "dsa_port.json").read_text(encoding="utf-8"))[
            "patched_files"]["vllm/v1/attention/backends/mla/triton_mla_sparse.py"]["diff"]
        self.assertIn("+    def supports_batch_invariance(cls) -> bool:", diff)
        self.assertIn("num_kv_splits=1 if envs.VLLM_BATCH_INVARIANT else None", diff)
        for path in (ROOT / "aml" / "jobs").glob("*.yml"):
            text = path.read_text(encoding="utf-8")
            if "instance_count: 2" in text:
                with self.subTest(job=path.name):
                    self.assertIn('IB_PROBE: "1"', text)
                    self.assertNotIn("NCCL_IB_DISABLE", text)

    def test_nvfp4_single_node_plan(self):
        import re
        sys.path.insert(0, str(ROOT / "aml" / "src"))
        try:
            import apply_overlay
            import verify_suites
        finally:
            sys.path.pop(0)
        port = json.loads((ROOT / "aml" / "src" / "nvfp4_sm80_port.json").read_text(encoding="utf-8"))
        self.assertEqual([p["merge_commit"] for p in port["upstream_prs"]],
                         ["a8c86eeb1695a3d35d0c748a68a1451379ea497a", "9eaacb23ec1826ddac31657e0eab699de6de3c59"])
        path = "vllm/model_executor/layers/quantization/modelopt.py"
        self.assertEqual(sorted(port["patched_files"]), [path])
        item = port["patched_files"][path]
        self.assertEqual(item["before_sha256"], "1c58627e479e13aea2ad26c5da122a3a65a7335c3a06a812d4a7f5a20e94ef11")
        self.assertEqual(item["after_sha256"], "10ec868fde521d580b73302facfd7783b455b57f0f1059d41cd1d5bbabcda351")
        pre_image, active = [], False
        for line in item["diff"].splitlines(keepends=True):
            if line.startswith("@@"):
                pre_image.append("# unrelated code\n")
                active = True
            elif active and line[:1] in (" ", "-"):
                pre_image.append(line[1:])
        before = "".join(pre_image)
        after = apply_overlay.apply_unified_diff(before, item["diff"])
        self.assertEqual(after.count("\n") - before.count("\n"), 7)
        self.assertIn("        return 89\n", before)
        self.assertNotIn("        return 89\n", after)
        self.assertIn("        return 80\n", after)
        self.assertIn("        layer.orig_dtype = params_dtype\n", after)
        job = (ROOT / "aml" / "jobs" / "nvfp4-a100-nd96.yml").read_text(encoding="utf-8")
        for needle in ("instance_count: 1", "FULL_REPO: skt/A.X-K2-NVFP4", 'NVFP4_SM80_PORT: "1"', 'PP: "1"',
                       "hf:9e2e804e80f8d1b3afba5d7938173cec1ed46b49"):
            self.assertIn(needle, job)
        self.assertNotIn("VLLM_USE_FLASHINFER_MOE_FP4", job, "FlashInfer FP4 MoE needs Blackwell")
        plan = re.search(r'PHASE_PLAN: "([^"]+)"', job).group(1).split()
        self.assertEqual(plan, ["nvfp4-tp8", "nvfp4-tp8-b2048"])
        entry = (ROOT / "aml" / "src" / "entry.sh").read_text(encoding="utf-8")
        self.assertTrue(set(plan) <= set(re.findall(r"^\s+([a-z0-9-]+)\) model=", entry, re.M)))
        self.assertIn('--repo "${FULL_REPO:-skt/A.X-K2}"', entry)
        self.assertIn('--max-num-batched-tokens "$batched"', entry)
        self.assertIn('os.environ.get("NVFP4_SM80_PORT") == "1"',
                      (ROOT / "aml" / "src" / "apply_overlay.py").read_text(encoding="utf-8"))
        earlier = json.loads((ROOT / "evidence" / "a100-native-vs-dense-full-model.json").read_text(encoding="utf-8"))
        self.assertEqual([p[0] for p in verify_suites.benchmark_plan(65536)],
                         [b["name"] for b in earlier["modes"]["native"]["benchmarks"]],
                         "the latency suite replays the earlier 16-GPU FP8 table")

    def test_nvfp4_single_node_evidence(self):
        import re

        text = (ROOT / "evidence" / "a100-nvfp4-single-node.json").read_text(encoding="utf-8")
        self.assertNotIn("onmicrosoft", text)
        self.assertNotIn("b0af194e", text)
        self.assertNotIn("PENDING", text)
        self.assertIsNone(re.search(r"/subscriptions/(?!0{8}-)", text))
        data = json.loads(text)
        self.assertTrue(data["checkpoint"]["revision"].startswith("9e2e804e"))
        self.assertEqual(data["engine_change"]["applied_in_run"]["mixed_precision_min_capability"], 80)
        self.assertEqual((data["environment"]["compute_capability"], data["environment"]["gpus"]), ([8, 0], 8))
        self.assertEqual({p["phase"]: p["status"] for p in data["phases"]},
                         {"full_download": "passed", "nvfp4-tp8": "passed", "nvfp4-tp8-b2048": "passed"})
        main = data["per_phase"]["nvfp4-tp8"]
        self.assertEqual(main["max_model_len"]["auto_fit"], 254016)
        self.assertEqual(data["per_phase"]["nvfp4-tp8-b2048"]["max_model_len"]["auto_fit"], 262144)
        self.assertEqual((main["functional"]["passed"], main["functional"]["total"]), (4, 4))
        self.assertEqual((main["tools"]["passed"], main["tools"]["total"]), (9, 9))
        self.assertEqual((main["niah"]["hits"], main["niah"]["total"]), (9, 9))
        points = data["doc_fig7_conditions"]["points"]
        self.assertEqual([p["input_tokens"] for p in points], [1024, 2048, 4096, 8192, 16384, 32768])
        for point in points[:3]:
            with self.subTest(isl=point["input_tokens"]):
                self.assertGreater(point["total_vs_doc_b200"], 0.45)
                self.assertGreater(point["per_gpu_total_vs_fp8_16_a100"], 1.9)
        matched = {p["name"]: p for p in data["latency_and_throughput_vs_fp8_16_gpus"]["nvfp4-tp8-b2048"]["points"]}
        self.assertGreaterEqual(matched["throughput-c32"]["output_tok_s_ratio"], 1.0)
        self.assertLess(matched["throughput-c128"]["output_tok_s_ratio"], 0.9)
        logs = data["from_vllm_logs"]
        self.assertEqual(logs["nvfp4-tp8"]["kv_cache_tokens"], 254016)
        self.assertEqual(logs["nvfp4-tp8-b2048"]["kv_cache_tokens"], 272960)
        selection = "\n".join(logs["nvfp4-tp8"]["selection"])
        self.assertIn("'MARLIN' NvFp4", selection)
        self.assertIn("TRITON_MLA_SPARSE", selection)
        cost = data["cost_per_million_output_tokens_usd"]
        c32, c128 = cost["concurrency_32_input_1k_output_1k"], cost["concurrency_128_input_1k_output_256"]
        for nvfp4, fp8 in ((c32["nvfp4_1_node"], c32["fp8_2_nodes"]),
                           (c128["nvfp4_1_node_batched_2048"], c128["fp8_2_nodes_batched_2048"])):
            for meter, price in nvfp4.items():
                self.assertLess(price, fp8[meter])
        verification = json.loads((ROOT / "evidence" / "a100-verification-and-doc-speed.json").read_text(encoding="utf-8"))
        self.assertIn("nvfp4_checkpoint", verification["not_possible_on_a100"], "the original record is kept")
        correction = verification["corrections"][0]
        self.assertEqual(correction["field"], "not_possible_on_a100.nvfp4_checkpoint")
        self.assertEqual(correction["evidence"], "evidence/a100-nvfp4-single-node.json")

    def test_verification_evidence(self):
        data = json.loads((ROOT / "evidence" / "a100-verification-and-doc-speed.json").read_text(encoding="utf-8"))
        for node in ("node0", "node1"):
            probe = data["interconnect_probe"][node]
            self.assertEqual((probe["tcp"]["transport"], probe["ib"]["transport"]), ("Socket", "IB"))
            self.assertTrue(probe["ib"]["gpudirect_rdma"])
            self.assertGreater(probe["ib"]["send_recv_GBps"], 20 * probe["tcp"]["send_recv_GBps"])
        for phase in ("native-pp2", "dense-pp2"):
            review = data["tool_calling_review"][phase]
            self.assertEqual((review["behaviour_correct"], review["total"]), (9, 9))
        native = data["per_phase"]["native-pp2"]["niah"]["by_length"]
        dense = data["per_phase"]["dense-pp2"]["niah"]["by_length"]
        self.assertEqual([native[k]["hits"] for k in ("32768", "131072", "262144")], [3, 3, 3])
        self.assertEqual([dense[k]["hits"] for k in ("32768", "131072", "262144")], [3, 0, 0])
        status = {p["phase"]: p["status"] for p in data["phases"]}
        self.assertEqual(status["native-pp2-bi"], "failed")
        self.assertEqual(status["dense-pp2-bi"], "failed")
        causes = data["from_vllm_logs"]["startup_failure_root_causes"]
        self.assertTrue(any("No FP8 MoE backend" in line for line in causes["native-pp2-bi"]))
        self.assertTrue(any("reorder_batch_threshold" in line for line in causes["dense-tp16-eagle3-compiled"]))
        points = data["doc_speed_comparison"]["points"]
        self.assertEqual([p["input_tokens"] for p in points], [1024, 2048, 4096, 8192, 16384, 32768, 65536, 120000])
        for point in points:
            with self.subTest(isl=point["input_tokens"]):
                self.assertEqual(point["native"]["returncode"], 0)
                self.assertLess(point["native_total_vs_doc"], point["dense_total_vs_doc"])
                self.assertLess(point["dense_total_vs_doc"], 0.7)
        self.assertLess(points[-1]["native_total_vs_doc"], points[3]["native_total_vs_doc"])
        self.assertEqual(data["from_vllm_logs"]["kv_cache_tokens"]["native-pp2"], 673792)
        self.assertLess(data["speculative_decoding"]["acceptance"]["mean_acceptance_length"], 1.2)
        for row in data["speculative_decoding"]["points"]:
            self.assertLess(row["eagle3_speedup_total"], 1)
        text = (ROOT / "evidence" / "a100-verification-and-doc-speed.json").read_text(encoding="utf-8")
        self.assertNotIn("onmicrosoft", text)
        self.assertNotIn("b0af194e", text)

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
