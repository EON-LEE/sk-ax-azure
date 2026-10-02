"""Create the dense-mode serving directory for A.X-K2 on GPUs without DSA sparse kernels.

Dense mode = the official checkpoint with the three DSA indexer keys removed from config.json.
The SKT fork's AXK2Config only attaches the indexer when those keys are present, and its weight
loader skips indexer tensors in that case. For every token whose causal context is within
index_topk (2048) the top-k selection keeps every key, so dense attention is mathematically
identical to the official sparse attention there; beyond it dense attention is an approximation
that needs a quality evaluation. All other files are symlinked, never copied.
"""
import argparse
import json
from pathlib import Path

INDEXER_KEYS = ("index_topk", "index_n_heads", "index_head_dim")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("target")
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    source, target = Path(args.source).resolve(), Path(args.target)
    target.mkdir(parents=True, exist_ok=True)
    linked = 0
    for item in source.iterdir():
        if item.name == "config.json":
            continue
        link = target / item.name
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(item)
        linked += 1
    config = json.loads((source / "config.json").read_text())
    removed = {key: config.pop(key) for key in INDEXER_KEYS if key in config}
    (target / "config.json").write_text(json.dumps(config, indent=2))
    report = {"source": str(source), "target": str(target), "linked_entries": linked,
              "removed_indexer_keys": removed, "num_hidden_layers": config.get("num_hidden_layers"),
              "exact_dsa_equivalence_up_to_tokens": removed.get("index_topk")}
    Path(args.report).write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
