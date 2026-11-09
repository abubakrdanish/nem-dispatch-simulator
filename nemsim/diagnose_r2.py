"""What rung 2 still gets wrong, and why: python -m nemsim.diagnose_r2 <what>

  ceiling    the single-price oracle bound (nemsim/ceiling.py), and rung 2's error split into
             "level" (rung 2 against the median regional price) and "separation" (each region
             against that median), by hour
  held-off   cheap supply real dispatch did not use: MW offered below the region's actual price
             but not cleared, by fuel and hour, against rung 2's level error
  ramp       rung 2 plus ramp limits from initial MW, split into the ramp-up cap and the
             ramp-down floor (variants, not rungs)
  leak       rung 2 with wind and solar capped at realised end-of-interval output instead of
             AEMO's dispatch availability (deliberately leaky; never a rung)
  fcas       how much of the held-off supply sits on units enabled for raise FCAS (headroom kept
             back for frequency control), and how much run-above-price supply on units enabled for
             lower FCAS; by region and fuel; and rung 2 with each unit's energy cap reduced by its raise
             enablement (a diagnostic using outcomes), to size what an energy-only rung cannot recover
  batteries  battery unit-intervals spent charging while offering discharge below the regional
             price: how many, whether they also bid to charge at that price, and how full they were

Every diagnostic runs over the eight month sets and needs rung 2's results first:
for m in 2024-10 ... 2026-05; do python -m nemsim.evaluate r2 --set $m; done.
"held-off" and "leak" use dispatch outcomes. That is why they are diagnostics, and none of
them is ever an input to a rung.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from nemsim.config import ROOT, path
from nemsim.explain_r1 import fuel_group
from nemsim.harness import evaluate, summarise
from nemsim.inputs import REGIONS
from nemsim.intervals import MONTHS, SETS
from nemsim.rungs.r2_dispatch_availability import DispatchAvailability
from nemsim.stack import cap_bands, clear

COLS = ["region_intervals", "within_$1", "within_$5", "within_$10", "within_$20",
        "median_abs_error", "mean_abs_error", "mean_error"]


def _r2() -> pd.DataFrame:
    r = pd.concat([pd.read_csv(ROOT / "results" / f"{m}__r2__rrp.csv", low_memory=False, parse_dates=["interval"])
                   for m in MONTHS])
    return r[r.excluded.isna()].copy()


def _hour(t: pd.Series) -> pd.Series:
    return (t - pd.Timedelta("5min")).dt.hour


def ceiling(con) -> None:
    from nemsim.ceiling import oracle_hits
    r = _r2()
    bound = oracle_hits(r[["interval", "region", "rrp"]]).oracle_hit.mean() * 100
    print(f"Single-price oracle bound (chosen per interval knowing the answer): {bound:.1f}% within $5\n")
    r["median_rrp"] = r.groupby("interval").rrp.transform("median")
    r["level"] = r.price - r.median_rrp          # error = level + separation
    r["separation"] = r.median_rrp - r.rrp
    w5 = lambda s: (s.abs() <= 5).mean() * 100  # noqa: E731
    g = r.groupby(_hour(r.interval))
    table = pd.DataFrame({"bias": g["error"].mean(), "level bias": g["level"].mean(),
                          "level median": g["level"].median(), "level within $5": g["level"].apply(w5),
                          "separation within $5": g["separation"].apply(w5)})
    print(table.round(1).T.to_markdown(floatfmt=".1f"), "\n")
    print(r.groupby("region")["separation"].agg(within_5=w5, bias="mean").round(1).to_markdown(), "\n")
    print(f"Overall: level within $5 {w5(r.level):.1f}%, separation within $5 {w5(r.separation):.1f}%")


def held_off(con) -> None:
    explainer = Path(path("explainer_raw_dir")).parents[2] / "warehouse" / "nem.duckdb"
    con.execute(f"attach if not exists '{explainer}' as ex (read_only)")
    con.execute(f"""create or replace temp table fuel as select a.duid,
        {fuel_group('any_value(g.co2e_energy_source)')} grp from ex.raw.aemo_dualloc a
        join ex.raw.aemo_genunits g on g.gensetid = a.gensetid group by 1""")
    bands = ",".join(f"struct_pack(band := {i}, mw := p.bandavail{i}, pr := o.priceband{i})" for i in range(1, 11))
    frames = []
    for m in MONTHS:
        frames.append(con.execute(f"""
            with o as (select p.interval_datetime t, p.duid, p.maxavail, d.regionid region,
                         d.transmissionlossfactor * coalesce(d.distributionlossfactor, 1) mlf, unnest([{bands}]) b
                       from raw.bidperoffer_d p
                       join raw.biddayoffer_d o on o.settlementdate = p.settlementdate and o.duid = p.duid
                                               and o.direction = p.direction
                       join raw.dudetailsummary d on d.duid = p.duid
                         and p.interval_datetime - interval 5 minute >= d.start_date
                         and p.interval_datetime - interval 5 minute < d.end_date
                       where p.direction = 'GEN' and strftime(p.interval_datetime - interval 5 minute, '%Y-%m') = '{m}'),
            c as (select *, b.mw mw, b.pr / mlf price, sum(b.mw) over (partition by t, duid order by b.band) cum from o),
            k as (select c.t, c.duid, l.totalcleared,
                    sum(least(greatest(cum, 0), least(maxavail, l.availability))
                        - least(greatest(cum - mw, 0), least(maxavail, l.availability)))
                      filter (where c.price < pr.rrp - 0.01) in_merit
                  from c join raw.dispatchload l on l.duid = c.duid and l.settlementdate = c.t and l.intervention = 0
                  join raw.dispatchprice pr on pr.settlementdate = c.t and pr.regionid = c.region and pr.intervention = 0
                  group by all)
            -- Generation side only: a charging battery has a negative TOTALCLEARED, which must not
            -- count as generation held off. An earlier version subtracted it and so reported
            -- battery charging (800-940 MW at midday) as held-off discharge.
            select k.t, coalesce(f.grp, 'other') fuel,
                   sum(greatest(coalesce(in_merit, 0) - greatest(totalcleared, 0), 0)) held_off,
                   sum(greatest(greatest(totalcleared, 0) - coalesce(in_merit, 0), 0)) run_above_price
            from k left join fuel f using (duid) group by all""").df())
    h = pd.concat(frames)
    n = h.t.nunique()
    by = h.assign(hour=_hour(h.t)).groupby(["fuel", "hour"])[["held_off", "run_above_price"]].sum()
    per_hour = h.assign(hour=_hour(h.t)).groupby("hour").t.nunique()
    print("Held off, MW per interval, by fuel and hour\n")
    print(by.held_off.unstack().div(per_hour, axis=1).round(0).to_markdown(), "\n")
    print("Run above the regional price, MW per interval\n")
    print(by.run_above_price.unstack().div(per_hour, axis=1).round(0).to_markdown(), "\n")
    print((h.groupby("fuel")[["held_off", "run_above_price"]].sum() / n).round(0).to_markdown(), "\n")
    r = _r2()
    r["level"] = r.price - r.groupby("interval").rrp.transform("median")
    lv = r.drop_duplicates("interval")[["interval", "level"]]
    net = h.groupby("t").apply(lambda g: g.held_off.sum() - g.run_above_price.sum(), include_groups=False)
    lv = lv.merge(net.rename("net_held_off").reset_index().rename(columns={"t": "interval"}))
    lv["decile"] = pd.qcut(lv.net_held_off, 10)
    print(lv.groupby("decile", observed=True).agg(
        net_held_off=("net_held_off", "mean"), level_median=("level", "median"),
        level_within_5=("level", lambda s: (s.abs() <= 5).mean() * 100)).round(1).to_markdown(), "\n")
    print(f"Spearman correlation, net held off vs level error: "
          f"{lv[['net_held_off', 'level']].corr(method='spearman').iloc[0, 1]:.3f}")


class _WithRamps(DispatchAvailability):
    def __init__(self, up: bool, down: bool):
        self.up, self.down = up, down
        self.key = f"r2_ramp_{'up' if up else ''}{'down' if down else ''}"

    def solve(self, x):
        gen = x.offers[x.offers.direction == "GEN"]
        u = x.units.set_index("duid")
        limit = np.minimum(gen.maxavail.to_numpy(float), gen.duid.map(u.availability).fillna(0).to_numpy(float))
        if self.up:
            limit = np.minimum(limit, gen.duid.map(u.initialmw + u.rampuprate / 12).fillna(0).to_numpy(float))
        floor = np.zeros(len(gen))
        if self.down:
            floor = np.minimum(gen.duid.map(np.maximum(0, u.initialmw - u.rampdownrate / 12)).fillna(0)
                               .to_numpy(float), np.maximum(limit, 0))
        d, b, m = gen.duid.to_numpy(), gen.band.to_numpy(), gen.mw.to_numpy()
        capped = cap_bands(d, b, m, limit)
        forced = cap_bands(d, b, capped, floor)
        forced_total = pd.Series(floor, index=d).groupby(level=0).first().sum()
        requirement = x.demand.totaldemand.sum() + x.scheduled_load.dispatchableload.sum()
        c = clear(gen.price.to_numpy(), capped - forced, requirement - forced_total, x.price_floor, x.price_cap)
        return pd.DataFrame({"region": list(REGIONS), "price": c.price})


class _RealisedOutput(DispatchAvailability):
    """Leaky on purpose: wind and solar capped at the next interval's INITIALMW."""
    key = "r2_leak_realised_output"

    def __init__(self, con):
        realised = con.execute("""select l.settlementdate t, l.duid, greatest(n.initialmw, 0) realised
            from raw.dispatchload l
            join raw.dudetailsummary d on d.duid = l.duid and l.settlementdate - interval 5 minute >= d.start_date
                                      and l.settlementdate - interval 5 minute < d.end_date
            join raw.dispatchload n on n.duid = l.duid and n.settlementdate = l.settlementdate + interval 5 minute
                                   and n.intervention = 0
            where l.intervention = 0 and d.schedule_type = 'SEMI-SCHEDULED'""").df()
        self.by_t = {t: g.set_index("duid").realised for t, g in realised.groupby("t")}

    def solve(self, x):
        rz = self.by_t.get(pd.Timestamp(x.interval))
        if rz is not None:
            u = x.units.set_index("duid")
            both = u.index.intersection(rz.index)
            u.loc[both, "availability"] = rz.reindex(both)
            x.units = u.reset_index()
        return super().solve(x)


def _compare(con, models: dict) -> None:
    base = _r2()
    rows = {"rung 2": summarise(base)[COLS].iloc[0]}
    hours = {"rung 2": base.groupby(_hour(base.interval)).error.mean()}
    for name, model in models.items():
        res = pd.concat([evaluate(con, model, SETS[m].intervals()) for m in MONTHS])
        res = res[res.excluded.isna()]
        rows[name] = summarise(res)[COLS].iloc[0]
        hours[name] = res.groupby(_hour(res.interval)).error.mean()
    print(pd.DataFrame(rows).T.round(1).to_markdown(), "\n")
    print("Mean error by hour\n")
    print(pd.DataFrame(hours).T.round(0).to_markdown())


RAISE = "raisereg + greatest(coalesce(raise1sec, 0), raise6sec, raise60sec, raise5min)"
LOWER = "lowerreg + greatest(coalesce(lower1sec, 0), lower6sec, lower60sec, lower5min)"


def _unit_held_off(con, month: str) -> pd.DataFrame:
    """Per unit-interval: in-merit MW not cleared, MW cleared above the price, and FCAS enablement."""
    bands = ",".join(f"struct_pack(band := {i}, mw := p.bandavail{i}, pr := o.priceband{i})" for i in range(1, 11))
    return con.execute(f"""
        with o as (select p.interval_datetime t, p.duid, p.maxavail, d.regionid region,
                     d.transmissionlossfactor * coalesce(d.distributionlossfactor, 1) mlf, unnest([{bands}]) b
                   from raw.bidperoffer_d p
                   join raw.biddayoffer_d o on o.settlementdate = p.settlementdate and o.duid = p.duid
                                           and o.direction = p.direction
                   join raw.dudetailsummary d on d.duid = p.duid
                     and p.interval_datetime - interval 5 minute >= d.start_date
                     and p.interval_datetime - interval 5 minute < d.end_date
                   where p.direction = 'GEN' and strftime(p.interval_datetime - interval 5 minute, '%Y-%m') = '{month}'),
        c as (select *, b.mw mw, b.pr / mlf price, sum(b.mw) over (partition by t, duid order by b.band) cum from o),
        k as (select c.t, c.duid, c.region,
                sum(least(greatest(cum, 0), least(maxavail, l.availability))
                    - least(greatest(cum - mw, 0), least(maxavail, l.availability)))
                  filter (where c.price < pr.rrp - 0.01) in_merit,
                any_value(greatest(l.totalcleared, 0)) cleared,
                any_value({RAISE}) raise_mw, any_value({LOWER}) lower_mw
              from c join raw.dispatchload l on l.duid = c.duid and l.settlementdate = c.t and l.intervention = 0
              join raw.dispatchprice pr on pr.settlementdate = c.t and pr.regionid = c.region and pr.intervention = 0
              group by 1, 2, 3)
        select t, duid, region, greatest(coalesce(in_merit, 0) - cleared, 0) held_off,
               greatest(cleared - coalesce(in_merit, 0), 0) run_above, coalesce(raise_mw, 0) raise_mw,
               coalesce(lower_mw, 0) lower_mw
        from k""").df()


def fcas(con) -> None:
    explainer = Path(path("explainer_raw_dir")).parents[2] / "warehouse" / "nem.duckdb"
    con.execute(f"attach if not exists '{explainer}' as ex (read_only)")
    fuel = dict(con.execute(f"""select a.duid, {fuel_group('any_value(g.co2e_energy_source)')} from ex.raw.aemo_dualloc a
        join ex.raw.aemo_genunits g on g.gensetid = a.gensetid group by 1""").fetchall())
    u = pd.concat([_unit_held_off(con, m) for m in MONTHS])
    u["fuel"] = u.duid.map(fuel).fillna("other")
    # The part of held-off MW that raise enablement could account for, unit by unit.
    u["held_by_raise"] = np.minimum(u.held_off, u.raise_mw)
    u["above_by_lower"] = np.minimum(u.run_above, u.lower_mw)
    n = u.t.nunique()
    tot = pd.Series({"held off, MW per interval": u.held_off.sum() / n,
                     "  on units with raise enablement > 0": u.held_off[u.raise_mw > 0].sum() / n,
                     "  covered by raise enablement": u.held_by_raise.sum() / n,
                     "run above price, MW per interval": u.run_above.sum() / n,
                     "  on units with lower enablement > 0": u.run_above[u.lower_mw > 0].sum() / n,
                     "  covered by lower enablement": u.above_by_lower.sum() / n})
    print(tot.round(1).to_markdown(), "\n")
    g = lambda by: (u.groupby(by)[["held_off", "held_by_raise", "run_above", "above_by_lower"]].sum() / n)  # noqa: E731
    for by in ("region", "fuel"):
        t = g(by)
        t["held covered %"] = t.held_by_raise / t.held_off * 100
        t["above covered %"] = t.above_by_lower / t.run_above * 100
        print(t.round(1).to_markdown(), "\n")


class _MinusRaise(DispatchAvailability):
    """Diagnostic using outcomes: each unit's energy cap reduced by its raise FCAS enablement."""
    key = "r2_minus_raise_enablement"

    def __init__(self, con):
        e = con.execute(f"""select settlementdate t, duid, {RAISE} raise_mw from raw.dispatchload
                            where intervention = 0""").df()
        self.by_t = {t: g.set_index("duid").raise_mw for t, g in e.groupby("t")}

    def solve(self, x):
        r = self.by_t.get(pd.Timestamp(x.interval))
        if r is not None:
            u = x.units.set_index("duid")
            u["availability"] = np.maximum(u.availability - r.reindex(u.index).fillna(0), 0)
            x.units = u.reset_index()
        return super().solve(x)


def batteries(con) -> None:
    bands = ",".join(f"struct_pack(mw := p.bandavail{i}, pr := o.priceband{i})" for i in range(1, 11))
    df = con.execute(f"""
        with u as (select duid, regionid region, transmissionlossfactor * coalesce(distributionlossfactor, 1) mlf,
                          start_date, end_date from raw.dudetailsummary where dispatchtype = 'BIDIRECTIONAL'),
        o as (select p.interval_datetime t, p.duid, p.direction, p.maxavail, u.region, u.mlf, unnest([{bands}]) b
              from raw.bidperoffer_d p
              join raw.biddayoffer_d o on o.settlementdate = p.settlementdate and o.duid = p.duid
                                      and o.direction = p.direction
              join u on u.duid = p.duid and p.interval_datetime - interval 5 minute >= u.start_date
                    and p.interval_datetime - interval 5 minute < u.end_date),
        g as (select o.t, o.duid,
                sum(least(o.b.mw, o.maxavail)) filter (where o.direction = 'GEN' and o.b.mw > 0
                                                         and o.b.pr / o.mlf < pr.rrp - 0.01) gen_below,
                sum(o.b.mw) filter (where o.direction = 'LOAD' and o.b.mw > 0 and o.b.pr / o.mlf >= pr.rrp) load_at_price
              from o join raw.dispatchprice pr on pr.settlementdate = o.t and pr.regionid = o.region
                                              and pr.intervention = 0
              group by 1, 2)
        select g.*, l.totalcleared, l.initial_energy_storage soc,
               max(l.initial_energy_storage) over (partition by g.duid) soc_max
        from g join raw.dispatchload l on l.duid = g.duid and l.settlementdate = g.t and l.intervention = 0""").df()
    charging = df[df.totalcleared < -0.1]
    held = charging[charging.gen_below.fillna(0) > 0]
    frac = held.soc / held.soc_max
    print(f"Battery unit-intervals: {len(df):,}; charging: {len(charging):,}; charging while offering discharge "
          f"below the regional price: {len(held):,} ({held.gen_below.sum() / df.t.nunique():.1f} MW per interval)")
    print(f"Of those, also bid to charge at or above that price: {(held.load_at_price.fillna(0) > 0).mean() * 100:.1f}%")
    print(f"Stored energy as a share of the unit's maximum: quantiles {frac.quantile([.1, .25, .5, .75, .9]).round(2).tolist()}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("what", choices=["ceiling", "held-off", "ramp", "leak", "batteries", "fcas", "fcas-points"])
    args = parser.parse_args()
    with duckdb.connect(str(path("warehouse")), read_only=True) as con:
        if args.what == "ceiling":
            ceiling(con)
        elif args.what == "held-off":
            held_off(con)
        elif args.what == "fcas":
            fcas(con)
        elif args.what == "fcas-points":
            _compare(con, {"energy cap minus raise FCAS enablement (uses outcomes)": _MinusRaise(con)})
        elif args.what == "batteries":
            batteries(con)
        elif args.what == "ramp":
            _compare(con, {"+ ramp-up cap": _WithRamps(True, False), "+ ramp-down floor": _WithRamps(False, True),
                           "+ both": _WithRamps(True, True)})
        else:
            _compare(con, {"wind and solar at realised output (leak)": _RealisedOutput(con)})


if __name__ == "__main__":
    main()
