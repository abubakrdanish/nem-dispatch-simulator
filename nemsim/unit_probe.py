"""The unit channel of FCAS: python -m nemsim.unit_probe run | report

**A probe, never a rung.** It holds units' energy at what AEMO actually dispatched (DISPATCHLOAD.TOTALCLEARED,
an outcome) to measure how much of rung 4's error on expensive intervals comes from energy and FCAS
competing for the same unit's capacity, the mechanism the AER names for Tasmania in June 2025. The link
channel was measured by nemsim/link_sweep.py; this is the other half.

Population: the expensive misses of rung 4 *with fixed bids applied* (nemsim/fixed_bids.py): region-intervals
in the seventeen months, on the rows all five rungs scored, with DISPATCHPRICE.RRP (pricing run) at $300 or
more and rung 4's corrected price more than $5 away. Every probe re-solves rung 4 with fixed bids applied,
on every interval holding such a miss, and is scored on all five regions of each interval.

- **U1, as asked:** every unit with any FCAS enabled in AEMO's dispatch that interval (any RAISE* or LOWER*
  column above zero) is held at its actual energy. This pins most scheduled units, so it comes close to
  handing the model AEMO's dispatch: a loose upper bound.
- **U2, FCAS-crowded units only:** a unit whose actual energy plus its enabled raise FCAS (regulation plus
  the largest contingency raise service) reaches its dispatch availability (within 1 MW). For these units
  the FCAS they were providing used up headroom that energy would otherwise have had.
- **U3, U2 plus the FCAS-limited links:** U2, with every link that AEMO held at an import limit set by an
  FCAS constraint into an expensive-miss region also held at AEMO's flow (as in link_sweep's probe_fcas).
  The combined upper bound for an FCAS co-optimisation rung.

A unit is held by setting its availability and initial MW to TOTALCLEARED and its ramp rates to zero, so
rung 4's own limits pin it there (or as close as its offers allow). Intervals whose re-solve does not
reproduce the corrected scored prices to the cent (a degenerate LP price) are set aside.
Writes results/unit_probe.csv.
"""
from __future__ import annotations

import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta

import duckdb
import numpy as np
import pandas as pd

from nemsim.config import ROOT, path
from nemsim.inputs import INTERCONNECTORS, REGIONS, Window
from nemsim.ladder import SAMPLES, frame
from nemsim.link_sweep import TOL_MW, setters
from nemsim.rungs import LADDER

OUT = ROOT / "results" / "unit_probe.csv"
STEP = timedelta(minutes=5)
SPIKE = "d. $300+"
PRICE_TOL = 0.01
SUFFIX = "__fixedbids"
PROBES = ("u1", "u2", "u3")


def targets() -> pd.DataFrame:
    f, _ = frame(SAMPLES["seventeen"], SUFFIX)
    r4 = f["r4"][["interval", "region", "rrp", "price", "abs_error", "price_class", "month", "weight"]]
    miss = r4[(r4.price_class == SPIKE) & (r4.abs_error > 5)]
    rows = r4[r4.interval.isin(miss.interval.unique())].copy()
    rows["expensive_miss"] = (rows.price_class == SPIKE) & (rows.abs_error > 5)
    return rows


def _day(args) -> list[dict]:
    day, todo = args
    from nemsim import inputs
    inputs.FIXED_BIDS = True
    R = type(LADDER["r4"])
    con = duckdb.connect(str(path("warehouse")), read_only=True)
    t0 = datetime.fromisoformat(day) + STEP
    t1 = t0 + timedelta(days=1) - STEP
    w = Window(con, t0, t1, R.uses)
    have = set.intersection(*(w.intervals_with(u) for u in R.uses))
    dl = con.execute("""select settlementdate t, duid, totalcleared tc, availability av,
                               greatest(coalesce(raisereg,0), 0) + greatest(coalesce(raise1sec,0), coalesce(raise6sec,0),
                                        coalesce(raise60sec,0), coalesce(raise5min,0)) raise_mw,
                               coalesce(raisereg,0) + coalesce(raise1sec,0) + coalesce(raise6sec,0) + coalesce(raise60sec,0)
                               + coalesce(raise5min,0) + coalesce(lowerreg,0) + coalesce(lower1sec,0) + coalesce(lower6sec,0)
                               + coalesce(lower60sec,0) + coalesce(lower5min,0) fcas_total
                        from raw.dispatchload where intervention = 0 and settlementdate between ? and ?""", [t0, t1]).df()
    dl = {t: g for t, g in dl.groupby("t")}
    out = []

    def solve(t, pin: pd.DataFrame | None, hold: dict | None):
        x = w.at(t)
        if pin is not None and len(pin):
            u = x.units.copy().set_index("duid")
            p = pin.set_index("duid").tc.reindex(u.index).dropna()
            u.loc[p.index, "availability"] = p
            u.loc[p.index, "initialmw"] = p
            u.loc[p.index, ["rampuprate", "rampdownrate"]] = 0.0
            x.units = u.reset_index()
        if hold:
            ra = x.interconnector_ratings.copy()
            for link, flow in hold.items():
                ra.loc[ra.interconnectorid == link, ["import_rating", "export_rating"]] = [-flow, flow]
            x.interconnector_ratings = ra
        res = R().solve(x)
        return dict(zip(res.region, res.price.astype(float)))

    for t, job in todo.items():
        t = pd.Timestamp(t).to_pydatetime()
        if pd.Timestamp(t) not in have:
            continue
        base = solve(t, None, None)
        degenerate = any(abs(base[r] - s) > PRICE_TOL for r, s in job["stored"].items())
        g = dl.get(pd.Timestamp(t), pd.DataFrame(columns=["duid", "tc", "av", "raise_mw", "fcas_total"]))
        u1 = g[g.fcas_total > 0]
        u2 = g[(g.raise_mw > 0) & (g.tc + g.raise_mw >= g.av - TOL_MW)]
        p1, p2 = solve(t, u1, None), solve(t, u2, None)
        p3 = solve(t, u2, job["hold"]) if job["hold"] else p2
        for region in REGIONS:
            out.append({"interval": t, "region": region, "resolved": base[region], "u1": p1[region], "u2": p2[region],
                        "u3": p3[region], "degenerate": degenerate, "units_u1": len(u1), "units_u2": len(u2)})
    con.close()
    return out


def run(workers: int = 4) -> None:
    tic = time.monotonic()
    rows = targets()
    intervals = sorted(rows.interval.unique())
    with duckdb.connect(str(path("warehouse")), read_only=True) as con:
        ic, cls = setters(con, [pd.Timestamp(t).to_pydatetime() for t in intervals])
    kind = {(r.interval, r.gid): r.kind for r in cls.itertuples()}
    miss = rows[rows.expensive_miss][["interval", "region"]]
    holds = {}
    for r in ic.itertuples():
        a, b = INTERCONNECTORS[r.link]
        for region, bound, gid in ((b, r.exportlimit, r.exportgenconid), (a, r.importlimit, r.importgenconid)):
            if abs(r.flow - bound) <= TOL_MW and gid and kind.get((r.interval, gid)) == "FCAS":
                holds.setdefault(r.interval, {}).setdefault(region, {})[r.link] = r.flow
    miss_regions = {t: set(g.region) for t, g in miss.groupby("interval")}
    jobs = {}
    for t in intervals:
        hold = {}
        for region, links in holds.get(t, {}).items():
            if region in miss_regions.get(t, set()):
                hold.update(links)
        d = (pd.Timestamp(t) - STEP).strftime("%Y-%m-%d")
        jobs.setdefault(d, {})[str(t)] = {"stored": dict(zip(*rows[rows.interval == t][["region", "price"]].T.values)),
                                          "hold": hold}
    print(f"{len(miss)} expensive misses, {len(intervals)} intervals, {len(jobs)} days", flush=True)
    solved = []
    with ProcessPoolExecutor(workers) as ex:
        for i, part in enumerate(ex.map(_day, sorted(jobs.items()))):
            solved += part
            if i % 25 == 0:
                print(f"  {i + 1}/{len(jobs)} days, {time.monotonic() - tic:.0f} s", flush=True)
    out = rows.merge(pd.DataFrame(solved), on=["interval", "region"], how="left")
    out.to_csv(OUT, index=False)
    print(f"wrote {OUT} ({len(out)} rows) in {time.monotonic() - tic:.0f} s")


def counts(o: pd.DataFrame, col: str) -> dict:
    o = o[o[col].notna()]
    was, now = o.abs_error <= 5, (o[col] - o.rrp).abs() <= 5
    return {"n": len(o), "fixed": int((~was & now).sum()), "still_wrong": int((~was & ~now).sum()),
            "newly_broken": int((was & ~now).sum())}


def report() -> None:
    from nemsim.spike_sample import boot
    out = pd.read_csv(OUT, parse_dates=["interval"], low_memory=False)
    nd = out[~out.degenerate.astype(bool)]
    print("degenerate share of probed intervals:", round(out.drop_duplicates("interval").degenerate.mean() * 100, 1), "%")
    print("units held per interval, median: U1", nd.drop_duplicates("interval").units_u1.median(),
          " U2", nd.drop_duplicates("interval").units_u2.median())
    for name, sel in (("all", nd.region.notna()), ("Tasmania", nd.region == "TAS1")):
        for col in PROBES:
            print(f"{name:9s} {col}  expensive misses:", counts(nd[sel & nd.expensive_miss], col),
                  " all $300+:", counts(nd[sel & (nd.price_class == SPIKE)], col),
                  " all region-intervals:", counts(nd[sel], col))
    f, _ = frame(SAMPLES["seventeen"], SUFFIX)
    r4 = f["r4"]
    w = r4.weight.abs().to_numpy()
    for col in PROBES:
        x = r4.merge(nd[["interval", "region", col]], how="left", on=["interval", "region"])
        newp = np.where(x[col].notna(), x[col], x.price)
        hit0, hit1 = (x.abs_error <= 5).to_numpy(), np.abs(newp - x.rrp) <= 5
        sp = (x.price_class == SPIKE).to_numpy()
        g = x[sp].assign(hit=hit1[sp])
        lo, hi = boot(g, "hit")
        tas = (x.region == "TAS1").to_numpy() & sp
        print(f"{col}: $300+ within $5 {hit0[sp].mean()*100:.1f} -> {hit1[sp].mean()*100:.1f} ({lo:.1f}-{hi:.1f});"
              f" by value {(w*hit0).sum()/w.sum()*100:.1f} -> {(w*hit1).sum()/w.sum()*100:.1f};"
              f" all {hit0.mean()*100:.1f} -> {hit1.mean()*100:.1f}; Tasmania $300+ {hit0[tas].mean()*100:.1f} -> {hit1[tas].mean()*100:.1f}")


if __name__ == "__main__":
    {"run": run, "report": report}[sys.argv[1] if len(sys.argv) > 1 else "run"]()
