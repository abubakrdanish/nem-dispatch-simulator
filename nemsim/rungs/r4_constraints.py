"""Rung 4: network constraints. Rung 3b plus every invoked generic constraint it can represent.

**Inputs only.** Which constraints are invoked in an interval, and their right-hand sides, are set
before the solve from system conditions, so they are inputs; they come from DISPATCHCONSTRAINT
(CONSTRAINTID, GENCONID_EFFECTIVEDATE, GENCONID_VERSIONNO, RHS). The equations come from the
definition tables: the type (<=, >=, =) and weight from GENCONDATA, and the left-hand-side terms from
SPDCONNECTIONPOINTCONSTRAINT (unit terms), SPDINTERCONNECTORCONSTRAINT (link terms) and
SPDREGIONCONSTRAINT (region terms). Not read, because the solve produces them: MARGINALVALUE,
VIOLATIONDEGREE and LHS in DISPATCHCONSTRAINT, and the calculated interconnector limits. The rung is
told which constraints exist, not which bind.

Every invoked constraint is applied as an equation in the LP unless the model cannot represent it:

- **no definition**: its equation version was not found in the definition tables;
- **FCAS terms**: a unit term with a bid type other than ENERGY. FCAS is not modelled, and dropping
  those terms would change the constraint, so the whole constraint is left out;
- **region terms**: SPDREGIONCONSTRAINT terms, which refer to regional quantities the LP does not
  have as variables;
- **no terms**: an equation with nothing on its left-hand side.

A unit term for a unit with no energy offer in the LP (a non-scheduled unit, or a load held at actual)
is dropped and its MW counted as zero; counts are reported by `coverage`. Each applied constraint can be
violated at a penalty of its GENCONDATA weight (35 when missing) times the market price cap, so it gives
way only when nothing else can. Ratings, losses and ramp limits are as in rung 3b.
"""
from __future__ import annotations

from collections import Counter

import pandas as pd

from nemsim import lp
from nemsim.inputs import INTERCONNECTORS, IntervalInputs
from nemsim.rungs.r3b_flows_solved import FlowsSolved

DEFAULT_WEIGHT = 35.0


def classify(terms: pd.DataFrame) -> tuple[str, dict[str, float], dict[str, float]]:
    """One constraint version's equation: (status, unit coefficients, link coefficients)."""
    if terms.empty:
        return "no terms", {}, {}
    kind, bidtype, target, factor = (terms[c].to_numpy() for c in ("kind", "bidtype", "target", "factor"))
    if (kind == "region").any():
        return "region terms", {}, {}
    if ((kind == "unit") & (bidtype != "ENERGY")).any():
        return "FCAS terms", {}, {}
    units, links = {}, {}
    for k, tg, f in zip(kind, target, factor):
        if k == "link":
            if tg not in INTERCONNECTORS:
                return "unknown link", {}, {}
            links[tg] = links.get(tg, 0.0) + float(f)
        else:
            units[tg] = units.get(tg, 0.0) + float(f)
    return "ok", units, links


def build(x: IntervalInputs, gen_units: set[str], cache: dict | None = None) -> tuple[list[lp.Constraint], Counter]:
    """The constraints rung 4 applies this interval, and why the rest were left out.

    Equations depend only on the constraint version, so they are classified once and cached; only the
    RHS, and which units have an energy offer, change from interval to interval.
    """
    cache = {} if cache is None else cache
    why = Counter()
    keys = list(zip(x.constraints.constraintid, x.constraints.eff, x.constraints.ver))
    new = {k for k in keys if k not in cache}
    if new:
        t = x.constraint_terms
        t = t[[k in new for k in zip(t.constraintid, t.eff, t.ver)]]
        grouped = {k: g for k, g in t.groupby(["constraintid", "eff", "ver"])}
        for k in new:
            cache[k] = classify(grouped.get(k, t.iloc[0:0]))
    out = []
    for key, sense, rhs, weight in zip(keys, x.constraints.sense, x.constraints.rhs, x.constraints.weight):
        if sense is None or pd.isna(sense):
            why["no definition"] += 1
            continue
        status, units, links = cache[key]
        if status != "ok":
            why[status] += 1
            continue
        kept = {u: a for u, a in units.items() if u in gen_units}
        why["unit terms dropped (no energy offer)"] += len(units) - len(kept)
        if not kept and not links:
            why["no representable terms"] += 1
            continue
        w = weight if weight and weight > 0 else DEFAULT_WEIGHT
        out.append(lp.Constraint(units=kept, links=links, sense=str(sense).strip(), rhs=float(rhs),
                                 penalty=w * x.price_cap, name=key[0]))
        why["applied"] += 1
    return out, why


class Constraints(FlowsSolved):
    key = "r4"
    name = "Network constraints: rung 3b plus every invoked generic constraint it can represent"
    summary = ("Rung 3b with the invoked generic constraints applied as equations: their RHS (set before the "
               "solve) from DISPATCHCONSTRAINT, their terms from the definition tables. Never reads marginal "
               "values, binding flags or calculated limits")
    uses = FlowsSolved.uses | {"constraints"}

    def __init__(self):
        super().__init__()
        self.coverage = Counter()
        self._cache: dict = {}

    def generic_constraints(self, x: IntervalInputs, gen: pd.DataFrame) -> list[lp.Constraint]:
        out, why = build(x, set(gen.duid), self._cache)
        self.coverage.update(why)
        return out
