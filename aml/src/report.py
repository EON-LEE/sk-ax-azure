"""Return results from inside an Azure ML job without touching blob storage.

The subscription's storage policy disables public network access, so job artifacts are only
reachable from inside the workspace's managed VNet. Small JSON summaries are therefore written to
the job record as chunked MLflow tags and numeric series as MLflow metric histories; both are read
back through the workspace's MLflow API. Everything is also echoed to stdout.
"""
import argparse
import json
import os
import sys

CHUNK = 4000


def mlflow_or_none():
    try:
        import mlflow
        return mlflow
    except Exception as exc:  # reporting must never break the run itself
        print(f"AXK2_REPORT_WARN mlflow unavailable: {exc}", flush=True)
        return None


def send_json(key, text):
    print(f"AXK2_REPORT {key} {text}", flush=True)
    mlflow = mlflow_or_none()
    if mlflow is None:
        return
    chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)] or [""]
    tags = {f"{key}.{index:03d}": chunk for index, chunk in enumerate(chunks)}
    tags[f"{key}.chunks"] = str(len(chunks))
    try:
        mlflow.set_tags(tags)
    except Exception as exc:
        print(f"AXK2_REPORT_WARN set_tags failed for {key}: {exc}", flush=True)


def send_series(prefix, series):
    mlflow = mlflow_or_none()
    if mlflow is None:
        return
    from mlflow.entities import Metric
    from mlflow.tracking import MlflowClient
    client = MlflowClient()
    run_id = os.environ.get("MLFLOW_RUN_ID") or mlflow.active_run().info.run_id
    batch = []
    for name, values in series.items():
        for step, value in enumerate(values):
            if value is None:
                continue
            batch.append(Metric(f"{prefix}.{name}", float(value), 0, step))
            if len(batch) == 1000:
                client.log_batch(run_id, metrics=batch)
                batch = []
    if batch:
        client.log_batch(run_id, metrics=batch)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("key")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file")
    source.add_argument("--text")
    parser.add_argument("--series", action="store_true",
                        help="file holds {name: [numbers]}; logged as metric histories")
    args = parser.parse_args()
    text = open(args.file, encoding="utf-8").read() if args.file else args.text
    try:
        if args.series:
            send_series(args.key, json.loads(text))
            print(f"AXK2_REPORT {args.key} series:{list(json.loads(text))}", flush=True)
        else:
            send_json(args.key, text)
    except Exception as exc:
        print(f"AXK2_REPORT_WARN {args.key}: {exc}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
