"""The NEM's long-run price distribution: python -m nemsim.decade [fetch|load|report]

Settled value needs regional prices and regional demand only, so this fetches two small tables
per month (DISPATCHPRICE about 2 MB, DISPATCHREGIONSUM about 5 MB) plus TRADINGPRICE (0.14 MB),
for ten financial years, FY2017 (July 2016) to FY2026 (June 2026). Months the nem-market-explainer
project already holds (2021-07 onward) are read from its directory in place, never fetched again.

It loads into its own DuckDB file (warehouse/prices.duckdb), not the evaluation warehouse, so
nothing a rung reads changes. Only the columns needed are kept.

Two measures of the money, by price band (negative, $0-50, $50-300, $300+, on the actual price):

  5-minute basis   RRP x TOTALDEMAND / 12 per region-interval, the definition the harness uses for
                   "settled value" throughout. This is the measure the dollar-weighting argument uses.
  settlement basis what the market actually settled on. From 1 October 2021 that is the 5-minute
                   price. Before it, energy settled on the 30-minute trading price (the average of
                   six dispatch prices), so a single 5-minute spike was diluted six-fold. Reported
                   for the years before five-minute settlement, as a check on the first measure.

Bands use nominal dollars. The market price cap rose from $14,000 (FY2017) to $17,500 (FY2026);
$300 is not inflation-adjusted. Writes results/decade_bands.csv.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date

import duckdb
import pandas as pd

from nemsim.config import ROOT, path, settings
from nemsim.http_client import PoliteClient
from nemsim.mmsdm import Table, download, ensure_schema, files_for, load_file

TS, TXT, F, I = "timestamp", "varchar", "double", "integer"
DECADE_TABLES = {
    "DISPATCHPRICE": Table("DISPATCHPRICE", {"SETTLEMENTDATE": TS, "REGIONID": TXT, "INTERVENTION": I, "RRP": F}),
    "DISPATCHREGIONSUM": Table("DISPATCHREGIONSUM", {"SETTLEMENTDATE": TS, "REGIONID": TXT, "INTERVENTION": I,
                                                     "TOTALDEMAND": F}),
    "TRADINGPRICE": Table("TRADINGPRICE", {"SETTLEMENTDATE": TS, "REGIONID": TXT, "INVALIDFLAG": TXT, "RRP": F},
                          optional=frozenset({"INVALIDFLAG"})),
}
FIRST, LAST = date(2016, 7, 1), date(2026, 6, 1)
FIVE_MINUTE_SETTLEMENT = pd.Timestamp("2021-10-01 00:05")
RAW = ROOT / "data" / "raw" / "aemo_prices"
DB = ROOT / "warehouse" / "prices.duckdb"
BANDS = [-1e9, 0, 50, 300, 1e9]
LABELS = ["a. negative", "b. $0-50", "c. $50-300", "d. $300+"]


def months() -> list[date]:
    out, m = [], FIRST
    while m <= LAST:
        out.append(m)
        m = date(m.year + (m.month == 12), 1 if m.month == 12 else m.month + 1, 1)
    return out


def source(name: str, m: date):
    """The file for one table-month: the explainer's copy if it has one, else this project's."""
    return files_for(path("explainer_raw_dir"), name, m) or files_for(RAW, name, m)


def fetch(limit_mb: float = 1000) -> None:
    cfg = settings()
    client = PoliteClient(cfg["nemweb"]["user_agent"], cfg["nemweb"]["min_request_gap_seconds"])
    plan = {m: [t for t in DECADE_TABLES if not source(t, m)] for m in months()}
    plan = {m: ts for m, ts in plan.items() if ts}
    start = time.monotonic()
    report = download(client, cfg["nemweb"]["archive_url"], plan, RAW, int(limit_mb * 1e6))
    print(json.dumps({"files": len(report), "mb": round(sum(r["bytes"] for r in report) / 1e6, 1),
                      "requests": client.requests, "seconds": round(time.monotonic() - start, 1)}))


def load() -> None:
    start, n = time.monotonic(), 0
    with duckdb.connect(str(DB)) as con:
        ensure_schema(con)
        for m in months():
            for name, table in DECADE_TABLES.items():
                found = source(name, m)
                if not found:
                    raise FileNotFoundError(f"{name} {m:%Y-%m}: run python -m nemsim.decade fetch")
                for f in found:
                    n += not load_file(con, table, f).get("skipped")
        con.execute("checkpoint")
    print(f"loaded {n} files in {time.monotonic() - start:.0f} s; {DB.stat().st_size / 1e6:.0f} MB")


def frame(con) -> pd.DataFrame:
    return con.execute("""
        with p as (select settlementdate t, regionid, any_value(rrp) rrp from raw.dispatchprice
                   where intervention = 0 group by 1, 2),
             d as (select settlementdate t, regionid, any_value(totaldemand) totaldemand from raw.dispatchregionsum
                   where intervention = 0 group by 1, 2),
             tp as (select settlementdate tt, regionid, any_value(rrp) trrp from raw.tradingprice group by 1, 2)
        select p.t, p.regionid, p.rrp, d.totaldemand, tp.trrp,
               -- the half-hour this interval settles in: its end time rounded up to :00 or :30
               time_bucket(interval 30 minute, p.t - interval 5 minute) + interval 30 minute tt
        from p join d using (t, regionid)
        left join tp on tp.regionid = p.regionid
                    and tp.tt = time_bucket(interval 30 minute, p.t - interval 5 minute) + interval 30 minute""").df()


def fy(t: pd.Series) -> pd.Series:
    s = t - pd.Timedelta(minutes=5)
    return "FY" + (s.dt.year + (s.dt.month >= 7)).astype(str)


def shares(f: pd.DataFrame, price: str, value: str, by: str) -> pd.DataFrame:
    f = f.assign(band=pd.cut(f[price], BANDS, right=False, labels=LABELS).astype(str))
    g = f.groupby([by, "band"]).agg(n=(value, "size"), v=(value, "sum"), av=(value, lambda x: x.abs().sum()))
    tot = g.groupby(level=0).transform("sum")
    out = pd.DataFrame({"share_intervals_pct": g.n / tot.n * 100, "share_settled_pct": g.v / tot.v * 100,
                        "share_abs_settled_pct": g.av / tot.av * 100, "settled_$m": g.v / 1e6,
                        "region_intervals": g.n})
    return out.reset_index()


def split_5ms() -> pd.DataFrame:
    """Is the rise in spike share since five-minute settlement (1 Oct 2021) the settlement change, or
    the 2022 energy crisis, which sits inside the post-5MS years? The $300+ share for periods either
    side, with and without the crisis, on the 5-minute basis; and post-5MS months re-valued at the
    half-hour average of their dispatch prices (the pre-5MS settlement rule), to separate the
    measurement basis from the market. Writes results/decade_5ms_split.csv."""
    with duckdb.connect(str(DB), read_only=True) as con:
        f = frame(con)
    f = f[(f.t > pd.Timestamp(FIRST)) & (f.t <= pd.Timestamp(LAST) + pd.offsets.MonthBegin(1))].copy()
    f["fy"] = fy(f.t)
    f["value"] = f.rrp * f.totaldemand / 12
    # Half-hour average of the six dispatch prices, as settled before 5MS (computed, not AEMO's figure)
    f["hh_avg"] = f.groupby(["regionid", "tt"]).rrp.transform("mean")
    s = f.t - pd.Timedelta(minutes=5)
    pre, post = f.t < FIVE_MINUTE_SETTLEMENT, f.t >= FIVE_MINUTE_SETTLEMENT
    periods = {
        "pre-5MS, Jul 2016 - Sep 2021": pre,
        "pre-5MS, FY2017-FY2021": pre & (f.fy <= "FY2021"),
        "post-5MS, Oct 2021 - Jun 2026": post,
        "post-5MS excluding FY2022 (FY2023-FY2026)": f.fy >= "FY2023",
        "post-5MS excluding calendar 2022 (Jan 2023 - Jun 2026)": post & (s.dt.year >= 2023),
        "FY2022 alone (Jul 2021 - Jun 2022)": f.fy == "FY2022",
    }
    rows = []
    for name, sel in periods.items():
        g = f[sel]
        for basis, price, value in (("5-minute dispatch price", "rrp", g.value),
                                    ("half-hour average of dispatch prices", "hh_avg", g.hh_avg * g.totaldemand / 12)):
            hi = g[price] >= 300
            rows.append({"period": name, "basis": basis, "region_intervals": len(g),
                         "share_settled_300_pct": value[hi].sum() / value.sum() * 100,
                         "share_intervals_300_pct": hi.mean() * 100, "settled_$bn": value.sum() / 1e9})
    out = pd.DataFrame(rows)
    out.to_csv(ROOT / "results" / "decade_5ms_split.csv", index=False)
    print(out.round(2).to_string(index=False))
    return out


def report() -> pd.DataFrame:
    with duckdb.connect(str(DB), read_only=True) as con:
        f = frame(con)
    f = f[(f.t > pd.Timestamp(FIRST)) & (f.t <= pd.Timestamp(LAST) + pd.offsets.MonthBegin(1))]
    f["fy"] = fy(f.t)
    f["value"] = f.rrp * f.totaldemand / 12
    f["all"] = "FY2017-FY2026"
    five = pd.concat([shares(f, "rrp", "value", "fy"), shares(f, "rrp", "value", "all").rename(columns={"all": "fy"})])
    five["basis"] = "5-minute dispatch price"
    # Settlement basis before 5MS: each 5-minute demand at the half-hour's trading price.
    pre = f[f.t < FIVE_MINUTE_SETTLEMENT].dropna(subset=["trrp"]).copy()
    pre["value"] = pre.trrp * pre.totaldemand / 12
    pre["all"] = "FY2017-FY2021 (pre-5MS)"
    thirty = pd.concat([shares(pre, "trrp", "value", "fy"),
                        shares(pre, "trrp", "value", "all").rename(columns={"all": "fy"})])
    thirty["basis"] = "30-minute trading price (what settled before Oct 2021)"
    out = pd.concat([five, thirty]).rename(columns={"fy": "period"})
    out.to_csv(ROOT / "results" / "decade_bands.csv", index=False)
    cov = f.groupby("fy").agg(region_intervals=("t", "size"), first=("t", "min"), last=("t", "max"))
    print(cov.to_string())
    piv = five.pivot(index="fy", columns="band", values=["share_settled_pct", "share_intervals_pct"]).round(2)
    print(piv.to_string())
    print(thirty.pivot(index="fy", columns="band", values="share_settled_pct").round(2).to_string())
    return out


if __name__ == "__main__":
    {"fetch": fetch, "load": load, "report": report, "split": split_5ms}[sys.argv[1] if len(sys.argv) > 1 else "report"]()
