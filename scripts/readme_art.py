"""Render the README banner and the study chart as light/dark SVGs.

    python scripts/readme_art.py docs/study/repos.csv

Writes docs/banner-{light,dark}.svg and docs/study/share-{light,dark}.svg.
Chart colours are the validated single-series blue from the dataviz reference palette.
"""

import csv
import math
import statistics
import sys
from pathlib import Path
from xml.sax.saxutils import escape

DOCS = Path(__file__).resolve().parent.parent / "docs"
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, 'Cascadia Mono', Menlo, Consolas, monospace"

# Minimum red runs for a repo to be ranked: shares from a handful of runs are noise.
MIN_RED = 50

THEMES = {
    "light": {
        "surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "grid": "#e1e0d9",
        "bar": "#2a78d6", "brand": "#d1242f", "banner_ink": "#1f2328", "banner_ink2": "#59636e",
    },
    "dark": {
        "surface": "#1a1a19", "ink": "#ffffff", "ink2": "#c3c2b7", "grid": "#2c2c2a",
        "bar": "#3987e5", "brand": "#f85149", "banner_ink": "#f0f6fc", "banner_ink2": "#9198a1",
    },
}


def fish(x: float, y: float, color: str, bg: str, s: float = 1.0) -> str:
    """A small herring: body, tail, eye. (x, y) is the top-left of the body."""
    return (
        f'<ellipse cx="{x + 60 * s}" cy="{y + 26 * s}" rx="{60 * s}" ry="{26 * s}" fill="{color}"/>'
        f'<polygon points="{x + 108 * s},{y + 26 * s} {x + 150 * s},{y + 2 * s} {x + 150 * s},{y + 50 * s}" fill="{color}"/>'
        f'<circle cx="{x + 26 * s}" cy="{y + 22 * s}" r="{6 * s}" fill="{bg}"/>'
    )


def banner(t: dict, mode: str) -> str:
    w, h = 880, 190
    bg = "#ffffff" if mode == "light" else "#0d1117"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="redherring: find the CI failures that weren't your fault">
<title>redherring: find the CI failures that weren't your fault</title>
{fish(150, 44, t["brand"], bg, 0.9)}
<text x="310" y="100" font-family="{FONT}" font-size="76" font-weight="700" fill="{t["brand"]}">red<tspan fill="{t["banner_ink"]}">herring</tspan></text>
<text x="440" y="158" text-anchor="middle" font-family="{FONT}" font-size="26" fill="{t["banner_ink2"]}">Find the CI failures that weren't your fault.</text>
</svg>
"""


def chart(rows: list[dict], t: dict) -> str:
    eligible = [r for r in rows if int(r["went_red"]) >= MIN_RED]
    med = statistics.median(float(r["red_herring_share"]) for r in eligible)
    top = sorted(eligible, key=lambda r: -float(r["red_herring_share"]))[:20]
    left, right, top_pad, row_h, bar_h = 250, 60, 104, 26, 16
    w = 900
    plot_w = w - left - right
    h = top_pad + row_h * len(top) + 76
    # Axis always covers the largest bar: nothing is cut off.
    xmax = max(0.4, math.ceil(max(float(r['red_herring_share']) for r in top) * 10) / 10)

    def x(v: float) -> float:
        return left + plot_w * min(v, xmax) / xmax

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" '
        f'aria-label="Bar chart: the 20 repos with the highest share of red CI runs that were red herrings. '
        f'Median across {len(eligible)} repos is {med:.0%}.">',
        f"<title>Share of red CI runs that were red herrings, top 20 of {len(eligible)} repos</title>",
        f'<rect width="{w}" height="{h}" rx="12" fill="{t["surface"]}"/>',
        f'<text x="28" y="40" font-family="{FONT}" font-size="20" font-weight="600" fill="{t["ink"]}">'
        f"Share of red CI runs that were red herrings</text>",
        f'<text x="28" y="64" font-family="{FONT}" font-size="13" fill="{t["ink2"]}">'
        f"Top 20 of the {len(eligible)} repos with {MIN_RED}+ red runs in 14 days · "
        f"red herring = passed on a plain re-run of the same commit</text>",
    ]
    base = top_pad + row_h * len(top)
    for tick in [i / 10 for i in range(int(round(xmax * 10)) + 1)]:
        out.append(f'<line x1="{x(tick)}" y1="{top_pad - 8}" x2="{x(tick)}" y2="{base}" stroke="{t["grid"]}" stroke-width="1"/>')
        out.append(
            f'<text x="{x(tick)}" y="{base + 20}" text-anchor="middle" font-family="{FONT}" font-size="12" '
            f'fill="{t["ink2"]}">{tick:.0%}</text>'
        )
    for i, r in enumerate(top):
        y = top_pad + i * row_h + (row_h - bar_h) / 2
        v = float(r["red_herring_share"])
        bw = max(2.0, x(v) - left)
        # Bar anchored at the baseline, 4px rounded data end.
        out.append(
            f'<path d="M{left},{y} h{bw - 4} a4,4 0 0 1 4,4 v{bar_h - 8} a4,4 0 0 1 -4,4 h{-(bw - 4)} z" fill="{t["bar"]}">'
            f"<title>{escape(r['repo'])}: {v:.1%} ({r['red_herring_runs']} of {r['went_red']} red runs)</title></path>"
        )
        out.append(
            f'<text x="{left - 10}" y="{y + bar_h - 3}" text-anchor="end" font-family="{MONO}" font-size="12.5" '
            f'fill="{t["ink2"]}">{escape(r["repo"])}</text>'
        )
        out.append(
            f'<text x="{left + bw + 6}" y="{y + bar_h - 3}" font-family="{FONT}" font-size="12" '
            f'fill="{t["ink"]}">{v:.0%}</text>'
        )
    mx = x(med)
    out.append(
        f'<line x1="{mx}" y1="{top_pad - 18}" x2="{mx}" y2="{base}" stroke="{t["ink2"]}" stroke-width="1.5" '
        f'stroke-dasharray="4 3"/>'
    )
    out.append(
        f'<text x="{mx + 6}" y="{top_pad - 14}" font-family="{FONT}" font-size="12" fill="{t["ink2"]}">'
        f"median of these {len(eligible)}: {med:.0%}</text>"
    )
    out.append(
        f'<text x="28" y="{h - 18}" font-family="{FONT}" font-size="12" fill="{t["ink2"]}">'
        f"Source: redherring study, 28 Sep 2026 · all {len(rows)} repos in docs/study/repos.csv</text>"
    )
    out.append("</svg>\n")
    return "\n".join(out)


def main(csv_path: str) -> None:
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))
    for mode, t in THEMES.items():
        (DOCS / f"banner-{mode}.svg").write_text(banner(t, mode), encoding="utf-8")
        (DOCS / "study" / f"share-{mode}.svg").write_text(chart(rows, t), encoding="utf-8")
    print("wrote banners and charts")


if __name__ == "__main__":
    main(sys.argv[1])
