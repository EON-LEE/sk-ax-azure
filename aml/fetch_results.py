"""Fetch what Azure ML jobs reported (chunked JSON tags, metric series) through the workspace MLflow API.

Workspace storage is private by policy, so this is the operator-side path to results.

usage:
  python aml/fetch_results.py show <job_name>
  python aml/fetch_results.py compare <smoke_job> <oracle_job> --out evidence/...json

Set AXK2_MLFLOW_URI to the workspace `mlflow_tracking_uri` (az ml workspace show), and
AXK2_ORACLE_MLFLOW_URI when the reference job ran in a different workspace.
"""
import argparse
import json
import math
import os

from mlflow.tracking import MlflowClient

INDEX_TOPK = 2048


def client():
    import mlflow
    mlflow.set_tracking_uri(os.environ["AXK2_MLFLOW_URI"])
    return MlflowClient()


def tags_json(mlflow_client, run_id):
    run = mlflow_client.get_run(run_id)
    tags = run.data.tags
    out = {}
    for key in sorted(k[: -len(".chunks")] for k in tags if k.endswith(".chunks")):
        text = "".join(tags.get(f"{key}.{i:03d}", "") for i in range(int(tags[key + ".chunks"])))
        try:
            out[key] = json.loads(text)
        except ValueError:
            out[key] = text
    return run.info.status, out


def series(mlflow_client, run_id, prefix):
    _, data = tags_json(mlflow_client, run_id)
    return data.get(f"{prefix}.series", {})


def stats(left, right):
    pairs = [(a, b) for a, b in zip(left, right) if a is not None and b is not None]
    if not pairs:
        return None
    diffs = [abs(a - b) for a, b in pairs]
    mean_a = sum(a for a, _ in pairs) / len(pairs)
    mean_b = sum(b for _, b in pairs) / len(pairs)
    cov = sum((a - mean_a) * (b - mean_b) for a, b in pairs)
    var_a = sum((a - mean_a) ** 2 for a, _ in pairs)
    var_b = sum((b - mean_b) ** 2 for _, b in pairs)
    corr = cov / math.sqrt(var_a * var_b) if var_a > 0 and var_b > 0 else None
    return {"positions": len(pairs), "max_abs_diff": round(max(diffs), 5),
            "mean_abs_diff": round(sum(diffs) / len(diffs), 6),
            "pearson": round(corr, 6) if corr is not None else None}


def agreement(left, right):
    pairs = [(a, b) for a, b in zip(left, right) if a is not None and b is not None]
    return round(sum(a == b for a, b in pairs) / len(pairs), 4) if pairs else None


def compare(reference, candidate):
    out = {}
    for key in sorted(k[: -len(".chosen_logprob")] for k in reference if k.endswith(".chosen_logprob")):
        ref_lp, cand_lp = reference[f"{key}.chosen_logprob"], candidate.get(f"{key}.chosen_logprob", [])
        ref_top, cand_top = reference[f"{key}.top1"], candidate.get(f"{key}.top1", [])
        split = INDEX_TOPK
        out[key] = {
            "within_index_topk": {"chosen_logprob": stats(ref_lp[:split], cand_lp[:split]),
                                  "top1_agreement": agreement(ref_top[:split], cand_top[:split])},
        }
        if len(ref_lp) > split:
            out[key]["beyond_index_topk"] = {"chosen_logprob": stats(ref_lp[split:], cand_lp[split:]),
                                             "top1_agreement": agreement(ref_top[split:], cand_top[split:])}
    return out


def pp_prefix(mlflow_client, run_id):
    _, data = tags_json(mlflow_client, run_id)
    tags = sorted({k.split(".")[1] for k in data if k.startswith("node0.tp") and k.endswith(".series")})
    if len(tags) != 1:
        raise SystemExit(f"expected one multi-node series in {run_id}, found {tags}")
    return f"node0.{tags[0]}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["show", "compare"])
    parser.add_argument("job")
    parser.add_argument("oracle_job", nargs="?")
    parser.add_argument("--out")
    args = parser.parse_args()
    mlflow_client = client()
    if args.mode == "show":
        status, data = tags_json(mlflow_client, args.job)
        result = {"job": args.job, "status": status, "reports": data}
    else:
        oracle_client = MlflowClient(tracking_uri=os.environ.get("AXK2_ORACLE_MLFLOW_URI", os.environ["AXK2_MLFLOW_URI"]))
        oracle = series(oracle_client, args.oracle_job, "oracle")
        single = series(mlflow_client, args.job, "node0.pp1")
        prefix = pp_prefix(mlflow_client, args.job)
        multi = series(mlflow_client, args.job, prefix)
        result = {
            "smoke_job": args.job, "oracle_job": args.oracle_job, "index_topk": INDEX_TOPK,
            "reference": "official Transformers AXK2, FP32 CPU, sparse DSA",
            "multi_node_series": prefix,
            "vllm_single_node_vs_official": compare(oracle, single),
            "vllm_multi_node_vs_official": compare(oracle, multi),
            "vllm_multi_node_vs_single_node": compare(single, multi),
        }
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
