"""A five-region dispatch linear program, solved with HiGHS.

Minimise the cost of the offers dispatched, subject to each region's demand being met by its own
generation plus net imports, and to any generic constraints supplied. Each regional price is the dual
of that region's balance row: the cost of one more MW of demand there.

    variables   x_b   MW from offer band b, 0 <= x_b <= the band's MW after the unit's upper limit
                f_i   MW on interconnector i, positive from its first region to its second,
                      -import_limit_i <= f_i <= export_limit_i
                d_r   unmet demand in region r, priced at the market price cap
                s_r   surplus in region r (forced generation with nowhere to go), priced at -floor
                v_k   violation of generic constraint k, priced at its penalty
    rows        for each region r:  sum(x_b in r) + sum(f_i into r) - sum(f_i out of r)
                                    - (r's share of each link's losses) + d_r - s_r = D_r
                for each unit u with a lower limit:  sum(x_b of u) >= L_u
                for each generic constraint k:  sum(a_ku * unit u's MW) + sum(a_ki * f_i)  (<=, >=, =)  RHS_k

Losses on link i are linear in its flow here, loss_i = alpha_i + beta_i * f_i, shared between its two
regions in the proportion share_i (the "from" region's share) : 1 - share_i. A caller with a nonlinear
loss function linearises it and re-solves (see rungs/r3b_flows_solved.py). With no loss terms the
links are lossless.

The deficit, surplus and violation columns make every interval feasible; when a deficit or surplus is
used the price lands at the cap or floor.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import highspy
import numpy as np


@dataclass(frozen=True)
class Constraint:
    units: dict[str, float]        # DUID -> coefficient on the unit's energy MW
    links: dict[str, float]        # interconnector -> coefficient on its flow
    sense: str                     # "<=", ">=" or "="
    rhs: float
    penalty: float                 # $/MW of violation
    name: str = ""


@dataclass(frozen=True)
class Solution:
    prices: dict[str, float]       # region -> dual of its balance row, clipped to [floor, cap]
    flows: dict[str, float]        # interconnector -> MW
    dispatch: np.ndarray           # MW per band, aligned with the band arrays passed in
    deficit: dict[str, float]
    surplus: dict[str, float]
    violated: list[str] = field(default_factory=list)   # generic constraints violated by more than 0.1 MW


def solve(regions: list[str], band_region: np.ndarray, band_price: np.ndarray, band_mw: np.ndarray,
          band_unit: np.ndarray, unit_floor: dict[str, float], demand: dict[str, float],
          links: list[tuple[str, str, str, float, float]], floor: float, cap: float,
          losses: list[tuple[float, float, float]] | None = None,
          constraints: list[Constraint] | None = None) -> Solution:
    """links: (id, from_region, to_region, import_limit, export_limit), limits as positive MW.
    losses: per link (alpha, beta, from_share), or None for lossless links.
    constraints: generic constraints, or None."""
    losses = losses or [(0.0, 0.0, 0.5)] * len(links)
    constraints = constraints or []
    nb, nl, nr = len(band_price), len(links), len(regions)
    ridx = {r: k for k, r in enumerate(regions)}
    lidx = {lk[0]: k for k, lk in enumerate(links)}
    floors = [(u, lo) for u, lo in unit_floor.items() if lo > 1e-6]
    bands_of = defaultdict(list)
    for k, u in enumerate(band_unit):
        bands_of[u].append(k)

    rows_i, cols_i, vals = [], [], []

    def put(r, c, v):
        rows_i.append(r); cols_i.append(c); vals.append(v)

    # Region balances.
    for k in range(nb):
        put(ridx[band_region[k]], k, 1.0)
    for k, ((_, a, b, _, _), (_, beta, share)) in enumerate(zip(links, losses)):
        put(ridx[a], nb + k, -1.0 - share * beta)
        put(ridx[b], nb + k, 1.0 - (1 - share) * beta)
    ncol = nb + nl
    deficit_col, surplus_col = ncol, ncol + nr
    for k in range(nr):
        put(k, deficit_col + k, 1.0)
        put(k, surplus_col + k, -1.0)
    ncol += 2 * nr
    rhs = np.array([demand[r] for r in regions], dtype=float)
    for (_, a, b, _, _), (alpha, _, share) in zip(links, losses):
        rhs[ridx[a]] += share * alpha          # the constant part of each link's losses is extra demand
        rhs[ridx[b]] += (1 - share) * alpha
    row_lo, row_hi = list(rhs), list(rhs)
    nrow = nr

    # Unit lower limits.
    for u, lo in floors:
        for k in bands_of[u]:
            put(nrow, k, 1.0)
        row_lo.append(lo); row_hi.append(highspy.kHighsInf)
        nrow += 1

    # Generic constraints, each with violation slack priced at its penalty.
    cost_extra, ub_extra, viol_cols = [], [], []
    for c in constraints:
        for u, a in c.units.items():
            for k in bands_of.get(u, ()):
                put(nrow, k, a)
        for i, a in c.links.items():
            put(nrow, nb + lidx[i], a)
        if c.sense in ("<=", "="):
            put(nrow, ncol, -1.0); cost_extra.append(c.penalty); viol_cols.append((ncol, c.name)); ncol += 1
        if c.sense in (">=", "="):
            put(nrow, ncol, 1.0); cost_extra.append(c.penalty); viol_cols.append((ncol, c.name)); ncol += 1
        row_lo.append(c.rhs if c.sense in (">=", "=") else -highspy.kHighsInf)
        row_hi.append(c.rhs if c.sense in ("<=", "=") else highspy.kHighsInf)
        nrow += 1

    cost = np.concatenate([band_price, np.zeros(nl), np.full(nr, cap), np.full(nr, -floor), cost_extra])
    lower = np.concatenate([np.zeros(nb), [-imp for *_, imp, _ in links], np.zeros(2 * nr + len(cost_extra))])
    upper = np.concatenate([np.maximum(band_mw, 0), [exp for *_, exp in links],
                            np.full(2 * nr + len(cost_extra), highspy.kHighsInf)])

    # Triplets to column-wise sparse.
    cols_a, rows_a, vals_a = np.array(cols_i), np.array(rows_i, dtype=np.int32), np.array(vals)
    order = np.argsort(cols_a, kind="stable")
    starts = np.zeros(ncol + 1, dtype=np.int32)
    np.add.at(starts, cols_a + 1, 1)
    starts = np.cumsum(starts).astype(np.int32)

    lp = highspy.HighsLp()
    lp.num_col_, lp.num_row_ = ncol, nrow
    lp.col_cost_, lp.col_lower_, lp.col_upper_ = cost, lower, upper
    lp.row_lower_, lp.row_upper_ = np.array(row_lo, dtype=float), np.array(row_hi, dtype=float)
    lp.a_matrix_.format_ = highspy.MatrixFormat.kColwise
    lp.a_matrix_.start_ = starts
    lp.a_matrix_.index_ = rows_a[order]
    lp.a_matrix_.value_ = vals_a[order]
    h = highspy.Highs()
    h.setOptionValue("output_flag", False)
    h.passModel(lp)
    h.run()
    if h.getModelStatus() != highspy.HighsModelStatus.kOptimal:
        raise RuntimeError(f"dispatch LP not optimal: {h.modelStatusToString(h.getModelStatus())}")
    sol = h.getSolution()
    col, dual = np.array(sol.col_value), np.array(sol.row_dual)
    clip = lambda v: float(min(max(v, floor), cap))  # noqa: E731
    return Solution(
        prices={r: clip(dual[ridx[r]]) for r in regions},
        flows={links[k][0]: float(col[nb + k]) for k in range(nl)},
        dispatch=col[:nb],
        deficit={r: float(col[deficit_col + k]) for k, r in enumerate(regions)},
        surplus={r: float(col[surplus_col + k]) for k, r in enumerate(regions)},
        violated=sorted({name for c_, name in viol_cols if col[c_] > 0.1}))
