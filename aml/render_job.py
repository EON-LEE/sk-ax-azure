"""Render an Azure ML job YAML whose command carries aml/src as an embedded tarball.

The subscription's storage policy disables public network access on every storage account, so
`az ml job create` cannot upload a local `code:` snapshot from outside the workspace's managed VNet.
The helper scripts are small, so they travel inside the job definition instead (base64 tar.gz) and
are unpacked on each node before anything runs.

usage: python aml/render_job.py aml/jobs/<template>.yml [more templates...]
writes: aml/jobs/.rendered/<template>.yml
"""
import base64
import io
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLACEHOLDER = "__AXK2_SRC_B64__"


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


def main(templates):
    encoded = payload()
    target_dir = HERE / "jobs" / ".rendered"
    target_dir.mkdir(exist_ok=True)
    for template in map(Path, templates):
        text = template.read_text(encoding="utf-8")
        if PLACEHOLDER not in text:
            raise SystemExit(f"{template} has no {PLACEHOLDER}")
        target = target_dir / template.name
        target.write_text(text.replace(PLACEHOLDER, encoded), encoding="utf-8", newline="\n")
        print(f"{target} ({len(encoded)} base64 chars)")


if __name__ == "__main__":
    main(sys.argv[1:])
