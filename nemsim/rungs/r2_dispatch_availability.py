"""Rung 2: merit order, each offer capped at AEMO's dispatch availability for the unit. Nothing else.

Rung 1 plus one change: each unit's bands are filled up to min(MAXAVAIL, AVAILABILITY), where
AVAILABILITY comes from DISPATCHLOAD. For a scheduled unit AVAILABILITY equals its MAXAVAIL in
every row checked, so nothing changes. For a wind or solar (semi-scheduled) unit it is
min(MAXAVAIL, UIGF), and UIGF is AEMO's forecast of what the unit could produce in the interval,
made before dispatch runs. This is the only change from rung 1.

Why AVAILABILITY and not what the unit actually produced: AVAILABILITY is an input to dispatch.
Realised output (SCADA at the end of the interval, or the dispatch target TOTALCLEARED) is an
outcome. It already includes the curtailment NEMDE chose for price and network reasons, so
capping at it would give this rung part of what interconnectors, losses and constraints explain.
See docs/stage2_results.md, rung 2.

Not modelled: ramp rates (neither the ramp-up cap nor the ramp-down floor), interconnectors,
losses, network constraints, FCAS, fast-start profiles. One NEM-wide price, as in rung 1.
Scheduled load is held at actual, as in rung 1.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from nemsim.inputs import REGIONS, IntervalInputs
from nemsim.rungs.base import Rung
from nemsim.stack import cap_bands, clear


class DispatchAvailability(Rung):
    key = "r2"
    name = "Merit order, offers capped at AEMO's dispatch availability"
    summary = ("Rung 1 with each unit capped at min(MAXAVAIL, DISPATCHLOAD AVAILABILITY): for wind and solar "
               "that is AEMO's forecast (UIGF), not realised output. No ramp rates, no network, no losses; "
               "scheduled load held at actual")
    uses = frozenset({"offers", "demand", "scheduled_load", "units"})

    def solve(self, x: IntervalInputs) -> pd.DataFrame:
        gen = x.offers[x.offers.direction == "GEN"]
        available = gen.duid.map(x.units.set_index("duid").availability)
        # A unit with no DISPATCHLOAD row is not dispatched by NEMDE, so it offers nothing here.
        limit = np.minimum(gen.maxavail.to_numpy(dtype=float), available.fillna(0).to_numpy(dtype=float))
        mw = cap_bands(gen.duid.to_numpy(), gen.band.to_numpy(), gen.mw.to_numpy(), limit)
        requirement = x.demand.totaldemand.sum() + x.scheduled_load.dispatchableload.sum()
        c = clear(gen.price.to_numpy(), mw, requirement, x.price_floor, x.price_cap)
        duid = gen.duid.iloc[c.marginal] if c.marginal is not None else None
        return pd.DataFrame({"region": list(REGIONS), "price": c.price, "marginal_duid": duid,
                             "bracket_lo": c.bracket_lo, "bracket_hi": c.bracket_hi, "status": c.status})
