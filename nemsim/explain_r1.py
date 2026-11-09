"""Why rung 1 clears at negative prices: python -m nemsim.explain_r1 2025-09

Compares the supply rung 1 sees (each offer capped at its declared MAXAVAIL) with the supply
AEMO lets units deliver (also capped at DISPATCHLOAD availability, which for wind and solar is
AEMO's forecast, UIGF). Needs rung 1's results for the month (python -m nemsim.evaluate r1 --set
<month>), DISPATCHLOAD for the month, and nem-market-explainer's warehouse for fuel types
(read-only). Prints MW averaged over the month's scored intervals.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
import pandas as pd

from nemsim.config import ROOT, path
from nemsim.intervals import SETS

def fuel_group(col: str) -> str:
    """SQL grouping GENUNITS.CO2E_ENERGY_SOURCE into a few fuels."""
    rules = [("wind", ["wind"]), ("solar", ["solar"]), ("coal", ["coal"]), ("hydro", ["hydro", "water"]),
             ("battery", ["battery", "storage"]), ("gas", ["gas"])]
    whens = " ".join(f"when {' or '.join(f'{col} ilike {chr(39)}%{k}%{chr(39)}' for k in keys)} then '{g}'"
                     for g, keys in rules)
    return f"case {whens} else 'other' end"


def explain(con: duckdb.DuckDBPyConnection, month: str) -> dict[str, pd.DataFrame]:
    r1 = pd.read_csv(ROOT / "results" / f"{month}__r1__rrp.csv", low_memory=False, parse_dates=["interval"])
    r1 = r1[r1.excluded.isna()]
    prices = r1.groupby("interval", as_index=False).agg(model=("price", "first"), marginal=("marginal_duid", "first"))
    con.register("model_prices", prices)
    explainer = Path(path("explainer_raw_dir")).parents[2] / "warehouse" / "nem.duckdb"
    con.execute(f"attach if not exists '{explainer}' as ex (read_only)")
    iv = SETS[month].intervals()
    bands = ",".join(f"struct_pack(band := {i}, mw := p.bandavail{i}, pr := o.priceband{i})" for i in range(1, 11))
    con.execute(f"""create or replace temp table supply as
        with fuel as (select a.duid, any_value(g.co2e_energy_source) fuel from ex.raw.aemo_dualloc a
                      join ex.raw.aemo_genunits g on g.gensetid = a.gensetid group by 1),
        o as (select p.interval_datetime t, p.duid, p.maxavail,
                     d.transmissionlossfactor * coalesce(d.distributionlossfactor, 1) mlf, unnest([{bands}]) b
              from raw.bidperoffer_d p
              join raw.biddayoffer_d o on o.settlementdate = p.settlementdate and o.duid = p.duid
                                      and o.direction = p.direction
              join raw.dudetailsummary d on d.duid = p.duid and p.interval_datetime - interval 5 minute >= d.start_date
                                        and p.interval_datetime - interval 5 minute < d.end_date
              where p.direction = 'GEN' and p.interval_datetime in (select interval from model_prices)),
        c as (select *, b.mw mw, b.pr / mlf price, sum(b.mw) over (partition by t, duid order by b.band) cum from o)
        select c.t, c.duid, {fuel_group("f.fuel")} grp, c.price,
               least(greatest(cum, 0), maxavail) - least(greatest(cum - mw, 0), maxavail) declared,
               least(greatest(cum, 0), least(maxavail, l.availability))
                 - least(greatest(cum - mw, 0), least(maxavail, l.availability)) deliverable,
               case when c.b.band = 1 then greatest(l.totalcleared, 0) else 0 end cleared
        from c join raw.dispatchload l on l.duid = c.duid and l.settlementdate = c.t and l.intervention = 0
        left join fuel f on f.duid = c.duid""")
    n = con.execute("select count(distinct t) from supply").fetchone()[0]
    if n == 0:
        raise SystemExit(f"No intervals with offers, DISPATCHLOAD and rung 1 results for {month}")
    by_fuel = con.execute(f"""select grp as fuel,
            sum(declared) / {n} declared_mw, sum(deliverable) / {n} deliverable_mw,
            sum(declared) filter (where price < 0) / {n} declared_below_0,
            sum(deliverable) filter (where price < 0) / {n} deliverable_below_0,
            sum(declared) filter (where price between -100 and -40) / {n} declared_minus100_to_minus40,
            sum(declared) filter (where s.price < m.model) / {n} rung1_dispatch_mw,
            sum(cleared) / {n} actual_dispatch_mw,
            count(distinct s.t) filter (where s.duid = m.marginal) / {n} * 100 sets_rung1_price_pct
        from supply s join model_prices m on m.interval = s.t group by 1 order by 2 desc""").df()
    coverage = con.execute(f"""with neg as (select t, sum(declared) filter (where price < 0) declared,
                                          sum(deliverable) filter (where price < 0) deliverable from supply group by 1),
        d as (select settlementdate t, sum(totaldemand + dispatchableload) req from raw.dispatchregionsum
              where intervention = 0 group by 1),
        a as (select settlementdate t, avg((rrp < 0)::int) neg from raw.dispatchprice where intervention = 0 group by 1)
        select count(*) intervals, avg(req) demand_mw, avg(declared) declared_below_0_mw,
               avg(deliverable) deliverable_below_0_mw,
               avg((declared >= req)::int) * 100 declared_below_0_covers_demand_pct,
               avg((deliverable >= req)::int) * 100 deliverable_below_0_covers_demand_pct,
               avg(a.neg) * 100 actual_price_negative_pct
        from neg join d using (t) join a using (t)""").df()
    by_hour = con.execute(f"""select hour(t - interval 5 minute) as hour_starting,
            sum(declared) filter (where grp = 'solar') / count(distinct t) solar_declared,
            sum(deliverable) filter (where grp = 'solar') / count(distinct t) solar_deliverable,
            sum(declared) filter (where grp = 'wind') / count(distinct t) wind_declared,
            sum(deliverable) filter (where grp = 'wind') / count(distinct t) wind_deliverable
        from supply group by 1 order by 1""").df()
    return {"coverage": coverage, "by_fuel": by_fuel, "by_hour": by_hour}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("month", help="a month set with offers and DISPATCHLOAD loaded, such as 2025-09")
    args = parser.parse_args()
    with duckdb.connect(str(path("warehouse")), read_only=True) as con:
        for name, frame in explain(con, args.month).items():
            print(f"### {name}\n\n{frame.to_markdown(index=False, floatfmt='.1f')}\n")


if __name__ == "__main__":
    main()
