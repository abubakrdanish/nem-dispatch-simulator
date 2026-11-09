"""The local frontend: python -m nemsim.web   (then open http://127.0.0.1:8765)

A small FastAPI service over the scored results and the warehouse, both read-only, and one static
page. Every price shown is the scored one; only the flows of rungs 3b and 4 for the interval on screen
are re-solved, with any differing price flagged. Needs the optional extras: pip install -e ".[frontend]".
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from nemsim.inputs import REGIONS
from nemsim.web import data

STATIC = Path(__file__).parent / "static"
state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["prices"] = data.Prices(data.build_price_cache())
    state["db"] = data.warehouse()
    episodes = []
    for day, label in data.EPISODES:
        w = state["prices"].worst_interval(day)
        if w:
            episodes.append({"day": day, "label": label, "interval": f"{w[0]:%Y-%m-%dT%H:%M}", "region": w[1],
                             "rung4_abs_error": round(w[2], 2)})
    state["episodes"] = episodes
    yield
    state["db"].close()


app = FastAPI(title="NEM dispatch simulator", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _t(t: str) -> datetime:
    try:
        v = datetime.fromisoformat(t)
    except ValueError:
        raise HTTPException(400, "t must be an ISO datetime, interval ending, e.g. 2020-01-31T15:00")
    if v.minute % 5 or v.second:
        raise HTTPException(400, "intervals end on a 5-minute boundary")
    return v


def _db():
    return state["db"].cursor()     # one cursor per request; the connection itself is read-only


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/meta")
def meta():
    return {"regions": list(REGIONS), "rungs": list(data.RUNGS), "rung_names": data.RUNG_NAMES,
            "rungs_by_month": state["prices"].rungs_by_month(), "episodes": state["episodes"],
            "time_basis": "interval ending, NEM time (AEST, UTC+10)"}


@app.get("/api/interval")
def interval(t: str = Query(...)):
    when = _t(t)
    prices = state["prices"].interval(when)
    if prices.empty:
        raise HTTPException(404, f"no scored results for {when:%Y-%m-%d %H:%M}; loaded months: "
                                 + ", ".join(data.months()))
    con = _db()
    reg = data.regional(con, when).set_index("region")
    rows = data.interval_rows(prices)
    stored = {(r.rung, r.region): float(r.price) for r in prices.itertuples()
              if (r.excluded is None or r.excluded != r.excluded) and r.price == r.price}
    for r in rows:
        if r["region"] in reg.index:
            r.update({k: float(v) for k, v in reg.loc[r["region"]].items()})
    return {"interval": f"{when:%Y-%m-%dT%H:%M}", "regions": rows,
            "flows": data.flows(con, when, stored), "constraints": data.constraints(con, when)}


@app.get("/api/stack")
def stack(t: str = Query(...), region: str = Query("NEM")):
    if region != "NEM" and region not in REGIONS:
        raise HTTPException(400, f"region is NEM or one of {list(REGIONS)}")
    try:
        return data.stack(_db(), _t(t), None if region == "NEM" else region)
    except LookupError as e:
        raise HTTPException(404, str(e))


@app.get("/api/day")
def day(date: str = Query(...), region: str = Query("VIC1")):
    if region not in REGIONS:
        raise HTTPException(400, f"region is one of {list(REGIONS)}")
    df = state["prices"].day(date, region)
    if df.empty:
        raise HTTPException(404, f"no scored results on {date}")
    out = {}
    for t, g in df.groupby("interval"):
        out[f"{t:%Y-%m-%dT%H:%M}"] = {"rrp": float(g.rrp.iloc[0]),
                                      **{r: (None if (e is not None and e == e) or p != p else float(p))
                                         for r, p, e in zip(g.rung, g.price, g.excluded)}}
    return {"date": date, "region": region, "intervals": out}
