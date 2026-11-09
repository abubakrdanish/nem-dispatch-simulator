"""Is a month's ingest complete and correct? python -m nemsim.validate [YYYY-MM ...]

A month's raw files may be deleted only after this passes for it. Three checks:

  files      every planned dispatch-table file for the month is recorded in meta.loaded_files
             (the loader already refuses a file whose rows do not match its END OF REPORT count)
  intervals  DISPATCHLOAD, DISPATCHCONSTRAINT and BIDPEROFFER_D each cover every 5-minute interval
  lhs        for every applied energy constraint-interval, the left-hand side rebuilt from the ingested
             unit dispatch, link flows and constraint definitions matches the LHS AEMO reports within
             1 MW (the "full" rebuild of nemsim.audit_terms). Pass: at least 99.9%.

The LHS check ties together four tables (DISPATCHLOAD, DISPATCHCONSTRAINT, DISPATCHINTERCONNECTORRES
and the definitions), so a truncated or mis-parsed file in any of them shows up as a miss. It is
also reported on the intervals where DISPATCHLOAD has rows, so a gap in AEMO's own file is told apart
from a fault in the ingest. A month whose equations reconcile but whose tables are short of intervals
is reported as "interval gap", not as a pass: whether the gap is in AEMO's file or in the ingest is
then checked against the raw file by hand, and the raw files are kept until it has been (docs/DATA.md
records the two found: 2025-09 DISPATCHLOAD missing 240 intervals, 2024-11 BIDPEROFFER_D missing 4).

Writes results/validation.csv (one row per month; appends, newest result per month wins).
"""
from __future__ import annotations

import argparse
import time

import duckdb
import pandas as pd

from nemsim.audit_terms import chunks, month_frame
from nemsim.config import ROOT, path, settings

DISPATCH_TABLES = ["BIDPEROFFER_D", "BIDDAYOFFER_D", "DISPATCHLOAD", "DISPATCHCONSTRAINT"]
THRESHOLD = 99.9


def expected_intervals(m: str) -> int:
    start = pd.Timestamp(m + "-01")
    return int(((start + pd.offsets.MonthBegin(1)) - start) / pd.Timedelta(minutes=5))


def validate_month(con, m: str, tables: list[str]) -> dict:
    t0 = time.monotonic()
    ym = m.replace("-", "")
    loaded = set(con.execute("select tablename from meta.loaded_files where regexp_matches(file, ?)",
                             [f"_{ym}_"]).df().tablename)
    missing_files = [t for t in tables if t in DISPATCH_TABLES and t not in loaded]
    s0, s1 = next(chunks(m, days=40))
    n = {}
    # A month's bid file holds its trading days, which run 04:05 to 04:00, so it covers the month
    # shifted by four hours (its last 48 intervals are the next month's first 48).
    b0, b1 = (str(pd.Timestamp(x) + pd.Timedelta(hours=4)) for x in (s0, s1))
    for t, col, a, b in [("dispatchload", "settlementdate", s0, s1), ("dispatchconstraint", "settlementdate", s0, s1),
                         ("bidperoffer_d", "interval_datetime", b0, b1)]:
        n[t] = con.execute(f"select count(distinct {col}) from raw.{t} where {col} >= ? and {col} < ?",
                           [a, b]).fetchone()[0]
    dl_times = set(con.execute("select distinct settlementdate from raw.dispatchload "
                               "where settlementdate >= ? and settlementdate < ?", [s0, s1]).df().settlementdate)
    ci = match = ci_dl = match_dl = 0
    for a, b in chunks(m):
        f = month_frame(con, m, 1.0, a, b)
        ok = (f.lhs_full - f.lhs).abs() < 1
        ci += len(f)
        match += int(ok.sum())
        has = f.t.isin(dl_times)
        ci_dl += int(has.sum())
        match_dl += int(ok[has].sum())
    exp = expected_intervals(m)
    pct = match / ci * 100 if ci else float("nan")
    pct_dl = match_dl / ci_dl * 100 if ci_dl else float("nan")
    complete = all(v == exp for v in n.values())
    if missing_files or not ci:
        verdict = "fail"
    elif pct >= THRESHOLD and complete:
        verdict = "pass"
    elif pct_dl >= THRESHOLD and n["dispatchconstraint"] == exp:
        verdict = "interval gap"   # equations reconcile, but a table is short of intervals: check the source
    else:
        verdict = "fail"
    return {"month": m, "verdict": verdict, "missing_files": ";".join(missing_files),
            "intervals_expected": exp, "intervals_dispatchload": n["dispatchload"],
            "intervals_dispatchconstraint": n["dispatchconstraint"], "intervals_bids": n["bidperoffer_d"],
            "constraint_intervals": ci, "lhs_within_1mw_pct": round(pct, 3),
            "lhs_within_1mw_pct_where_dispatchload": round(pct_dl, 3),
            "seconds": round(time.monotonic() - t0, 1), "checked_at": pd.Timestamp.now().isoformat(timespec="seconds")}


def record(rows: list[dict]) -> pd.DataFrame:
    out = ROOT / "results" / "validation.csv"
    new = pd.DataFrame(rows)
    if out.exists():
        new = pd.concat([pd.read_csv(out, dtype={"missing_files": str}), new])
    new = new.drop_duplicates("month", keep="last").sort_values("month")
    new.to_csv(out, index=False)
    return new


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("months", nargs="*", help="default: every month in config downloads")
    args = parser.parse_args()
    plan = settings()["downloads"]
    months = args.months or list(plan)
    rows = []
    with duckdb.connect(str(path("warehouse")), read_only=True) as con:
        con.execute("set threads = 4")
        con.execute("set memory_limit = '6GB'")
        con.execute("set preserve_insertion_order = false")
        for m in months:
            rows.append(validate_month(con, m, plan.get(m, DISPATCH_TABLES)))
            print(rows[-1], flush=True)
    print(record(rows).to_string(index=False))


if __name__ == "__main__":
    main()
