"""Inputs for a window of 5-minute intervals, read from this project's DuckDB file.

A model is handed an `IntervalInputs` for one interval. It holds only the inputs the model
declares in `Rung.uses`; the actual price is never in it. The harness reads actual prices
and interval classes separately (see harness.py), so a rung cannot see the answer.

All times are interval ends in NEM market time, as AEMO publishes them. Dispatch rows are the
pricing run (INTERVENTION = 0).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import duckdb
import pandas as pd

REGIONS = ("NSW1", "QLD1", "SA1", "TAS1", "VIC1")

# Interconnector endpoints: positive MWFLOW runs from the first region to the second.
INTERCONNECTORS = {
    "NSW1-QLD1": ("NSW1", "QLD1"), "N-Q-MNSP1": ("NSW1", "QLD1"), "VIC1-NSW1": ("VIC1", "NSW1"),
    "V-SA": ("VIC1", "SA1"), "V-S-MNSP1": ("VIC1", "SA1"), "T-V-MNSP1": ("TAS1", "VIC1"),
}

# Every input a rung can ask for. "outcome" inputs are AEMO's own dispatch results, held at
# actual by any rung that uses them; a rung that uses one is told what happened, not modelling it.
INPUTS = {
    "offers": "final energy offers: MW per band (BIDPEROFFER_D) at band prices (BIDDAYOFFER_D) / (TLF x DLF)",
    "demand": "regional TOTALDEMAND, the demand NEMDE balances (DISPATCHREGIONSUM)",
    "scheduled_load": "outcome: cleared scheduled load per region (DISPATCHABLELOAD)",
    "regional_dispatch": "outcome: cleared generation and net interchange per region",
    # AVAILABILITY is an input to dispatch: MAXAVAIL for scheduled units, and min(MAXAVAIL, UIGF) for
    # wind and solar, where UIGF is AEMO's forecast of the unit's output, made before the interval.
    # INITIALMW is the output measured at the start of the interval, which dispatch starts from.
    # Withheld because they are dispatch outcomes: TOTALCLEARED (the target) and SEMIDISPATCHCAP
    # (whether dispatch capped the unit). Realised output at the end of the interval is not offered.
    "units": "per-unit dispatch availability (UIGF-capped for wind and solar), initial MW and ramp rates "
             "(DISPATCHLOAD); TOTALCLEARED and SEMIDISPATCHCAP are withheld",
    # The standing ratings are set before dispatch. The limits in DISPATCHINTERCONNECTORRES are
    # calculated by the dispatch run from the constraints that bind at its solution, and can cross
    # (export below import), which a bound set before the solve never could; so they are outcomes.
    "interconnector_ratings": "standing import and export ratings of each interconnector "
                              "(INTERCONNECTORCONSTRAINT, version in force)",
    # The loss equation: each link's marginal loss factor is LOSSCONSTANT + LOSSFLOWCOEFFICIENT x flow
    # + sum(DEMANDCOEFFICIENT x regional TOTALDEMAND), from the standing tables in force (changed each
    # 1 July). Checked against AEMO's per-interval losses in docs/stage2_results.md, rung 3b.
    "interconnector_loss_model": "per link: LOSSCONSTANT, LOSSFLOWCOEFFICIENT, FROMREGIONLOSSSHARE "
                                 "(INTERCONNECTORCONSTRAINT) and the demand term sum(DEMANDCOEFFICIENT x "
                                 "TOTALDEMAND) (LOSSFACTORMODEL), versions in force",
    # Rung 4. Which constraints are invoked in an interval, and their right-hand sides, are set before the
    # solve from system conditions: inputs. Only the constraint ID, its equation version and RHS are read
    # from DISPATCHCONSTRAINT. MARGINALVALUE, VIOLATIONDEGREE and LHS are produced by the solve and are
    # never read here. The equations come from the definition tables (GENCONDATA, SPD*CONSTRAINT).
    "constraints": "invoked generic constraints with their RHS (DISPATCHCONSTRAINT) and equations "
                   "(GENCONDATA type and weight; SPDCONNECTIONPOINT, SPDINTERCONNECTOR and SPDREGION terms)",
    "interconnectors": "outcome: dispatch-calculated interconnector limits and marginal loss factors "
                       "(DISPATCHINTERCONNECTORRES); flows withheld",
}


@dataclass
class IntervalInputs:
    interval: datetime
    price_floor: float
    price_cap: float
    offers: pd.DataFrame | None = None            # duid, region, direction, band, price, mw, raw_price, tlf, dlf, maxavail
    demand: pd.DataFrame | None = None            # region, totaldemand
    scheduled_load: pd.DataFrame | None = None    # region, dispatchableload
    regional_dispatch: pd.DataFrame | None = None  # region, dispatchablegeneration, netinterchange
    units: pd.DataFrame | None = None             # duid, initialmw, availability, rampuprate, rampdownrate, uigf
    interconnectors: pd.DataFrame | None = None   # interconnectorid, from/to region, limits, marginalloss
    interconnector_ratings: pd.DataFrame | None = None  # interconnectorid, from/to region, import/export rating
    interconnector_loss_model: pd.DataFrame | None = None  # interconnectorid, lossconstant, lossflowcoefficient,
    #                                                        fromregionlossshare, demand_term
    constraints: pd.DataFrame | None = None       # constraintid, rhs, sense, weight (invoked this interval)
    constraint_terms: pd.DataFrame | None = None  # constraintid, kind (unit/link/region), target, factor, bidtype


class Window:
    """All inputs for intervals ending in [start, end], loaded once and sliced per interval."""

    def __init__(self, con: duckdb.DuckDBPyConnection, start: datetime, end: datetime, uses: frozenset[str]):
        unknown = uses - INPUTS.keys()
        if unknown:
            raise ValueError(f"unknown inputs {sorted(unknown)}; choose from {sorted(INPUTS)}")
        self.uses = uses
        p = [start, end]
        self.thresholds = con.execute("""
            with t as (select distinct settlementdate as interval from raw.dispatchprice
                       where settlementdate between ? and ?)
            select t.interval, m.marketpricefloor as floor, m.voll as cap from t
            asof join (select * from raw.market_price_thresholds
                       qualify versionno = max(versionno) over (partition by effectivedate)) m
              on t.interval > m.effectivedate""", p).df().set_index("interval")
        self._frames = {}
        if "offers" in uses:
            self._frames["offers"] = offers(con, start, end)
        if uses & {"demand", "scheduled_load", "regional_dispatch"}:
            rs = con.execute("""select settlementdate as interval, regionid as region, totaldemand,
                    dispatchableload, dispatchablegeneration, netinterchange from raw.dispatchregionsum
                where intervention = 0 and settlementdate between ? and ?""", p).df()
            if "demand" in uses:
                self._frames["demand"] = rs[["interval", "region", "totaldemand"]]
            if "scheduled_load" in uses:
                self._frames["scheduled_load"] = rs[["interval", "region", "dispatchableload"]]
            if "regional_dispatch" in uses:
                self._frames["regional_dispatch"] = rs[["interval", "region", "dispatchablegeneration",
                                                        "netinterchange"]]
        if "units" in uses:
            self._frames["units"] = con.execute("""select settlementdate as interval, duid, initialmw,
                    availability, rampuprate, rampdownrate, uigf from raw.dispatchload
                where intervention = 0 and settlementdate between ? and ?""", p).df()
        if "interconnectors" in uses:
            ic = con.execute("""select settlementdate as interval, interconnectorid, exportlimit, importlimit,
                    marginalloss from raw.dispatchinterconnectorres
                where intervention = 0 and settlementdate between ? and ?""", p).df()
            ic["from_region"] = ic.interconnectorid.map(lambda i: INTERCONNECTORS[i][0])
            ic["to_region"] = ic.interconnectorid.map(lambda i: INTERCONNECTORS[i][1])
            self._frames["interconnectors"] = ic
        if "interconnector_ratings" in uses:
            r = con.execute(f"""
                with t as (select distinct settlementdate as interval from raw.dispatchprice
                           where settlementdate between ? and ?),
                ic as (select * from raw.interconnectorconstraint
                       where interconnectorid in ({", ".join(f"'{i}'" for i in INTERCONNECTORS)})
                       qualify versionno = max(versionno) over (partition by interconnectorid, effectivedate))
                select t.interval, ic.interconnectorid, ic.importlimit as import_rating,
                       ic.exportlimit as export_rating
                from t join ic on ic.effectivedate <= t.interval
                qualify row_number() over (partition by t.interval, ic.interconnectorid
                                           order by ic.effectivedate desc) = 1""", p).df()
            r["from_region"] = r.interconnectorid.map(lambda i: INTERCONNECTORS[i][0])
            r["to_region"] = r.interconnectorid.map(lambda i: INTERCONNECTORS[i][1])
            self._frames["interconnector_ratings"] = r
        if "interconnector_loss_model" in uses:
            ids = ", ".join(f"'{i}'" for i in INTERCONNECTORS)
            self._frames["interconnector_loss_model"] = con.execute(f"""
                with t as (select distinct settlementdate as interval from raw.dispatchprice
                           where settlementdate between ? and ?),
                ic as (select * from raw.interconnectorconstraint where interconnectorid in ({ids})
                       qualify versionno = max(versionno) over (partition by interconnectorid, effectivedate)),
                eq as (select t.interval, ic.interconnectorid, ic.lossconstant, ic.lossflowcoefficient,
                              ic.fromregionlossshare
                       from t join ic on ic.effectivedate <= t.interval
                       qualify row_number() over (partition by t.interval, ic.interconnectorid
                                                  order by ic.effectivedate desc) = 1),
                lf as (select * from raw.lossfactormodel where interconnectorid in ({ids})
                       qualify versionno = max(versionno) over (partition by interconnectorid, effectivedate, regionid)),
                lfv as (select t.interval, lf.interconnectorid, lf.regionid, lf.demandcoefficient
                        from t join lf on lf.effectivedate <= t.interval
                        qualify rank() over (partition by t.interval, lf.interconnectorid
                                             order by lf.effectivedate desc) = 1),
                dt as (select lfv.interval, lfv.interconnectorid, sum(lfv.demandcoefficient * r.totaldemand) demand_term
                       from lfv join raw.dispatchregionsum r on r.settlementdate = lfv.interval
                         and r.regionid = lfv.regionid and r.intervention = 0
                       group by 1, 2)
                select eq.*, coalesce(dt.demand_term, 0) as demand_term
                from eq left join dt using (interval, interconnectorid)""", p).df()
        if "constraints" in uses:
            c = con.execute("""
                select d.settlementdate as interval, d.constraintid, d.rhs, d.genconid_effectivedate as eff,
                       d.genconid_versionno as ver, g.constrainttype as sense, g.genericconstraintweight as weight
                from raw.dispatchconstraint d
                left join raw.gencondata g on g.genconid = d.constraintid and g.effectivedate = d.genconid_effectivedate
                                          and g.versionno = d.genconid_versionno
                where d.intervention = 0 and d.settlementdate between ? and ?""", p).df()
            self._frames["constraints"] = c
            con.register("_versions", c[["constraintid", "eff", "ver"]].drop_duplicates())
            # Unit terms name a connection point; each maps to exactly one DUID on a given date.
            self._terms = con.execute("""
                with du as (select connectionpointid, any_value(duid order by start_date desc) duid
                            from raw.dudetailsummary where start_date <= ? and end_date > ? group by 1)
                select v.constraintid, v.eff, v.ver, 'unit' as kind,
                       coalesce(du.duid, s.connectionpointid) as target, s.factor, s.bidtype
                from _versions v join raw.spdconnectionpointconstraint s on s.genconid = v.constraintid
                  and s.effectivedate = v.eff and s.versionno = v.ver
                left join du on du.connectionpointid = s.connectionpointid
                union all
                select v.constraintid, v.eff, v.ver, 'link', s.interconnectorid, s.factor, 'ENERGY'
                from _versions v join raw.spdinterconnectorconstraint s on s.genconid = v.constraintid
                  and s.effectivedate = v.eff and s.versionno = v.ver
                union all
                select v.constraintid, v.eff, v.ver, 'region', s.regionid, s.factor, s.bidtype
                from _versions v join raw.spdregionconstraint s on s.genconid = v.constraintid
                  and s.effectivedate = v.eff and s.versionno = v.ver""", [start, start]).df()
            con.unregister("_versions")
        self._groups = {k: dict(tuple(v.groupby("interval"))) for k, v in self._frames.items()}

    def intervals_with(self, name: str) -> set:
        return set(self._groups.get(name, {}))

    def at(self, interval: datetime) -> IntervalInputs:
        floor, cap = self.thresholds.loc[interval, ["floor", "cap"]]
        x = IntervalInputs(interval=interval, price_floor=float(floor), price_cap=float(cap))
        for name, groups in self._groups.items():
            frame = groups.get(pd.Timestamp(interval))
            setattr(x, name, None if frame is None else frame.drop(columns="interval").reset_index(drop=True))
        if x.constraints is not None:
            x.constraint_terms = x.constraints[["constraintid", "eff", "ver"]].merge(
                self._terms, on=["constraintid", "eff", "ver"])
        return x


# Fixed bids (stage 8). A generating unit can submit a fixed bid, FIXEDLOAD in its energy offer: dispatch
# then holds it at exactly that MW whatever its band prices. AEMO represents each one as a constraint
# named "$" + DUID whose right-hand side equals FIXEDLOAD (measured: 99.1% of 198,265 invoked "$"
# constraint-intervals, docs/stage2_results.md section 18), which the definition tables do not hold.
# FIXEDLOAD comes from the participant's offer, made before the interval: an input. With FIXED_BIDS on,
# a unit with FIXEDLOAD > 0 is offered as one band of exactly FIXEDLOAD MW at the market price floor, so
# every rung takes it as a price-taker; its band offers are not used. Off reproduces the results scored
# before stage 8. Scheduled loads' fixed bids need nothing: every rung holds scheduled load at actual.
FIXED_BIDS = False
FLOOR = -1000.0      # the market price floor, -$1,000/MWh throughout the sample (MARKET_PRICE_THRESHOLDS)


def offers(con: duckdb.DuckDBPyConnection, start: datetime, end: datetime) -> pd.DataFrame:
    """One row per unit, direction, band and interval with MW > 0.

    `price` is the band price divided by the unit's marginal loss factor, TLF x DLF, which refers
    it to the regional reference node. The stage 1 research divided by TLF alone; that is right
    only where DLF = 1. For the 77 of 433 generators offering on 2025-09-01 with DLF != 1, the
    band floor and cap are exactly -1,000 and 20,300 times TLF x DLF (docs/stage2_results.md).
    Region and loss factors come from the DUDETAILSUMMARY row in force at the interval.
    """
    bands = ", ".join(f"struct_pack(band := {i}, mw := p.bandavail{i}, raw_price := d.priceband{i})"
                      for i in range(1, 11))
    has_fixed = con.execute("select count(*) from information_schema.columns where table_schema = 'raw' "
                            "and table_name = 'bidperoffer_d' and column_name = 'fixedload'").fetchone()[0]
    fixed = "p.fixedload" if has_fixed else "cast(null as double)"
    use_fixed = "true" if FIXED_BIDS else "false"
    return con.execute(f"""
        with per as (
            select * from raw.bidperoffer_d where interval_datetime between ? and ?),
        joined as (
            select p.interval_datetime as interval, p.duid, p.direction, p.maxavail, {fixed} as fixedload,
                   unnest([{bands}]) as b
            from per p join raw.biddayoffer_d d
              on d.settlementdate = p.settlementdate and d.duid = p.duid and d.direction = p.direction),
        fixed as (
            select j.*, coalesce(j.fixedload, 0) > 0 and j.direction = 'GEN' and {use_fixed} as is_fixed from joined j),
        bands as (
            select interval, duid, direction, maxavail, b.band as band, b.mw as mw, b.raw_price as raw_price
            from fixed where not is_fixed
            union all
            select distinct on (interval, duid, direction) interval, duid, direction, maxavail, 1, fixedload, null
            from fixed where is_fixed),
        units as (
            select duid, regionid, transmissionlossfactor as tlf, coalesce(distributionlossfactor, 1) as dlf,
                   dispatchtype, schedule_type,
                   start_date, end_date from raw.dudetailsummary)
        select j.interval, j.duid, u.regionid as region, j.direction, j.band, j.mw,
               coalesce(j.raw_price, {FLOOR} * u.tlf * u.dlf) as raw_price, u.tlf, u.dlf,
               coalesce(j.raw_price / (u.tlf * u.dlf), {FLOOR}) as price, j.maxavail,
               u.dispatchtype, u.schedule_type
        from bands j
        join units u on u.duid = j.duid
          and j.interval - interval 5 minute >= u.start_date and j.interval - interval 5 minute < u.end_date
        where j.mw > 0
        order by j.interval, price""", [start, end]).df()
