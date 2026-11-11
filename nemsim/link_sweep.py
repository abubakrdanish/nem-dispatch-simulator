"""How much of rung 4's failure on expensive intervals is interconnectors flowing past limits that FCAS
constraints were holding? python -m nemsim.link_sweep run | report

**A diagnostic with a probe in it, never a rung.** It reads AEMO's dispatch outcomes (flows, calculated
interconnector limits, the constraint AEMO records as setting each limit, marginal values) to explain
rung 4's misses. Nothing here feeds back into any rung.

Population: region-intervals in the seventeen months with AEMO's dispatch price (DISPATCHPRICE.RRP,
pricing run) at $300 or more, on the rows all five rungs scored (nemsim/ladder.py), where rung 4's
stored, scored price is more than $5 away: an **expensive miss**.

For each expensive miss in region R, a link touching R is **import-limited into R** in AEMO's dispatch
when its flow sits within 1 MW of the bound that stops more power reaching R: EXPORTLIMIT if R is the
link's second region (flow positive towards R), IMPORTLIMIT if R is its first (AEMO's IMPORTLIMIT is a
signed lower bound on the flow). The limit's setter is AEMO's EXPORTGENCONID or IMPORTGENCONID,
classified from the definition tables of the version invoked that interval:

- **FCAS**: the equation has region terms (every region term is an FCAS bid type) or a unit term with a
  non-ENERGY bid type. Rung 4 leaves these out; an FCAS co-optimisation rung would add them.
- **energy**: only energy unit terms and link terms. Rung 4 applies these.
- **no definition**: its version is not in the definition tables (three 2008 versions AEMO's archive does
  not hold). No rung built this way can represent it.
- **own limit**: DEFAULT, the link's own rating, or for an MNSP (Basslink, Murraylink, Terranora) its
  offered availability, which no rung here models.
- **other**: anything else (blank, or a setter that was not invoked).

Rung 4 **over-flows** the link when, re-solved for that interval exactly as scored, it carries more than
5 MW more towards R than AEMO did. The re-solve must reproduce the scored prices of that interval to
the cent; where it does not (a degenerate LP price) the interval is set aside as degenerate, so no flow
difference is counted that the scored price did not come from.

Two probes, both using AEMO's flows, an outcome, so both measure the size of the prize, not a rung:
- **probe_all**: every interconnector held at AEMO's flow;
- **probe_fcas**: only the links that were import-limited by an FCAS constraint into some expensive-miss
  region that interval, held at AEMO's flow.
Each is scored on all five regions of every probed interval: fixed (miss becomes within $5), unchanged,
and newly broken (within $5 as scored, more than $5 off under the probe).

Writes results/link_sweep.csv (one row per region-interval of every probed interval).
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
from nemsim.rungs import LADDER

OUT = ROOT / "results" / "link_sweep.csv"
STEP = timedelta(minutes=5)
SPIKE = "d. $300+"
TOL_MW, OVER_MW, PRICE_TOL = 1.0, 5.0, 0.01
MNSP = {"T-V-MNSP1", "V-S-MNSP1", "N-Q-MNSP1"}


def targets() -> pd.DataFrame:
    """Every region-interval of every interval that holds at least one expensive miss, with the stored price."""
    f, _ = frame(SAMPLES["seventeen"])
    r4 = f["r4"][["interval", "region", "rrp", "price", "abs_error", "price_class", "month", "weight"]]
    miss = r4[(r4.price_class == SPIKE) & (r4.abs_error > 5)]
    rows = r4[r4.interval.isin(miss.interval.unique())].copy()
    rows["expensive_miss"] = (rows.price_class == SPIKE) & (rows.abs_error > 5)
    return rows


def setters(con, intervals: list) -> pd.DataFrame:
    """AEMO's flow, limits and limit setters per link, with each setter classified."""
    con.register("_t", pd.DataFrame({"t": intervals}))
    ic = con.execute("""
        select i.settlementdate interval, i.interconnectorid link, i.mwflow flow, i.exportlimit, i.importlimit,
               i.exportgenconid, i.importgenconid
        from raw.dispatchinterconnectorres i join _t on _t.t = i.settlementdate where i.intervention = 0""").df()
    cls = con.execute("""
        with s0 as (select distinct i.settlementdate t, unnest([i.exportgenconid, i.importgenconid]) gid
                    from raw.dispatchinterconnectorres i join _t on _t.t = i.settlementdate where i.intervention = 0),
             s as (select * from s0 where gid is not null),
             v as (select s.t, s.gid, d.genconid_effectivedate eff, d.genconid_versionno ver, d.marginalvalue mv
                   from s left join raw.dispatchconstraint d on d.settlementdate = s.t and d.constraintid = s.gid
                                                          and d.intervention = 0)
        select v.t interval, v.gid, v.mv,
               case when v.gid = 'DEFAULT' then 'own limit'
                    when v.eff is null then 'other'
                    when not exists (select 1 from raw.gencondata g where g.genconid = v.gid and g.effectivedate = v.eff
                                     and g.versionno = v.ver) then 'no definition'
                    when exists (select 1 from raw.spdregionconstraint r where r.genconid = v.gid and r.effectivedate = v.eff
                                 and r.versionno = v.ver)
                      or exists (select 1 from raw.spdconnectionpointconstraint c where c.genconid = v.gid
                                 and c.effectivedate = v.eff and c.versionno = v.ver and c.bidtype <> 'ENERGY') then 'FCAS'
                    else 'energy' end as kind
        from v""").df()
    con.unregister("_t")
    return ic, cls


def _day(args) -> list[dict]:
    """Solve one day's probed intervals: as scored, probe_all, probe_fcas. Run in a worker process."""
    day, todo = args          # todo: {interval: {"stored": {region: price}, "aemo": {link: flow}, "fcas_links": [...]}}
    R = type(LADDER["r4"])
    con = duckdb.connect(str(path("warehouse")), read_only=True)
    t0 = datetime.fromisoformat(day) + STEP
    w = Window(con, t0, t0 + timedelta(days=1) - STEP, R.uses)
    have = set.intersection(*(w.intervals_with(u) for u in R.uses))
    out = []

    def solve(t, hold):
        x = w.at(t)
        if hold:
            ra = x.interconnector_ratings.copy()
            for link, flow in hold.items():           # lower bound -import, upper bound export: both at the flow
                ra.loc[ra.interconnectorid == link, ["import_rating", "export_rating"]] = [-flow, flow]
            x.interconnector_ratings = ra
        r = R()
        res = r.solve(x)
        return dict(zip(res.region, res.price.astype(float))), dict(zip(r.last["links"], map(float, r.last["flows"])))

    for t, job in todo.items():
        t = pd.Timestamp(t).to_pydatetime()
        if pd.Timestamp(t) not in have:
            continue
        p, fl = solve(t, None)
        degenerate = any(abs(p[r] - s) > PRICE_TOL for r, s in job["stored"].items())
        pa, _ = solve(t, job["aemo"])
        pf = solve(t, {k: job["aemo"][k] for k in job["fcas_links"]})[0] if job["fcas_links"] else p
        for region in REGIONS:
            out.append({"interval": t, "region": region, "resolved": p[region], "probe_all": pa[region],
                        "probe_fcas": pf[region], "degenerate": degenerate,
                        **{f"r4flow_{k}": v for k, v in fl.items()}})
    con.close()
    return out


def run(workers: int = 3, only_day: str | None = None) -> None:
    tic = time.monotonic()
    rows = targets()
    if only_day:
        rows = rows[(rows.interval - STEP).dt.strftime('%Y-%m-%d') == only_day]
    intervals = sorted(rows.interval.unique())
    with duckdb.connect(str(path("warehouse")), read_only=True) as con:
        ic, cls = setters(con, [pd.Timestamp(t).to_pydatetime() for t in intervals])
    kind = {(r.interval, r.gid): r.kind for r in cls.itertuples()}
    # Per region-interval: links import-limited into the region, and by what
    lim = []
    for r in ic.itertuples():
        a, b = INTERCONNECTORS[r.link]
        for region, bound, gid in ((b, r.exportlimit, r.exportgenconid), (a, r.importlimit, r.importgenconid)):
            if abs(r.flow - bound) <= TOL_MW:
                lim.append({"interval": r.interval, "region": region, "link": r.link, "aemo_flow": r.flow,
                            "into_sign": 1 if region == b else -1, "setter": gid,
                            "setter_kind": kind.get((r.interval, gid), "other") if gid else "other"})
    lim = pd.DataFrame(lim)
    miss = rows[rows.expensive_miss][["interval", "region"]]
    fcas_lim = lim[lim.setter_kind == "FCAS"].merge(miss)
    aemo = {t: dict(zip(g.link, g.flow)) for t, g in ic.groupby("interval")}
    stored = {t: dict(zip(g.region, g.price)) for t, g in rows.groupby("interval")}
    fcas_links = {t: sorted(set(g.link)) for t, g in fcas_lim.groupby("interval")}
    jobs = {}
    for t in intervals:
        d = (pd.Timestamp(t) - STEP).strftime("%Y-%m-%d")
        jobs.setdefault(d, {})[str(t)] = {"stored": stored[t], "aemo": aemo.get(t, {}), "fcas_links": fcas_links.get(t, [])}
    print(f"{len(rows[rows.expensive_miss])} expensive misses, {len(intervals)} intervals, {len(jobs)} days", flush=True)
    solved = []
    with ProcessPoolExecutor(workers) as ex:
        for i, part in enumerate(ex.map(_day, sorted(jobs.items()))):
            solved += part
            if i % 25 == 0:
                print(f"  {i + 1}/{len(jobs)} days, {time.monotonic() - tic:.0f} s", flush=True)
    s = pd.DataFrame(solved)
    out = rows.merge(s, on=["interval", "region"], how="left")
    suffix = f"__{only_day}" if only_day else ""
    lim.to_csv(ROOT / "results" / f"link_sweep_limits{suffix}.csv", index=False)
    out.to_csv(OUT.with_name(f"link_sweep{suffix}.csv"), index=False)
    print(f"wrote {OUT} ({len(out)} rows) in {time.monotonic() - tic:.0f} s")


ORDER = ["degenerate", "FCAS-limited link, over-flowed", "energy-limited link, over-flowed",
         "own-limit link, over-flowed", "no-definition link, over-flowed", "import-limited, not over-flowed",
         "no import limit"]
EPISODE_DAYS = ["2020-01-31", "2025-06-12", "2025-06-26", "2025-01-22", "2024-08-05"]


def classify_misses(out: pd.DataFrame, lim: pd.DataFrame) -> pd.DataFrame:
    """One cause class per expensive miss, in ORDER's priority, plus the links behind it."""
    m = out[out.expensive_miss].copy()
    j = m[["interval", "region", "degenerate"] + [c for c in m if c.startswith("r4flow_")]].merge(lim, how="left")
    # Column lookup, not itertuples: link names contain hyphens, which itertuples renames away.
    flows = {c[len("r4flow_"):]: j[c].to_numpy() for c in j if c.startswith("r4flow_")}
    j["r4_into"] = [flows[l][i] if isinstance(l, str) and l in flows else np.nan for i, l in enumerate(j.link.to_numpy())]
    j["over_mw"] = (j.r4_into - j.aemo_flow) * j.into_sign
    j["over"] = j.over_mw > OVER_MW
    cause, links, over = [], [], []
    for (t, reg), g in j.groupby(["interval", "region"], sort=False):
        if g.degenerate.iloc[0] is True or g.degenerate.iloc[0] == 1:
            cause.append("degenerate"); links.append(""); over.append(np.nan); continue
        lim_g = g[g.link.notna()]
        hit = None
        for kind, name in (("FCAS", ORDER[1]), ("energy", ORDER[2]), ("own limit", ORDER[3]), ("no definition", ORDER[4])):
            h = lim_g[(lim_g.setter_kind == kind) & lim_g.over]
            if len(h):
                hit = (name, ",".join(sorted(h.link)), h.over_mw.max()); break
        if hit is None:
            hit = (ORDER[5], ",".join(sorted(lim_g.link)), np.nan) if len(lim_g) else (ORDER[6], "", np.nan)
        cause.append(hit[0]); links.append(hit[1]); over.append(hit[2])
    keys = j.groupby(["interval", "region"], sort=False).size().index
    c = pd.DataFrame({"interval": keys.get_level_values(0), "region": keys.get_level_values(1),
                      "cause": cause, "links": links, "over_mw": over})
    return m.merge(c, on=["interval", "region"])


def probe_counts(out: pd.DataFrame, col: str, sel=None) -> dict:
    o = out if sel is None else out[sel]
    o = o[o[col].notna()]
    was = o.abs_error <= 5
    now = (o[col] - o.rrp).abs() <= 5
    return {"region_intervals": len(o), "fixed": int((~was & now).sum()), "still_wrong": int((~was & ~now).sum()),
            "newly_broken": int((was & ~now).sum()), "still_right": int((was & now).sum())}


def report(suffix: str = "") -> None:
    out = pd.read_csv(OUT.with_name(f"link_sweep{suffix}.csv"), parse_dates=["interval"], low_memory=False)
    lim = pd.read_csv(ROOT / "results" / f"link_sweep_limits{suffix}.csv", parse_dates=["interval"])
    m = classify_misses(out, lim)
    m["day"] = (m.interval - STEP).dt.strftime("%Y-%m-%d")
    n = len(m)
    print(f"expensive misses: {n}")
    share = m.cause.value_counts().reindex(ORDER).fillna(0).astype(int)
    print((share.to_frame("misses").assign(pct=lambda d: (d.misses / n * 100).round(1))).to_string())
    fc = m.cause == ORDER[1]
    for by in ("region", "month"):
        g = m.groupby(by).agg(misses=("cause", "size"), fcas=("cause", lambda x: (x == ORDER[1]).sum()))
        g["pct"] = (g.fcas / g.misses * 100).round(1)
        print(g.to_string())
    print("FCAS-limited misses by link:", m[fc].links.str.split(",").explode().value_counts().to_dict())
    ep = m.day.isin(EPISODE_DAYS)
    print("episode days:", int(ep.sum()), "misses,", int((ep & fc).sum()), "FCAS-limited;",
          {d: (int((m.day == d).sum()), int(((m.day == d) & fc).sum())) for d in EPISODE_DAYS})
    print("outside episode days:", int((~ep).sum()), "misses,", int((~ep & fc).sum()), "FCAS-limited")
    print("degenerate share of probed intervals:", round(out.drop_duplicates("interval").degenerate.mean() * 100, 2), "%")
    nd = ~out.degenerate.astype(bool)
    for col in ("probe_all", "probe_fcas"):
        print(col, "all region-intervals of probed intervals:", probe_counts(out, col, nd))
        print(col, "expensive region-intervals ($300+):", probe_counts(out, col, nd & (out.price_class == SPIKE)))
        print(col, "the expensive misses:", probe_counts(out, col, nd & out.expensive_miss))
    tas = out.region == "TAS1"
    print("Tasmania misses by cause:", m[m.region == "TAS1"].cause.value_counts().to_dict())
    for col in ("probe_all", "probe_fcas"):
        print(" Tasmania", col, probe_counts(out, col, nd & tas & (out.price_class == SPIKE)))
    m.to_csv(ROOT / "results" / f"link_sweep_misses{suffix}.csv", index=False)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "day":
        run(only_day=sys.argv[2])
    elif cmd == "report":
        report(f"__{sys.argv[2]}" if len(sys.argv) > 2 else "")
    else:
        run()
