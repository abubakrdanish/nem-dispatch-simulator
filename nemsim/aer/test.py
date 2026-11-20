"""Does rung 4 do better on spikes the AER attributes to the network? python -m nemsim.aer.test

Fixed before any label was seen (2026-11-16), so the test cannot be fitted to its result:

- **Join.** An AER event is a trading day in one or more regions. It covers the scored region-intervals of
  those regions whose interval start (DISPATCHPRICE settlement time minus 5 minutes) falls on that date.
  Scored results are the published ones (results/<month>__<rung>__rrp.csv, nemsim/ladder.frame on the
  seventeen months, the rows all five rungs scored): no rung is re-scored.
- **Population.** Region-intervals with an actual price (DISPATCHPRICE.RRP, pricing run) of $5,000/MWh or
  more, the AER's reporting threshold. Reported beside it: every $300+ region-interval in an AER-labelled
  day-region. $300+ region-intervals on days the AER did not report form their own bucket.
- **Network-attributed.** Primary: the event carries the network_outage label. Broader: network_outage,
  interconnector_limit or constraint_violation.
- **The prediction holds** if rung 4's within-$5 share on network-attributed $5,000+ region-intervals is
  higher than on the rest and the 95% day-block intervals do not overlap. Higher but overlapping is
  "consistent, not shown". Lower is "does not hold".

Every cause cell gives region-intervals, events and days, so thin cells show. Labels are evidence about
what the AER said, not about the market: a cell is "intervals the AER attributed to X".

The four-way network-cause partition and its leave-one-month-out checks were added after seeing the
outage result.  They are exploratory, not part of the rule above.  They use the reasoning-on labels only
and do not borrow the outage test's pre-registered verdict rule.
"""
from __future__ import annotations

import json
import sys
from datetime import timedelta

import numpy as np
import pandas as pd

from nemsim.aer.accuracy import usable_labels
from nemsim.aer.extract import PRIMARY, labels_file
from nemsim.config import ROOT
from nemsim.ladder import SAMPLES, frame

OUT = ROOT / "results" / "aer_cause_test.csv"
NETWORK = {"network_outage"}
NETWORK_BROAD = {"network_outage", "interconnector_limit", "constraint_violation"}
REPORT_THRESHOLD = 5000.0
EXPLORATORY_CELLS = (
    "network outage named",
    "interconnector limit named, no outage",
    "constraint violation named, neither",
    "none of the three named",
)


def out_file(config: str = PRIMARY):
    return OUT if config == PRIMARY else OUT.with_name(f"aer_cause_test__{config}.csv")


def event_rows(config: str = PRIMARY) -> pd.DataFrame:
    """One row per (date, region), retaining both all named and usable causes.

    The pre-registered outage test uses only accuracy-qualified ``causes``.  The
    requested exploratory partition uses ``named_causes`` for its three named
    categories, so its zero-sized constraint-violation-only cell remains visible
    even though that label failed the accuracy threshold.
    """
    ev = json.loads(labels_file(config).read_text())["events"]
    keep = usable_labels(config)
    rows = []
    for e in ev:
        named = {c["label"] for c in e["causes"]}
        causes = named & keep
        for r in e["regions"]:
            rows.append({"date": e["event_date"], "region": r, "causes": causes, "named_causes": named,
                         "url": e["url"],
                         "network_statement": e["network_outages"]["value"]})
    d = pd.DataFrame(rows)
    if d.empty:
        return d
    return d.groupby(["date", "region"]).agg(
        causes=("causes", lambda s: set().union(*s)), named_causes=("named_causes", lambda s: set().union(*s)),
        urls=("url", lambda s: sorted(set(s))),
        network_statement=("network_statement", lambda s: "contributed" if "contributed" in set(s)
                           else "did_not_contribute" if "did_not_contribute" in set(s) else "not_stated")
    ).reset_index()


def scored() -> pd.DataFrame:
    f, _ = frame(SAMPLES["seventeen"])
    r4, r2 = f["r4"], f["r2"]
    x = r4[["interval", "region", "rrp", "price_class", "weight", "abs_error", "target", "month"]].copy()
    x["r2_abs_error"] = r2.abs_error.to_numpy()
    x["date"] = (x.interval - timedelta(minutes=5)).dt.strftime("%Y-%m-%d")
    return x


def _day_interval(g: pd.DataFrame, numerator, denominator=None, reps: int = 2000) -> tuple[float, float]:
    """95% interval for a mean or ratio, resampling whole market days."""
    if not len(g):
        return float("nan"), float("nan")
    n = np.asarray(numerator, dtype=float)
    d = np.ones(len(g)) if denominator is None else np.asarray(denominator, dtype=float)
    day = pd.DataFrame({"date": g.date.to_numpy(), "n": n, "d": d}).groupby("date")[["n", "d"]].sum()
    rng = np.random.default_rng(0)
    idx = rng.integers(0, len(day), (reps, len(day)))
    ns = day.n.to_numpy()[idx].sum(1)
    ds = day.d.to_numpy()[idx].sum(1)
    return tuple(np.percentile(ns / ds * 100, [2.5, 97.5]))


def measures(g: pd.DataFrame) -> dict:
    out = {"region_intervals": len(g), "days": g.date.nunique(), "day_regions": len(g[["date", "region"]].drop_duplicates())}
    if not len(g):
        return out
    w = g.weight.abs()
    for rung, col in (("r4", "abs_error"), ("r2", "r2_abs_error")):
        hit5 = g[col] <= 5
        hit10 = g[col] <= 0.1 * g.target.abs()
        lo5, hi5 = _day_interval(g, hit5)
        lo10, hi10 = _day_interval(g, hit10)
        low, hiw = _day_interval(g, w * hit5, w)
        out.update({
            f"{rung} within $5": hit5.mean() * 100,
            f"{rung} ci_lo": lo5, f"{rung} ci_hi": hi5,
            f"{rung} within 10%": hit10.mean() * 100,
            f"{rung} within 10% ci_lo": lo10, f"{rung} within 10% ci_hi": hi10,
            f"{rung} within $5 by $": (w * hit5).sum() / w.sum() * 100,
            f"{rung} within $5 by $ ci_lo": low, f"{rung} within $5 by $ ci_hi": hiw,
        })
    return out


def _exploratory_cell(causes: set[str]) -> str:
    if "network_outage" in causes:
        return EXPLORATORY_CELLS[0]
    if "interconnector_limit" in causes:
        return EXPLORATORY_CELLS[1]
    if "constraint_violation" in causes:
        return EXPLORATORY_CELLS[2]
    return EXPLORATORY_CELLS[3]


def leave_one_month_out(x: pd.DataFrame) -> list[dict]:
    """Exploratory rung-4 within-$5 contrasts with each sample month removed."""
    rows = []
    p = x[(x.rrp >= REPORT_THRESHOLD) & x.labelled]
    for month in SAMPLES["seventeen"]:
        g = p[p.month != month]
        for split, has in (
            ("outage", g.named_causes.apply(lambda c: "network_outage" in c)),
            ("any network cause", g.named_causes.apply(lambda c: bool(c & NETWORK_BROAD))),
        ):
            a, b = g[has], g[~has]
            ah, bh = (a.abs_error <= 5).mean() * 100, (b.abs_error <= 5).mean() * 100
            rows.append({
                "population": "$5,000+ leave one month out", "group": f"exploratory: {split} contrast",
                "held_out_month": month, "region_intervals": len(g),
                "group_a_region_intervals": len(a), "group_b_region_intervals": len(b),
                "r4 group A within $5": ah, "r4 group B within $5": bh,
                "r4 within $5 gap pp": ah - bh,
            })
    return rows


def run(config: str = PRIMARY) -> pd.DataFrame:
    """The test on one configuration's labels. The June 2025 rows were added after the first run showed that the
    three June 2025 days carried most of the non-network side (2026-11-17); they are a robustness check on the
    pre-registered rule, which is applied to the full population only."""
    x = scored()
    ev = event_rows(config)
    x = x.merge(ev, how="left", on=["date", "region"])
    x["labelled"] = x.urls.notna()
    x["causes"] = x.causes.apply(lambda c: c if isinstance(c, set) else set())
    x["named_causes"] = x.named_causes.apply(lambda c: c if isinstance(c, set) else set())
    rows = []
    june25 = x.date.str.startswith("2025-06")
    for pop, sel in (("$5,000+", x.rrp >= REPORT_THRESHOLD), ("$5,000+ without June 2025", (x.rrp >= REPORT_THRESHOLD) & ~june25),
                     ("$300+", x.price_class == "d. $300+")):
        p = x[sel]
        rows.append({"population": pop, "group": "all", **measures(p)})
        rows.append({"population": pop, "group": "in an AER-reported day-region", **measures(p[p.labelled])})
        rows.append({"population": pop, "group": "not in an AER-reported day-region", **measures(p[~p.labelled])})
        lab = p[p.labelled]
        for label in sorted(set().union(*lab.causes) if len(lab) else []):
            rows.append({"population": pop, "group": f"cause: {label}", **measures(lab[lab.causes.apply(lambda c: label in c)])})
        rows.append({"population": pop, "group": "cause: none usable", **measures(lab[lab.causes.apply(len) == 0])})
        for name, net in (("network", NETWORK), ("network, broad", NETWORK_BROAD)):
            has = lab.causes.apply(lambda c: bool(c & net))
            rows.append({"population": pop, "group": f"{name}-attributed", **measures(lab[has])})
            rows.append({"population": pop, "group": f"not {name}-attributed", **measures(lab[~has])})
        for v in ("contributed", "did_not_contribute", "not_stated"):
            rows.append({"population": pop, "group": f"AER statement on network outages: {v}",
                         **measures(lab[lab.network_statement == v])})
    if config == PRIMARY:
        # Exploratory: mutually exclusive priority order specified after the
        # outage result was known.  Raw, verbatim reasoning-on names are used
        # for the three requested labels; no pre-registered verdict is applied.
        p = x[(x.rrp >= REPORT_THRESHOLD) & x.labelled].copy()
        p["exploratory_cell"] = p.named_causes.apply(_exploratory_cell)
        for cell in EXPLORATORY_CELLS:
            rows.append({"population": "$5,000+", "group": f"exploratory: {cell}",
                         **measures(p[p.exploratory_cell == cell])})
        has = p.named_causes.apply(lambda c: bool(c & NETWORK_BROAD))
        rows.append({"population": "$5,000+", "group": "exploratory: any network cause named",
                     **measures(p[has])})
        # The comparison row is already the fourth mutually exclusive cell.
        assert (p[~has].exploratory_cell == EXPLORATORY_CELLS[3]).all()
        rows.extend(leave_one_month_out(x))
    out = pd.DataFrame(rows).assign(config=config)
    out.to_csv(out_file(config), index=False)
    with pd.option_context("display.width", 250, "display.max_columns", 30, "display.max_rows", 200):
        cols = ["population", "group", "region_intervals", "day_regions", "days", "r4 within $5", "r4 ci_lo", "r4 ci_hi",
                "r4 within 10%", "r4 within $5 by $", "r2 within $5", "r2 within 10%", "r2 within $5 by $"]
        print(out[[c for c in cols if c in out]].round(1).to_string(index=False))
        lomo = out[out.population == "$5,000+ leave one month out"]
        if len(lomo):
            print("\nExploratory leave-one-month-out rung-4 within-$5 contrasts (percentage points):")
            print(lomo[["held_out_month", "group", "r4 within $5 gap pp"]].round(1).to_string(index=False))
    return out


def verdict(out: pd.DataFrame, pop: str = "$5,000+", name: str = "network") -> str:
    a = out[(out.population == pop) & (out.group == f"{name}-attributed")].iloc[0]
    b = out[(out.population == pop) & (out.group == f"not {name}-attributed")].iloc[0]
    if a["r4 within $5"] <= b["r4 within $5"]:
        return "does not hold"
    return "holds" if a["r4 ci_lo"] > b["r4 ci_hi"] else "consistent, not shown"


if __name__ == "__main__":
    for cfg in ([sys.argv[1]] if len(sys.argv) > 1 else [PRIMARY]):
        o = run(cfg)
        for pop in ("$5,000+", "$5,000+ without June 2025"):
            for name in ("network", "network, broad"):
                print(cfg, pop, name, "prediction:", verdict(o, pop=pop, name=name))
