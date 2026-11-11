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


def test_error_classes():
    assert data.error_class(43.0, 40.0) == "within $5"
    assert data.error_class(108.0, 100.0) == "within 10%"
    assert data.error_class(70.0, 100.0) == "off"
    assert data.error_class(13.53, 14699.0) == "order of magnitude"
    assert data.error_class(None, 40.0) is None


def _region(name, rrp, r4, **flags):
    return {"region": name, "rrp": rrp, "flags": flags,
            "rungs": {"r4": {"price": r4, "excluded": None if r4 is not None else "no_inputs"}}}


def test_summary_names_the_limit_aemo_held_that_rung_4_left_out():
    regions = [_region("VIC1", 14699.0, 13.53), _region("TAS1", 12.0, 12.0)]
    flows = {"rows": [{"link": "T-V-MNSP1", "from": "TAS1", "to": "VIC1", "flow": 445.0, "r4": 560.4,
                       "exportlimit": 444.99, "importlimit": 245.0,
                       "exportgenconid": "F_QNV++MG_R6", "importgenconid": "VTBL_ROC"}]}
    cons = {"rows": [{"constraintid": "F_QNV++MG_R6", "status": "region terms", "binding": True, "marginalvalue": 693.8}]}
    s = data.summary(regions, flows, cons, "VIC1")
    assert s.startswith("AEMO cleared Victoria at $14,699. Rung 4 said $13.53.")
    assert "560 MW over Basslink" in s and "445 MW" in s and "F_QNV++MG_R6" in s and "because" not in s


def test_summary_says_when_nothing_is_happening_and_when_prices_were_administered():
    quiet = [_region(r, 80.0, 90.0) for r in ("NSW1", "QLD1", "SA1", "TAS1", "VIC1")]
    assert "Nothing much happening here" in data.summary(quiet, {"rows": []}, {"rows": []}, "NSW1")
    apc = [_region("SA1", 300.0, 5000.0, apc_flag=True), _region("VIC1", 80.0, 80.0)]
    assert "administered" in data.summary(apc, {"rows": []}, {"rows": []}, "SA1")


def test_link_sweep_reads_rung_4_flows_for_hyphenated_links():
    """Link names contain hyphens; the sweep must still find rung 4's flow on each (a bug it once had)."""
    from nemsim.link_sweep import ORDER, classify_misses
    t = pd.Timestamp("2020-01-31 18:45")
    out = pd.DataFrame([{"interval": t, "region": "VIC1", "expensive_miss": True, "degenerate": False,
                         "r4flow_T-V-MNSP1": 560.0, "r4flow_V-SA": 0.0}])
    lim = pd.DataFrame([{"interval": t, "region": "VIC1", "link": "T-V-MNSP1", "aemo_flow": 445.0, "into_sign": 1,
                         "setter": "F_QNV++MG_R6", "setter_kind": "FCAS"}])
    m = classify_misses(out, lim)
    assert m.cause.iloc[0] == ORDER[1] and m.links.iloc[0] == "T-V-MNSP1" and m.over_mw.iloc[0] == pytest.approx(115)
