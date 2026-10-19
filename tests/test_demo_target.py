"""Subscription isolation and zero-cost idle contracts; no Azure calls."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("demo_target", ROOT / "demo/target.py")
target = importlib.util.module_from_spec(spec)
spec.loader.exec_module(target)

SUB = "11111111-1111-1111-1111-111111111111"


class TargetTests(unittest.TestCase):
    def test_invalid_target_names(self):
        for name in ("../source", "UPPER", "a", "has-dash"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                target.validate_config({"name": name, "subscription_id": SUB})

    def test_foreign_group_rejected(self):
        with self.assertRaises(ValueError):
            target.validate_config({"name": "targetdemo", "subscription_id": SUB, "resource_group": "source"})

    def plan(self, value, actions=None):
        return {"resource_changes": [{"change": {"before": None, "after": value,
                                                 "actions": actions or ["create"]}}]}

    def test_plan_target_only(self):
        target.safe_plan(self.plan({"id": f"/subscriptions/{SUB}/resourceGroups/rg-target",
                                    "resource_group_name": "rg-target"}), SUB, "rg-target")

    def test_plan_foreign_subscription_rejected(self):
        with self.assertRaises(ValueError):
            target.safe_plan(self.plan({"id": "/subscriptions/other/resourceGroups/rg-target"}), SUB, "rg-target")

    def test_plan_prefix_collision_rejected(self):
        with self.assertRaises(ValueError):
            target.safe_plan(self.plan({"id": f"/subscriptions/{SUB}/resourceGroups/rg-target-source"}), SUB, "rg-target")

    def test_deletion_and_replacement_rejected(self):
        for actions in (["delete"], ["delete", "create"]):
            with self.subTest(actions=actions), self.assertRaises(ValueError):
                target.safe_plan(self.plan({}, actions), SUB, "rg-target")

    def test_bundle_full_demo_no_env_or_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "frontend.zip"
            expected = target.bundle(ROOT, archive)
            with zipfile.ZipFile(archive) as value:
                self.assertIn("aml/jobs/demo-fp8-nd96.yml", value.namelist())
                self.assertIn("aml/jobs/demo-nvfp4-nd96.yml", value.namelist())
                self.assertIn("comparison.py", value.namelist())
                self.assertIn("static/chat.js", value.namelist())
                self.assertNotIn("hosted_model.py", value.namelist())
                self.assertFalse(any(".env" in name or "__pycache__" in name for name in value.namelist()))
                self.assertEqual(len(expected), len(value.namelist()))

    def test_private_atomic_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret.json"
            target.write_private(path, {"sample": "private"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text()), {"sample": "private"})

    def test_all_azure_calls_explicit_subscription(self):
        instance = object.__new__(target.Target)
        instance.sub, instance.env = SUB, {}
        with patch.object(target.subprocess, "run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "{}"
            instance.az("account", "show")
            self.assertEqual(run.call_args.args[0][-4:], ["--subscription", SUB, "-o", "json"])

    def test_run_requires_bounded_lease(self):
        instance = object.__new__(target.Target)
        for seconds in (0, 59, 7201):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                instance.run(seconds)

    def test_unbounded_on_requires_explicit_opt_in(self):
        instance = object.__new__(target.Target)
        with self.assertRaises(ValueError):
            instance.on(False)

    def test_run_cleanup_on_start_failure(self):
        instance = object.__new__(target.Target)
        with patch.object(instance, "power", side_effect=RuntimeError("start failed")), \
                patch.object(instance, "off") as off:
            with self.assertRaises(RuntimeError):
                instance.run(60)
            off.assert_called_once()

    def test_power_attempts_both_profiles_even_on_failure(self):
        instance = object.__new__(target.Target)
        instance.url = "https://target.invalid"
        with patch.object(instance, "request", side_effect=[
                target.urllib.error.URLError("offline"), {"status": {"desired": "off"}}]) as request:
            with self.assertRaises(RuntimeError):
                instance.power("off")
            self.assertEqual(request.call_count, 2)
            self.assertIn("/models/nvfp4/", request.call_args.args[0])

    def test_infrastructure_idle_and_single_region(self):
        text = (ROOT / "demo/infra/main.tf").read_text()
        self.assertIn("min_node_count", text)
        self.assertRegex(text, r"min_node_count\s*=\s*0")
        self.assertRegex(text, r"max_node_count\s*=\s*3")
        self.assertIn('vm_priority                   = "LowPriority"', text)
        self.assertNotIn("italynorth", text)
        self.assertNotIn("francecentral", text)
        self.assertIn("shared_access_key_enabled", text)
        self.assertRegex(text, r"shared_access_key_enabled\s*=\s*false")
        self.assertRegex(text, r"allow_nested_items_to_be_public\s*=\s*false")


if __name__ == "__main__":
    unittest.main()
