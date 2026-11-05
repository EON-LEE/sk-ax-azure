"""Shared drawing helpers for the pack's figures (1920x1080 PNG, Korean labels).

Everything is drawn in "slide units": 100 wide x 56.25 high (1 unit = 0.1333 in = 9.6 pt).
Text is measured with the real renderer and wrapped to the box; an overflow is recorded in
WARN and makes make_figures.py exit with an error instead of silently shipping a broken slide.
"""
import warnings
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib import font_manager as fm
from matplotlib.font_manager import FontProperties
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Rectangle
from matplotlib.ticker import FuncFormatter

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent.parent
EV = (REPO / "evidence", REPO / "docs" / "report", REPO / "docs" / "report" / "figures")
FIG = ROOT / "figures"
TAB = ROOT / "tables"

DPI = 144
W_IN, H_IN = 1920 / DPI, 1080 / DPI  # exactly 1920 x 1080 px
UW, UH = 100.0, 56.25
PT_PER_UNIT = 72 * W_IN / UW  # 9.6 pt per unit

NAVY, SKY, BLUE, ORANGE, VERM, GREEN, MINT = "#1B3A5C", "#56B4E9", "#0072B2", "#E69F00", "#D55E00", "#009E73", "#66C2A5"
INK, GREY, LIGHT, MID, RED = "#1F2933", "#5F6B7A", "#EEF1F5", "#C5CCD6", "#B42318"
PALE_BLUE, PALE_GREEN, PALE_AMBER, PALE_RED = "#E8F1FA", "#E6F5EF", "#FFF4DB", "#FDECEA"

LAYOUT = {
    "A": dict(key="epc-fp8-tp8pp2", label="TP8×PP2 (기준)", short="TP8×PP2", color=NAVY, fmt="FP8"),
    "B0": dict(key="epc-fp8-tp16", label="TP16", short="TP16", color=SKY, fmt="FP8"),
    "B": dict(key="epc-fp8-tp16ep16", label="TP16+EP16", short="TP16+EP16", color=BLUE, fmt="FP8"),
    "C1": dict(key="epc-fp8-tp8pp2ep8", label="TP8×PP2+EP8", short="TP8×PP2+EP8", color=ORANGE, fmt="FP8"),
    "C2": dict(key="epc-fp8-dp2tp8ep16", label="DP2×TP8+EP16", short="DP2×TP8+EP16", color=VERM, fmt="FP8"),
    "D": dict(key="epc-nvfp4-tp8", label="NVFP4 TP8 (기준)", short="NVFP4 TP8", color=GREEN, fmt="NVFP4"),
    "E": dict(key="epc-nvfp4-tp8ep8", label="NVFP4 TP8+EP8", short="NVFP4 TP8+EP8", color=MINT, fmt="NVFP4"),
}

WARN = []
_probe = None


def _mark(ax, y):
    """Remember the lowest drawn y on the overlay axes so save() can check it against the footnote."""
    ax._low = min(getattr(ax, "_low", 1e9), y)


def setup_fonts():
    names = {f.name for f in fm.fontManager.ttflist}
    for name in ("Malgun Gothic", "NanumGothic", "Noto Sans CJK KR", "Apple SD Gothic Neo", "AppleGothic"):
        if name in names:
            plt.rcParams["font.family"] = name
            break
    else:
        raise SystemExit("No Korean font found. Install Malgun Gothic or Noto Sans CJK KR.")
    plt.rcParams["axes.unicode_minus"] = False
    # Default hinting drops the vowel stem of some Hangul syllables (e.g. 같) at regular weight.
    plt.rcParams["text.hinting"] = "no_hinting"
    warnings.filterwarnings("error", message=r"Glyph \d+ .* missing from font")


setup_fonts()


def _renderer():
    global _probe
    if _probe is None:
        _probe = plt.figure(figsize=(W_IN, H_IN), dpi=DPI)
    return _probe.canvas.get_renderer()


def text_w(s, fs, bold=False):
    """Width of one line, in slide units."""
    fp = FontProperties(family=plt.rcParams["font.family"], size=fs, weight="bold" if bold else "normal")
    w, _, _ = _renderer().get_text_width_height_descent(s, fp, False)
    return w / (DPI * W_IN) * UW


def pitch(fs, factor=1.34):
    """Line-to-line distance for font size fs, in slide units."""
    return fs * factor / PT_PER_UNIT


def wrap(s, max_u, fs, bold=False):
    """Greedy word wrap that also breaks over-long words by character."""
    out = []
    for para in str(s).split("\n"):
        if para == "":
            out.append("")
            continue
        line = ""
        for word in para.split(" "):
            cand = f"{line} {word}" if line else word
            if text_w(cand, fs, bold) <= max_u:
                line = cand
                continue
            if line:
                out.append(line)
                line = ""
            while text_w(word, fs, bold) > max_u and len(word) > 1:
                cut = len(word)
                while cut > 1 and text_w(word[:cut], fs, bold) > max_u:
                    cut -= 1
                out.append(word[:cut])
                word = word[cut:]
            line = word
        out.append(line)
    return out


def fit_fs(s, max_u, fs, min_fs=10, bold=False):
    while fs > min_fs and text_w(s, fs, bold) > max_u:
        fs -= 0.5
    return fs


def put_lines(ax, x, y, w, h, lines, fs, color=INK, bold=False, ha="center", va="center", pad=1.0, tag="", factor=1.34):
    """Draw pre-wrapped lines inside the rectangle (x, y, w, h)."""
    p = pitch(fs, factor)
    total = p * len(lines)
    if total > h - 0.4 + 1e-6:
        WARN.append(f"text taller than box ({total:.1f} > {h:.1f}): {tag or lines[0][:40]!r}")
    top = {"top": y + h - pad * 0.6, "center": y + h / 2 + total / 2, "bottom": y + pad * 0.6 + total}[va]
    for i, ln in enumerate(lines):
        if ha == "center":
            px = x + w / 2
        elif ha == "left":
            px = x + pad
        else:
            px = x + w - pad
        ax.text(px, top - i * p - p * 0.5, ln, fontsize=fs, color=color, ha=ha, va="center",
                fontweight="bold" if bold else "normal", zorder=6)


def box(ax, x, y, w, h, text="", fc="white", ec=NAVY, lw=1.6, ls="-", fs=14, tc=INK, bold=False,
        ha="center", va="center", pad=1.0, radius=0.8, zorder=2, alpha=1.0):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={radius}", fc=fc, ec=ec,
                                lw=lw, ls=ls, zorder=zorder, alpha=alpha))
    _mark(ax, y)
    if text:
        lines = wrap(text, w - 2 * pad, fs, bold)
        put_lines(ax, x, y, w, h, lines, fs, tc, bold, ha, va, pad, tag=text[:40])


def label(ax, x, y, text, fs=13, color=INK, bold=False, ha="left", va="center", max_w=None):
    if max_w:
        fs = fit_fs(text, max_w, fs, bold=bold)
    _mark(ax, y - 0.6)
    ax.text(x, y, text, fontsize=fs, color=color, ha=ha, va=va, fontweight="bold" if bold else "normal", zorder=6)


def card(ax, x, y, w, h, head, body, ec=NAVY, fc="white", hfs=14, fs=12.5, hc=None, ls="-", lw=1.6, ha="left"):
    """Box with a bold heading and a wrapped body (paragraphs split by newline)."""
    box(ax, x, y, w, h, "", fc=fc, ec=ec, ls=ls, lw=lw)
    hl = wrap(head, w - 2.4, hfs, True)
    ph = pitch(hfs)
    put_lines(ax, x, y + h - ph * len(hl) - 1.0, w, ph * len(hl) + 0.8, hl, hfs, hc or ec, True, ha, "center", 1.2, tag=head)
    bl = wrap(body, w - 2.4, fs)
    put_lines(ax, x, y, w, h - ph * len(hl) - 1.2, bl, fs, INK, False, ha, "top", 1.2, tag=head + ": body")


def arrow(ax, x1, y1, x2, y2, color=NAVY, lw=2.4, style="-|>", ls="-", ms=16, rad=0.0, zorder=4):
    ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle=style, mutation_scale=ms, color=color, lw=lw,
                                 ls=ls, connectionstyle=f"arc3,rad={rad}", zorder=zorder, shrinkA=0, shrinkB=0))


def slide(title, subtitle=None, note=None):
    """New 16:9 figure with a drawing overlay (slide units); returns (fig, ax)."""
    fig = plt.figure(figsize=(W_IN, H_IN), dpi=DPI, facecolor="white")
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, UW)
    ax.set_ylim(0, UH)
    ax.axis("off")
    ax.set_zorder(10)
    ax.patch.set_alpha(0)
    fs = fit_fs(title, 93.6, 25, 18, bold=True)
    ax.text(3.2, 54.2, title, fontsize=fs, fontweight="bold", color=NAVY, va="top", ha="left")
    if subtitle:
        sfs = fit_fs(subtitle, 93.6, 14.5, 11)
        ax.text(3.2, 50.2, subtitle, fontsize=sfs, color=GREY, va="top", ha="left")
    if note:
        lines = wrap(note, 93.6, 10.5)
        p = pitch(10.5, 1.3)
        for i, ln in enumerate(lines):
            ax.text(3.2, 1.0 + (len(lines) - 1 - i) * p + p * 0.5, ln, fontsize=10.5, color=GREY, va="center", ha="left")
        fig._note_top = 1.0 + len(lines) * p
    else:
        fig._note_top = 1.0
    return fig, ax


def panel(fig, x, y, w, h):
    """Chart axes placed in slide units."""
    axc = fig.add_axes([x / UW, y / UH, w / UW, h / UH])
    axc.set_zorder(1)
    for s in ("top", "right"):
        axc.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        axc.spines[s].set_color(MID)
    axc.tick_params(colors=INK, labelsize=12, length=0)
    axc.yaxis.grid(True, color=LIGHT, lw=1.2)
    axc.set_axisbelow(True)
    axc.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
    return axc


def legend_row(ax, items, x, y, fs=13, gap=3.0, sq=1.7):
    cx = x
    for text, color in items:
        ax.add_patch(Rectangle((cx, y - sq / 2), sq, sq, fc=color, ec="none", zorder=6))
        label(ax, cx + sq + 0.8, y, text, fs=fs)
        cx += sq + 0.8 + text_w(text, fs) + gap
    return cx


def grouped_bars(axc, groups, series, fmt="{:,.0f}", ymax=None, fs_val=11, fs_tick=13, ratios=None, width=0.82):
    """series: [(label, values, color)]; ratios: parallel lists of ratio-or-None (drawn above the value)."""
    n = len(series)
    bw = width / n
    xs = np.arange(len(groups))
    top = 0
    for si, (_, vals, col) in enumerate(series):
        off = (si - (n - 1) / 2) * bw
        bars = axc.bar(xs + off, vals, bw * 0.92, color=col, zorder=3)
        for gi, (b, v) in enumerate(zip(bars, vals)):
            top = max(top, v)
            axc.text(b.get_x() + b.get_width() / 2, v, fmt.format(v), ha="center", va="bottom", fontsize=fs_val, color=INK)
            if ratios and ratios[si][gi] is not None:
                r = ratios[si][gi]
                axc.annotate(f"×{r:.2f}", (b.get_x() + b.get_width() / 2, v), xytext=(0, fs_val + 5),
                             textcoords="offset points", ha="center", va="bottom", fontsize=fs_val + 0.5,
                             fontweight="bold", color=GREEN if r >= 1 else VERM)
    axc.set_xticks(xs)
    axc.set_xticklabels(groups, fontsize=fs_tick, color=INK)
    axc.set_ylim(0, ymax or top * 1.28)
    return axc


def table(ax, x, y_top, cols, rows, fs=12.5, hfs=12.5, padx=0.9, pady=0.75, head_fc=NAVY, zebra=("white", LIGHT)):
    """Draw a table in slide units. cols: [(title, width, align)]. A cell is a str or (str, {c, b, fc}).
    Returns the y coordinate of the table's bottom edge."""
    y = y_top
    heads = [wrap(t, w - 2 * padx, hfs, True) for t, w, _ in cols]
    hh = max(len(h) for h in heads) * pitch(hfs) + 2 * pady
    ax.add_patch(Rectangle((x, y - hh), sum(c[1] for c in cols), hh, fc=head_fc, ec="none", zorder=2))
    cx = x
    for (t, w, al), lines in zip(cols, heads):
        put_lines(ax, cx, y - hh, w, hh, lines, hfs, "white", True, "center" if al == "c" else "left", "center", padx, tag=t)
        cx += w
    y -= hh
    for ri, row in enumerate(rows):
        cells = []
        for (t, w, al), cell in zip(cols, row):
            txt, st = (cell, {}) if isinstance(cell, str) else cell
            cells.append((wrap(txt, w - 2 * padx, fs, st.get("b", False)), st))
        rh = max(len(c[0]) for c in cells) * pitch(fs) + 2 * pady
        ax.add_patch(Rectangle((x, y - rh), sum(c[1] for c in cols), rh, fc=zebra[ri % 2], ec="none", zorder=2))
        cx = x
        for (t, w, al), (lines, st) in zip(cols, cells):
            if st.get("fc"):
                ax.add_patch(Rectangle((cx, y - rh), w, rh, fc=st["fc"], ec="none", zorder=2.5))
            put_lines(ax, cx, y - rh, w, rh, lines, fs, st.get("c", INK), st.get("b", False),
                      "center" if al == "c" else "left", "center", padx, tag=lines[0] if lines else "")
            cx += w
        ax.plot([x, x + sum(c[1] for c in cols)], [y - rh, y - rh], color=MID, lw=0.8, zorder=3)
        y -= rh
    _mark(ax, y)
    return y


def save(fig, name):
    ax = fig.axes[0]
    low, top = getattr(ax, "_low", None), getattr(fig, "_note_top", 1.0)
    if low is not None and low < top + 0.6:
        WARN.append(f"{name}: content reaches y={low:.1f}, footnote top is y={top:.1f}")
    fig.savefig(FIG / name, dpi=DPI, facecolor="white")
    plt.close(fig)
    print("  wrote figures/" + name)
