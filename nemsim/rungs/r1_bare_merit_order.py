"""Rung 1: merit order, each offer capped at its declared availability. Nothing else.

This is the "bare merit order" rung, and the name is exact on purpose: it is not "no limits at
all". The one limit it applies, MAXAVAIL, is part of the offer itself, not a constraint AEMO
imposes. Every other limit belongs to a later rung.

One stack for the whole NEM. Every final energy generation offer goes in as offered: its band
MW, filled in band order up to the MAXAVAIL in the same offer, at its band price referred to the
regional reference node. Cheapest first until demand is met; the band that meets it sets one
price for every region.

MAXAVAIL is part of the offer, so it is applied here. Band MW alone over-offers badly: on
2025-09-01 generators' bands summed to about 95 GW against about 52 GW of MAXAVAIL, because
bands are commonly spread across nameplate capacity.

Not modelled: AEMO's unit limits (availability with UIGF for wind and solar, ramp rates from
initial MW), interconnectors, losses, network constraints, FCAS, fast-start profiles. Without
interconnectors there is no way to tell regions apart, so all five regions get the same price.

Demand is the sum of regional TOTALDEMAND plus cleared scheduled load. Scheduled load
(pumping, battery charging) is held at what AEMO actually dispatched, because a supply-only
stack has no way to clear it. It is the only input held at an actual dispatch outcome.
"""
from __future__ import annotations

import pandas as pd

from nemsim.inputs import REGIONS, IntervalInputs
from nemsim.rungs.base import Rung
from nemsim.stack import cap_bands, clear


class BareMeritOrder(Rung):
    key = "r1"
    name = "Merit order, offers capped at declared availability"
    summary = ("One NEM-wide stack of final energy offers, each capped at its declared availability "
               "(MAXAVAIL), and nothing else: no AEMO unit limits, no network, no losses; "
               "scheduled load held at actual")
    uses = frozenset({"offers", "demand", "scheduled_load"})

    def solve(self, x: IntervalInputs) -> pd.DataFrame:
        gen = x.offers[x.offers.direction == "GEN"]
        mw = cap_bands(gen.duid.to_numpy(), gen.band.to_numpy(), gen.mw.to_numpy(), gen.maxavail.to_numpy())
        requirement = x.demand.totaldemand.sum() + x.scheduled_load.dispatchableload.sum()
        c = clear(gen.price.to_numpy(), mw, requirement, x.price_floor, x.price_cap)
        duid = gen.duid.iloc[c.marginal] if c.marginal is not None else None
        return pd.DataFrame({"region": list(REGIONS), "price": c.price, "marginal_duid": duid,
                             "bracket_lo": c.bracket_lo, "bracket_hi": c.bracket_hi, "status": c.status})
