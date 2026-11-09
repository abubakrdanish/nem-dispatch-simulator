"""A tiny made-up warehouse with the same tables and columns as the real one.

Two regions' worth of numbers spread over the five NEM regions, three intervals, a handful of
units. Every value is invented; nothing here comes from AEMO.
"""
from __future__ import annotations

from pathlib import Path

import duckdb

INTERVALS = ["2030-01-01 04:05:00", "2030-01-01 04:10:00", "2030-01-01 04:15:00"]
REGIONS = ["NSW1", "QLD1", "SA1", "TAS1", "VIC1"]

# unit, region, TLF, DLF, maxavail, band MW (10), band prices at the connection point (10)
UNITS = [
    ("COAL1", "NSW1", 0.95, 1.0, 400, [300, 200, 0, 0, 0, 0, 0, 0, 0, 0],
     [-950, 38, 95, 190, 285, 950, 1900, 4750, 9500, 19285]),
    ("GAS1", "VIC1", 1.0, 1.0, 200, [0, 100, 100, 0, 0, 0, 0, 0, 0, 0],
     [-1000, 80, 150, 300, 500, 1000, 2000, 5000, 10000, 20300]),
    ("WIND1", "SA1", 0.9, 0.98, 150, [150, 0, 0, 0, 0, 0, 0, 0, 0, 0],
     [-882, 0, 10, 20, 30, 40, 50, 60, 70, 18000]),
]


def build(target: Path) -> Path:
    con = duckdb.connect(str(target))
    con.execute("create schema raw")
    con.execute("""create table raw.market_price_thresholds as
        select timestamp '2029-07-01' as effectivedate, 1 as versionno, 20300.0 as voll,
               -1000.0 as marketpricefloor, 1000000.0 as administered_price_threshold""")
    con.execute("""create table raw.dudetailsummary (duid varchar, start_date timestamp, end_date timestamp,
        dispatchtype varchar, regionid varchar, transmissionlossfactor double, distributionlossfactor double,
        schedule_type varchar, connectionpointid varchar)""")
    con.execute("""create table raw.biddayoffer_d (settlementdate timestamp, duid varchar, bidtype varchar,
        direction varchar, """ + ", ".join(f"priceband{i} double" for i in range(1, 11)) + ")")
    con.execute("""create table raw.bidperoffer_d (settlementdate timestamp, duid varchar, bidtype varchar,
        direction varchar, interval_datetime timestamp, maxavail double, """
                + ", ".join(f"bandavail{i} double" for i in range(1, 11)) + ")")
    for duid, region, tlf, dlf, maxavail, mw, prices in UNITS:
        con.execute("insert into raw.dudetailsummary values (?, '2020-01-01', '2999-12-31', 'GENERATOR', ?, ?, ?, "
                    "'SCHEDULED', ?)", [duid, region, tlf, dlf, "CP_" + duid])
        con.execute("insert into raw.biddayoffer_d values ('2030-01-01', ?, 'ENERGY', 'GEN', "
                    + ", ".join("?" * 10) + ")", [duid, *prices])
        # Only the first two intervals have offers, so the third tests the no_inputs exclusion.
        for t in INTERVALS[:2]:
            con.execute("insert into raw.bidperoffer_d values ('2030-01-01', ?, 'ENERGY', 'GEN', ?, ?, "
                        + ", ".join("?" * 10) + ")", [duid, t, maxavail, *mw])
    # Dispatch availability: WIND1's forecast (UIGF) is 50 MW against 150 MW declared; the others
    # are at their MAXAVAIL, as scheduled units always are in the real data.
    con.execute("""create table raw.dispatchload (settlementdate timestamp, duid varchar, intervention integer,
        initialmw double, totalcleared double, availability double, rampuprate double, rampdownrate double,
        uigf double, semidispatchcap integer)""")
    for t in INTERVALS:
        # Ramp rates of 1,200 MW/h allow 100 MW per interval. COAL1 starts at 350 MW, so it can go no
        # lower than 250; GAS1 and WIND1 start low enough that their floors are 0.
        for duid, initial, avail, uigf in [("COAL1", 350, 400, 0), ("GAS1", 100, 200, 0), ("WIND1", 50, 50, 50)]:
            con.execute("insert into raw.dispatchload values (?, ?, 0, ?, 99, ?, 1200, 1200, ?, 0)",
                        [t, duid, initial, avail, uigf])
    # Standing ratings: generous everywhere except into SA, which can import only 40 MW in all.
    # Loss equation: LOSSCONSTANT 1 and no flow or demand terms make every link lossless, except QNI,
    # whose coefficients are tested separately.
    con.execute("""create table raw.interconnectorconstraint (effectivedate timestamp, versionno integer,
        interconnectorid varchar, importlimit double, exportlimit double, lossconstant double,
        lossflowcoefficient double, fromregionlossshare double)""")
    for ic, imp, exp in [("NSW1-QLD1", 1000, 1000), ("N-Q-MNSP1", 100, 100), ("VIC1-NSW1", 1000, 1000),
                         ("V-SA", 30, 30), ("V-S-MNSP1", 10, 10), ("T-V-MNSP1", 500, 500)]:
        con.execute("insert into raw.interconnectorconstraint values ('2029-07-01', 1, ?, ?, ?, 1, 0, 0.5)",
                    [ic, imp, exp])
    con.execute("""create table raw.lossfactormodel (effectivedate timestamp, versionno integer,
        interconnectorid varchar, regionid varchar, demandcoefficient double)""")
    con.execute("""create table raw.dispatchregionsum (settlementdate timestamp, regionid varchar,
        intervention integer, totaldemand double, dispatchableload double, dispatchablegeneration double,
        netinterchange double)""")
    con.execute("""create table raw.dispatchprice (settlementdate timestamp, regionid varchar, intervention integer,
        rrp double, rop double, apcflag integer, marketsuspendedflag integer)""")
    # Demand: 100 MW per region in the first interval (500 MW in total), 120 in the second.
    demand = {INTERVALS[0]: 100, INTERVALS[1]: 120, INTERVALS[2]: 100}
    for t in INTERVALS:
        for r in REGIONS:
            con.execute("insert into raw.dispatchregionsum values (?, ?, 0, ?, 0, ?, 0)", [t, r, demand[t], demand[t]])
            rrp = {"NSW1": 40, "QLD1": 45, "SA1": -10, "TAS1": 40, "VIC1": 80}[r]
            suspended = 2 if (t == INTERVALS[2] and r == "TAS1") else 0
            con.execute("insert into raw.dispatchprice values (?, ?, 0, ?, ?, 0, ?)", [t, r, rrp, rrp, suspended])
    con.execute("""create table raw.dispatchinterconnectorres (settlementdate timestamp, interconnectorid varchar,
        intervention integer, mwflow double, exportlimit double, importlimit double, marginalloss double)""")
    for t in INTERVALS:
        for ic in ["NSW1-QLD1", "N-Q-MNSP1", "VIC1-NSW1", "V-SA", "V-S-MNSP1", "T-V-MNSP1"]:
            flow = 600.0 if ic == "V-SA" else 0.0   # Heywood at its export limit: touches VIC1 and SA1
            con.execute("insert into raw.dispatchinterconnectorres values (?, ?, 0, ?, 600, -600, 1)", [t, ic, flow])
    # Invoked constraints. N>COAL_CAP caps COAL1 at 260 MW; F_MAIN+TEST has an FCAS term, so rung 4
    # leaves it out. MARGINALVALUE and LHS are outcomes the rungs never read.
    con.execute("""create table raw.dispatchconstraint (settlementdate timestamp, constraintid varchar,
        intervention integer, marginalvalue double, rhs double, genconid_effectivedate timestamp,
        genconid_versionno integer, lhs double)""")
    for t in INTERVALS:
        con.execute("insert into raw.dispatchconstraint values (?, 'Q>NIL_TEST', 0, -12.5, 9999, '2029-07-01', 1, 0)", [t])
        con.execute("insert into raw.dispatchconstraint values (?, 'F_MAIN+TEST', 0, 3.0, 10, '2029-07-01', 1, 0)", [t])
        con.execute("insert into raw.dispatchconstraint values (?, 'N>NIL_SLACK', 0, 0, 9999, '2029-07-01', 1, 0)", [t])
        con.execute("insert into raw.dispatchconstraint values (?, 'N>COAL_CAP', 0, 70, 260, '2029-07-01', 1, 260)", [t])
    con.execute("""create table raw.gencondata (effectivedate timestamp, versionno integer, genconid varchar,
        constrainttype varchar, constraintvalue double, genericconstraintweight double)""")
    con.execute("""create table raw.spdconnectionpointconstraint (connectionpointid varchar, effectivedate timestamp,
        versionno integer, genconid varchar, factor double, bidtype varchar)""")
    con.execute("""create table raw.spdinterconnectorconstraint (interconnectorid varchar, effectivedate timestamp,
        versionno integer, genconid varchar, factor double)""")
    con.execute("""create table raw.spdregionconstraint (regionid varchar, effectivedate timestamp, versionno integer,
        genconid varchar, factor double, bidtype varchar)""")
    for gid in ("Q>NIL_TEST", "F_MAIN+TEST", "N>NIL_SLACK", "N>COAL_CAP"):
        con.execute("insert into raw.gencondata values ('2029-07-01', 1, ?, '<=', 0, 35)", [gid])
    con.execute("insert into raw.spdconnectionpointconstraint values ('CP_COAL1', '2029-07-01', 1, 'N>COAL_CAP', 1, 'ENERGY')")
    con.execute("insert into raw.spdconnectionpointconstraint values ('CP_GAS1', '2029-07-01', 1, 'F_MAIN+TEST', 1, 'RAISEREG')")
    con.execute("insert into raw.spdinterconnectorconstraint values ('NSW1-QLD1', '2029-07-01', 1, 'Q>NIL_TEST', 1)")
    con.execute("insert into raw.spdinterconnectorconstraint values ('VIC1-NSW1', '2029-07-01', 1, 'N>NIL_SLACK', 1)")
    con.close()
    return target
