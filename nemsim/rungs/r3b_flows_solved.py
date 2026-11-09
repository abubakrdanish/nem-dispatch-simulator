"""Rung 3b: regional prices with interconnector flows solved, losses, and ramp limits.

The modelling half of rung 3. Rung 3a was told the actual flows; this rung works them out. Compare it
with 3a to see what solving the flows gains or loses, and never merge the two scores.

Rung 2, plus:

1. Five regions, each meeting its own demand (TOTALDEMAND plus its scheduled load held at actual) from
   its own offers plus net imports, in one linear program (nemsim/lp.py). Each regional price is the dual
   of that region's balance row.
2. Six interconnectors, each bounded by its standing import and export rating (INTERCONNECTORCONSTRAINT,
   the version in force), which is set before dispatch. Not the limits DISPATCHINTERCONNECTORRES reports:
   those are calculated by the dispatch run from the constraints binding at its solution, and are an
   outcome. Basslink is treated like the regulated links; its MNSP offers are not modelled.
3. Interconnector losses from AEMO's loss equation: marginal loss factor = LOSSCONSTANT +
   LOSSFLOWCOEFFICIENT x flow + sum(DEMANDCOEFFICIENT x regional TOTALDEMAND), so losses are
   (LOSSCONSTANT - 1 + demand term) x flow + LOSSFLOWCOEFFICIENT / 2 x flow^2, shared between the two
   regions by FROMREGIONLOSSSHARE. All standing data in force for the interval. The quadratic is
   linearised at the model's own flow and the LP re-solved until the flows settle (successive
   linearisation): a piecewise version would let the LP burn energy through losses at negative prices.
4. Ramp limits, folded in here rather than given a rung: each unit capped at INITIALMW + RAMPUPRATE / 12
   and held at or above INITIALMW - RAMPDOWNRATE / 12 (MW per hour, from DISPATCHLOAD; inputs).

Not modelled: intra-regional network constraints, FCAS, fast-start profiles, MNSP offers. Scheduled
load is held at actual, as in every rung so far.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from nemsim import lp
from nemsim.inputs import REGIONS, IntervalInputs
from nemsim.rungs.base import Rung
from nemsim.stack import cap_bands

MAX_ITERATIONS = 6
FLOW_TOLERANCE_MW = 1.0


def unit_limits(gen: pd.DataFrame, units: pd.DataFrame, ramps: bool = True) -> tuple[np.ndarray, dict[str, float]]:
    """Per-band capped MW, and each unit's lower limit (its ramp-down floor, within what it offers)."""
    u = units.set_index("duid")
    limit = np.minimum(gen.maxavail.to_numpy(float), gen.duid.map(u.availability).fillna(0).to_numpy(float))
    floor = pd.Series(0.0, index=u.index)
    if ramps:
        limit = np.minimum(limit, gen.duid.map(u.initialmw + u.rampuprate / 12).fillna(0).to_numpy(float))
        floor = np.maximum(0.0, u.initialmw - u.rampdownrate / 12)
    mw = cap_bands(gen.duid.to_numpy(), gen.band.to_numpy(), gen.mw.to_numpy(), limit)
    offered = pd.Series(mw, index=gen.duid.to_numpy()).groupby(level=0).sum()
    floors = floor.reindex(offered.index).fillna(0).clip(upper=offered)
    return mw, floors[floors > 1e-6].to_dict()


class FlowsSolved(Rung):
    key = "r3b"
    name = "Regional prices, flows solved, with losses and ramp limits"
    summary = ("Rung 2 as a five-region LP: interconnector flows solved within their standing ratings, losses "
               "from AEMO's loss equation, ramp limits from initial MW. No network constraints, no FCAS; "
               "scheduled load held at actual")
    uses = frozenset({"offers", "demand", "scheduled_load", "units", "interconnector_ratings",
                      "interconnector_loss_model"})

    def __init__(self, ramps: bool = True, losses: bool = True):
        self.ramps, self.losses = ramps, losses
        if not (ramps and losses):   # variants used only as diagnostics
            self.key = f"r3b{'' if ramps else '_noramps'}{'' if losses else '_lossless'}"

    def links(self, x: IntervalInputs) -> list[tuple[str, str, str, float, float]]:
        r = x.interconnector_ratings
        return list(zip(r.interconnectorid, r.from_region, r.to_region, r.import_rating, r.export_rating))

    def generic_constraints(self, x: IntervalInputs, gen: pd.DataFrame) -> list[lp.Constraint]:
        """Rung 3b applies none; rung 4 overrides this with the invoked constraint set."""
        return []

    def solve(self, x: IntervalInputs) -> pd.DataFrame:
        gen = x.offers[x.offers.direction == "GEN"].reset_index(drop=True)
        mw, floors = unit_limits(gen, x.units, self.ramps)
        demand = (x.demand.set_index("region").totaldemand
                  + x.scheduled_load.set_index("region").dispatchableload).to_dict()
        links = self.links(x)
        model = x.interconnector_loss_model.set_index("interconnectorid")
        a = np.array([model.lossconstant[i] - 1 + model.demand_term[i] for i, *_ in links])
        b = np.array([model.lossflowcoefficient[i] for i, *_ in links])
        share = np.array([model.fromregionlossshare[i] for i, *_ in links])
        constraints = self.generic_constraints(x, gen)
        flow = np.zeros(len(links))
        for _ in range(MAX_ITERATIONS):
            # Tangent to losses(f) = a f + b/2 f^2 at the current flow: alpha + beta f.
            beta, alpha = a + b * flow, -b / 2 * flow ** 2
            terms = list(zip(alpha, beta, share)) if self.losses else None
            s = lp.solve(list(REGIONS), gen.region.to_numpy(), gen.price.to_numpy(), mw, gen.duid.to_numpy(),
                         floors, demand, links, x.price_floor, x.price_cap, terms, constraints)
            new = np.array([s.flows[i] for i, *_ in links])
            settled = np.abs(new - flow).max() < FLOW_TOLERANCE_MW
            flow = new
            if settled or not self.losses:
                break
        # Kept for diagnostics (flows and modelled losses against the actual ones); never scored.
        self.last = {"links": [i for i, *_ in links], "flows": flow,
                     "losses": a * flow + b / 2 * flow ** 2 if self.losses else np.zeros(len(links)),
                     "violated": s.violated}
        status = ["short" if s.deficit[r] > 1e-6 else "long" if s.surplus[r] > 1e-6 else "lp" for r in REGIONS]
        return pd.DataFrame({"region": list(REGIONS), "price": [s.prices[r] for r in REGIONS], "status": status})
