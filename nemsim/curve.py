"""Draw the accuracy curve: python -m nemsim.curve  ->  docs/accuracy_curve.svg

Bars: each built rung's share of region-intervals within $5 of the actual price, over the region-
intervals every rung scores, with its share of the reachable bound. Reference lines: the single-price
oracle bound (nemsim/ceiling.py), drawn only over the single-price rungs, and nempy's published 89.5%,
which comes from a different sample. Only aggregate figures go into the SVG; no AEMO rows.

A static SVG cannot carry the hover layer an interactive chart would; the README's table holds the
same numbers.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from nemsim.config import ROOT
from nemsim.intervals import MONTHS

NEMPY = 89.5   # Eschenbaum, arXiv:2607.13002 v3, Appendix B; a different sample from this project's

SLOTS = [  # key, two-line label, bound key
    ("r1", ("1. Offers as declared", ""), "single_price"),
    ("r2", ("2. + AEMO dispatch", "availability"), "single_price"),
    ("r3a", ("3a. Regional, flows", "held at actual (lift)"), "regional_stack"),
    ("r3b", ("3b. Flows solved,", "losses, ramps"), None),
    ("r4", ("4. Network", "constraints"), "network"),
]
MECHANICAL = {"r3a"}   # drawn outlined: told the flows, so not a modelling gain
BOUND_LABEL = {"single_price": "single-price oracle bound", "regional_stack": "regional-stack bound",
               "network": "full-network bound"}

STYLE = """
  .surface { fill: #fcfcfb; } .bar { fill: #2a78d6; }
  .ink { fill: #0b0b0b; } .ink2 { fill: #52514e; } .muted { fill: #898781; }
  .grid { stroke: #e1e0d9; } .axis { stroke: #c3c2b7; }
  .ceiling { stroke: #52514e; } .nempy { stroke: #898781; } .empty { fill: none; stroke: #c3c2b7; }
  .lift { fill: none; stroke: #2a78d6; }
  @media (prefers-color-scheme: dark) {
    .surface { fill: #1a1a19; } .bar { fill: #3987e5; }
    .ink { fill: #f0efec; } .ink2 { fill: #c3c2b7; }
    .grid { stroke: #2c2c2a; } .axis { stroke: #383835; }
    .ceiling { stroke: #c3c2b7; } .empty { stroke: #383835; } .lift { stroke: #3987e5; }
  }
  text { font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; }
"""


RUNGS = ("r1", "r2", "r3a", "r3b", "r4")


def measures() -> tuple[dict, dict]:
    """Within $5 per rung and each bound, for all region-intervals and for the $300+ band."""
    from nemsim.ceiling import common_rows, load, oracle_hits
    keys = [k for k in RUNGS if (ROOT / "results" / f"{MONTHS[0]}__{k}__rrp.csv").exists()]
    common = common_rows(keys)
    frames = {k: load(k).merge(common) for k in keys}
    base = frames[keys[0]]
    oracle = base.merge(oracle_hits(base[["interval", "region", "rrp"]]))
    regional = pd.read_csv(ROOT / "results" / "bound_regional_stack.csv", parse_dates=["interval"])
    regional = base.merge(regional, how="left").stack_reachable.fillna(False).astype(bool)
    network = pd.read_csv(ROOT / "results" / "bound_network.csv", parse_dates=["interval"], low_memory=False)
    network = base.merge(network, how="left").network_reachable.fillna(False).astype(bool)
    out = {}
    for name, sel in (("all", slice(None)), ("300", base.price_class == "d. $300+")):
        mask = np.ones(len(base), bool) if name == "all" else sel.to_numpy()
        score = {k: (f.abs_error.to_numpy()[mask] <= 5).mean() * 100 for k, f in frames.items()}
        bounds = {"single_price": oracle.oracle_hit.to_numpy()[mask].mean() * 100,
                  "regional_stack": regional.to_numpy()[mask].mean() * 100,
                  "network": network.to_numpy()[mask].mean() * 100}
        out[name] = (score, bounds)
    return out["all"], out["300"]


def svg(score: dict[str, float], bounds: dict[str, float], title: str, subtitle: str, show_nempy: bool) -> str:
    w, h = 760, 466
    left, right, top, bottom = 56, 24, 76, 92
    pw, ph = w - left - right, h - top - bottom
    y = lambda v: top + ph * (1 - v / 100)  # noqa: E731
    slot = pw / len(SLOTS)
    bw = 52
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" '
           f'aria-labelledby="title desc">', f'<title id="title">{title}</title>',
           '<desc id="desc">' + "; ".join(f"Rung {k[1:]}: {v:.1f}%" for k, v in score.items())
           + "; " + "; ".join(f"{BOUND_LABEL[k]} {v:.1f}%" for k, v in bounds.items()) + ".</desc>",
           f"<style>{STYLE}</style>", f'<rect class="surface" width="{w}" height="{h}" rx="8"/>',
           f'<text class="ink" x="{left}" y="30" font-size="16" font-weight="600">{title}</text>',
           f'<text class="ink2" x="{left}" y="50" font-size="12">{subtitle}</text>']
    for v in (0, 25, 50, 75, 100):
        cls = "axis" if v == 0 else "grid"
        out.append(f'<line class="{cls}" x1="{left}" x2="{w - right}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke-width="1"/>')
        out.append(f'<text class="muted" x="{left - 8}" y="{y(v) + 4:.1f}" font-size="11" text-anchor="end">{v}%</text>')
    if show_nempy:
        out.append(f'<line class="nempy" x1="{left}" x2="{w - right}" y1="{y(NEMPY):.1f}" y2="{y(NEMPY):.1f}" '
                   f'stroke-width="1.5" stroke-dasharray="2 4"/>')
        out.append(f'<text class="ink2" x="{left + 4}" y="{y(NEMPY) - 6:.1f}" font-size="11">nempy, a '
                   f'full NEMDE model: {NEMPY}% (different sample)</text>')
    prev = None
    for i, (key, (l1, l2), bkey) in enumerate(SLOTS):
        cx = left + slot * (i + 0.5)
        if key in score:
            v = score[key]
            top_y, base = y(v), y(0)
            r = min(4, max((base - top_y) / 2, 0))
            cls = "lift" if key in MECHANICAL else "bar"
            stroke = ' stroke-width="2" stroke-dasharray="4 3"' if key in MECHANICAL else ""
            out.append(f'<path class="{cls}"{stroke} d="M{cx - bw / 2:.1f},{base:.1f} V{top_y + r:.1f} '
                       f'Q{cx - bw / 2:.1f},{top_y:.1f} {cx - bw / 2 + r:.1f},{top_y:.1f} H{cx + bw / 2 - r:.1f} '
                       f'Q{cx + bw / 2:.1f},{top_y:.1f} {cx + bw / 2:.1f},{top_y + r:.1f} V{base:.1f} Z"/>')
            out.append(f'<text class="ink" x="{cx:.1f}" y="{top_y - (22 if bkey else 8):.1f}" font-size="13" '
                       f'font-weight="600" text-anchor="middle">{v:.1f}%</text>')
            if bkey and bkey in bounds and bounds[bkey] > 0:
                share = v / bounds[bkey] * 100
                out.append(f'<text class="ink2" x="{cx:.1f}" y="{top_y - 8:.1f}" font-size="11" '
                           f'text-anchor="middle">{share:.{0 if share >= 10 else 1}f}% of bound</text>')
        else:
            out.append(f'<rect class="empty" x="{cx - bw / 2:.1f}" y="{y(100):.1f}" width="{bw}" height="{ph:.1f}" '
                       f'rx="4" stroke-dasharray="3 3"/>')
        if bkey and bkey in bounds:
            c = bounds[bkey]
            x0 = left + slot * i + (6 if bkey != prev else 0)
            x1 = left + slot * (i + 1) - (6 if i + 1 >= len(SLOTS) or SLOTS[i + 1][2] != bkey else 0)
            out.append(f'<line class="ceiling" x1="{x0:.1f}" x2="{x1:.1f}" y1="{y(c):.1f}" y2="{y(c):.1f}" '
                       f'stroke-width="2" stroke-dasharray="6 4"/>')
            if bkey != prev:
                out.append(f'<text class="ink2" x="{x0:.1f}" y="{y(c) - 7:.1f}" font-size="11">'
                           f'{BOUND_LABEL[bkey]}: {c:.1f}%</text>')
        prev = bkey
        out.append(f'<text class="ink" x="{cx:.1f}" y="{h - bottom + 20}" font-size="11.5" text-anchor="middle">{l1}</text>')
        out.append(f'<text class="ink2" x="{cx:.1f}" y="{h - bottom + 35}" font-size="11.5" text-anchor="middle">{l2}</text>')
    out.append(f'<text class="muted" x="{left}" y="{h - 38}" font-size="10.5">Outlined bar: the mechanical lift, told '
               f'the actual flows, not a modelling gain. Dashed steps: the most a model of that kind could</text>')
    out.append(f'<text class="muted" x="{left}" y="{h - 24}" font-size="10.5">score, found knowing the answer; '
               f'each is drawn only over the rungs it bounds.</text>')
    out.append(f'<text class="muted" x="{left}" y="{h - 10}" font-size="10.5">Contains AEMO data; see '
               f'docs/stage2_results.md.</text>')
    out.append("</svg>")
    return "\n".join(out)


def main() -> None:
    (all_score, all_bounds), (hi_score, hi_bounds) = measures()
    months = len(MONTHS)
    pages = {
        "accuracy_curve.svg": svg(all_score, all_bounds, "How much of the price each rung explains",
                                  f"Share of region-intervals within $5 of the actual price. {months} months, the same "
                                  f"region-intervals for every rung.", True),
        "accuracy_curve_300.svg": svg(hi_score, hi_bounds, "Where the money is: intervals above $300",
                                      "Share within $5, region-intervals with an actual price of $300 or more: 1.1% of "
                                      "intervals, about 30% of settled value.", False),
    }
    for name, text in pages.items():
        (ROOT / "docs" / name).write_text(text)
        print(f"wrote docs/{name}")


if __name__ == "__main__":
    main()
