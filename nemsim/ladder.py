"""The whole ladder on one sample at a time: python -m nemsim.ladder bounds | report

Every rung is measured on the same region-intervals, those all five rungs scored, so no figure in a
table comes from a different sample than its neighbours. Three samples:

- **ten**: the original ten months (nemsim.intervals.MONTHS), the sample of the stage 2 results;
- **seven**: the seven spike months added in stage 6b (SPIKE_MONTHS), chosen for their spike value;
- **seventeen**: both.

For each rung and sample (results/ladder.csv, and printed):

- within $5 of AEMO's dispatch price (DISPATCHPRICE.RRP, pricing run), overall and by actual-price band,
  region and month; within 10% of it; mean absolute error;
- within $5 weighted by settled value, |price x demand / 12|, using the 30-minute TRADINGPRICE for
  intervals before five-minute settlement (2020-01), as nemsim/spike_sample.py does;
- the $300+ band's within-$5 share with a 95% interval from resampling whole days, and the same with
  June 2025 left out (half the original ten's spike value is June 2025);
- the share of the reachable bound: rungs 1 and 2 against the single-price oracle, rung 3a against the
  regional-stack bound, rung 4 against the network bound (nemsim/ceiling.py). Rung 3b has no bound of
  its own kind: a regional LP without constraints is bounded by nothing narrower than the network bound,
  which is reported beside it for reference. Bounds use actual outcomes: they say what a model of each
  kind could reach, knowing the answer, never what any model did.

`bounds` computes the regional-stack, constraint-adjusted and network bounds for the seven spike months
into results/bound_*__spike7.csv, with the functions that produced the original ten months' files; on
2025-11 they reproduce those files exactly (0 of 43,200 region-intervals differ).
"""
from __future__ import annotations

import sys

import duckdb
import numpy as np
import pandas as pd

from nemsim.ceiling import constraint_adjusted_bound, network_bound, oracle_hits, regional_stack_bound
from nemsim.config import ROOT, path
from nemsim.intervals import MONTHS, SPIKE_MONTHS
from nemsim.spike_sample import boot, thirty_minute_weights

RUNGS = ("r1", "r2", "r3a", "r3b", "r4")
SAMPLES = {"ten": MONTHS, "seven": SPIKE_MONTHS, "seventeen": MONTHS + SPIKE_MONTHS}
BOUND_OF = {"r1": "oracle", "r2": "oracle", "r3a": "regional_stack", "r3b": None, "r4": "network"}
SPIKE = "d. $300+"
R = ROOT / "results"
SHADOW_PRICE_SIGN = -1.0      # constraint shadow prices are subtracted (README, "Conventions are measured")


def bounds() -> None:
    with duckdb.connect(str(path("warehouse")), read_only=True) as con:
        con.execute("set threads = 6")
        rs = regional_stack_bound(con, SPIKE_MONTHS)
        cb = constraint_adjusted_bound(con, SHADOW_PRICE_SIGN, SPIKE_MONTHS)
        nb = network_bound(con, cb, SPIKE_MONTHS)
    rs.to_csv(R / "bound_regional_stack__spike7.csv", index=False)
    cb.to_csv(R / "bound_constraint_adjusted__spike7.csv", index=False)
    nb.to_csv(R / "bound_network__spike7.csv", index=False)
    print({k: len(v) for k, v in (("regional_stack", rs), ("constraint_adjusted", cb), ("network", nb))})


def _bound(name: str, col: str) -> pd.DataFrame:
    b = pd.concat([pd.read_csv(R / f"bound_{name}.csv", parse_dates=["interval"], low_memory=False),
                   pd.read_csv(R / f"bound_{name}__spike7.csv", parse_dates=["interval"], low_memory=False)])
    return b[["interval", "region", col]].drop_duplicates(["interval", "region"])


def load(key: str, months) -> pd.DataFrame:
    r = pd.concat([pd.read_csv(R / f"{m}__{key}__rrp.csv", low_memory=False, parse_dates=["interval"]).assign(month=m)
                   for m in months])
    return r[r.excluded.isna() & r.price.notna()]


def frame(months) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """Each rung on the region-intervals all five scored, plus the bounds for those rows."""
    frames = {k: load(k, months) for k in RUNGS}
    common = frames["r1"][["interval", "region"]]
    for k in RUNGS[1:]:
        common = common.merge(frames[k][["interval", "region"]])
    frames = {k: f.merge(common).sort_values(["interval", "region"]).reset_index(drop=True) for k, f in frames.items()}
    base = frames["r1"]
    weight = thirty_minute_weights(base)
    b = base[["interval", "region", "rrp", "month", "price_class"]].assign(weight=weight.to_numpy())
    b = b.merge(oracle_hits(base[["interval", "region", "rrp"]]), how="left")
    b = b.merge(_bound("regional_stack", "stack_reachable"), how="left")
    b = b.merge(_bound("network", "network_reachable"), how="left")
    b = b.rename(columns={"oracle_hit": "oracle", "stack_reachable": "regional_stack", "network_reachable": "network"})
    for c in ("oracle", "regional_stack", "network"):
        b[c] = b[c].fillna(False).astype(bool)
    for k in RUNGS:
        frames[k]["weight"] = b.weight.to_numpy()
    return frames, b


def measures(f: pd.DataFrame, b: pd.DataFrame) -> dict:
    hit = (f.abs_error <= 5).to_numpy()
    w = f.weight.abs().to_numpy()
    sp = (f.price_class == SPIKE).to_numpy()
    nj = (f.month != "2025-06").to_numpy()
    out = {"region_intervals": len(f), "within_$5": hit.mean() * 100,
           "within_10%": (f.abs_error <= 0.1 * f.target.abs()).mean() * 100,
           "within_$5_by_$": (w * hit).sum() / w.sum() * 100, "mean_abs_error": f.abs_error.mean()}
    for name, sel in (("$300+", sp), ("$300+ without June 2025", sp & nj)):
        g = f[sel].assign(hit=hit[sel])
        lo, hi = boot(g, "hit") if len(g) else (np.nan, np.nan)
        out.update({f"{name}: n": int(sel.sum()), f"{name}: within_$5": hit[sel].mean() * 100 if sel.any() else np.nan,
                    f"{name}: ci_lo": lo, f"{name}: ci_hi": hi})
        for bound in ("oracle", "regional_stack", "network"):
            out[f"{name}: bound {bound}"] = b[bound].to_numpy()[sel].mean() * 100 if sel.any() else np.nan
    for bound in ("oracle", "regional_stack", "network"):
        out[f"bound {bound}"] = b[bound].mean() * 100
    return out


def report() -> pd.DataFrame:
    rows = []
    for sample, months in SAMPLES.items():
        frames, b = frame(months)
        for k, f in frames.items():
            m = measures(f, b)
            own = BOUND_OF[k]
            m["own_bound"] = own
            m["share_of_own_bound_%"] = m["within_$5"] / m[f"bound {own}"] * 100 if own else np.nan
            m["$300+ share_of_own_bound_%"] = (m["$300+: within_$5"] / m[f"$300+: bound {own}"] * 100
                                               if own and m[f"$300+: bound {own}"] else np.nan)
            rows.append({"sample": sample, "rung": k, "breakdown": "overall", "group": "all", **m})
            for col, name in (("price_class", "band"), ("region", "region"), ("month", "month")):
                for g, gf in f.groupby(col):
                    sel = (f[col] == g).to_numpy()
                    gw = gf.weight.abs()
                    rows.append({"sample": sample, "rung": k, "breakdown": name, "group": g,
                                 "region_intervals": len(gf), "within_$5": (gf.abs_error <= 5).mean() * 100,
                                 "within_10%": (gf.abs_error <= 0.1 * gf.target.abs()).mean() * 100,
                                 "within_$5_by_$": (gw * (gf.abs_error <= 5)).sum() / gw.sum() * 100 if gw.sum() else np.nan,
                                 "mean_abs_error": gf.abs_error.mean(),
                                 **({f"bound {own}": b[own].to_numpy()[sel].mean() * 100} if own else {})})
    out = pd.DataFrame(rows)
    out.to_csv(R / "ladder.csv", index=False)
    ov = out[out.breakdown == "overall"]
    cols = ["sample", "rung", "region_intervals", "within_$5", "within_10%", "within_$5_by_$", "mean_abs_error",
            "own_bound", "share_of_own_bound_%", "$300+: n", "$300+: within_$5", "$300+: ci_lo", "$300+: ci_hi",
            "$300+ without June 2025: within_$5", "$300+ without June 2025: ci_lo", "$300+ without June 2025: ci_hi",
            "bound oracle", "bound regional_stack", "bound network", "$300+: bound network"]
    with pd.option_context("display.width", 300, "display.max_columns", 40):
        print(ov[cols].round(1).to_string(index=False))
        bands = out[out.breakdown == "band"].pivot_table(index=["sample", "group"], columns="rung", values="within_$5")
        print(bands.round(1).to_string())
    return out


if __name__ == "__main__":
    {"bounds": bounds, "report": report}[sys.argv[1] if len(sys.argv) > 1 else "report"]()
