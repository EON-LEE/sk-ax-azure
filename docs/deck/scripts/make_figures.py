"""Builds every figure and CSV table of the pack.

Usage (from this folder):
    python make_figures.py                 # all figures + tables
    python make_figures.py s05 s09         # only figures whose function name starts with these prefixes
    python make_figures.py --review DIR    # also write 1280x720 preview copies into DIR

Reads the repository evidence/ and docs/report/ JSON, writes ../figures (1920x1080 PNG) and ../tables (CSV, UTF-8 with BOM).
Needs: Python 3.10+, matplotlib, numpy, pillow, and a Korean font (Malgun Gothic, NanumGothic or Noto Sans CJK KR).
"""
import importlib
import sys
from pathlib import Path

import common
import data

MODULES = ["figs_context", "figs_parallel", "figs_summary", "figs_layouts", "figs_extra"]


def main(argv):
    review = None
    if "--review" in argv:
        i = argv.index("--review")
        review = Path(argv[i + 1])
        review.mkdir(parents=True, exist_ok=True)
        argv = argv[:i] + argv[i + 2:]
    only = [a for a in argv if not a.startswith("--")]

    common.FIG.mkdir(parents=True, exist_ok=True)
    data.write_all_tables()
    made = []
    for name in MODULES:
        if not (Path(__file__).parent / f"{name}.py").exists():
            continue
        mod = importlib.import_module(name)
        for fn in mod.ALL:
            if only and not any(fn.__name__.startswith(o) for o in only):
                continue
            fn()
            made.append(fn.__name__)

    if review:
        from PIL import Image
        for p in sorted(common.FIG.glob("*.png")):
            im = Image.open(p)
            im.resize((1280, 720), Image.LANCZOS).save(review / p.name)

    if common.WARN:
        print("\nLAYOUT WARNINGS:")
        for w in common.WARN:
            print("  -", w)
        return 1
    print(f"\nOK: {len(made)} figure functions ran, no layout warnings.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
