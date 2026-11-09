"""Check what the stage 1 research says about each test case against the data, and say which
cases a rung can be scored on: python -m nemsim.cases

A case is scorable when its intervals have offers loaded. The suspension case must never be
scorable; the harness excludes suspended intervals.
"""
from __future__ import annotations

import duckdb
import pandas as pd

from nemsim.config import path
from nemsim.harness import link_states
from nemsim.intervals import CASES, Case

MAINLAND = ("NSW1", "QLD1", "SA1", "VIC1")


def _prices(con, case: Case) -> pd.DataFrame:
    iv = case.intervals()
    return con.execute("""select settlementdate as interval, regionid as region, rrp, marketsuspendedflag
        from raw.dispatchprice where intervention = 0 and settlementdate between ? and ?""",
                       [min(iv), max(iv)]).df()


def _links(con, start, end) -> pd.DataFrame:
    return con.execute("""select settlementdate as interval, interconnectorid, mwflow, exportlimit, importlimit,
        exportlimit < importlimit as crossed,
        (case when mwflow >= 0 then exportlimit - mwflow else mwflow - importlimit end) < 0.1 as at_limit
        from raw.dispatchinterconnectorres where intervention = 0 and settlementdate between ? and ?""",
                       [start, end]).df()


def claims(con, case: Case) -> list[tuple[str, str, bool]]:
    """(claim, what the data shows, holds?) for each stated fact."""
    p = _prices(con, case)
    if p.empty:
        return [("prices loaded", "no DISPATCHPRICE rows for these intervals", False)]
    iv = case.intervals()
    links = _links(con, min(iv), max(iv))
    at = lambda t: pd.Timestamp(t)  # noqa: E731
    out = []
    for t, region, rrp in case.prices:
        got = p[(p.interval == at(t)) & (p.region == region)].rrp
        seen = float(got.iloc[0]) if len(got) else None
        # Compare at the precision the research states: whole dollars or cents.
        ok = seen is not None and (round(seen) == rrp if float(rrp).is_integer() else abs(seen - rrp) < 0.006)
        out.append((f"{region} RRP {rrp} at {t}", f"{seen}", ok))
    for name in case.claims:
        out.append(_claim(name, p, links, con))
    return out


def _claim(name: str, p: pd.DataFrame, links: pd.DataFrame, con) -> tuple[str, str, bool]:
    def rrp(t, r):
        return float(p[(p.interval == pd.Timestamp(t)) & (p.region == r)].rrp.iloc[0])

    def link(t, i):
        return links[(links.interval == pd.Timestamp(t)) & (links.interconnectorid == i)].iloc[0]

    if name == "case1_mainland_mean_66.48_spread_1.37":
        s = p[(p.interval == pd.Timestamp("2026-05-14 14:05")) & p.region.isin(MAINLAND)].rrp
        return (name, f"mean {s.mean():.2f}, spread {s.max() - s.min():.2f}",
                abs(s.mean() - 66.48) < 0.006 and abs(s.max() - s.min() - 1.37) < 0.006)
    if name == "case1_no_link_at_limit":
        n = int(links.at_limit.sum())
        return (name, f"{n} link-intervals at a limit in the case", n == 0)
    if name == "case2_nsw_range_-10_600":
        s = p[p.region == "NSW1"].rrp
        return (name, f"calendar day {s.min():.2f} to {s.max():.2f}", round(s.min()) == -10 and round(s.max()) == 600)
    if name == "case2_sa_range_6_760":
        s = p[p.region == "SA1"].rrp
        return (name, f"calendar day {s.min():.2f} to {s.max():.2f}", round(s.min()) == 6 and round(s.max()) == 760)
    if name == "case3_nsw_at_cap_11:00-11:15":
        s = p[p.region == "NSW1"].rrp
        return (name, f"NSW1 {list(s.round(2))}", bool((s >= 20300 - 0.10).all()))
    if name == "case3_qni_at_limit_782_qld_to_nsw":
        r = link("2025-11-26 11:00", "NSW1-QLD1")
        return (name, f"MWFLOW {r.mwflow:.1f} (negative = QLD to NSW), at limit {r.at_limit}",
                bool(r.at_limit) and round(-r.mwflow) == 782)
    if name == "case3_vni_not_at_limit":
        r = link("2025-11-26 11:00", "VIC1-NSW1")
        return (name, f"MWFLOW {r.mwflow:.1f}, limits {r.importlimit:.0f} to {r.exportlimit:.0f}", not bool(r.at_limit))
    if name == "case4_heywood_496_into_sa_crossed_limits":
        r = link("2026-01-26 20:40", "V-SA")
        return (name, f"MWFLOW {r.mwflow:.1f}, limits {r.importlimit:.0f} to {r.exportlimit:.0f}, crossed {r.crossed}",
                round(r.mwflow) == 496 and bool(r.crossed))
    if name == "case5_qni_at_limit_qld_exporting_1102":
        r = link("2025-06-26 18:00", "NSW1-QLD1")
        return (name, f"MWFLOW {r.mwflow:.1f}, at limit {r.at_limit}", bool(r.at_limit) and round(-r.mwflow) == 1102)
    if name == "case5_52_intervals_ge_1500":
        n = {r: int((p[p.region == r].rrp >= 1500).sum()) for r in ("NSW1", "VIC1", "SA1")}
        return (name, f"intervals >= $1,500 in the case window: {n}", all(v == 52 for v in n.values()))
    if name == "case6_sa_median_-439":
        s = p[p.region == "SA1"].rrp
        return (name, f"SA1 median {s.median():.2f}", round(s.median()) == -439)
    if name == "case6_heywood_and_murraylink_at_limit":
        h, m = link("2024-10-07 13:00", "V-SA"), link("2024-10-07 13:00", "V-S-MNSP1")
        return (name, f"Heywood {h.mwflow:.1f} MW at limit {h.at_limit}; Murraylink {m.mwflow:.1f} at limit {m.at_limit}",
                bool(h.at_limit) and bool(m.at_limit) and round(-h.mwflow) == 197)
    if name == "case7_sa_energy_mean_82_max_346":
        s = p[p.region == "SA1"].rrp
        return (name, f"SA1 energy mean {s.mean():.2f}, max {s.max():.2f}", round(s.mean()) == 82 and round(s.max()) == 346)
    if name == "case8_suspended_flag_2_all_regions":
        flags = sorted(p.marketsuspendedflag.unique())
        return (name, f"flags {flags} on {len(p)} region-intervals", flags == [2] and len(p) == 16 * 5)
    if name == "case9_intervention_rows_present":
        n = con.execute("""select count(*) from raw.dispatchprice where intervention = 1
                           and settlementdate between '2024-11-27 15:50' and '2024-11-27 16:45'""").fetchone()[0]
        return (name, f"{n} INTERVENTION = 1 rows", n > 0)
    if name == "case9_nsw_2154_at_18:00":
        v = con.execute("""select rrp from raw.dispatchprice where intervention = 0 and regionid = 'NSW1'
                           and settlementdate = '2024-11-27 18:00'""").fetchone()
        return (name, f"{v[0] if v else None}", v is not None and round(v[0]) == 2154)
    raise KeyError(name)


def scorable(con, case: Case) -> str:
    iv = case.intervals()
    have = con.execute("select count(distinct interval_datetime) from raw.bidperoffer_d "
                       "where interval_datetime between ? and ?", [min(iv), max(iv)]).fetchone()[0]
    return f"{have} of {len(iv)} intervals have offers"


def main() -> None:
    with duckdb.connect(str(path("warehouse")), read_only=True) as con:
        failed = 0
        for case in CASES:
            print(f"\n### Case {case.number}: {case.description}\n\n{scorable(con, case)}.\n")
            print("| claim | data | holds |\n|---|---|---|")
            for claim, seen, ok in claims(con, case):
                failed += not ok
                print(f"| {claim} | {seen} | {'yes' if ok else '**no**'} |")
        print(f"\n{failed} claims do not hold.")


if __name__ == "__main__":
    main()
