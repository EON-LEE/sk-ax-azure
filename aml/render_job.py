"""Render an Azure ML job YAML whose command carries aml/src as an embedded tarball.

The subscription's storage policy disables public network access on every storage account, so
`az ml job create` cannot upload a local `code:` snapshot from outside the workspace's managed VNet.
The helper scripts are small, so they travel inside the job definition instead (base64 tar.gz) and
are unpacked on each node before anything runs.

AML caps the total size of a job's docker arguments (environment plus command); a payload past roughly
64K base64 characters fails before the container starts (ArgumentTooLong, visible only in the run's
RunHistory details). The demo job therefore carries no payload: it downloads the same tarball from the
frontend (GET /api/link/src with its job token) and checks it against AXK2_SRC_SHA256.

usage: python aml/render_job.py aml/jobs/<template>.yml [more templates...]
writes: aml/jobs/.rendered/<template>.yml
The demo template also needs AXK2_LINK_URL and AXK2_LINK_TOKEN in the environment; the demo
frontend's supervisor calls render() with a fresh token for every submission instead.
"""
import base64
import hashlib
import io
import os
import re
import sys
import tarfile
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
PLACEHOLDER = "__AXK2_SRC_B64__"
SRC_URL = "__AXK2_SRC_URL__"
LINK = ("AXK2_LINK_URL", "AXK2_LINK_TOKEN")
SAFE = re.compile(r"[A-Za-z0-9._~:/?=&%+-]+")  # URL/token characters; no quotes, $ or braces (AML expressions)


def payload():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted((HERE / "src").iterdir()):
            if not path.is_file() or path.suffix not in {".py", ".sh", ".json"}:
                continue
            data = path.read_bytes().replace(b"\r\n", b"\n")
            info = tarfile.TarInfo(path.name)
            info.size, info.mtime = len(data), 0
            info.mode = 0o755 if path.suffix == ".sh" else 0o644
            tar.addfile(info, io.BytesIO(data))
    return base64.b64encode(buffer.getvalue()).decode()


def digest(encoded):
    return hashlib.sha256(base64.b64decode(encoded)).hexdigest()


def src_url(link_url):
    """wss://host/ws/link -> https://host/api/link/src (where a demo job downloads its scripts)."""
    parts = urlsplit(link_url)
    return f"{'https' if parts.scheme == 'wss' else 'http'}://{parts.netloc}/api/link/src" if parts.netloc else ""


def render(text, encoded, values=None):
    """Fills the payload (or its digest) and __NAME__ for every NAME in values; refuses to leave any behind."""
    if PLACEHOLDER not in text and SRC_URL not in text:
        raise ValueError(f"the template has neither {PLACEHOLDER} nor {SRC_URL}")
    text = text.replace(PLACEHOLDER, encoded).replace("__AXK2_SRC_SHA256__", digest(encoded))
    values = dict(values or {})
    if SRC_URL in text and "AXK2_SRC_URL" not in values and values.get("AXK2_LINK_URL"):
        values["AXK2_SRC_URL"] = src_url(values["AXK2_LINK_URL"])
    for name, value in values.items():
        if not SAFE.fullmatch(value):
            raise ValueError(f"{name} has characters a job template cannot carry")
        text = text.replace(f"__{name}__", value)
    left = sorted(set(re.findall(r"__AXK2_[A-Z0-9_]+__", text)))
    if left:
        raise ValueError(f"unfilled placeholders: {', '.join(left)}")
    return text


def main(templates):
    encoded = payload()
    values = {name: os.environ[name] for name in LINK if os.environ.get(name)}
    target_dir = HERE / "jobs" / ".rendered"
    target_dir.mkdir(exist_ok=True)
    for template in map(Path, templates):
        try:
            text = render(template.read_text(encoding="utf-8"), encoded, values)
        except ValueError as error:
            raise SystemExit(f"{template}: {error}")
        target = target_dir / template.name
        target.write_text(text, encoding="utf-8", newline="\n")
        carried = len(encoded) if PLACEHOLDER in template.read_text(encoding="utf-8") else 0
        print(f"{target} ({carried} base64 chars embedded, {len(text)} chars)")


if __name__ == "__main__":
    main(sys.argv[1:])
