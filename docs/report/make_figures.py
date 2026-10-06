import argparse
import ast
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = REPORT_DIR / "figures"
INPUTS_FIG7 = [1024, 2048, 4096, 8192, 16384, 32768, 65536, 120000]
PRICE_METERS = ["on_demand", "reserved_1_year", "reserved_3_years", "spot", "low_priority"]
PRICE_LABELS_KO = {
    "on_demand": "종량제",
    "reserved_1_year": "1년 예약",
    "reserved_3_years": "3년 예약",
    "spot": "Spot",
    "low_priority": "Low priority",
}


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _extract_verify_constants():
    path = ROOT / "aml" / "src" / "verify_suites.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    wanted = {"DOC_FIG7", "DOC_FIG8_TOTAL"}
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in wanted:
                    found[target.id] = ast.literal_eval(node.value)
    missing = wanted - set(found)
    if missing:
        raise KeyError(f"Missing constants in {path}: {sorted(missing)}")
    return found


def _points_to_map(points, total_key="total_tok_s"):
    return {int(p["input_tokens"]): p for p in points}


def _series_from_pairs(name, label, hardware, gpu_count, source, conditions, pairs):
    return {
        "name": name,
        "label": label,
        "hardware": hardware,
        "gpu_count": gpu_count,
        "source": source,
        "conditions": conditions,
        "points": [
            {"input_tokens": int(k), "total_tok_s": float(v[0]), "output_tok_s": float(v[1])}
            for k, v in sorted(pairs.items())
        ],
    }


def load_throughput():
    constants = _extract_verify_constants()
    fp8 = _read_json(ROOT / "evidence" / "a100-verification-and-doc-speed.json")
    nvfp4 = _read_json(ROOT / "evidence" / "a100-nvfp4-single-node.json")
    published = _read_json(REPORT_DIR / "skt_published.json")

    doc_fig7 = constants["DOC_FIG7"]
    doc_fig8 = constants["DOC_FIG8_TOTAL"]
    series = {}
    series["b200_fp8_k2"] = _series_from_pairs(
        "b200_fp8_k2",
        "B200 A.X K2 FP8 (8 GPU)",
        "B200",
        8,
        "aml/src/verify_suites.py DOC_FIG7, SKT tech report Fig. 7",
        "random dataset, concurrency 32, 1,024 output tokens, FP8 weights, BF16 KV cache",
        doc_fig7,
    )
    series["b200_fp8_k2_eagle3_total"] = {
        "name": "b200_fp8_k2_eagle3_total",
        "label": "B200 A.X K2 FP8 + EAGLE3 (Fig. 8 total)",
        "hardware": "B200",
        "gpu_count": 8,
        "source": "aml/src/verify_suites.py DOC_FIG8_TOTAL, SKT tech report Fig. 8",
        "conditions": "same serving benchmark family; total throughput only",
        "points": [{"input_tokens": int(k), "total_tok_s": float(v)} for k, v in sorted(doc_fig8.items())],
    }

    for key, item in published["series"].items():
        series[key] = {
            "name": key,
            "label": key.replace("_", " "),
            "hardware": item["hardware"],
            "gpu_count": item["gpu_count"],
            "source": item["source"],
            "conditions": item["conditions"],
            "points": item["points"],
        }

    fig7_points = fp8["doc_speed_comparison"]["points"]
    series["a100_fp8_native"] = {
        "name": "a100_fp8_native",
        "label": "A100x16 FP8 native DSA (demo)",
        "hardware": "A100",
        "gpu_count": 16,
        "source": "evidence/a100-verification-and-doc-speed.json doc_speed_comparison.points[].native",
        "conditions": fp8["doc_speed_comparison"]["this_run"],
        "points": [
            {"input_tokens": p["input_tokens"], **p["native"]}
            for p in fig7_points
        ],
    }
    series["a100_fp8_dense"] = {
        "name": "a100_fp8_dense",
        "label": "A100x16 FP8 dense",
        "hardware": "A100",
        "gpu_count": 16,
        "source": "evidence/a100-verification-and-doc-speed.json doc_speed_comparison.points[].dense",
        "conditions": fp8["doc_speed_comparison"]["this_run"],
        "points": [
            {"input_tokens": p["input_tokens"], **p["dense"]}
            for p in fig7_points
        ],
    }

    nv_points = nvfp4["doc_fig7_conditions"]["points"]
    series["a100_nvfp4"] = {
        "name": "a100_nvfp4",
        "label": "A100x8 NVFP4",
        "hardware": "A100",
        "gpu_count": 8,
        "source": "evidence/a100-nvfp4-single-node.json doc_fig7_conditions.points[].nvfp4_8_a100",
        "conditions": nvfp4["doc_fig7_conditions"]["this_run"],
        "points": [
            {"input_tokens": p["input_tokens"], **p["nvfp4_8_a100"]}
            for p in nv_points
        ],
    }

    return {
        "schema_version": 1,
        "sources": {
            "verify_suites": "aml/src/verify_suites.py",
            "a100_fp8": "evidence/a100-verification-and-doc-speed.json",
            "a100_nvfp4": "evidence/a100-nvfp4-single-node.json",
            "skt_published": "docs/report/skt_published.json",
        },
        "series": series,
        "cost_per_million_output_tokens_usd": nvfp4["cost_per_million_output_tokens_usd"],
    }


def _import_matplotlib():
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import font_manager
    from matplotlib import pyplot as plt
    font_path = Path(r"C:\Windows\Fonts\malgun.ttf")
    korean = font_path.exists()
    if korean:
        font_manager.fontManager.addfont(str(font_path))
        plt.rcParams["font.family"] = "Malgun Gothic"
    else:
        plt.rcParams["font.family"] = "DejaVu Sans"
    plt.rcParams["axes.unicode_minus"] = False
    return plt, korean


def _fmt_input(value):
    if value == 120000:
        return "120K"
    if value % 1024 == 0:
        return f"{value // 1024}K"
    return f"{value // 1000}K"


def _fmt_int(value):
    return f"{int(round(value)):,}"


def _point_map(data, key):
    return _points_to_map(data["series"][key]["points"])


def _total(point):
    return point.get("total_tok_s", point.get("total_token_throughput"))


def _output(point):
    return point.get("output_tok_s", point.get("output_throughput"))


def _line_plot(ax, xs, values, label, color, marker="o", linewidth=2.2, highlight=False, label_offset=(0, 7)):
    ax.plot(xs, values, marker=marker, label=label, color=color, linewidth=linewidth + (0.8 if highlight else 0), markersize=6)
    offsets = label_offset if isinstance(label_offset, list) else [label_offset] * len(values)
    for x, y, offset in zip(xs, values, offsets):
        va = "bottom" if offset[1] >= 0 else "top"
        ax.annotate(_fmt_int(y), (x, y), textcoords="offset points", xytext=offset, ha="center", va=va, fontsize=8, color=color)


def _save(fig, path):
    fig.tight_layout(rect=(0, 0.08, 1, 0.96))
    fig.savefig(path, dpi=155, bbox_inches="tight")
    fig.clf()


def render_fig7_overlay(data, out_dir):
    plt, ko = _import_matplotlib()
    inputs = INPUTS_FIG7
    xs = list(range(len(inputs)))
    labels = [_fmt_input(v) for v in inputs]
    fig, ax = plt.subplots(figsize=(10, 5.3))
    specs = [
        ("b200_fp8_k2", "B200 A.X K2 FP8", "#1f77b4", "s", False, [(0, -11), (0, -11), (0, -11), (0, -11), (0, -11), (0, 8), (0, 8), (0, 8)]),
        ("b200_fp8_k1_fig7", "B200 A.X K1", "#7f7f7f", "o", False, [(0, 8), (0, 8), (0, 8), (0, 8), (0, 8), (0, -11), (0, -11), (0, -11)]),
        ("a100_fp8_native", "A100x16 FP8 native DSA (demo)", "#d62728", "D", True, (0, -11)),
        ("a100_fp8_dense", "A100x16 FP8 dense", "#ff7f0e", "^", False, (0, 8)),
    ]
    for key, label, color, marker, highlight, offset in specs:
        points = _point_map(data, key)
        _line_plot(ax, xs, [_total(points[i]) for i in inputs], label, color, marker, highlight=highlight, label_offset=offset)
    ax.set_xticks(xs, labels)
    ax.set_ylabel("총 토큰 처리량 (tok/s)" if ko else "Total token throughput (tok/s)")
    ax.set_xlabel("입력 토큰" if ko else "Input tokens")
    ax.set_title("Fig. 7 조건 처리량: B200 공개값 vs A100 측정값" if ko else "Fig. 7 throughput: published B200 vs measured A100")
    ax.grid(axis="y", alpha=0.28)
    ax.legend(ncol=2, fontsize=9)
    foot = "조건: random dataset, concurrency 32, 출력 1,024 tokens, FP8/BF16 KV. 출처: SKT Fig. 7, verify_suites.py, evidence/*.json." if ko else "Conditions: random dataset, concurrency 32, 1,024 output tokens, FP8/BF16 KV. Sources: SKT Fig. 7, verify_suites.py, evidence/*.json."
    fig.text(0.01, 0.015, foot, fontsize=8, color="#444")
    _save(fig, out_dir / "fig7-overlay.png")


def render_fig9_overlay(data, out_dir):
    plt, ko = _import_matplotlib()
    b200 = _point_map(data, "b200_nvfp4_k2_fig9")
    a100 = _point_map(data, "a100_nvfp4")
    inputs = [i for i in sorted(a100) if i in b200]
    xs = list(range(len(inputs)))
    fig, ax = plt.subplots(figsize=(9.2, 5.1))
    _line_plot(ax, xs, [_total(b200[i]) for i in inputs], "B200 NVFP4 (4 GPU, Fig. 9)", "#1f77b4", "s")
    _line_plot(ax, xs, [_total(a100[i]) for i in inputs], "A100x8 NVFP4 (ours)", "#d62728", "D", highlight=True)
    ax.set_xticks(xs, [_fmt_input(v) for v in inputs])
    ax.set_ylabel("총 토큰 처리량 (tok/s)" if ko else "Total token throughput (tok/s)")
    ax.set_xlabel("입력 토큰" if ko else "Input tokens")
    ax.set_title("NVFP4 처리량: SKT Fig. 9 B200 vs A100 1노드" if ko else "NVFP4 throughput: SKT Fig. 9 B200 vs A100 one node")
    ax.grid(axis="y", alpha=0.28)
    ax.legend(fontsize=9)
    foot = "Fig. 9 이미지는 GPU 수를 표시하지 않아 SERVING_DESIGN의 4 x B200(TP4+EP) 설명을 사용." if ko else "Fig. 9 image does not show GPU count; using SERVING_DESIGN assumption: 4 x B200 (TP4+EP)."
    fig.text(0.01, 0.015, foot, fontsize=8, color="#444")
    _save(fig, out_dir / "fig9-overlay.png")


def render_per_gpu(data, out_dir):
    plt, ko = _import_matplotlib()
    keys = ["b200_fp8_k2", "a100_fp8_native", "a100_fp8_dense", "a100_nvfp4", "b200_nvfp4_k2_fig9"]
    labels = ["B200 FP8 K2", "A100 FP8 native", "A100 FP8 dense", "A100 NVFP4", "B200 NVFP4"]
    colors = ["#1f77b4", "#d62728", "#ff7f0e", "#a23a3a", "#2ca02c"]
    common = set(_point_map(data, keys[0]))
    for key in keys[1:]:
        common &= set(_point_map(data, key))
    inputs = [i for i in [1024, 4096, 8192, 16384, 32768] if i in common]
    xbase = list(range(len(inputs)))
    width = 0.15
    fig, ax = plt.subplots(figsize=(10, 5.4))
    for idx, (key, label, color) in enumerate(zip(keys, labels, colors)):
        points = _point_map(data, key)
        gpu_count = data["series"][key]["gpu_count"]
        vals = [_total(points[i]) / gpu_count for i in inputs]
        xs = [x + (idx - 2) * width for x in xbase]
        bars = ax.bar(xs, vals, width=width, label=label, color=color, alpha=0.9)
        ax.bar_label(bars, labels=[_fmt_int(v) for v in vals], fontsize=7, padding=2, rotation=90)
    ax.set_xticks(xbase, [_fmt_input(v) for v in inputs])
    ax.set_ylabel("GPU당 총 tok/s" if ko else "Total tok/s per GPU")
    ax.set_xlabel("입력 토큰" if ko else "Input tokens")
    ax.set_title("GPU당 처리량 비교" if ko else "Per-GPU throughput comparison")
    ax.grid(axis="y", alpha=0.25)
    ax.legend(ncol=3, fontsize=8)
    foot = "FP8 A100은 B200 FP8 대비 GPU당 약 1/4, NVFP4 A100은 짧은 입력에서 B200 FP8 대비 약 1/2 수준." if ko else "A100 FP8 is about one quarter of B200 FP8 per GPU; A100 NVFP4 is about one half of B200 FP8 at short inputs."
    fig.text(0.01, 0.015, foot, fontsize=8, color="#444")
    _save(fig, out_dir / "per-gpu-throughput.png")


def render_cost(data, out_dir):
    plt, ko = _import_matplotlib()
    cost = data["cost_per_million_output_tokens_usd"]
    configs = [
        ("concurrency_32_input_1k_output_1k", "nvfp4_1_node", "C32 1K/1K NVFP4 1 node", "#d62728"),
        ("concurrency_32_input_1k_output_1k", "fp8_2_nodes", "C32 1K/1K FP8 2 nodes", "#ff7f0e"),
        ("concurrency_128_input_1k_output_256", "nvfp4_1_node_batched_2048", "C128 1K/256 NVFP4 1 node", "#a23a3a"),
        ("concurrency_128_input_1k_output_256", "fp8_2_nodes_batched_2048", "C128 1K/256 FP8 2 nodes", "#1f77b4"),
    ]
    xbase = list(range(len(PRICE_METERS)))
    width = 0.19
    fig, ax = plt.subplots(figsize=(10, 5.3))
    for idx, (workload, key, label, color) in enumerate(configs):
        vals = [cost[workload][key][m] for m in PRICE_METERS]
        xs = [x + (idx - 1.5) * width for x in xbase]
        bars = ax.bar(xs, vals, width=width, label=label, color=color, alpha=0.9)
        ax.bar_label(bars, labels=[f"{v:.2f}" for v in vals], fontsize=7, padding=2, rotation=90)
    ax.set_xticks(xbase, [PRICE_LABELS_KO[m] if ko else m.replace("_", " ") for m in PRICE_METERS])
    ax.set_ylabel("USD / 1M 출력 토큰" if ko else "USD per 1M output tokens")
    ax.set_title("출력 100만 토큰당 비용" if ko else "Cost per 1M output tokens")
    ax.grid(axis="y", alpha=0.25)
    ax.legend(ncol=2, fontsize=8)
    fig.text(0.01, 0.015, cost["formula"], fontsize=8, color="#444")
    _save(fig, out_dir / "cost-per-1m-output-tokens.png")


def write_throughput_json(data, out_dir):
    with (out_dir / "throughput.json").open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _ratio(num, den):
    if den == 0 or den is None:
        return ""
    return f"{num / den:.2f}"


def write_tables(data, out_dir):
    lines = ["# 처리량 표", ""]
    b200 = _point_map(data, "b200_fp8_k2")
    native = _point_map(data, "a100_fp8_native")
    dense = _point_map(data, "a100_fp8_dense")
    lines += [
        "## Fig. 7 조건: FP8 총 tok/s",
        "",
        "| 입력 | B200 K2 FP8 | A100x16 native | native/B200 | A100x16 dense | dense/B200 |",
        "| ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for i in INPUTS_FIG7:
        bt, nt, dt = _total(b200[i]), _total(native[i]), _total(dense[i])
        lines.append(f"| {_fmt_input(i)} | {_fmt_int(bt)} | {_fmt_int(nt)} | {_ratio(nt, bt)} | {_fmt_int(dt)} | {_ratio(dt, bt)} |")

    nv = _point_map(data, "a100_nvfp4")
    b200_nv = _point_map(data, "b200_nvfp4_k2_fig9")
    lines += ["", "## NVFP4: A100 측정값과 B200 공개값", "", "| 입력 | A100x8 NVFP4 | vs B200 FP8 Fig.7 | B200 NVFP4 Fig.9 | A100/B200 NVFP4 |", "| ---: | ---: | ---: | ---: | ---: |"]
    for i in sorted(nv):
        at = _total(nv[i])
        lines.append(f"| {_fmt_input(i)} | {_fmt_int(at)} | {_ratio(at, _total(b200[i]))} | {_fmt_int(_total(b200_nv[i]))} | {_ratio(at, _total(b200_nv[i]))} |")

    per_keys = ["b200_fp8_k2", "a100_fp8_native", "a100_fp8_dense", "a100_nvfp4", "b200_nvfp4_k2_fig9"]
    common = set(_point_map(data, per_keys[0]))
    for key in per_keys[1:]:
        common &= set(_point_map(data, key))
    per_inputs = [i for i in [1024, 4096, 8192, 16384, 32768] if i in common]
    lines += ["", "## GPU당 총 tok/s", "", "| 입력 | B200 FP8 K2 | A100 FP8 native | A100 FP8 dense | A100 NVFP4 | B200 NVFP4 |", "| ---: | ---: | ---: | ---: | ---: | ---: |"]
    for i in per_inputs:
        vals = []
        for key in per_keys:
            vals.append(_total(_point_map(data, key)[i]) / data["series"][key]["gpu_count"])
        lines.append(f"| {_fmt_input(i)} | " + " | ".join(_fmt_int(v) for v in vals) + " |")

    cost = data["cost_per_million_output_tokens_usd"]
    lines += ["", "## 출력 100만 토큰당 비용 (USD)", "", "| 워크로드 | 구성 | 종량제 | 1년 예약 | 3년 예약 | Spot | Low priority |", "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    rows = [
        ("C32, 1K in, 1K out", "NVFP4 1 node", cost["concurrency_32_input_1k_output_1k"]["nvfp4_1_node"]),
        ("C32, 1K in, 1K out", "FP8 2 nodes", cost["concurrency_32_input_1k_output_1k"]["fp8_2_nodes"]),
        ("C128, 1K in, 256 out", "NVFP4 1 node", cost["concurrency_128_input_1k_output_256"]["nvfp4_1_node_batched_2048"]),
        ("C128, 1K in, 256 out", "FP8 2 nodes", cost["concurrency_128_input_1k_output_256"]["fp8_2_nodes_batched_2048"]),
    ]
    for workload, config, row in rows:
        lines.append(f"| {workload} | {config} | " + " | ".join(f"{row[m]:.2f}" for m in PRICE_METERS) + " |")
    with (out_dir / "throughput-tables.md").open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


def render_all(out_dir=DEFAULT_OUT, only="throughput"):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    data = load_throughput()
    if only not in ("all", "throughput"):
        raise ValueError(f"Unsupported group: {only}")
    render_fig7_overlay(data, out_dir)
    render_fig9_overlay(data, out_dir)
    render_per_gpu(data, out_dir)
    render_cost(data, out_dir)
    write_throughput_json(data, out_dir)
    write_tables(data, out_dir)
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description="Render SKT/A.X K2 throughput report figures.")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="Output directory (default: docs/report/figures)")
    parser.add_argument("--only", default="throughput", choices=["throughput", "all"], help="Figure group to render")
    args = parser.parse_args(argv)
    render_all(Path(args.out), args.only)


if __name__ == "__main__":
    main()
