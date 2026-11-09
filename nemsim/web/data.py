"""What the frontend shows, read from what already exists. No rung is solved here.

Two sources, both read-only:

- **Scored prices**, from the per-month result files every rung already wrote
  (results/<month>__<rung>__rrp.csv). They are gathered once into results/web/prices.parquet, rebuilt
  whenever a result file is newer, and queried with an in-memory DuckDB.
- **Per-interval detail**, from the warehouse, opened with `read_only=True` so DuckDB itself refuses
  any write: offers for the supply stack, invoked constraints, demand and actual flows.

Modelled interconnector flows are not in the result files: the harness keeps only regional prices. So
for the interval on screen, rungs 3b and 4 are re-solved (about 0.1-0.3 s) to show the flows they chose;
the prices on the page stay the scored ones, and any re-solved price that differs from its scored price
is flagged (a degenerate LP price). nemsim/web/detail.py does the same for whole days as a batch check.

Times are AEMO's SETTLEMENTDATE: the end of the 5-minute interval, in NEM time (AEST, UTC+10, no
daylight saving). Prices $/MWh, volumes MW.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from nemsim.config import ROOT, path
from nemsim.inputs import INTERCONNECTORS, REGIONS, Window
from nemsim.intervals import MONTHS, SPIKE_MONTHS
from nemsim.rungs import LADDER
from nemsim.rungs.r3b_flows_solved import unit_limits
from nemsim.rungs.r4_constraints import classify
from nemsim.stack import cap_bands

RUNGS = ("r1", "r2", "r3a", "r3b", "r4")
RUNG_NAMES = {
    "r1": "1 · merit order, declared availability",
    "r2": "2 · merit order, AEMO availability",
    "r3a": "3a · regional, flows held at actual",
    "r3b": "3b · regional, flows solved",
    "r4": "4 · network constraints",
}
WEB = ROOT / "results" / "web"
STEP = timedelta(minutes=5)

# The days the project has written about. The landing interval is the one with the largest rung 4
# error on that day, in any region.
EPISODES = [
    ("2020-01-31", "31 Jan 2020 · SA separation"),
    ("2025-06-12", "12 Jun 2025 · cold snap"),
    ("2025-06-26", "26 Jun 2025 · cold snap"),
    ("2025-01-22", "22 Jan 2025 · record QLD demand"),
    ("2024-08-05", "5 Aug 2024 · winter, links at limits"),
]


def warehouse(db: Path | None = None) -> duckdb.DuckDBPyConnection:
    """The warehouse, read-only: enforced by the connection, not by discipline."""
    return duckdb.connect(str(db or path("warehouse")), read_only=True)


def months() -> tuple[str, ...]:
    return tuple(sorted(MONTHS + SPIKE_MONTHS))


# ---------------------------------------------------------------------------------------------
# Scored prices

def result_files(results: Path) -> list[tuple[str, str, Path]]:
    return [(m, r, results / f"{m}__{r}__rrp.csv") for m in months() for r in RUNGS
            if (results / f"{m}__{r}__rrp.csv").exists()]


def build_price_cache(results: Path = ROOT / "results", out: Path | None = None, force: bool = False) -> Path:
    """Gather every scored result file into one long table: one row per region-interval and rung."""
    out = out or results / "web" / "prices.parquet"
    files = result_files(results)
    if not files:
        raise FileNotFoundError(f"no scored results in {results}; run python -m nemsim.evaluate first")
    newest = max(f.stat().st_mtime for *_, f in files)
    if out.exists() and not force and out.stat().st_mtime >= newest:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    parts = []
    for m, r, f in files:
        parts.append(f"""select '{m}' as month, '{r}' as rung, cast(interval as timestamp) as interval,
                            region, rrp, totaldemand, cast(price as double) as price,
                            cast(status as varchar) as status, cast(excluded as varchar) as excluded,
                            cast(suspended as boolean) as suspended, cast(apc_flag as boolean) as apc_flag,
                            cast(price_overridden as boolean) as price_overridden,
                            cast(intervention_run as boolean) as intervention_run, price_class
                     from read_csv('{f}', header = true, all_varchar = false, sample_size = -1)""")
    tmp = out.with_suffix(".tmp.parquet")
    con.execute(f"copy ({' union all '.join(parts)}) to '{tmp}' (format parquet)")
    tmp.replace(out)
    return out


class Prices:
    """Scored prices, queried from the parquet cache in an in-memory DuckDB."""

    def __init__(self, parquet: Path):
        self.con = duckdb.connect()
        self.con.execute(f"create view p as select * from read_parquet('{parquet}')")
        self.lock = threading.Lock()

    def q(self, sql: str, params=()) -> pd.DataFrame:
        with self.lock:
            return self.con.execute(sql, list(params)).df()

    def rungs_by_month(self) -> dict[str, list[str]]:
        df = self.q("select month, list(distinct rung order by rung) rungs from p group by 1 order by 1")
        return {m: [r for r in RUNGS if r in rs] for m, rs in zip(df.month, df.rungs)}

    def interval(self, t: datetime) -> pd.DataFrame:
        return self.q("select * from p where interval = ? order by region, rung", [t])

    def day(self, day: str, region: str) -> pd.DataFrame:
        start = datetime.fromisoformat(day) + STEP
        return self.q("""select interval, rung, rrp, price, excluded from p
                         where region = ? and interval between ? and ? order by interval, rung""",
                      [region, start, start + timedelta(days=1) - STEP])

    def worst_interval(self, day: str, rung: str = "r4") -> tuple[datetime, str, float] | None:
        start = datetime.fromisoformat(day) + STEP
        # Skips administered or overridden prices: there AEMO's published price is not what the offers
        # produced, so the error says nothing about the model.
        df = self.q("""select interval, region, abs(price - rrp) err from p
                       where rung = ? and excluded is null and price is not null
                         and not coalesce(apc_flag, false) and not coalesce(price_overridden, false)
                         and interval between ? and ? order by err desc, interval limit 1""",
                    [rung, start, start + timedelta(days=1) - STEP])
        return None if df.empty else (df.interval[0].to_pydatetime(), df.region[0], float(df.err[0]))


def interval_rows(prices: pd.DataFrame) -> list[dict]:
    """Per region: actual price, flags, and each rung's price and error."""
    out = []
    for region in REGIONS:
        g = prices[prices.region == region]
        if g.empty:
            continue
        first = g.iloc[0]
        rungs = {}
        for r in g.itertuples():
            scored = r.excluded is None or (isinstance(r.excluded, float) and np.isnan(r.excluded))
            price = None if not scored or pd.isna(r.price) else float(r.price)
            rungs[r.rung] = {"price": price, "error": None if price is None else price - float(r.rrp),
                             "status": None if pd.isna(r.status) else r.status,
                             "excluded": None if scored else r.excluded}
        out.append({"region": region, "rrp": float(first.rrp), "totaldemand": _f(first.totaldemand),
                    "price_class": first.price_class, "rungs": rungs,
                    "flags": {k: bool(first[k]) for k in ("suspended", "apc_flag", "price_overridden",
                                                          "intervention_run") if not pd.isna(first[k])}})
    return out


def _f(v) -> float | None:
    return None if v is None or pd.isna(v) else float(v)


# ---------------------------------------------------------------------------------------------
# Per-interval detail from the warehouse

def regional(con, t: datetime) -> pd.DataFrame:
    return con.execute("""select regionid region, totaldemand, dispatchableload, netinterchange
                          from raw.dispatchregionsum where settlementdate = ? and intervention = 0
                          order by 1""", [t]).df()


def actual_flows(con, t: datetime) -> pd.DataFrame:
    """AEMO's dispatch outcome for each link: flow and the limits the dispatch run calculated."""
    return con.execute("""select interconnectorid link, mwflow flow, exportlimit, importlimit, marginalvalue
                          from raw.dispatchinterconnectorres where settlementdate = ? and intervention = 0
                          order by 1""", [t]).df()


def constraint_table(x, gen_units: set[str]) -> pd.DataFrame:
    """Every invoked constraint and what rung 4 does with it, with the same rules as
    nemsim.rungs.r4_constraints.build (tests/test_web.py checks the counts agree)."""
    terms = x.constraint_terms
    grouped = {k: g for k, g in terms.groupby(["constraintid", "eff", "ver"])} if terms is not None else {}
    rows = []
    for c in x.constraints.itertuples():
        if c.sense is None or pd.isna(c.sense):
            status, dropped = "no definition", 0
        else:
            st, units, links = classify(grouped.get((c.constraintid, c.eff, c.ver), terms.iloc[0:0]))
            kept = {u for u in units if u in gen_units}
            dropped = len(units) - len(kept) if st == "ok" else 0
            status = st if st != "ok" else ("no representable terms" if not kept and not links else "applied")
        rows.append({"constraintid": c.constraintid, "sense": None if pd.isna(c.sense) else str(c.sense).strip(),
                     "rhs": float(c.rhs), "status": status, "unit_terms_dropped": dropped})
    return pd.DataFrame(rows, columns=["constraintid", "sense", "rhs", "status", "unit_terms_dropped"])


def constraints(con, t: datetime) -> dict:
    x = Window(con, t, t, frozenset({"constraints", "offers"})).at(t)
    gen = x.offers[x.offers.direction == "GEN"] if x.offers is not None else pd.DataFrame(columns=["duid"])
    if x.constraints is None:
        return {"rows": [], "counts": {}, "binding_counts": {}}
    table = constraint_table(x, set(gen.duid))
    # AEMO's outcome for each constraint, shown beside rung 4's treatment. Never an input to any rung.
    aemo = con.execute("""select constraintid, marginalvalue, lhs, violationdegree from raw.dispatchconstraint
                          where settlementdate = ? and intervention = 0""", [t]).df()
    table = table.merge(aemo, how="left", on="constraintid")
    table["binding"] = table.marginalvalue.fillna(0) != 0
    table = table.sort_values(["binding", "status"], ascending=[False, True])
    return {"rows": table.replace({np.nan: None}).to_dict("records"),
            "counts": table.status.value_counts().to_dict(),
            "binding_counts": table[table.binding].status.value_counts().to_dict()}


def stack(con, t: datetime, region: str | None) -> dict:
    """Energy offers for one region (or the whole NEM), each band at its loss-adjusted price, with the MW
    each rung could use: rung 1 caps at MAXAVAIL, rungs 2 and 3a at AEMO's dispatch availability, and
    rungs 3b and 4 additionally at ramp limits from initial MW."""
    x = Window(con, t, t, frozenset({"offers", "units", "demand", "scheduled_load", "regional_dispatch"})).at(t)
    if x.offers is None or x.units is None:
        raise LookupError("no offers loaded for this interval: a month's first 48 intervals sit in the previous "
                          "month's bid file, which is loaded only where that month is in the sample")
    gen = x.offers[x.offers.direction == "GEN"].reset_index(drop=True)
    duid, band, mw = gen.duid.to_numpy(), gen.band.to_numpy(), gen.mw.to_numpy()
    avail = gen.duid.map(x.units.set_index("duid").availability).fillna(0).to_numpy(float)
    declared = cap_bands(duid, band, mw, gen.maxavail.to_numpy(float))
    available = cap_bands(duid, band, mw, np.minimum(gen.maxavail.to_numpy(float), avail))
    ramped, _ = unit_limits(gen, x.units, ramps=True)
    keep = np.ones(len(gen), bool) if region is None else (gen.region == region).to_numpy()
    bands = pd.DataFrame({"duid": duid, "band": band, "price": gen.price.to_numpy(), "region": gen.region,
                          "declared": declared, "available": available, "ramped": ramped})[keep]
    bands = bands[(bands.declared > 1e-6) | (bands.available > 1e-6)].sort_values(["price", "duid", "band"])
    d = x.demand.set_index("region").totaldemand
    sl = x.scheduled_load.set_index("region").dispatchableload
    ni = x.regional_dispatch.set_index("region").netinterchange
    if region is None:
        lines = {"demand": float(d.sum() + sl.sum())}
    else:
        lines = {"demand": float(d[region] + sl[region]), "local": float(d[region] + sl[region] + ni[region])}
    return {"bands": bands.round(4).to_dict("records"), "lines": lines,
            "price_floor": float(x.price_floor), "price_cap": float(x.price_cap)}


LIVE_RUNGS = ("r3b", "r4")
TOLERANCE = 0.01


def resolve(con, t: datetime, stored: dict[tuple[str, str], float]) -> dict:
    """Re-solve rungs 3b and 4 for one interval, to show the flows they chose, which the harness does not
    store. The rung classes are the scored ones, unchanged, fed by the same Window loader the harness
    uses (here for one interval; the harness loads a day at a time, and the inputs for an interval are the
    same either way). The page's prices stay the scored ones. Each re-solved price is compared with the
    scored price for the same region: where they differ the LP's price is degenerate there (a range of
    prices satisfies it, and the solver's pick depends on its path), and the page says so. About 0.1-0.3 s."""
    out = {}
    for key in LIVE_RUNGS:
        rung = type(LADDER[key])()
        w = Window(con, t, t, rung.uses)
        if pd.Timestamp(t) not in set.intersection(*(w.intervals_with(u) for u in rung.uses)):
            out[key] = {"available": False}        # the harness scores these as no_inputs too
            continue
        x = w.at(t)
        tic = time.perf_counter()
        res = rung.solve(x)
        ms = (time.perf_counter() - tic) * 1e3
        prices = dict(zip(res.region, res.price.astype(float)))
        differs = [[r, round(stored[(key, r)], 2), round(p, 2)] for r, p in prices.items()
                   if (key, r) in stored and abs(p - stored[(key, r)]) > TOLERANCE]
        out[key] = {"available": True, "solve_ms": round(ms, 1), "prices": prices, "differs": differs,
                    "flows": dict(zip(rung.last["links"], (float(f) for f in rung.last["flows"]))),
                    "violated": list(rung.last.get("violated") or [])}
    return out


def flows(con, t: datetime, stored: dict[tuple[str, str], float]) -> dict:
    a = actual_flows(con, t)
    live = resolve(con, t, stored)
    rows = []
    for link in a.link:
        row = a[a.link == link].iloc[0].to_dict()
        frm, to = INTERCONNECTORS.get(link, ("?", "?"))
        row.update({"from": frm, "to": to})
        for r, v in live.items():
            if v.get("available"):
                row[r] = v["flows"].get(link)
        rows.append(row)
    return {"rows": pd.DataFrame(rows).replace({np.nan: None}).to_dict("records"),
            "live": {r: {k: v[k] for k in v if k != "flows"} for r, v in live.items()}}
