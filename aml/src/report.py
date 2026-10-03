"""Return results from inside an Azure ML job without touching blob storage.

The subscription's storage policy disables public network access, so job artifacts are only
reachable from inside the workspace's managed VNet. JSON summaries and per-position series are
therefore written to the job record as chunked MLflow tags (AML's metric store truncates long step
histories) and read back through the workspace's MLflow API. Everything is also echoed to stdout.
"""
import argparse
import base64
import json
import sys
import time
import zlib

CHUNK = 4000
TAGS_PER_CALL = 90  # MLflow accepts at most 100 tags per log_batch request


def mlflow_or_none():
    try:
        import mlflow
        return mlflow
    except Exception as exc:  # reporting must never break the run itself
        print(f"AXK2_REPORT_WARN mlflow unavailable: {exc}", flush=True)
        return None


def ascii_safe(text):
    """Tag limits are counted in bytes; keep every chunk 1 char = 1 byte."""
    try:
        return json.dumps(json.loads(text), ensure_ascii=True, separators=(",", ":"))
    except ValueError:
        return text.encode("ascii", "backslashreplace").decode("ascii")


def send_json(key, text):
    text = ascii_safe(text)
    shown = text if len(text) <= 3000 else text[:3000] + f"... [{len(text)} chars]"
    print(f"AXK2_REPORT {key} {shown}", flush=True)
    mlflow = mlflow_or_none()
    if mlflow is None:
        return
    chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)] or [""]
    tags = [(f"{key}.{index:03d}", chunk) for index, chunk in enumerate(chunks)]
    tags.append((f"{key}.chunks", str(len(chunks))))
    for start in range(0, len(tags), TAGS_PER_CALL):
        for attempt in range(4):
            try:
                mlflow.set_tags(dict(tags[start:start + TAGS_PER_CALL]))
                break
            except Exception as exc:
                print(f"AXK2_REPORT_WARN set_tags attempt {attempt + 1} failed for {key}: {exc}", flush=True)
                time.sleep(2 ** attempt)


def send_series(prefix, series):
    # AML's MLflow metric store truncates/duplicates long step histories, so per-position series
    # travel as zlib-compressed, base64-encoded chunked tags ("z:" marks the encoding).
    raw = json.dumps(series, separators=(",", ":")).encode()
    send_json(f"{prefix}.series", "z:" + base64.b64encode(zlib.compress(raw, 9)).decode())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("key")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file")
    source.add_argument("--text")
    parser.add_argument("--series", action="store_true",
                        help="file holds {name: [numbers]}; sent compressed")
    args = parser.parse_args()
    text = open(args.file, encoding="utf-8").read() if args.file else args.text
    try:
        if args.series:
            send_series(args.key, json.loads(text))
        else:
            send_json(args.key, text)
    except Exception as exc:
        print(f"AXK2_REPORT_WARN {args.key}: {exc}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
