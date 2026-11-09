"""Rungs 2 and 4 on the original ten months against the expanded sample: python -m nemsim.spike_sample

Reads the scored region-intervals each rung wrote (results/<month>__<rung>__rrp.csv, from
`python -m nemsim.evaluate r2 r4 --set <month>`) and compares them on the region-intervals both rungs
scored, so the two rungs are measured on identical rows.

Dollar weights. Settled value is price x demand / 12. From October 2021 energy settled on the
5-minute dispatch price, which is what the harness uses. Before it, energy settled on the 30-minute
trading price, so for those months (2020-01 here) the weight is the half-hour's TRADINGPRICE x the
interval's demand / 12, from warehouse/prices.duckdb, the same basis as the decade's pre-5MS figure.
Price accuracy (within $5 of the dispatch price) is unaffected: the target is still the 5-minute RRP.

The $300+ figures are clumpy: a few days carry most spike intervals. Their 95% intervals resample
whole days (day-block bootstrap, 2,000 draws, seed 0).

The expanded sample was chosen for its spikes (README, "the selection rule"). It describes how the
rungs do on spikes of several kinds, not how they do on the market; the market's spike share comes
only from the decade (nemsim/decade.py). Writes results/spike_sample.csv.
"""
from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd

from nemsim.config import ROOT
from nemsim.decade import DB, FIVE_MINUTE_SETTLEMENT
from nemsim.intervals import MONTHS, SPIKE_MONTHS

RUNGS = ("r2", "r4")
SPIKE = "d. $300+"


def load(key: str, months) -> pd.DataFrame:
    r = pd.concat([pd.read_csv(ROOT / "results" / f"{m}__{key}__rrp.csv", low_memory=False, parse_dates=["interval"])
                   .assign(month=m) for m in months])
    return r[r.excluded.isna() & r.price.notna()]


def thirty_minute_weights(f: pd.DataFrame) -> pd.Series:
    """Settled value at the half-hour trading price, for intervals before five-minute settlement."""
    pre = f.interval < FIVE_MINUTE_SETTLEMENT
    if not pre.any():
        return f.settled_value
    with duckdb.connect(str(DB), read_only=True) as con:
        tp = con.execute("select settlementdate tt, regionid region, any_value(rrp) trrp from raw.tradingprice "
                         "where settlementdate between ? and ? group by 1, 2",
                         [f.interval[pre].min(), f.interval[pre].max() + pd.Timedelta(minutes=30)]).df()
    tt = (f.interval - pd.Timedelta(minutes=5)).dt.floor("30min") + pd.Timedelta(minutes=30)
    m = pd.DataFrame({"tt": tt, "region": f.region}).merge(tp, how="left", on=["tt", "region"])
    if m.trrp[pre.to_numpy()].isna().any():
        raise RuntimeError("missing TRADINGPRICE for a pre-5MS interval")
    return pd.Series(np.where(pre, m.trrp.to_numpy() * f.totaldemand.to_numpy() / 12, f.settled_value), index=f.index)


def boot(f: pd.DataFrame, hit: str, reps: int = 2000) -> tuple[float, float]:
    day = (f.interval - pd.Timedelta(minutes=5)).dt.normalize()
    g = f.assign(day=day).groupby("day")[hit].agg(["sum", "size"])
    rng = np.random.default_rng(0)
    idx = rng.integers(0, len(g), (reps, len(g)))
    s, n = g["sum"].to_numpy()[idx].sum(1), g["size"].to_numpy()[idx].sum(1)
    return tuple(np.percentile(s / n * 100, [2.5, 97.5]))


def metrics(f: pd.DataFrame) -> dict:
    w = f.weight.abs()
    hit = f.abs_error <= 5
    sp = f[f.price_class == SPIKE]
    lo, hi = boot(sp.assign(hit=sp.abs_error <= 5), "hit") if len(sp) else (np.nan, np.nan)
    return {"region_intervals": len(f), "within_$5_pct": hit.mean() * 100,
            "within_$5_by_$_pct": (w * hit).sum() / w.sum() * 100,
            "mean_abs_error": f.abs_error.mean(), "spike_region_intervals": len(sp),
            "spike_within_$5_pct": (sp.abs_error <= 5).mean() * 100 if len(sp) else np.nan,
            "spike_ci_lo": lo, "spike_ci_hi": hi,
            "spike_share_of_sample_$_pct": sp.weight.sum() / f.weight.sum() * 100}


def main() -> None:
    every = MONTHS + SPIKE_MONTHS
    frames = {k: load(k, every) for k in RUNGS}
    common = frames["r2"][["interval", "region"]].merge(frames["r4"][["interval", "region"]])
    rows = []
    for k, f in frames.items():
        f = f.merge(common)
        f["weight"] = thirty_minute_weights(f)
        groups = {"original ten": f.month.isin(MONTHS), "seven spike months": f.month.isin(SPIKE_MONTHS),
                  "expanded (seventeen)": f.month.isin(every),
                  "expanded, without 2020-01": f.month.isin(every) & (f.month != "2020-01")}
        groups.update({m: f.month == m for m in every})
        for name, sel in groups.items():
            rows.append({"rung": k, "sample": name, **metrics(f[sel])})
    out = pd.DataFrame(rows)
    out.to_csv(ROOT / "results" / "spike_sample.csv", index=False)
    with pd.option_context("display.width", 250):
        print(out.round(1).to_string(index=False))


if __name__ == "__main__":
    main()
