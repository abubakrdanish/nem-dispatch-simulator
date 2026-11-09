"""Add one month to the warehouse, one at a time, and clean up after it:
python -m nemsim.add_month YYYY-MM [--delete-raw] [--memory-limit 4GB]

The month must be listed under `downloads` in config/settings.yml. Steps, each idempotent:

  1. download  the month's files, through the polite client (3-second gap, project user agent),
               skipping any file already on disk or already loaded (meta.loaded_files)
  2. load      into the warehouse with DuckDB capped at --memory-limit and spilling to
               warehouse/duckdb_tmp/ beyond it, so a month never needs more than a few GB of RAM
  3. definitions  fetch and load any constraint-equation versions the new month references that are not
               loaded yet (nemsim/definitions.py; those files are kept permanently)
  4. validate  nemsim/validate.py for the month: files, interval coverage and the 1 MW LHS rebuild
  5. delete    with --delete-raw only, and only on a "pass": remove this month's dispatch-table zips,
               each named, from data/raw/aemo. Definitions, standing tables and the warehouse stay.

Peak memory (this process's maximum resident set) and disk are printed at the end of each step.
"""
from __future__ import annotations

import argparse
import json
import resource
import sys
import time
from datetime import date

import duckdb

from nemsim import definitions
from nemsim.config import ROOT, path, settings
from nemsim.http_client import PoliteClient
from nemsim.mmsdm import download, ensure_schema, files_for, load_file, loaded_files
from nemsim.tables import TABLES
from nemsim.validate import validate_month, record

DELETABLE = {"BIDPEROFFER_D", "BIDDAYOFFER_D", "DISPATCHLOAD", "DISPATCHCONSTRAINT",
             "DISPATCHPRICE", "DISPATCHREGIONSUM", "DISPATCHINTERCONNECTORRES"}


def peak_gb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / 1e9 if sys.platform == "darwin" else r * 1024 / 1e9   # bytes on macOS, KiB on Linux


def footprint() -> dict:
    raw = sum(p.stat().st_size for p in path("raw_dir").glob("*.zip"))
    return {"warehouse_gb": round(path("warehouse").stat().st_size / 1e9, 2), "raw_gb": round(raw / 1e9, 2),
            "peak_rss_gb": round(peak_gb(), 2)}


def connect(memory_limit: str) -> duckdb.DuckDBPyConnection:
    tmp = ROOT / "warehouse" / "duckdb_tmp"
    tmp.mkdir(exist_ok=True)
    con = duckdb.connect(str(path("warehouse")))
    con.execute(f"set memory_limit = '{memory_limit}'")
    con.execute(f"set temp_directory = '{tmp}'")
    con.execute("set threads = 4")
    con.execute("set preserve_insertion_order = false")
    return con


IESS = "2024-06-03"     # integrating energy storage systems: offers carry DIRECTION from this trading day

# One row per DUID: the direction its offers had before IESS, when each DUID offered in one direction
# only. A scheduled load offers LOAD, except Wholesale Demand Response units (LOAD, subtype WDR, from
# October 2021), which offer demand reduction as GEN. No DUID's dispatch type changes anywhere in
# DUDETAILSUMMARY's history, so the newest record stands for every date. Checked against the offers
# that do carry DIRECTION in tests/test_direction.py.
DIRECTION_RULE = """
    select duid, case when any_value(dispatchtype order by end_date desc) = 'LOAD'
                       and coalesce(any_value(dispatchsubtype order by end_date desc), '') <> 'WDR'
                      then 'LOAD' else 'GEN' end as direction
    from raw.dudetailsummary group by 1"""


def fill_direction(con) -> int:
    """Offers before IESS (3 June 2024) carry no DIRECTION; fill it from DIRECTION_RULE. Refuses to
    continue if any offer is left without one (a DUID missing from DUDETAILSUMMARY)."""
    n = 0
    for t in ("bidperoffer_d", "biddayoffer_d"):
        n += con.execute(f"""update raw.{t} b set direction = u.direction from ({DIRECTION_RULE}) u
            where b.duid = u.duid and b.direction is null and b.settlementdate < date '{IESS}'""").fetchone()[0]
        left = con.execute(f"select count(*) from raw.{t} where direction is null").fetchone()[0]
        if left:
            raise RuntimeError(f"raw.{t}: {left} offers still have no DIRECTION; refusing to continue")
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("month")
    ap.add_argument("--delete-raw", action="store_true")
    ap.add_argument("--memory-limit", default="4GB")
    ap.add_argument("--limit-mb", type=float, default=1000)
    a = ap.parse_args()
    cfg = settings()
    tables = cfg["downloads"][a.month]
    month = date.fromisoformat(a.month + "-01")
    log = {"month": a.month, "steps": []}

    def step(name: str, t0: float, **extra) -> None:
        log["steps"].append({"step": name, "seconds": round(time.monotonic() - t0, 1), **footprint(), **extra})
        print(json.dumps(log["steps"][-1]), flush=True)

    t0 = time.monotonic()
    client = PoliteClient(cfg["nemweb"]["user_agent"], cfg["nemweb"]["min_request_gap_seconds"])
    got = download(client, cfg["nemweb"]["archive_url"], {month: tables}, path("raw_dir"),
                   int(a.limit_mb * 1e6), done=loaded_files(path("warehouse")))
    step("download", t0, mb=round(sum(g["bytes"] for g in got) / 1e6, 1), requests=client.requests)

    t0 = time.monotonic()
    with connect(a.memory_limit) as con:
        ensure_schema(con)
        rows = 0
        for name in tables:
            for f in files_for(path("raw_dir"), name, month):
                rows += load_file(con, TABLES[name], f).get("rows_loaded", 0)
        filled = fill_direction(con)
        con.execute("checkpoint")
    step("load", t0, rows=rows, direction_filled=filled)

    t0 = time.monotonic()
    definitions.main()
    step("definitions", t0)

    t0 = time.monotonic()
    with duckdb.connect(str(path("warehouse")), read_only=True) as con:
        con.execute(f"set memory_limit = '{a.memory_limit}'")
        con.execute(f"set temp_directory = '{ROOT / 'warehouse' / 'duckdb_tmp'}'")
        con.execute("set threads = 4")
        con.execute("set preserve_insertion_order = false")
        v = validate_month(con, a.month, tables)
    record([v])
    step("validate", t0, verdict=v["verdict"], lhs_pct=v["lhs_within_1mw_pct"])

    t0 = time.monotonic()
    removed = []
    if a.delete_raw and v["verdict"] == "pass":
        done = loaded_files(path("warehouse"))
        for name in tables:
            if name not in DELETABLE:
                continue
            for f in files_for(path("raw_dir"), name, month):
                if f.name in done:          # never delete a file the warehouse has not recorded as loaded
                    removed.append((f.name, f.stat().st_size))
                    f.unlink()
    step("delete", t0, files_removed=len(removed), mb_removed=round(sum(s for _, s in removed) / 1e6, 1),
         kept_because=None if removed or not a.delete_raw else v["verdict"])
    out = ROOT / "results" / "add_month_log.jsonl"
    with out.open("a") as fh:
        fh.write(json.dumps(log) + "\n")


if __name__ == "__main__":
    main()
