"""What rung 3 does to the ceiling, and what the dispatch-calculated limits would add:
python -m nemsim.diagnose_r3 <what>

  ceiling   the best score any lossless interconnector model could reach, (a) if it separated
            regions exactly where real links were full and (b) given the price zones rung 3 itself
            produced; and how often rung 3 separates any region at all
  lossless, noramps, calculated-limits
            3b variants, each scored on every month and saved as results/diagnostic__<key>.csv: without
            losses, without ramp limits, and with dispatch-calculated limits (the ceiling probe)
  leak      rung 3 with each link bounded by the limits the dispatch run calculated
            (DISPATCHINTERCONNECTORRES) instead of its standing rating. Those limits are an outcome:
            they come from the constraints binding at dispatch's own solution, and can cross. A
            diagnostic, never a rung. Crossed limits are widened to [min, max] so the LP stays
            feasible.

Needs rung 3's results for the eight month sets first.
"""
from __future__ import annotations

import argparse
import json

import duckdb
import numpy as np
import pandas as pd

from nemsim.config import ROOT, path
from nemsim.harness import LIMIT_TOLERANCE_MW, evaluate, summarise
from nemsim.inputs import INTERCONNECTORS, REGIONS
from nemsim.intervals import MONTHS, SETS
from nemsim.rungs.r3b_flows_solved import FlowsSolved

COLS = ["region_intervals", "within_$1", "within_$5", "within_$10", "within_$20",
        "median_abs_error", "mean_abs_error", "mean_error"]


def _results(key: str) -> pd.DataFrame:
    r = pd.concat([pd.read_csv(ROOT / "results" / f"{m}__{key}__rrp.csv", low_memory=False, parse_dates=["interval"])
                   for m in MONTHS])
    return r[r.excluded.isna()].copy()


def _zones(open_links: list[tuple[str, str]]) -> dict[str, str]:
    """Union regions joined by a link that is not full; returns region -> zone label."""
    parent = {r: r for r in REGIONS}

    def find(r):
        while parent[r] != r:
            r = parent[r]
        return r
    for a, b in open_links:
        parent[find(a)] = find(b)
    return {r: find(r) for r in REGIONS}


def _best_within(prices: np.ndarray, tol: float = 5) -> int:
    candidates = np.concatenate([prices - tol, prices, prices + tol])
    return max(int((np.abs(prices - c) <= tol).sum()) for c in candidates)


def _zone_ceiling(r: pd.DataFrame, zone_of: dict) -> float:
    """Share within $5 if each zone of each interval got the best single price for it."""
    hit = 0
    for t, g in r.groupby("interval"):
        zones = zone_of[t]
        for _, zg in g.groupby(g.region.map(zones)):
            hit += _best_within(zg.rrp.to_numpy())
    return hit / len(r) * 100


def ceiling(con) -> None:
    r3 = _results("r3")
    ic = con.execute(f"""select settlementdate as interval, interconnectorid,
            exportlimit < importlimit
            or (case when mwflow >= 0 then exportlimit - mwflow else mwflow - importlimit end) < {LIMIT_TOLERANCE_MW}
            as at_limit
        from raw.dispatchinterconnectorres where intervention = 0""").df()
    actual = {t: _zones([INTERCONNECTORS[i] for i in g.interconnectorid[~g.at_limit]])
              for t, g in ic.groupby("interval")}
    actual = {t: z for t, z in actual.items() if t in set(r3.interval)}
    own = {}
    for t, g in r3.groupby("interval"):
        p = g.set_index("region").price
        # In a lossless LP two neighbours share a price unless every link between them is full.
        own[t] = _zones([(a, b) for a, b in set(INTERCONNECTORS.values()) if abs(p[a] - p[b]) < 0.01])
    n_zones = pd.Series({t: len(set(z.values())) for t, z in own.items()})
    n_actual = pd.Series({t: len(set(z.values())) for t, z in actual.items()})
    one = {t: {r: "NEM" for r in REGIONS} for t in own}
    out = {"single_price": _zone_ceiling(r3, one), "lossless_actual_zones": _zone_ceiling(r3, actual),
           "lossless_rung3_zones": _zone_ceiling(r3, own)}
    (ROOT / "results" / "ceilings.json").write_text(json.dumps(out, indent=1))
    print(f"Single price for the whole NEM (rungs 1 and 2): {out['single_price']:.1f}%")
    print(f"Lossless, separating exactly where real links were full: {out['lossless_actual_zones']:.1f}%")
    print(f"Lossless, with the zones rung 3 produced: {out['lossless_rung3_zones']:.1f}%")
    print(f"Rung 3 separates at least one region in {(n_zones > 1).mean() * 100:.1f}% of intervals; "
          f"real links leave more than one zone in {(n_actual > 1).mean() * 100:.1f}%")
    print("Zones per interval, rung 3:", n_zones.value_counts(normalize=True).sort_index().mul(100).round(1).to_dict())
    print("Zones per interval, real full links:", n_actual.value_counts(normalize=True).sort_index().mul(100).round(1).to_dict())


class _CalculatedLimits(FlowsSolved):
    key = "r3b_leak_calculated_limits"
    uses = FlowsSolved.uses | {"interconnectors"}

    def links(self, x):
        ic = x.interconnectors
        lo = np.minimum(ic.importlimit, ic.exportlimit)
        hi = np.maximum(ic.importlimit, ic.exportlimit)
        return list(zip(ic.interconnectorid, ic.from_region, ic.to_region, -lo, hi))


def leak(con) -> None:
    base = _results("r3")
    res = pd.concat([evaluate(con, _CalculatedLimits(), SETS[m].intervals()) for m in MONTHS])
    res.to_csv(ROOT / "results" / "diagnostic__r3_leak_calculated_limits.csv", index=False)
    res = res[res.excluded.isna()]
    k = ["interval", "region"]
    both = base[k].merge(res[k])
    print(pd.DataFrame({"rung 3 (standing ratings)": summarise(base.merge(both))[COLS].iloc[0],
                        "leak: dispatch-calculated limits": summarise(res.merge(both))[COLS].iloc[0]}).T
          .round(1).to_markdown())
    print()
    print(summarise(res.merge(both), "region")[COLS].round(1).to_markdown())


def variants(con, which: str) -> None:
    """Score one 3b variant on every month and save it as results/diagnostic__<key>.csv."""
    model = {"lossless": FlowsSolved(losses=False), "noramps": FlowsSolved(ramps=False),
             "calculated-limits": _CalculatedLimits()}[which]
    res = pd.concat([evaluate(con, model, SETS[m].intervals()) for m in MONTHS])
    res.to_csv(ROOT / "results" / f"diagnostic__{model.key}.csv", index=False)
    print(f"wrote results/diagnostic__{model.key}.csv")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("what", choices=["ceiling", "leak", "lossless", "noramps", "calculated-limits"])
    args = parser.parse_args()
    with duckdb.connect(str(path("warehouse")), read_only=True) as con:
        if args.what == "ceiling":
            ceiling(con)
        elif args.what == "leak":
            leak(con)
        else:
            variants(con, args.what)


if __name__ == "__main__":
    main()


def flows(con, every: int = 12, model=None) -> pd.DataFrame:
    """A rung's solved flows and losses against the actual ones, on every `every`-th interval of each month.

    Works for rung 3b (the default) and rung 4, which keep their last solution in `.last`.
    """
    from nemsim.inputs import Window
    model = model or FlowsSolved()
    rows = []
    for m in MONTHS:
        iv = SETS[m].intervals()[::every]
        for day_start in sorted({t.replace(hour=0, minute=0) for t in iv}):
            todo = [t for t in iv if day_start <= t < day_start + pd.Timedelta(days=1)]
            w = Window(con, min(todo), max(todo), model.uses | {"interconnectors"})
            have = set.intersection(*(w.intervals_with(u) for u in model.uses))
            for t in todo:
                if pd.Timestamp(t) not in have:
                    continue
                x = w.at(t)
                model.solve(x)
                act = x.interconnectors.set_index("interconnectorid")
                for i, f, loss in zip(model.last["links"], model.last["flows"], model.last["losses"]):
                    rows.append({"interval": t, "interconnectorid": i, "flow_model": f, "loss_model": loss,
                                 "calc_import": min(act.importlimit[i], act.exportlimit[i]),
                                 "calc_export": max(act.importlimit[i], act.exportlimit[i])})
    out = pd.DataFrame(rows)
    actual = con.execute("""select settlementdate as interval, interconnectorid, mwflow, mwlosses
                            from raw.dispatchinterconnectorres where intervention = 0""").df()
    return out.merge(actual, on=["interval", "interconnectorid"])


def fault_summary(f: pd.DataFrame) -> dict:
    """The three faults stage 4 diagnosed in 3b: counter-flow on parallel paths, loss size, link overload."""
    w = f.pivot(index="interval", columns="interconnectorid", values="flow_model")
    a = f.pivot(index="interval", columns="interconnectorid", values="mwflow")

    def loop(d, x, y):
        return ((np.sign(d[x]) != np.sign(d[y])) & (d[x].abs() > 10) & (d[y].abs() > 10)).mean() * 100
    over = (f.flow_model > f.calc_export + 1) | (f.flow_model < f.calc_import - 1)
    tot = f.groupby("interval")[["loss_model", "mwlosses"]].sum()
    return {"QLD counter-flow, model %": loop(w, "NSW1-QLD1", "N-Q-MNSP1"),
            "QLD counter-flow, actual %": loop(a, "NSW1-QLD1", "N-Q-MNSP1"),
            "SA counter-flow, model %": loop(w, "V-SA", "V-S-MNSP1"),
            "SA counter-flow, actual %": loop(a, "V-SA", "V-S-MNSP1"),
            "losses per interval, model MW": tot.loss_model.mean(), "losses per interval, actual MW": tot.mwlosses.mean(),
            "link-intervals beyond dispatch's calculated limits %": over.mean() * 100,
            "by link beyond limits %": over.groupby(f.interconnectorid).mean().mul(100).round(1).to_dict()}
