"""Score any rung on any set of intervals, the same way every time.

evaluate() runs a rung over a set of intervals and returns one row per region-interval: the
model's price, the actual price, the error, and the classes the scores are broken down by.
summarise() turns those rows into the accuracy statistics. Neither knows which rung it is
scoring.

Actual prices are the pricing run (INTERVENTION = 0) of DISPATCHPRICE. The default target is
RRP; ROP, the price before administered-price and other overrides, can be chosen instead.

Region-intervals are excluded, never silently dropped: each excluded row keeps its reason.
  suspended   MARKETSUSPENDEDFLAG <> 0: suspension pricing replaced dispatch

Flagged but scored (column `administered_or_intervention`):
  price_overridden  RRP differs from ROP. In the eight months with offers this is the market price
                    cap or floor clipping a stack price outside the range (20 region-intervals) or that
                    cap flowing on to a neighbour (2). Every rung clips to the same cap and floor, so
                    the clipped RRP is a fair target. No administered-pricing period (cumulative price
                    threshold) falls in those months; if one does, exclude it.
  intervention_run  an intervention run exists. The price compared is the pricing run's
                    (INTERVENTION = 0), which is the offer stack's, so it is scored.
  no_inputs   the rung needs an input this interval does not have (usually offers not loaded)
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

import duckdb
import numpy as np
import pandas as pd

from nemsim.inputs import INTERCONNECTORS, REGIONS, Window
from nemsim.rungs.base import Rung

TOLERANCES = (1, 5, 10, 20)       # $/MWh; 20 is the research's widest band
LIMIT_TOLERANCE_MW = 0.1          # same rule as nem-market-explainer's mart_interconnector_interval
# Actual price bands, the headline breakdown. Earlier versions cut at 0, 300 and 5,000.
PRICE_CLASSES = [(-np.inf, 0, "a. negative"), (0, 50, "b. $0-50"), (50, 300, "c. $50-300"), (300, np.inf, "d. $300+")]
RELATIVE_TOLERANCE = 0.10        # "within 10%": |error| <= 10% of |actual price|
REGION_LETTER = {"Q": "QLD1", "N": "NSW1", "V": "VIC1", "S": "SA1", "T": "TAS1"}


# ---------------------------------------------------------------------------------------------
# What actually happened, and how each region-interval is classed

def actuals(con: duckdb.DuckDBPyConnection, start: datetime, end: datetime) -> pd.DataFrame:
    return con.execute("""
        select p.settlementdate as interval, p.regionid as region, p.rrp, p.rop,
               p.marketsuspendedflag <> 0 as suspended, p.apcflag <> 0 as apc_flag,
               abs(p.rop - p.rrp) > 0.005 as price_overridden,
               exists (select 1 from raw.dispatchprice q where q.settlementdate = p.settlementdate
                       and q.regionid = p.regionid and q.intervention = 1) as intervention_run,
               r.totaldemand,
               -- Settled energy value of the region-interval, $: price x demand x 5 minutes.
               p.rrp * r.totaldemand / 12 as settled_value
        from raw.dispatchprice p
        left join raw.dispatchregionsum r on r.settlementdate = p.settlementdate and r.regionid = p.regionid
                                         and r.intervention = 0
        where p.intervention = 0 and p.settlementdate between ? and ?""", [start, end]).df()


def link_states(con: duckdb.DuckDBPyConnection, start: datetime, end: datetime) -> pd.DataFrame:
    """Per region-interval: is any interconnector touching the region at a limit?"""
    ic = con.execute(f"""
        select settlementdate as interval, interconnectorid,
               exportlimit < importlimit
               or (case when mwflow >= 0 then exportlimit - mwflow else mwflow - importlimit end)
                  < {LIMIT_TOLERANCE_MW} as at_limit
        from raw.dispatchinterconnectorres
        where intervention = 0 and settlementdate between ? and ?""", [start, end]).df()
    rows = []
    for side in (0, 1):
        part = ic.assign(region=ic.interconnectorid.map(lambda i: INTERCONNECTORS[i][side]))
        rows.append(part[["interval", "region", "at_limit"]])
    out = pd.concat(rows).groupby(["interval", "region"], as_index=False).at_limit.any()
    return out.rename(columns={"at_limit": "link_at_limit"})


def constraint_region(constraint_id: str, duid_region: dict[str, str]) -> str | None:
    """Region a generic constraint belongs to, from AEMO's naming convention. A heuristic.

    #UNIT_... and $UNIT constraints apply to one unit and take its region. C_X_, NC_X_ and NSA_X_
    carry the region letter after the prefix. Otherwise the first letter names the region.
    Returns None for FCAS (F...) and for names that fit none of these.
    """
    if constraint_id.startswith("F"):
        return None
    if constraint_id[0] in "#$":
        # DUIDs can contain underscores (CPP_3, CALL_B_2), so try the longest prefix first.
        parts = constraint_id[1:].split("_")
        for k in range(len(parts), 0, -1):
            if (region := duid_region.get("_".join(parts[:k]))) is not None:
                return region
        return None
    if m := re.match(r"(?:C|NC|NSA)_([QNVST])_", constraint_id):
        return REGION_LETTER[m.group(1)]
    return REGION_LETTER.get(constraint_id[0])


def constraint_states(con: duckdb.DuckDBPyConnection, start: datetime, end: datetime) -> pd.DataFrame:
    """Per region-interval: did a non-FCAS generic constraint in that region bind?

    Only for intervals whose DISPATCHCONSTRAINT month is loaded; other intervals are left out of
    the frame, so their class is unknown rather than false.
    """
    binding = con.execute("""
        select settlementdate as interval, constraintid from raw.dispatchconstraint
        where intervention = 0 and marginalvalue <> 0 and settlementdate between ? and ?""", [start, end]).df()
    covered = con.execute("""select distinct settlementdate as interval from raw.dispatchconstraint
                             where settlementdate between ? and ?""", [start, end]).df()
    if covered.empty:
        return pd.DataFrame({"interval": pd.Series(dtype="datetime64[us]"), "region": pd.Series(dtype=object),
                             "constraint_binding": pd.Series(dtype=object)})
    duid_region = dict(con.execute("""select duid, any_value(regionid order by end_date desc)
                                      from raw.dudetailsummary group by 1""").fetchall())
    ids = binding.constraintid.unique()
    binding["region"] = binding.constraintid.map(dict(zip(ids, (constraint_region(i, duid_region) for i in ids))))
    hit = binding.dropna(subset=["region"]).groupby(["interval", "region"]).size().rename("n_binding").reset_index()
    grid = covered.merge(pd.DataFrame({"region": REGIONS}), how="cross")
    out = grid.merge(hit, how="left", on=["interval", "region"]).fillna({"n_binding": 0})
    out["constraint_binding"] = out.n_binding > 0
    return out[["interval", "region", "constraint_binding"]]


def price_class(rrp: pd.Series) -> pd.Series:
    out = pd.Series(index=rrp.index, dtype=object)
    for lo, hi, label in PRICE_CLASSES:
        out[(rrp >= lo) & (rrp < hi)] = label
    return out


# ---------------------------------------------------------------------------------------------
# Running a rung

def evaluate(con: duckdb.DuckDBPyConnection, rung: Rung, intervals: list[datetime],
             target: str = "rrp") -> pd.DataFrame:
    """Run `rung` on every interval and return one scored row per region-interval."""
    if target not in ("rrp", "rop"):
        raise ValueError("target is rrp or rop")
    start, end = min(intervals), max(intervals)
    truth = actuals(con, start, end)
    truth = truth[truth.interval.isin(intervals)]
    truth = (truth.merge(link_states(con, start, end), how="left", on=["interval", "region"])
                  .merge(constraint_states(con, start, end), how="left", on=["interval", "region"]))
    truth["price_class"] = price_class(truth.rrp)
    # Administered or intervened: the published price is not what the offer stack alone produced.
    truth["administered_or_intervention"] = truth.apc_flag | truth.price_overridden | truth.intervention_run

    rows, timings = [], []
    # One window per day keeps memory flat for month-long sets.
    day = timedelta(days=1)
    t0 = start
    while t0 <= end:
        t1 = min(t0 + day - timedelta(minutes=5), end)
        todo = [t for t in intervals if t0 <= t <= t1]
        if todo:
            window = Window(con, t0, t1, rung.uses)
            have = set.intersection(*(window.intervals_with(u) for u in rung.uses)) if rung.uses else set()
            for t in todo:
                if pd.Timestamp(t) not in have:
                    rows.append(pd.DataFrame({"interval": t, "region": list(REGIONS), "excluded": "no_inputs"}))
                    continue
                tic = time.perf_counter()
                out = rung.solve(window.at(t))
                timings.append(time.perf_counter() - tic)
                missing = {"region", "price"} - set(out.columns)
                if missing:
                    raise ValueError(f"{rung.key} returned no {sorted(missing)} at {t}")
                rows.append(out.assign(interval=t, excluded=None))
        t0 += day
    model = pd.concat(rows, ignore_index=True)
    result = truth.merge(model, how="left", on=["interval", "region"])
    # Suspension is per region, and outranks every other reason. The rung still ran on the
    # other regions of that interval; only the suspended region-intervals are set aside.
    result.loc[result.suspended, "excluded"] = "suspended"
    result.loc[result.excluded.notna(), ["price", "bracket_lo", "bracket_hi"]
               if "bracket_lo" in result else ["price"]] = np.nan
    result["target"] = result[target]
    result["error"] = result.price - result.target
    result["abs_error"] = result.error.abs()
    if "bracket_lo" in result:
        inside = ((result.target >= result.bracket_lo - 0.005) & (result.target <= result.bracket_hi + 0.005))
        result["bracket_contains"] = inside.astype(float).where(result.bracket_lo.notna())
    result.attrs.update(rung=rung.key, target=target,
                        solve_ms_median=float(np.median(timings) * 1e3) if timings else None)
    return result.sort_values(["interval", "region"]).reset_index(drop=True)


# ---------------------------------------------------------------------------------------------
# Scoring

def _metrics(g: pd.DataFrame) -> pd.Series:
    scored = g[g.excluded.isna() & g.price.notna()]
    n = len(scored)
    out = {"region_intervals": n}
    for tol in TOLERANCES:
        out[f"within_${tol}"] = (scored.abs_error <= tol).mean() * 100 if n else np.nan
    out["within_10%"] = (scored.abs_error <= RELATIVE_TOLERANCE * scored.target.abs()).mean() * 100 if n else np.nan
    out["median_abs_error"] = scored.abs_error.median() if n else np.nan
    out["mean_abs_error"] = scored.abs_error.mean() if n else np.nan
    out["mean_error"] = scored.error.mean() if n else np.nan
    if "settled_value" in scored:
        w = scored.settled_value.abs()
        # Weighted by settled dollars: the share of the market's money in region-intervals within $5.
        out["within_$5_by_$"] = (w * (scored.abs_error <= 5)).sum() / w.sum() * 100 if w.sum() else np.nan
        out["mean_abs_error_by_demand"] = ((scored.abs_error * scored.totaldemand).sum()
                                           / scored.totaldemand.sum()) if n else np.nan
        out["settled_$m"] = scored.settled_value.sum() / 1e6
    if "bracket_contains" in scored:
        out["bracket_contains_%"] = scored.bracket_contains.astype(float).mean() * 100 if n else np.nan
    return pd.Series(out)


def summarise(result: pd.DataFrame, by: str | list[str] | None = None) -> pd.DataFrame:
    """Accuracy statistics for the scored rows, overall or broken down by one or more columns."""
    if by is None:
        return _metrics(result).to_frame("all").T
    return result.groupby(by, dropna=False).apply(_metrics, include_groups=False)


def exclusions(result: pd.DataFrame) -> pd.Series:
    return result.excluded.fillna("scored").value_counts()


BREAKDOWNS = {
    "actual price band": "price_class",
    "region": "region",
    "interconnector at a limit (any link touching the region)": "link_at_limit",
    "non-FCAS constraint binding in the region (naming heuristic; blank = not loaded)": "constraint_binding",
    "administered price or intervention run": "administered_or_intervention",
}
