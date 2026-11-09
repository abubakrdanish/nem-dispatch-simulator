"""What dropping constraint terms does to rung 4: python -m nemsim.audit_terms

Rung 4 drops a constraint's unit term when the unit has no energy offer in the LP (loads held at actual,
non-scheduled units, units not offering that interval), and it models only the generation side of a
battery. Dropping a term is not neutral: it moves the constraint's left-hand side, so the constraint
becomes looser or tighter than the real one.

For every constraint-interval rung 4 applies (no region terms, no FCAS terms), this rebuilds the
left-hand side from actual dispatch in three ways and compares each with the LHS AEMO reports:

  kept    what rung 4 can represent: generation-side MW of units with an energy offer, plus link flows
  full    kept + the dropped terms at their actual MW + the battery charging side
  proxy   kept + the dropped terms at INITIALMW, the output measured at the start of the interval,
          before dispatch (a pre-dispatch proxy)

The dropped contribution D = full - kept is signed by the constraint's sense into "looseness":
for a <= constraint the model is looser than reality when D > 0; for >= when D < 0. Positive looseness
means rung 4's version of the constraint has room the real one did not, so it tends to under-price
scarcity; negative means it is tighter and tends to over-price.

Everything here uses outcomes (actual MW, AEMO's reported LHS, marginal values for the binding flag).
It measures the rung; nothing from it feeds the rung. Writes results/audit_terms.csv (aggregated).
"""
from __future__ import annotations

import time

import duckdb
import pandas as pd

from nemsim.config import ROOT, path
from nemsim.harness import constraint_region
from nemsim.intervals import MONTHS


def month_frame(con, m: str, load_sign: float, start: str, end: str) -> pd.DataFrame:
    """One chunk of intervals [start, end) within month m (a whole month needs over 19 GB in one query)."""
    w = f"settlementdate >= '{start}' and settlementdate < '{end}'"
    return con.execute(f"""
        with gen as (select distinct interval_datetime t, duid from raw.bidperoffer_d
                     where direction = 'GEN' and interval_datetime >= '{start}' and interval_datetime < '{end}'),
        bad as (select distinct genconid, effectivedate, versionno from raw.spdregionconstraint
                union select distinct genconid, effectivedate, versionno from raw.spdconnectionpointconstraint
                      where bidtype <> 'ENERGY'),
        dc as (select d.settlementdate t, d.constraintid c, d.genconid_effectivedate eff, d.genconid_versionno ver,
                      d.lhs, d.marginalvalue <> 0 as binding, trim(g.constrainttype) sense
               from raw.dispatchconstraint d
               join raw.gencondata g on g.genconid = d.constraintid and g.effectivedate = d.genconid_effectivedate
                                    and g.versionno = d.genconid_versionno
               where d.intervention = 0 and d.{w}
                 and not exists (select 1 from bad b where b.genconid = d.constraintid
                                 and b.effectivedate = d.genconid_effectivedate and b.versionno = d.genconid_versionno)),
        du as (select connectionpointid, duid, dispatchtype, start_date, end_date from raw.dudetailsummary),
        ut as (select dc.t, dc.c, s.factor, du.dispatchtype, gen.duid is not null as kept,
                      l.totalcleared mw, l.initialmw, l.duid is not null as has_dl
               from dc join raw.spdconnectionpointconstraint s on s.genconid = dc.c and s.effectivedate = dc.eff
                                                               and s.versionno = dc.ver
               left join du on du.connectionpointid = s.connectionpointid
                 and dc.t - interval 5 minute >= du.start_date and dc.t - interval 5 minute < du.end_date
               left join gen on gen.t = dc.t and gen.duid = du.duid
               left join raw.dispatchload l on l.duid = du.duid and l.settlementdate = dc.t and l.intervention = 0),
        u as (select t, c,
                sum(factor * greatest(coalesce(mw, 0), 0)) filter (where kept) kept_gen,
                sum(factor * least(coalesce(mw, 0), 0)) filter (where kept) kept_charge,
                sum(factor * coalesce(mw, 0) * case when dispatchtype = 'LOAD' then {load_sign} else 1 end)
                  filter (where not kept) dropped_actual,
                sum(factor * coalesce(initialmw, 0) * case when dispatchtype = 'LOAD' then {load_sign} else 1 end)
                  filter (where not kept) dropped_proxy,
                count(*) filter (where not kept) n_dropped,
                count(*) filter (where not kept and not has_dl) n_dropped_no_dl,
                count(*) filter (where not kept and dispatchtype = 'LOAD') n_dropped_load,
                count(*) n_unit_terms
              from ut group by 1, 2),
        lk as (select dc.t, dc.c, sum(s.factor * r.mwflow) links
               from dc join raw.spdinterconnectorconstraint s on s.genconid = dc.c and s.effectivedate = dc.eff
                                                              and s.versionno = dc.ver
               join raw.dispatchinterconnectorres r on r.interconnectorid = s.interconnectorid
                                                   and r.settlementdate = dc.t and r.intervention = 0
               group by 1, 2)
        select dc.t, dc.c, dc.sense, dc.binding, dc.lhs,
               coalesce(u.kept_gen, 0) + coalesce(lk.links, 0) as lhs_kept,
               coalesce(u.kept_gen, 0) + coalesce(u.kept_charge, 0) + coalesce(u.dropped_actual, 0)
                 + coalesce(lk.links, 0) as lhs_full,
               coalesce(u.kept_gen, 0) + coalesce(u.dropped_proxy, 0) + coalesce(lk.links, 0) as lhs_proxy,
               coalesce(u.dropped_actual, 0) as d_dropped, coalesce(u.kept_charge, 0) as d_charge,
               coalesce(u.n_dropped, 0) n_dropped, coalesce(u.n_dropped_no_dl, 0) n_dropped_no_dl,
               coalesce(u.n_dropped_load, 0) n_dropped_load, coalesce(u.n_unit_terms, 0) n_unit_terms
        from dc left join u using (t, c) left join lk using (t, c)""").df()


def summarise_month(f: pd.DataFrame, regions: dict, prices: pd.DataFrame) -> pd.DataFrame:
    f["d_total"] = f.d_dropped + f.d_charge
    s = f.sense.map({"<=": 1.0, ">=": -1.0}).fillna(0.0)          # "=" constraints: shift, not loosening
    f["loose"] = s * f.d_total
    f["affected"] = (f.n_dropped > 0) | (f.d_charge.abs() > 0.01)
    f["region"] = f.c.map(regions).fillna("other")
    f["family"] = f.c.str.extract(r"^([#$]|[A-Z]+[>:^_+\-]{1,2}|[A-Z])")[0].fillna("other")
    f = f.merge(prices, how="left", left_on=["t", "region"], right_on=["interval", "region"])
    f["band"] = pd.cut(f.rrp, [-1e9, 0, 50, 300, 1e9], right=False,
                       labels=["a. negative", "b. $0-50", "c. $50-300", "d. $300+"]).astype(str)
    for k in ("kept", "full", "proxy"):
        f[f"err_{k}"] = f[f"lhs_{k}"] - f.lhs
    keys = ["region", "family", "band", "binding", "sense"]
    g = f.groupby(keys, dropna=False)
    out = g.agg(ci=("c", "size"), affected=("affected", "sum"), n_dropped=("n_dropped", "sum"),
                n_dropped_no_dl=("n_dropped_no_dl", "sum"), n_dropped_load=("n_dropped_load", "sum"),
                n_unit_terms=("n_unit_terms", "sum"), loose_sum=("loose", "sum"),
                loose_pos=("loose", lambda x: (x > 1).sum()), loose_neg=("loose", lambda x: (x < -1).sum()),
                d_total_sum=("d_total", "sum"),
                kept_match=("err_kept", lambda x: (x.abs() < 1).sum()),
                full_match=("err_full", lambda x: (x.abs() < 1).sum()),
                proxy_match=("err_proxy", lambda x: (x.abs() < 1).sum()),
                err_kept_sum=("err_kept", "sum"), err_full_sum=("err_full", "sum"),
                err_proxy_sum=("err_proxy", "sum")).reset_index()
    out["distinct_affected"] = g.apply(lambda x: x.loc[x.affected, "c"].nunique(), include_groups=False).to_numpy()
    return out


def chunks(m: str, days: int = 3):
    start = pd.Timestamp(m + "-01") + pd.Timedelta(minutes=5)
    end = start + pd.offsets.MonthBegin(1)
    t = start
    while t < end:
        u = min(t + pd.Timedelta(days=days), end)
        yield str(t), str(u)
        t = u


def main() -> None:
    start = time.monotonic()
    out, affected = [], []
    with duckdb.connect(str(path("warehouse")), read_only=True) as con:
        con.execute("set threads = 4")
        con.execute("set memory_limit = '10GB'")
        con.execute("set preserve_insertion_order = false")
        ids = con.execute("select distinct constraintid from raw.dispatchconstraint").df().constraintid
        duid_region = dict(con.execute("""select duid, any_value(regionid order by end_date desc)
                                          from raw.dudetailsummary group by 1""").fetchall())
        regions = {c: constraint_region(c, duid_region) for c in ids}
        # Which sign do load terms enter with? Settled on one day before the full run.
        a = month_frame(con, "2025-09", 1.0, "2025-09-10 00:05", "2025-09-11 00:05")
        b = month_frame(con, "2025-09", -1.0, "2025-09-10 00:05", "2025-09-11 00:05")
        has_load = a.n_dropped_load > 0
        plus = ((a.lhs_full - a.lhs).abs() < 1)[has_load].mean() * 100
        minus = ((b.lhs_full - b.lhs).abs() < 1)[has_load].mean() * 100
        sign = 1.0 if plus >= minus else -1.0
        print(f"load terms: LHS within 1 MW {plus:.1f}% with +MW, {minus:.1f}% with -MW "
              f"({int(has_load.sum())} constraint-intervals, 2025-09-10); using {sign:+.0f}", flush=True)
        for m in MONTHS:
            for s0, s1 in chunks(m):
                prices = con.execute("""select settlementdate as interval, regionid as region, rrp from raw.dispatchprice
                                        where intervention = 0 and settlementdate >= ? and settlementdate < ?""",
                                     [s0, s1]).df()
                f = month_frame(con, m, sign, s0, s1)
                agg = summarise_month(f, regions, prices).assign(month=m)
                out.append(agg.drop(columns="distinct_affected"))
                affected.append(f.loc[(f.n_dropped > 0) | (f.d_charge.abs() > 0.01), ["c"]].drop_duplicates()
                                .assign(month=m))
            print(m, f"{time.monotonic() - start:.0f}s", flush=True)
    keys = ["region", "family", "band", "binding", "sense", "month"]
    res = pd.concat(out).groupby(keys, dropna=False).sum(numeric_only=True).reset_index()
    res.to_csv(ROOT / "results" / "audit_terms.csv", index=False)
    pd.concat(affected).drop_duplicates().to_csv(ROOT / "results" / "audit_terms_affected_constraints.csv", index=False)
    print("wrote results/audit_terms.csv and results/audit_terms_affected_constraints.csv")


if __name__ == "__main__":
    main()
