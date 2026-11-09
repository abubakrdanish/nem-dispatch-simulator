"""The single-price oracle bound, and every rung's score as a share of it: python -m nemsim.ceiling

**An oracle bound, not a model.** For each interval it chooses, knowing every region's actual
price, the one NEM-wide price that puts the most regions within $5 (candidates are each actual
price and each actual price +/- $5, which is enough to find the maximum). No model could pick
that price without knowing the answer. It bounds what any single-price model (rungs 1 and 2) can
score on the within-$5 measure; it says nothing about a model with regional prices, so it is
dropped once regional prices exist.

Writes results/oracle_single_price.csv (one row per region-interval: did the oracle price hit it?)
and prints each single-price rung's score as a share of the bound, overall, by actual price band
and by month.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from nemsim.config import ROOT
from nemsim.harness import summarise
from nemsim.intervals import MONTHS

TOL = 5.0
SINGLE_PRICE_RUNGS = ("r1", "r2")


def oracle_hits(rows: pd.DataFrame) -> pd.DataFrame:
    """rows: interval, region, rrp for the scored region-intervals. Returns interval, region, oracle_hit."""
    wide = rows.pivot(index="interval", columns="region", values="rrp")
    p = wide.to_numpy(float)                                    # (intervals, regions), NaN if not scored
    cand = np.concatenate([p - TOL, p, p + TOL], axis=1)        # (intervals, 3 x regions)
    with np.errstate(invalid="ignore"):
        hit = np.abs(p[:, None, :] - cand[:, :, None]) <= TOL + 1e-9   # NaN compares False
    best = np.nan_to_num(hit.sum(axis=2) * np.isfinite(cand), nan=-1).argmax(axis=1)
    chosen = hit[np.arange(len(p)), best, :]
    out = pd.DataFrame(chosen, index=wide.index, columns=wide.columns).stack().rename("oracle_hit").reset_index()
    return out.merge(rows[["interval", "region"]])


def load(key: str) -> pd.DataFrame:
    r = pd.concat([pd.read_csv(ROOT / "results" / f"{m}__{key}__rrp.csv", low_memory=False, parse_dates=["interval"])
                   .assign(month=m) for m in MONTHS])
    return r[r.excluded.isna()]


def common_rows(keys) -> pd.DataFrame:
    frames = [load(k) for k in keys]
    common = frames[0][["interval", "region"]]
    for f in frames[1:]:
        common = common.merge(f[["interval", "region"]])
    return common


def normalised(keys=SINGLE_PRICE_RUNGS) -> dict[str, pd.DataFrame]:
    common = common_rows(keys)
    base = load(keys[0]).merge(common)
    oracle = oracle_hits(base[["interval", "region", "rrp"]])
    oracle.to_csv(ROOT / "results" / "oracle_single_price.csv", index=False)
    out = {}
    for by in (None, "price_class", "month"):
        cols = {}
        o = base.merge(oracle)
        bound = o.oracle_hit.mean() * 100 if by is None else o.groupby(by).oracle_hit.mean() * 100
        cols["oracle bound, within $5"] = bound
        for k in keys:
            s = summarise(load(k).merge(common), by)["within_$5"]
            s = s.iloc[0] if by is None else s
            cols[f"{k} within $5"] = s
            cols[f"{k} as % of bound"] = s / bound * 100
        out[by or "all"] = pd.DataFrame(cols, index=["all"]) if by is None else pd.DataFrame(cols)
    return out


def main() -> None:
    for name, table in normalised().items():
        print(f"### {name}\n\n{table.round(1).to_markdown()}\n")



def regional_stack_bound(con, months=MONTHS) -> pd.DataFrame:
    """Per region-interval: could the region's own offers have produced a price within $5 of the actual?

    A regional merit order (rung 3a) can only output the price of one of its region's deliverable bands
    (capped at min(MAXAVAIL, AVAILABILITY), divided by TLF x DLF), or the market price cap or floor.
    If the actual price is within $5 of none of those, no regional merit order could hit it, however it
    chose: the price was set by something outside the region's stack (an import with losses, or a
    network constraint). Like the single-price bound, this is computed knowing the answer.
    """
    bands = ",".join(f"struct_pack(band := {i}, mw := p.bandavail{i}, pr := o.priceband{i})" for i in range(1, 11))
    frames = []
    for m in months:
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
            deliverable as (select c.t, c.region, c.price from c
                            join raw.dispatchload l on l.duid = c.duid and l.settlementdate = c.t and l.intervention = 0
                            where c.mw > 0 and c.cum - c.mw < least(c.maxavail, l.availability)),
            th as (select distinct p.settlementdate t, p.regionid region, p.rrp, m.voll cap, m.marketpricefloor floor
                   from raw.dispatchprice p asof join raw.market_price_thresholds m on p.settlementdate > m.effectivedate
                   where p.intervention = 0 and strftime(p.settlementdate - interval 5 minute, '%Y-%m') = '{m}')
            select th.t as interval, th.region,
                   bool_or(abs(dl.price - th.rrp) <= {TOL}) or abs(th.rrp - th.cap) <= {TOL}
                     or abs(th.rrp - th.floor) <= {TOL} as stack_reachable
            from th left join deliverable dl on dl.t = th.t and dl.region = th.region
            group by th.t, th.region, th.rrp, th.cap, th.floor""").df())
    return pd.concat(frames)


def constraint_adjusted_bound(con, sign: float, months=MONTHS) -> pd.DataFrame:
    """Per region-interval: could an offer in the region, adjusted by the shadow prices of the binding
    constraints it appears in, have produced a price within $5 of the actual?

    In the LP, a partly dispatched band of unit u satisfies
        regional price = band price / (TLF x DLF) + sign x sum_k (marginal value_k x coefficient of u in k).
    This takes each deliverable band in the region (as in regional_stack_bound), adds the actual marginal
    values of the binding constraints that contain the unit (energy terms, from the definition tables),
    and asks whether any lands within $5 of the actual price; the cap and floor also count. Marginal
    values are outcomes, so like every bound here this is found knowing the answer, never a model.
    `sign` is AEMO's sign convention for MARGINALVALUE, settled by a direction test.
    """
    bands = ",".join(f"struct_pack(band := {i}, mw := p.bandavail{i}, pr := o.priceband{i})" for i in range(1, 11))
    frames = []
    for m in months:
        frames.append(con.execute(f"""
            with du as (select connectionpointid, any_value(duid order by end_date desc) duid
                        from raw.dudetailsummary group by 1),
            binding as (select d.settlementdate t, d.constraintid, d.marginalvalue mv, d.genconid_effectivedate eff,
                               d.genconid_versionno ver
                        from raw.dispatchconstraint d
                        where d.intervention = 0 and d.marginalvalue <> 0
                          and strftime(d.settlementdate - interval 5 minute, '%Y-%m') = '{m}'),
            adj as (select b.t, coalesce(du.duid, s.connectionpointid) duid, sum(b.mv * s.factor) adj
                    from binding b join raw.spdconnectionpointconstraint s on s.genconid = b.constraintid
                      and s.effectivedate = b.eff and s.versionno = b.ver and s.bidtype = 'ENERGY'
                    left join du on du.connectionpointid = s.connectionpointid
                    group by 1, 2),
            o as (select p.interval_datetime t, p.duid, p.maxavail, d.regionid region,
                         d.transmissionlossfactor * coalesce(d.distributionlossfactor, 1) mlf, unnest([{bands}]) b
                  from raw.bidperoffer_d p
                  join raw.biddayoffer_d o on o.settlementdate = p.settlementdate and o.duid = p.duid
                                          and o.direction = p.direction
                  join raw.dudetailsummary d on d.duid = p.duid
                    and p.interval_datetime - interval 5 minute >= d.start_date
                    and p.interval_datetime - interval 5 minute < d.end_date
                  where p.direction = 'GEN' and strftime(p.interval_datetime - interval 5 minute, '%Y-%m') = '{m}'),
            c as (select *, b.mw mw, b.pr / mlf price, sum(b.mw) over (partition by t, duid order by b.band) cum from o),
            deliverable as (select c.t, c.region, c.price + {sign} * coalesce(a.adj, 0) as price
                            from c join raw.dispatchload l on l.duid = c.duid and l.settlementdate = c.t
                                                          and l.intervention = 0
                            left join adj a on a.t = c.t and a.duid = c.duid
                            where c.mw > 0 and c.cum - c.mw < least(c.maxavail, l.availability)),
            th as (select distinct p.settlementdate t, p.regionid region, p.rrp, m.voll cap, m.marketpricefloor floor
                   from raw.dispatchprice p asof join raw.market_price_thresholds m on p.settlementdate > m.effectivedate
                   where p.intervention = 0 and strftime(p.settlementdate - interval 5 minute, '%Y-%m') = '{m}')
            select th.t as interval, th.region,
                   bool_or(abs(dl.price - th.rrp) <= {TOL}) or abs(th.rrp - th.cap) <= {TOL}
                     or abs(th.rrp - th.floor) <= {TOL} as constraint_reachable
            from th left join deliverable dl on dl.t = th.t and dl.region = th.region
            group by th.t, th.region, th.rrp, th.cap, th.floor""").df())
    return pd.concat(frames)


def network_bound(con, constraint_bound: pd.DataFrame, months=MONTHS) -> pd.DataFrame:
    """The constraint-adjusted bound widened to the whole network. A region-interval is reachable if

    - its own offers, adjusted by constraint shadow prices, reach the price (constraint_bound); or
    - a scheduled load's bid in the region (pumping, battery charging), divided by TLF x DLF, is within
      $5 of the price, because a load can be the marginal participant; or
    - its price is a neighbour's price carried across a link, scaled by that link's marginal loss factor
      in either direction (within $5), *and that neighbour is itself reachable by its own offers and
      shadow prices*, so an unexplained price cannot explain another.

    Uses actual prices, marginal loss factors and marginal values: outcomes, so this is a bound, found
    knowing the answer, never a model.
    """
    con.register("_cb", constraint_bound)
    frames = []
    links = ", ".join(f"('{i}', '{a}', '{b}')" for i, (a, b) in __import__("nemsim.inputs", fromlist=["x"])
                      .INTERCONNECTORS.items())
    for m in months:
        frames.append(con.execute(f"""
            with p as (select p.settlementdate t, p.regionid r, p.rrp, cb.constraint_reachable own
                       from raw.dispatchprice p join _cb cb on cb.interval = p.settlementdate and cb.region = p.regionid
                       where p.intervention = 0 and strftime(p.settlementdate - interval 5 minute, '%Y-%m') = '{m}'),
            links as (select * from (values {links}) v(id, a, b)),
            imp as (select p.t, p.r, bool_or(n.own and (abs(p.rrp - n.rrp / ic.marginalloss) <= {TOL}
                                                       or abs(p.rrp - n.rrp * ic.marginalloss) <= {TOL})) import_set
                    from p join links l on p.r in (l.a, l.b)
                    join raw.dispatchinterconnectorres ic on ic.interconnectorid = l.id and ic.settlementdate = p.t
                                                         and ic.intervention = 0
                    join p n on n.t = p.t and n.r = case when p.r = l.a then l.b else l.a end
                    group by 1, 2),
            ld as (select p.t, p.r, bool_or(abs(o.pr / (d.transmissionlossfactor * coalesce(d.distributionlossfactor, 1))
                                                - p.rrp) <= {TOL}) load_set
                   from p join raw.bidperoffer_d b on b.interval_datetime = p.t and b.direction = 'LOAD'
                   join raw.dudetailsummary d on d.duid = b.duid and d.regionid = p.r
                     and p.t - interval 5 minute >= d.start_date and p.t - interval 5 minute < d.end_date
                   join raw.biddayoffer_d bd on bd.settlementdate = b.settlementdate and bd.duid = b.duid
                                            and bd.direction = 'LOAD'
                   cross join unnest([bd.priceband1, bd.priceband2, bd.priceband3, bd.priceband4, bd.priceband5,
                                      bd.priceband6, bd.priceband7, bd.priceband8, bd.priceband9, bd.priceband10]) as o(pr)
                   group by 1, 2)
            select p.t as interval, p.r as region, p.own as constraint_reachable,
                   coalesce(ld.load_set, false) as load_set, coalesce(imp.import_set, false) as import_set,
                   p.own or coalesce(ld.load_set, false) or coalesce(imp.import_set, false) as network_reachable
            from p left join imp using (t, r) left join ld using (t, r)""").df())
    con.unregister("_cb")
    return pd.concat(frames)


if __name__ == "__main__":
    main()
