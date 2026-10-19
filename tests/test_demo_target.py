"""Subscription isolation and zero-cost idle contracts; no Azure calls."""
import importlib.util
import contextlib
import hashlib
import io
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

    def test_model_key_private_idempotent_and_rotation_staged_only(self):
        with tempfile.TemporaryDirectory() as directory:
            instance = object.__new__(target.Target)
            instance.directory = Path(directory)
            output = io.StringIO()
            with contextlib.redirect_stdout(output), patch.object(instance, "az") as az:
                instance.model_key()
                key_path = instance.directory / "model-api-backend.key"
                first = key_path.read_text().strip()
                self.assertEqual(len(bytes.fromhex(first)), 32)
                self.assertEqual(key_path.stat().st_mode & 0o777, 0o600)
                instance.model_key()
                self.assertEqual(key_path.read_text().strip(), first)
                instance.model_key(rotate=True)
                second = key_path.read_text().strip()
                self.assertNotEqual(first, second)
                setting_path = instance.directory / "model-api-settings.json"
                digests = json.loads(setting_path.read_text())["AXK2_MODEL_API_KEY_SHA256"].split(",")
                self.assertEqual(digests, [hashlib.sha256(value.encode()).hexdigest()
                                          for value in (second, first)])
                with self.assertRaises(ValueError):
                    instance.model_key(rotate=True)
                instance.model_key(retire_previous=True)
                self.assertEqual(json.loads(setting_path.read_text())["AXK2_MODEL_API_KEY_SHA256"],
                                 digests[0])
                az.assert_not_called()
            for secret in (first, second, *digests):
                self.assertNotIn(secret, output.getvalue())

    def test_model_key_refuses_insecure_existing_key(self):
        with tempfile.TemporaryDirectory() as directory:
            instance = object.__new__(target.Target)
            instance.directory = Path(directory)
            path = instance.directory / "model-api-backend.key"
            path.write_text("a" * 64)
            path.chmod(0o644)
            with self.assertRaises(PermissionError):
                instance.model_key()

    def test_revision_deploy_requires_explicit_recycle_opt_in(self):
        instance = object.__new__(target.Target)
        with patch.object(instance, "az") as az, patch.object(instance, "power") as power:
            with self.assertRaises(ValueError):
                instance.deploy_revision("a" * 40)
            az.assert_not_called()
            power.assert_not_called()

    def test_revision_bundle_rejects_branch_or_abbreviated_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            for revision in ("main", "a" * 7, "A" * 40, None):
                with self.subTest(revision=revision), self.assertRaises(ValueError):
                    target.bundle_revision(revision, Path(directory) / "candidate.zip")

    def test_exact_revision_bundle_matches_committed_frontend(self):
        revision = target.git_read("rev-parse", "HEAD", text=True).strip()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            expected = target.bundle_revision(revision, path / "revision.zip")
            committed = target.git_read("show", revision + ":demo/frontend/app.py")
            self.assertEqual(expected["app.py"],
                             hashlib.sha256(committed.replace(b"\r\n", b"\n")).hexdigest())
            self.assertIn("aml/jobs/demo-nvfp4-nd96.yml", expected)

    def test_appsettings_failures_do_not_log_response_or_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            instance = object.__new__(target.Target)
            instance.directory, instance.sub, instance.env = Path(directory), SUB, {}
            with patch.object(target.subprocess, "run") as run:
                run.return_value.returncode = 1
                run.return_value.stderr = "credential-or-digest-must-not-appear"
                with self.assertRaises(RuntimeError):
                    instance.az("webapp", "config", "appsettings", "set")
            self.assertNotIn("credential-or-digest-must-not-appear",
                             (instance.directory / "azure-error.log").read_text())

    def test_model_and_webiq_external_settings_survive_terraform(self):
        text = (ROOT / "demo/infra/main.tf").read_text()
        for name in ("AXK2_MODEL_API_KEY_SHA256", "AXK2_WEBIQ_TOOL",
                     "AXK2_WEBIQ_QUERY_FIELD", "AXK2_WEBIQ_ARGUMENTS_JSON",
                     "AXK2_WEBIQ_BEARER_TOKEN"):
            self.assertIn('app_settings["' + name + '"]', text)
        for name in target.MODEL_API_DEFAULTS:
            self.assertIn('app_settings["' + name + '"]', text)

    def test_optional_model_settings_remain_local_and_survive_rotation(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            instance = object.__new__(target.Target)
            instance.directory = Path(directory)
            with patch.object(instance, "az") as azure:
                instance.model_key(option_values=["AXK2_MODEL_API_MAX_ACTIVE=1"])
                instance.model_key()
                instance.model_key(rotate=True)
                settings = json.loads(instance.staged_model_settings().read_text())
                self.assertEqual(settings["AXK2_MODEL_API_MAX_ACTIVE"], "1")
                azure.assert_not_called()

    def test_invalid_optional_model_settings_rejected_before_key_creation(self):
        invalid = [
            ["AXK2_MODEL_API_KEY_SHA256=" + "a" * 64],
            ["AXK2_MODEL_API_MAX_ACTIVE=0"],
            ["AXK2_MODEL_API_MAX_ACTIVE=garbage"],
            ["AXK2_MODEL_API_NAME_FP8=invalid\nvalue"],
            ["AXK2_MODEL_API_MAX_ACTIVE=1", "AXK2_MODEL_API_MAX_ACTIVE=2"],
            ["AXK2_MODEL_API_MAX_TOKENS=64"],
            ["UNRELATED_SETTING=1"],
        ]
        for options in invalid:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                instance = object.__new__(target.Target)
                instance.directory = Path(directory)
                with self.assertRaises(ValueError):
                    instance.model_key(option_values=options)
                self.assertFalse((instance.directory / "model-api-backend.key").exists())

    def test_web_recovery_rejects_changed_gpu_binding(self):
        instance = object.__new__(target.Target)
        instance.url = "https://target.invalid"
        models = {"models": [
            {"id": name, "status": {"active_since": 1, "healthy": True,
                                   "queue": {"active": 0, "waiting": 0}},
             "runtime": {"state": "verified"}} for name in ("fp8", "nvfp4")]}
        changed = json.loads(json.dumps(models))
        changed["models"][0]["status"]["active_since"] = 2
        with patch.object(instance, "request", return_value=changed):
            with self.assertRaisesRegex(ValueError, "binding changed"):
                instance.wait_web_recovery(models)

    def test_settings_activation_requires_explicit_recycle_opt_in(self):
        instance = object.__new__(target.Target)
        with patch.object(instance, "az") as az:
            with self.assertRaises(ValueError):
                instance.model_settings()
            az.assert_not_called()

    def test_revision_rollout_regression_precedes_key_activation(self):
        for closed in (True, False):
            with self.subTest(closed=closed), tempfile.TemporaryDirectory() as directory:
                instance = object.__new__(target.Target)
                instance.directory = Path(directory)
                instance.url, instance.rg, instance.name = "https://target.invalid", "rg-target", "target"
                manifest = {"app.py": hashlib.sha256(b"old").hexdigest()}
                with zipfile.ZipFile(instance.directory / "frontend.zip", "w") as archive:
                    archive.writestr("app.py", b"old")
                target.write_private(instance.directory / "frontend-expected.json", manifest)
                target.private_text(instance.directory / "model-api-backend.key", "a" * 64)
                target.write_private(instance.directory / "model-api-settings.json",
                                     {"AXK2_MODEL_API_KEY_SHA256": hashlib.sha256(b"a" * 64).hexdigest()})
                models = {"models": [
                    {"id": name, "status": {"healthy": True, "active_since": 1},
                     "runtime": {"state": "verified"}} for name in ("fp8", "nvfp4")]}
                events = []

                def candidate(revision, path):
                    path.write_bytes(b"candidate")
                    return {"app.py": "candidate"}

                def request(url, body=None, anonymous=False):
                    if "/v1/" in url:
                        self.assertTrue(anonymous)
                        events.append("disabled-check")
                        if closed:
                            raise target.urllib.error.HTTPError(url, 404, "disabled", {}, None)
                        return {}
                    if url.endswith("/api/models"):
                        return models
                    if url.endswith("/api/capabilities"):
                        return {"web_iq": {"state": "ready"}}
                    return {}

                def azure(*args):
                    if args[3] == "list":
                        return [{"name": "unrelated", "value": "preserved"}]
                    events.append("activate")
                    return {}

                with patch.object(target, "bundle_revision", side_effect=candidate), \
                        patch.object(instance, "request", side_effect=request), \
                        patch.object(instance, "az", side_effect=azure), \
                        patch.object(instance, "verify_archive"), \
                        patch.object(instance, "wait_web_recovery", return_value={"healthy": True}), \
                        patch.object(instance, "publish", side_effect=lambda *args: events.append("publish")), \
                        patch.object(instance, "power") as power, contextlib.redirect_stdout(io.StringIO()):
                    if closed:
                        instance.deploy_revision("a" * 40, True)
                        self.assertEqual(events, ["publish", *["disabled-check"] * 4, "activate"])
                        result = json.loads((instance.directory / "candidate-proof.json").read_text())
                        self.assertEqual(result["state"], "deployed_links_recovered")
                        self.assertIn("not_run", result["model_api_smoke"])
                    else:
                        with self.assertRaisesRegex(ValueError, "unexpectedly accepted"):
                            instance.deploy_revision("a" * 40, True)
                        self.assertNotIn("activate", events)
                    power.assert_not_called()

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
