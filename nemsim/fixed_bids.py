"""Re-score every rung with fixed bids applied: python -m nemsim.fixed_bids run | report

A unit's fixed bid (BIDPEROFFER_D.FIXEDLOAD > 0) holds it at exactly that MW in dispatch. It is part of
the participant's offer, made before the interval: an input, which every rung had been ignoring
(nemsim/inputs.py, FIXED_BIDS). This applies it as a correction to how offers are read, not as a new rung.

Only intervals where the correction changes some unit's offer are re-scored. Elsewhere every fixed-bid
unit's offer already amounts to "FIXEDLOAD MW at the price floor" (its MAXAVAIL is at most FIXEDLOAD
and its floor-priced bands cover it), so its inputs are identical and its scored price is kept as it
was. Re-solving those would only add LP degeneracy noise. The re-scored rows are spliced into copies of
the stored results, written as results/<month>__<rung>__rrp__fixedbids.csv; the stored files are not
touched.
"""
from __future__ import annotations

import sys
import time
from concurrent.futures import ProcessPoolExecutor

import duckdb
import pandas as pd

from nemsim.config import ROOT, path
from nemsim.intervals import MONTHS, SPIKE_MONTHS

RUNGS = ("r1", "r2", "r3a", "r3b", "r4")
R = ROOT / "results"


def changed_intervals(con, month: str) -> list:
    """Intervals of `month` where applying fixed bids changes at least one unit's offer."""
    bands = " + ".join(f"case when d.priceband{i} / (u.transmissionlossfactor * coalesce(u.distributionlossfactor, 1)) "
                       f"<= -999.99 then p.bandavail{i} else 0 end" for i in range(1, 11))
    total = " + ".join(f"p.bandavail{i}" for i in range(1, 11))
    return [r[0] for r in con.execute(f"""
        with f as (
            select p.interval_datetime t, p.fixedload, p.maxavail, {bands} floor_mw, {total} total_mw
            from raw.bidperoffer_d p
            join raw.biddayoffer_d d on d.settlementdate = p.settlementdate and d.duid = p.duid and d.direction = p.direction
            join raw.dudetailsummary u on u.duid = p.duid and p.interval_datetime - interval 5 minute >= u.start_date
                                      and p.interval_datetime - interval 5 minute < u.end_date
            where p.direction = 'GEN' and p.fixedload > 0
              and strftime(p.interval_datetime - interval 5 minute, '%Y-%m') = ?)
        select distinct t from f
        where not (least(maxavail, total_mw) <= fixedload + 0.5 and least(maxavail, floor_mw) >= fixedload - 0.5)
        order by t""", [month]).fetchall()]


def _month(month: str) -> dict:
    from nemsim import inputs
    from nemsim.harness import evaluate
    from nemsim.rungs import LADDER
    inputs.FIXED_BIDS = True
    con = duckdb.connect(str(path("warehouse")), read_only=True)
    todo = changed_intervals(con, month)
    out = {"month": month, "intervals_changed": len(todo)}
    for key in RUNGS:
        stored = pd.read_csv(R / f"{month}__{key}__rrp.csv", low_memory=False, parse_dates=["interval"])
        tic = time.monotonic()
        if todo:
            new = evaluate(con, type(LADDER[key])(), todo)
            keep = stored[~stored.interval.isin(pd.to_datetime(todo))]
            merged = pd.concat([keep, new[stored.columns.intersection(new.columns)]], ignore_index=True)
            merged = merged.sort_values(["interval", "region"]).reset_index(drop=True)
        else:
            merged = stored
        merged.to_csv(R / f"{month}__{key}__rrp__fixedbids.csv", index=False)
        out[f"{key}_seconds"] = round(time.monotonic() - tic)
    con.close()
    return out


def run(workers: int = 4) -> None:
    tic = time.monotonic()
    with ProcessPoolExecutor(workers) as ex:
        for r in ex.map(_month, MONTHS + SPIKE_MONTHS):
            print(r, f"{time.monotonic() - tic:.0f} s", flush=True)


if __name__ == "__main__":
    {"run": run}[sys.argv[1] if len(sys.argv) > 1 else "run"]()
