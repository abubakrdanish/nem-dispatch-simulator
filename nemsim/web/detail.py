"""Precompute modelled interconnector flows for chosen days:
python -m nemsim.web.detail [YYYY-MM-DD ...]      (no dates: the episode days)

The harness stores only each rung's regional prices, so the flows rungs 3b and 4 solve for are not in
results/. This runs those two rungs on every interval of each day exactly as nemsim.harness.evaluate
does (one input window per day, the same rung classes, no change to either), keeps each interval's
solved flows and the constraints rung 4 violated, and compares every regional price it gets with the
stored, scored price. A few intervals do not reproduce: the LP's price there is degenerate (supply meets
demand exactly at a band edge, so a range of prices satisfies it) and the solver's choice within that
range depends on its path. Re-running rung 3b on June and September 2025 changed 1.35% and 0.60% of
prices but not one region-interval's within-$5 outcome. Those intervals are listed in the check file and
flagged on the page. If more than MAX_FLIP_SHARE of region-intervals change their within-$5 outcome, or
more than MAX_MISMATCH_SHARE of prices differ, something systematic is wrong and nothing is written.
About 30 seconds a day.

Writes results/web/detail/<day>.parquet: interval, rung, link, flow (MW, positive in AEMO's direction
from the first region of the link to the second), and results/web/detail/<day>.json with the check.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta

import duckdb
import pandas as pd

from nemsim.inputs import Window
from nemsim.rungs import LADDER
from nemsim.web.data import EPISODES, STEP, WEB, build_price_cache, Prices, warehouse

TOLERANCE = 0.01
MAX_MISMATCH_SHARE = 0.05
MAX_FLIP_SHARE = 0.005


def day(con, prices: Prices, d: str) -> dict:
    t0 = datetime.fromisoformat(d) + STEP
    t1 = t0 + timedelta(days=1) - STEP
    rows, check = [], {}
    for key in ("r3b", "r4"):
        rung = type(LADDER[key])()                       # a fresh instance, as the harness gets
        window = Window(con, t0, t1, rung.uses)
        have = set.intersection(*(window.intervals_with(u) for u in rung.uses))
        stored = prices.q("select interval, region, price, rrp from p where rung = ? and interval between ? and ? "
                          "and excluded is null", [key, t0, t1]).set_index(["interval", "region"])
        actual, stored = stored.rrp, stored.price
        worst, n, violated, mismatched, flips = 0.0, 0, {}, {}, 0
        t = t0
        while t <= t1:
            if pd.Timestamp(t) in have:
                out = rung.solve(window.at(t))
                for region, price in zip(out.region, out.price):
                    k = (pd.Timestamp(t), region)
                    if k in stored.index:
                        diff = abs(float(price) - float(stored[k]))
                        worst, n = max(worst, diff), n + 1
                        flips += (abs(float(price) - actual[k]) <= 5) != (abs(float(stored[k]) - actual[k]) <= 5)
                        if diff > TOLERANCE:
                            mismatched.setdefault(f"{t:%H:%M}", []).append(
                                [region, round(float(stored[k]), 2), round(float(price), 2)])
                for link, flow in zip(rung.last["links"], rung.last["flows"]):
                    rows.append({"interval": t, "rung": key, "link": link, "flow": float(flow)})
                if rung.last.get("violated"):
                    violated[f"{t:%H:%M}"] = rung.last["violated"]
            t += STEP
        bad = sum(len(v) for v in mismatched.values())
        check[key] = {"prices_compared": n, "prices_differing": bad, "within_5_flips": int(flips),
                      "worst_abs_difference": worst,
                      "differing": mismatched, "violated": violated}
        if n and (bad / n > MAX_MISMATCH_SHARE or flips / n > MAX_FLIP_SHARE):
            raise RuntimeError(f"{d} {key}: {bad} of {n} re-solved prices differ from the stored ones, "
                               f"{flips} change their within-$5 outcome "
                               f"(worst ${worst:.2f}); not writing flows that would not match the scored run")
    return {"rows": rows, "check": check}


def main(days: list[str]) -> None:
    prices = Prices(build_price_cache())
    out = WEB / "detail"
    out.mkdir(parents=True, exist_ok=True)
    with warehouse() as con:
        for d in days:
            tic = time.monotonic()
            r = day(con, prices, d)
            frame = pd.DataFrame(r["rows"])                       # written with DuckDB: no pyarrow needed
            duckdb.connect().register("f", frame).execute(f"copy f to '{out / f'{d}.parquet'}' (format parquet)")
            (out / f"{d}.json").write_text(json.dumps(r["check"], indent=1))
            print(d, {k: (v["prices_compared"], v["prices_differing"], v["within_5_flips"])
                      for k, v in r["check"].items()},
                  f"{time.monotonic() - tic:.0f} s", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:] or [d for d, _ in EPISODES])
