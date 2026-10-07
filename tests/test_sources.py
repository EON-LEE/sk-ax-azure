import base64
import hashlib
import json
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "aml"), str(ROOT / "demo" / "frontend")]
from render_job import payload, src_url
from sources import SourceArchives
del sys.path[:2]


class SourceTests(unittest.TestCase):
    def test_gzip_header_and_payload_are_reproducible(self):
        with patch("time.time", return_value=100):
            first = base64.b64decode(payload())
        with patch("time.time", return_value=10000000):
            second = base64.b64decode(payload())
        self.assertEqual(first, second)
        self.assertEqual(first[4:8], b"\x00" * 4)

    def test_existing_job_retains_its_original_archive_after_code_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            archives = SourceArchives(folder)
            old = archives.put(b"old gzip bytes, including original timestamp")
            archives.put(b"new code gzip bytes")
            Path(folder, "jobs", "old-job.json").write_text(json.dumps({"job":"old-job","source_sha":old}))
            self.assertEqual(archives.for_job({"name":"old-job"}), b"old gzip bytes, including original timestamp")
            self.assertEqual(hashlib.sha256(archives.for_job({"name":"old-job"})).hexdigest(), old)
            with self.assertRaisesRegex(ValueError, "no verified immutable"):
                archives.for_job({"name":"missing-job"})
            Path(folder, old + ".tgz").write_bytes(b"corrupt")
            with self.assertRaisesRegex(ValueError, "checksum"):
                archives.for_job({"name":"old-job"})

    def test_profile_link_preserves_namespace(self):
        self.assertEqual(src_url("wss://example.net/models/nvfp4/ws/link"),
                         "https://example.net/models/nvfp4/api/link/src")
        self.assertEqual(src_url("wss://example.net/ws/link"), "https://example.net/api/link/src")

    def test_single_node_checkpoint_barrier_runs_without_ray(self):
        text = (ROOT / "aml" / "src" / "entry.sh").read_text()
        block = text.split('if python3 - "${DOWNLOAD_TIMEOUT:-5400}"', 1)[1].split("<<'EOF'\n", 1)[1].split("\nEOF", 1)[0]
        with tempfile.TemporaryDirectory() as folder:
            ready = Path(folder, "ready")
            ready.write_text("10")
            block = block.replace('"/tmp/axk2-full.ready"', repr(str(ready)))
            block = block.replace('"/tmp/axk2-full.failed"', repr(str(Path(folder, "failed"))))
            block = block.replace("import ray", "raise AssertionError('MP must not import Ray')")
            result = subprocess.run([sys.executable, "-c", block, "1", "", "mp"],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)[0]["seconds"], "10")
