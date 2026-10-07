"""Content-addressed job bootstrap archives, retained across frontend deployments."""
import hashlib
import json
import os
import re
from pathlib import Path

SHA = re.compile(r"[0-9a-f]{64}")
JOB = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}")


class SourceArchives:
    def __init__(self, root):
        self.root = Path(root)
        (self.root / "jobs").mkdir(parents=True, exist_ok=True)

    def put(self, data):
        if not data or len(data) > 512 * 1024:
            raise ValueError("Job source archive must be nonempty and at most 512 KiB")
        digest = hashlib.sha256(data).hexdigest()
        path = self.root / (digest + ".tgz")
        if path.exists():
            self.get(digest)
        else:
            temporary = path.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_bytes(data)
            os.replace(temporary, path)
        return digest

    def get(self, digest):
        if not isinstance(digest, str) or not SHA.fullmatch(digest):
            raise ValueError("Invalid source archive checksum")
        data = (self.root / (digest + ".tgz")).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Persisted job source archive failed its checksum")
        return data

    def manifest(self, job):
        if not isinstance(job, str) or not JOB.fullmatch(job):
            raise ValueError("Invalid job source manifest name")
        path = self.root / "jobs" / (job + ".json")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if value.get("job") != job:
            raise ValueError("Source manifest refers to a different job")
        self.get(value.get("source_sha"))
        return value

    def for_job(self, job):
        digest = job.get("source_sha")
        if not digest:
            manifest = self.manifest(job.get("name"))
            digest = manifest.get("source_sha") if manifest else None
        if not digest:
            raise ValueError("This running job has no verified immutable source archive; refusing replacement bytes")
        return self.get(digest)
