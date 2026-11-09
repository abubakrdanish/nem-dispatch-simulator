"""The frontend's data layer (nemsim/web/data.py), on the synthetic warehouse. FastAPI is an optional
extra, so nothing here imports it; the service itself is checked only if it is installed."""
from collections import Counter
from datetime import datetime

import duckdb
import pandas as pd
import pytest

from nemsim.harness import evaluate
from nemsim.inputs import INTERCONNECTORS, Window
from nemsim.rungs import LADDER
from nemsim.rungs.r4_constraints import build
from nemsim.web import data
from tests.fixtures.synthetic import INTERVALS, build as build_fixture

T = [datetime.fromisoformat(t) for t in INTERVALS]


def test_warehouse_connection_refuses_writes(tmp_path):
    con = data.warehouse(build_fixture(tmp_path / "w.duckdb"))
    with pytest.raises(duckdb.Error, match="read-only"):
        con.execute("create table raw.x (a int)")
    with pytest.raises(duckdb.Error, match="read-only"):
        con.cursor().execute("delete from raw.dispatchprice")


def test_constraint_table_agrees_with_rung_4(warehouse):
    """The frontend lists constraint by constraint what rung 4 counts in aggregate; the two must agree."""
    checked = 0
    for t in T:
        x = Window(warehouse, t, t, LADDER["r4"].uses).at(t)
        if x.offers is None or x.constraints is None:      # the harness scores these as no_inputs
            continue
        checked += 1
        gen = set(x.offers[x.offers.direction == "GEN"].duid)
        _, why = build(x, gen)
        table = data.constraint_table(x, gen)
        mine = Counter(table.status)
        assert {k: v for k, v in why.items() if k != "unit terms dropped (no energy offer)"} == dict(mine)
        assert table.unit_terms_dropped.sum() == why.get("unit terms dropped (no energy offer)", 0)
    assert checked


def test_stack_caps_nest_and_lines_are_the_rungs_quantities(warehouse):
    s = data.stack(warehouse, T[0], None)
    b = pd.DataFrame(s["bands"])
    assert (b.available <= b.declared + 1e-9).all() and (b.ramped <= b.available + 1e-9).all()
    assert list(b.price) == sorted(b.price)
    x = Window(warehouse, T[0], T[0], frozenset({"demand", "scheduled_load"})).at(T[0])
    assert s["lines"]["demand"] == pytest.approx(x.demand.totaldemand.sum() + x.scheduled_load.dispatchableload.sum())
    sa = data.stack(warehouse, T[0], "SA1")
    assert set(pd.DataFrame(sa["bands"]).region) <= {"SA1"} and "local" in sa["lines"]


def test_price_cache_round_trip(warehouse, tmp_path, monkeypatch):
    monkeypatch.setattr(data, "months", lambda: ("2030-01",))
    for key in ("r2", "r4"):
        evaluate(warehouse, LADDER[key], T).to_csv(tmp_path / f"2030-01__{key}__rrp.csv", index=False)
    p = data.Prices(data.build_price_cache(tmp_path))
    assert p.rungs_by_month() == {"2030-01": ["r2", "r4"]}
    rows = data.interval_rows(p.interval(T[0]))
    assert {r["region"] for r in rows} == {"NSW1", "QLD1", "SA1", "TAS1", "VIC1"}
    r4 = evaluate(warehouse, LADDER["r4"], T)
    want = r4[(r4.interval == T[0]) & (r4.region == "SA1")].price.iloc[0]
    assert next(r for r in rows if r["region"] == "SA1")["rungs"]["r4"]["price"] == pytest.approx(want)
    worst = p.worst_interval("2030-01-01")
    assert worst is not None and worst[0] in T


def test_service_routes_exist():
    pytest.importorskip("fastapi")
    from nemsim.web.app import app
    paths = {r.path for r in app.routes}
    assert {"/", "/api/meta", "/api/interval", "/api/stack", "/api/day"} <= paths


def test_live_resolve_reproduces_the_scored_prices(warehouse):
    """The page re-solves rungs 3b and 4 for one interval to show their flows. Fed by a one-interval window,
    the re-solve must give the prices the harness scored with its day window."""
    stored = {}
    for key in data.LIVE_RUNGS:
        r = evaluate(warehouse, LADDER[key], T)
        stored.update({(key, row.region): row.price for row in r[r.interval == T[0]].itertuples() if row.price == row.price})
    live = data.resolve(warehouse, T[0], stored)
    for key in data.LIVE_RUNGS:
        assert live[key]["available"] and live[key]["differs"] == []
        assert live[key]["flows"] and set(live[key]["flows"]) <= set(INTERCONNECTORS)


def test_live_resolve_flags_a_price_that_differs_from_the_scored_one(warehouse):
    r = evaluate(warehouse, LADDER["r4"], T)
    row = r[(r.interval == T[0]) & r.price.notna()].iloc[0]
    live = data.resolve(warehouse, T[0], {("r4", row.region): row.price + 1.0})
    assert live["r4"]["differs"] == [[row.region, round(row.price + 1.0, 2), round(row.price, 2)]]
