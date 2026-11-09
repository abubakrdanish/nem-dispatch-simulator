"""Re-run the stage 1 research's four simple models through the harness: python -m nemsim.reproduce

This validates the harness against figures that already exist (docs/stage1_research.md,
section 4). These are NOT rungs of the ladder. Three of them use inputs a later rung will
model: AEMO's unit availability and ramp limits, and actual interconnector flows.

The research describes its method in prose only; its script was in a session scratchpad that
no longer exists. What it says, and how each part is rebuilt here:

- "real final bids, real TLFs": band prices divided by TLF, as written; and by TLF x DLF,
  which this project found to be how AEMO scales them (docs/stage2_results.md). Both are run.
- "DispatchLoad's availability, initial MW and ramp rates": each unit's bands are filled in
  band order up to min(MAXAVAIL, AVAILABILITY, INITIALMW + RAMPUPRATE / 12), and the first
  max(0, INITIALMW - RAMPDOWNRATE / 12) MW of each unit is dispatched whatever its price.
  Ramp rates are MW per hour (the research checked this).
- "each region's demand was fixed at what AEMO actually dispatched there": the requirement is
  DISPATCHABLEGENERATION, which equals TOTALDEMAND + DISPATCHABLELOAD + NETINTERCHANGE to
  within 0.01 MW. The NEM-wide stacks meet the sum over regions.
- "no unit bounds": the research's no-bounds rows still cap bands at MAXAVAIL. Without that
  cap the NEM-wide median error is $1,146, not the $622 reported; with it, $626.
"""
from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd

from nemsim.config import path
from nemsim.harness import evaluate, summarise
from nemsim.inputs import REGIONS, IntervalInputs
from nemsim.intervals import SETS
from nemsim.rungs.base import Rung
from nemsim.stack import cap_bands, clear

# docs/stage1_research.md, section 4 tables. NaN where the research gave no figure.
RESEARCH = pd.DataFrame([
    ("nem", False, 0.0, 0.0, 0.0, 622.0, np.nan),
    ("nem", True, np.nan, 9.1, 29.1, 36.87, np.nan),
    ("region", False, 0.6, 0.9, 2.5, 367.0, np.nan),
    ("region", True, 20.8, 28.1, 44.7, 26.29, 51.0),
], columns=["grouping", "bounds", "within_$1", "within_$5", "within_$20", "median_abs_error", "bracket_contains_%"])

RESEARCH_BY_REGION = pd.DataFrame([
    ("TAS1", 44, 10, 38), ("VIC1", 27, 17, 72), ("SA1", 24, 31, 85), ("NSW1", 29, 34, 41), ("QLD1", 16, 43, 18),
], columns=["region", "within_$5", "median_abs_error", "bracket_contains_%"]).set_index("region")


class ResearchStack(Rung):
    """One of the research's four simple models."""

    uses = frozenset({"offers", "regional_dispatch", "units"})

    def __init__(self, grouping: str, bounds: bool, loss_factor: str):
        self.grouping, self.bounds, self.loss_factor = grouping, bounds, loss_factor
        self.key = f"stage1_{grouping}_{'bounds' if bounds else 'nobounds'}_{loss_factor}"
        self.name = self.key
        self.summary = "Reproduction of a stage 1 research row"

    def solve(self, x: IntervalInputs) -> pd.DataFrame:
        gen = x.offers[x.offers.direction == "GEN"].copy()
        if self.loss_factor == "tlf":
            gen["price"] = gen.raw_price / gen.tlf
        limit = gen.maxavail.to_numpy(dtype=float)
        floor_mw = np.zeros(len(gen))
        if self.bounds:
            u = x.units.set_index("duid")
            up = np.minimum(u.availability, u.initialmw + u.rampuprate / 12)
            down = np.maximum(0, u.initialmw - u.rampdownrate / 12)
            ub = gen.duid.map(up).fillna(0).to_numpy()   # a unit with no DISPATCHLOAD row offers nothing
            limit = np.minimum(limit, ub)
            floor_mw = np.minimum(gen.duid.map(down).fillna(0).to_numpy(), limit)
        d, b, m = gen.duid.to_numpy(), gen.band.to_numpy(), gen.mw.to_numpy()
        capped = cap_bands(d, b, m, limit)
        forced = cap_bands(d, b, capped, floor_mw)
        gen["free"] = capped - forced
        # A unit's forced MW counts even when its bands offer less than its ramp-down floor.
        gen["floor"] = floor_mw
        need = x.regional_dispatch.set_index("region").dispatchablegeneration
        groups = [(list(REGIONS), gen)] if self.grouping == "nem" else \
                 [([r], gen[gen.region == r]) for r in REGIONS]
        rows = []
        for regions, g in groups:
            requirement = need[regions].sum() - g.drop_duplicates("duid").floor.sum()
            c = clear(g.price.to_numpy(), g.free.to_numpy(), requirement, x.price_floor, x.price_cap)
            duid = g.duid.iloc[c.marginal] if c.marginal is not None else None
            rows += [{"region": r, "price": c.price, "marginal_duid": duid, "bracket_lo": c.bracket_lo,
                      "bracket_hi": c.bracket_hi, "status": c.status} for r in regions]
        return pd.DataFrame(rows)


def main() -> None:
    cols = ["within_$1", "within_$5", "within_$20", "median_abs_error", "bracket_contains_%"]
    intervals = SETS["stage1"].intervals()
    rows, best = [], None
    with duckdb.connect(str(path("warehouse")), read_only=True) as con:
        for _, r in RESEARCH.iterrows():
            for lf in ("tlf", "mlf"):
                model = ResearchStack(r.grouping, r.bounds, lf)
                result = evaluate(con, model, intervals)
                s = summarise(result).iloc[0]
                rows.append({"grouping": r.grouping, "bounds": r.bounds, "loss_factor": lf,
                             **{f"{c} here": s[c] for c in cols}, **{f"{c} research": r[c] for c in cols}})
                if r.grouping == "region" and r.bounds and lf == "tlf":
                    best = result
    table = pd.DataFrame(rows)
    print("## Stage 1 research rows, re-run (set `stage1`, target RRP)\n")
    for c in cols:
        print(f"### {c}\n")
        print(table[["grouping", "bounds", "loss_factor", f"{c} here", f"{c} research"]]
              .to_markdown(index=False, floatfmt=".1f"), "\n")
    by_region = summarise(best, "region")[["within_$5", "median_abs_error", "bracket_contains_%"]]
    print("### Regional stacks, flows fixed, with bounds, TLF: by region\n")
    print(by_region.join(RESEARCH_BY_REGION.add_suffix(" research")).to_markdown(floatfmt=".1f"))


if __name__ == "__main__":
    main()
