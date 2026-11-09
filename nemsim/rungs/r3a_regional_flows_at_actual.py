"""Rung 3a: the mechanical ceiling lift. Regional prices, interconnector flows held at actual.

**Not a modelling gain.** This rung buys accuracy by being allowed five answers where rung 2 had one,
and by being told how much power actually crossed every interconnector. Report its score as the
mechanical lift from regional pricing, never as something the model worked out. Rung 3b, which solves
the flows, is the modelling comparison.

Rung 2, plus exactly one change: one stack per region instead of one for the NEM. Each region's offers
(capped as in rung 2, at min(MAXAVAIL, DISPATCHLOAD AVAILABILITY)) meet that region's demand plus its
actual net exports:

    requirement_r = TOTALDEMAND_r + DISPATCHABLELOAD_r + NETINTERCHANGE_r   (= DISPATCHABLEGENERATION_r)

NETINTERCHANGE is an outcome: the actual interconnector flows, with interconnector losses allocated
to the regions. That is the leak this rung is labelled for. No ramp limits, no solved flows, no network
constraints, no FCAS; scheduled load held at actual, as in rungs 1 and 2. It is the research's
"regional stacks, flows fixed" model under rung 2's inputs and without its ramp limits.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from nemsim.inputs import REGIONS, IntervalInputs
from nemsim.rungs.base import Rung
from nemsim.stack import cap_bands, clear


class RegionalFlowsAtActual(Rung):
    key = "r3a"
    name = "Regional merit order, flows held at actual (the mechanical ceiling lift)"
    summary = ("MECHANICAL CEILING LIFT, NOT A MODELLING GAIN: rung 2 split into five regional stacks, each meeting "
               "its demand plus its actual net interchange (flows held at actual). No ramps, no solved flows")
    uses = frozenset({"offers", "demand", "scheduled_load", "units", "regional_dispatch"})

    def solve(self, x: IntervalInputs) -> pd.DataFrame:
        gen = x.offers[x.offers.direction == "GEN"]
        available = gen.duid.map(x.units.set_index("duid").availability).fillna(0).to_numpy(float)
        mw = cap_bands(gen.duid.to_numpy(), gen.band.to_numpy(), gen.mw.to_numpy(),
                       np.minimum(gen.maxavail.to_numpy(float), available))
        need = (x.demand.set_index("region").totaldemand + x.scheduled_load.set_index("region").dispatchableload
                + x.regional_dispatch.set_index("region").netinterchange)
        rows = []
        for r in REGIONS:
            mask = (gen.region == r).to_numpy()
            c = clear(gen.price.to_numpy()[mask], mw[mask], float(need[r]), x.price_floor, x.price_cap)
            duid = gen.duid.to_numpy()[mask][c.marginal] if c.marginal is not None else None
            rows.append({"region": r, "price": c.price, "marginal_duid": duid, "bracket_lo": c.bracket_lo,
                         "bracket_hi": c.bracket_hi, "status": c.status})
        return pd.DataFrame(rows)
