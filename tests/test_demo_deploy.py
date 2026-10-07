"""Exercise the actual deployment bundle script without Azure or secret files."""
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DeploymentBundleTests(unittest.TestCase):
    def test_environment_inputs_never_enter_deployment_bundle(self):
        deploy = (ROOT / "demo" / "deploy.sh").read_text(encoding="utf-8")
        script = deploy.split('python3 - "$ROOT" "$ZIP" <<\'PY\'\n', 1)[1].split("\nPY\n", 1)[0]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            front = root / "demo" / "frontend"
            for name, content in {
                "demo/frontend/app.py": "public fixture",
                ".env.webiq": "never packaged",
                "demo/frontend/.env": "never packaged",
                "demo/frontend/.env.webiq": "never packaged",
                "demo/frontend/nested/.env.local": "never packaged",
                "demo/frontend/.env-secrets/nested.py": "never packaged",
                "demo/frontend/__pycache__/cached.pyc": "never packaged",
                "demo/frontend/static/chat.js": "public fixture",
                "aml/render_job.py": "public fixture",
                "aml/jobs/demo-fp8-nd96.yml": "public fixture",
                "aml/src/public.py": "public fixture",
                "docs/report/skt_published.json": "{}",
                "docs/report/figures/throughput.json": "{}",
            }.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            output = root / "bundle.zip"
            result = subprocess.run([sys.executable, "-c", script, str(root), str(output)],
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            with zipfile.ZipFile(output) as archive:
                names = archive.namelist()
                self.assertIn("app.py", names)
                self.assertIn("static/chat.js", names)
                self.assertIn("aml/src/public.py", names)
                self.assertFalse(any(part.startswith(".env") or part == "__pycache__"
                                     for name in names for part in name.split("/")))
                self.assertFalse(any(b"never packaged" in archive.read(name) for name in names))


if __name__ == "__main__":
    unittest.main()
