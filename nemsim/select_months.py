"""Rank the decade's months by spike money, to choose evaluation months on purpose:
python -m nemsim.select_months

Reads warehouse/prices.duckdb (nemsim/decade.py). For every month, FY2017-FY2026, it reports the
settled value of region-intervals at $300 or more (5-minute basis, RRP x demand / 12), how it splits
across regions, the season, its top days, and how many region-intervals were above $300 while the
regions' prices were separated (a neighbour more than $100 away), a cheap first sign of a network
limit. Which months are chosen, and why, is decided by hand from this table and the public record,
and written down in config/settings.yml and the README. Writes results/spike_months.csv and
results/spike_days.csv.

This ranks a spike-enriched sample. It describes nothing about the market as a whole: that is
nemsim/decade.py's job.
"""
from __future__ import annotations

import duckdb
import pandas as pd

from nemsim.config import ROOT
from nemsim.decade import DB

SEASON = {12: "summer", 1: "summer", 2: "summer", 3: "autumn", 4: "autumn", 5: "autumn",
          6: "winter", 7: "winter", 8: "winter", 9: "spring", 10: "spring", 11: "spring"}


def main() -> None:
    with duckdb.connect(str(DB), read_only=True) as con:
        f = con.execute("""
            with p as (select settlementdate t, regionid, any_value(rrp) rrp from raw.dispatchprice
                       where intervention = 0 group by 1, 2),
                 d as (select settlementdate t, regionid, any_value(totaldemand) dem from raw.dispatchregionsum
                       where intervention = 0 group by 1, 2),
                 x as (select p.t, p.regionid, p.rrp, p.rrp * d.dem / 12 v,
                              max(p.rrp) over (partition by p.t) - min(p.rrp) over (partition by p.t) spread
                       from p join d using (t, regionid))
            select strftime(t - interval 5 minute, '%Y-%m') as "month", cast(t - interval 5 minute as date) as "day",
                   regionid, count(*) n, sum(v) v, count(*) filter (where spread > 100) n_sep, max(rrp) max_rrp
            from x where rrp >= 300 and t > '2016-07-01' and t <= '2026-07-01'
            group by all""").df()
        # any_value: AEMO's September 2016 files repeat 10,080 region-intervals (the SA system black)
        total = con.execute("""with p as (select settlementdate, regionid, any_value(rrp) rrp from raw.dispatchprice
                                          where intervention = 0 group by 1, 2),
                                    d as (select settlementdate, regionid, any_value(totaldemand) dem
                                          from raw.dispatchregionsum where intervention = 0 group by 1, 2)
                               select sum(p.rrp * d.dem / 12) from p join d using (settlementdate, regionid)
                               where settlementdate > '2016-07-01' and settlementdate <= '2026-07-01'""").fetchone()[0]
    days = f.groupby(["month", "day"]).agg(v=("v", "sum"), n=("n", "sum"), n_sep=("n_sep", "sum"),
                                           max_rrp=("max_rrp", "max")).reset_index()
    days["regions"] = f.groupby(["month", "day"]).apply(
        lambda g: " ".join(f"{r}:{n}" for r, n in g.sort_values("v", ascending=False)[["regionid", "n"]].values),
        include_groups=False).values
    m = f.groupby("month").agg(spike_settled_m=("v", lambda x: x.sum() / 1e6), spike_ri=("n", "sum"),
                               separated_ri=("n_sep", "sum"), max_rrp=("max_rrp", "max")).reset_index()
    by_region = f.pivot_table(index="month", columns="regionid", values="v", aggfunc="sum").fillna(0)
    share = by_region.div(by_region.sum(axis=1), axis=0).mul(100).round(0)
    m = m.merge(share.add_prefix("pct_").reset_index(), on="month")
    m["season"] = m.month.str[5:].astype(int).map(SEASON)
    m["share_of_decade_spike_value_pct"] = m.spike_settled_m / m.spike_settled_m.sum() * 100
    top = days.sort_values("v", ascending=False).groupby("month").head(3)
    m["top_days"] = m.month.map(top.groupby("month").apply(
        lambda g: "; ".join(f"{d:%d} ${v / 1e6:.0f}m [{r}]" for d, v, r in g[["day", "v", "regions"]].values),
        include_groups=False))
    m = m.sort_values("spike_settled_m", ascending=False)
    m.to_csv(ROOT / "results" / "spike_months.csv", index=False)
    days.sort_values("v", ascending=False).to_csv(ROOT / "results" / "spike_days.csv", index=False)
    print(f"decade settled value ${total / 1e9:.1f}bn; $300+ ${m.spike_settled_m.sum() / 1e3:.1f}bn")
    with pd.option_context("display.width", 250, "display.max_colwidth", 120):
        print(m.head(40).round(1).to_string(index=False))


if __name__ == "__main__":
    main()
