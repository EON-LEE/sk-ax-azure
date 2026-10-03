"""Fetch what Azure ML jobs reported (chunked JSON tags, compressed series) through the workspace MLflow API.

Workspace storage is private by policy, so this is the operator-side path to results.

usage:
  python aml/fetch_results.py show <job_name>
  python aml/fetch_results.py compare <smoke_job> <oracle_job> --out evidence/...json

`compare` checks every per-position series the job reported (for example node0.smoke-native-tp8,
node0.smoke-dense-tp8, node0.smoke-native-tp8-pp2) against the official Transformers reference,
separately for positions inside and beyond index_topk, and compares related runs pairwise.

Set AXK2_MLFLOW_URI to the workspace `mlflow_tracking_uri` (az ml workspace show), and
AXK2_ORACLE_MLFLOW_URI when the reference job ran in a different workspace.
"""
import argparse
import base64
import json
import math
import os
import zlib

from mlflow.tracking import MlflowClient

INDEX_TOPK = 2048
PAIRS = [("node0.smoke-native-tp8", "node0.smoke-native-tp8-pp2"),
         ("node0.smoke-dense-tp8", "node0.smoke-native-tp8"),
         ("node0.pp1", "node0.tp8-pp2")]


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
        if text.startswith("z:"):
            text = zlib.decompress(base64.b64decode(text[2:])).decode()
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


def aggregate(per_prompt, region):
    positions = agree_positions = 0
    weighted_diff = weighted_agree = 0.0
    for prompt in per_prompt.values():
        part = prompt.get(region)
        if not part or not part["chosen_logprob"]:
            continue
        count = part["chosen_logprob"]["positions"]
        positions += count
        weighted_diff += part["chosen_logprob"]["mean_abs_diff"] * count
        if part["top1_agreement"] is not None:
            agree_positions += count
            weighted_agree += part["top1_agreement"] * count
    return {"positions": positions,
            "mean_abs_diff": round(weighted_diff / positions, 6) if positions else None,
            "top1_agreement": round(weighted_agree / agree_positions, 4) if agree_positions else None}


def summarize(per_prompt):
    return {region: aggregate(per_prompt, region) for region in ("within_index_topk", "beyond_index_topk")}


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
        _, data = tags_json(mlflow_client, args.job)
        runs = {key[: -len(".series")]: value for key, value in data.items()
                if key.endswith(".series") and isinstance(value, dict)}
        result = {"job": args.job, "oracle_job": args.oracle_job, "index_topk": INDEX_TOPK,
                  "reference": "official Transformers AXK2, FP32 CPU, sparse DSA",
                  "summary_vs_official": {}, "pairs": {}, "per_prompt_vs_official": {}}
        for name, values in sorted(runs.items()):
            per_prompt = compare(oracle, values)
            result["per_prompt_vs_official"][name] = per_prompt
            result["summary_vs_official"][name] = summarize(per_prompt)
        for left, right in PAIRS:
            if left in runs and right in runs:
                result["pairs"][f"{left} vs {right}"] = summarize(compare(runs[left], runs[right]))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
