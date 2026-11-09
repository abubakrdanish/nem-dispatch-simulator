"""Clear a merit-order stack: cheapest offers first until the requirement is met."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Clearing:
    price: float
    marginal: int | None   # index into the input arrays of the band that sets the price
    bracket_lo: float      # price of the last band dispatched
    bracket_hi: float      # price of the next band up; any price between the two clears the same MW
    status: str            # "marginal", "short" (requirement above the stack) or "long" (nothing needed)


def clear(price: np.ndarray, mw: np.ndarray, requirement: float, floor: float, cap: float) -> Clearing:
    """Price of the band that meets `requirement`, clipped to [floor, cap].

    When the stack is short the price is the cap; when nothing is needed it is the floor. A
    requirement that lands exactly on a band boundary takes the price of the band that reaches it.
    """
    price = np.asarray(price, dtype=float)
    mw = np.asarray(mw, dtype=float)
    keep = mw > 0
    index = np.flatnonzero(keep)
    if requirement <= 1e-9:
        return Clearing(floor, None, floor, float(price[keep].min()) if keep.any() else cap, "long")
    order = index[np.argsort(price[index], kind="stable")]
    cumulative = np.cumsum(mw[order])
    if cumulative.size == 0 or cumulative[-1] < requirement - 1e-9:
        top = float(price[order[-1]]) if order.size else cap
        return Clearing(cap, None, top, cap, "short")
    i = int(np.searchsorted(cumulative, requirement - 1e-9, side="left"))
    p = float(price[order[i]])
    nxt = float(price[order[i + 1]]) if i + 1 < order.size else cap
    clip = lambda v: float(min(max(v, floor), cap))  # noqa: E731
    return Clearing(clip(p), int(order[i]), clip(p), clip(nxt), "marginal")


def cap_bands(duid: np.ndarray, band: np.ndarray, mw: np.ndarray, limit: np.ndarray) -> np.ndarray:
    """MW of each band once a unit's bands are filled in band order up to `limit` for that unit.

    Arrays are aligned, one element per band. A unit offering 300 MW of bands with a limit of
    200 MW keeps its first 200 MW of bands; the rest become 0.
    """
    order = np.lexsort((band, duid))
    d, m, lim = duid[order], mw[order].astype(float), np.maximum(limit[order].astype(float), 0)
    starts = np.r_[True, d[1:] != d[:-1]]
    group = np.cumsum(starts) - 1
    cum = np.cumsum(m)
    base = (cum - m)[starts][group]          # cumulative MW before each unit's first band
    upto = cum - base
    capped = np.clip(upto, 0, lim) - np.clip(upto - m, 0, lim)
    out = np.empty_like(capped)
    out[order] = capped
    return out
