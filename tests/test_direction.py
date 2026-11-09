"""The DIRECTION backfill for offers made before IESS (3 June 2024), when bid files had no DIRECTION column.

This is era-specific logic, so it is tested two ways: on a toy warehouse, for each branch of the rule,
and on the real warehouse against every offer that does carry DIRECTION (post-IESS months), where a
unit that offers in only one direction must offer in the direction the rule gives it.
"""
from pathlib import Path

import duckdb
import pytest

from nemsim.add_month import DIRECTION_RULE, fill_direction

UNITS = [  # duid, start, end, dispatchtype, dispatchsubtype
    ("GEN1", "2010-01-01", "2999-12-31", "GENERATOR", None),
    ("PUMP1", "2010-01-01", "2015-01-01", "LOAD", None),
    ("PUMP1", "2015-01-01", "2999-12-31", "LOAD", None),
    ("WDR1", "2021-10-24", "2999-12-31", "LOAD", "WDR"),
    ("BDU1", "2024-06-03", "2999-12-31", "BIDIRECTIONAL", None),
]


def toy(offers):
    con = duckdb.connect()
    con.execute("create schema raw")
    con.execute("create table raw.dudetailsummary (duid varchar, start_date timestamp, end_date timestamp, "
                "dispatchtype varchar, dispatchsubtype varchar)")
    con.executemany("insert into raw.dudetailsummary values (?, ?, ?, ?, ?)", UNITS)
    for t in ("bidperoffer_d", "biddayoffer_d"):
        con.execute(f"create table raw.{t} (settlementdate timestamp, duid varchar, direction varchar)")
        con.executemany(f"insert into raw.{t} values (?, ?, ?)", offers)
    return con


def test_rule_gives_each_kind_of_unit_its_direction():
    con = toy([("2020-01-15", d, None) for d in ("GEN1", "PUMP1")] + [("2022-01-15", "WDR1", None)])
    assert fill_direction(con) == 6                          # three offers in each of the two tables
    got = dict(con.execute("select duid, direction from raw.bidperoffer_d").fetchall())
    assert got == {"GEN1": "GEN", "PUMP1": "LOAD", "WDR1": "GEN"}
    assert con.execute("select count(*) from raw.biddayoffer_d where direction is null").fetchone()[0] == 0


def test_offers_that_carry_direction_are_left_alone():
    con = toy([("2020-01-15", "PUMP1", "GEN"), ("2025-01-15", "BDU1", "LOAD")])
    assert fill_direction(con) == 0
    assert con.execute("select list(direction order by duid) from raw.bidperoffer_d").fetchone()[0] == ["LOAD", "GEN"]


def test_refuses_to_continue_with_an_unknown_unit_or_a_post_iess_gap():
    with pytest.raises(RuntimeError, match="no DIRECTION"):
        fill_direction(toy([("2020-01-15", "NOSUCH", None)]))
    with pytest.raises(RuntimeError, match="no DIRECTION"):
        fill_direction(toy([("2024-07-01", "GEN1", None)]))     # after IESS a missing DIRECTION is an error


WAREHOUSE = Path(__file__).resolve().parents[1] / "warehouse" / "sim.duckdb"


def test_rule_matches_every_post_iess_offer_from_a_one_direction_unit():
    if not WAREHOUSE.exists():
        pytest.skip("no local warehouse")
    try:
        con = duckdb.connect(str(WAREHOUSE), read_only=True)
    except duckdb.IOException:
        pytest.skip("warehouse is locked by a writer")
    with con:
        rows = con.execute(f"""
            with r as ({DIRECTION_RULE}),
                 t as (select duid, any_value(dispatchtype order by end_date desc) dt from raw.dudetailsummary group by 1),
                 o as (select distinct duid, direction from raw.bidperoffer_d
                       where settlementdate >= date '2024-06-03' and direction is not null)
            select o.duid, o.direction, r.direction from o join r using (duid) join t using (duid)
            where t.dt <> 'BIDIRECTIONAL'""").fetchall()
    if not rows:
        pytest.skip("no post-IESS offers loaded")
    wrong = [r for r in rows if r[1] != r[2]]
    assert not wrong, wrong[:10]
    assert len({r[0] for r in rows}) > 400
