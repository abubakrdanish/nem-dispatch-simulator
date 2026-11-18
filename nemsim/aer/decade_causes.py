"""What the AER attributes the decade's spikes to: python -m nemsim.aer.decade_causes

Descriptive, and about what the AER said, not about the market: "the AER attributed X% of events to wind".

- **Span.** FY2017 to FY2026 (1 July 2016 to 30 June 2026), the decade warehouse/prices.duckdb holds
  (nemsim/decade.py: DISPATCHPRICE, DISPATCHREGIONSUM, intervention = 0).
- **Reportable day-region.** A region on a calendar day (interval start, i.e. settlement time minus 5 minutes)
  on which some 30-minute price exceeded $5,000/MWh. The 30-minute price is the mean of the six 5-minute
  dispatch prices in the half-hour, the AER's own definition (its reports, "Obligation"). From 2026 the AER
  stopped using a fixed threshold; the definition here does not change with it.
- **Settled value of a day-region.** |RRP x TOTALDEMAND / 12| summed over the 5-minute intervals in its
  half-hours above $5,000: the 5-minute basis the harness uses throughout. Nominal dollars.
- **AER event.** A (date, region) named by any extracted event in any collected report, with the union of its
  usable labels (results/aer_accuracy.csv) across reports. A labelled event outside the reportable set
  (for example an FCAS-only day, or an event reported under the 2026 criteria below $5,000) is counted
  separately and carries no value.
- **Shares.** Per usable label: the share of AER-labelled reportable day-regions carrying it, and the share of
  their settled value. Labels overlap, so shares do not sum to 100%.

Every share is printed beside the label's measured precision and recall on the hand-coded sample.
Writes results/aer_decade_causes.csv.
"""
from __future__ import annotations

import json

import duckdb
import pandas as pd

import sys

from nemsim.aer.accuracy import OUT as ACCURACY, usable_labels
from nemsim.aer.extract import PRIMARY, labels_file
from nemsim.config import ROOT
from nemsim.decade import frame as decade_frame

OUT = ROOT / "results" / "aer_decade_causes.csv"
FIRST, LAST = "2016-07-01", "2026-06-30"
THRESHOLD = 5000.0


def reportable() -> pd.DataFrame:
    with duckdb.connect(str(ROOT / "warehouse" / "prices.duckdb"), read_only=True) as con:
        f = decade_frame(con)
    f["p30"] = f.groupby(["regionid", "tt"]).rrp.transform("mean")
    f = f[f.p30 > THRESHOLD].copy()
    f["date"] = (f.t - pd.Timedelta(minutes=5)).dt.strftime("%Y-%m-%d")
    f["value"] = (f.rrp * f.totaldemand / 12).abs()
    g = f.groupby(["date", "regionid"]).agg(value=("value", "sum"), half_hours=("tt", "nunique"),
                                             peak_30min=("p30", "max")).reset_index()
    return g.rename(columns={"regionid": "region"})


def aer_day_regions(config: str = PRIMARY) -> pd.DataFrame:
    ev = json.loads(labels_file(config).read_text())["events"]
    keep = usable_labels(config)
    rows = [{"date": e["event_date"], "region": r, "causes": {c["label"] for c in e["causes"]} & keep,
             "network_statement": e["network_outages"]["value"]} for e in ev for r in e["regions"]]
    d = pd.DataFrame(rows)
    return d.groupby(["date", "region"]).agg(
        causes=("causes", lambda s: set().union(*s)),
        network_statement=("network_statement", lambda s: "contributed" if "contributed" in set(s)
                           else "did_not_contribute" if "did_not_contribute" in set(s) else "not_stated")).reset_index()


def run(config: str = PRIMARY) -> pd.DataFrame:
    rep = reportable()
    aer = aer_day_regions(config)
    aer = aer[(aer.date >= FIRST) & (aer.date <= LAST)]
    x = rep.merge(aer, how="left", on=["date", "region"])
    x["labelled"] = x.causes.notna()
    x["causes"] = x.causes.apply(lambda c: c if isinstance(c, set) else set())
    acc = pd.read_csv(ACCURACY)
    acc = acc[acc.model == config].set_index("label")
    lab = x[x.labelled]
    rows = [{"group": "reportable day-regions (30-min price > $5,000)", "day_regions": len(x), "value_$m": x.value.sum() / 1e6},
            {"group": "  with an AER event", "day_regions": len(lab), "value_$m": lab.value.sum() / 1e6,
             "share_of_reportable_%": len(lab) / len(x) * 100, "value_share_of_reportable_%": lab.value.sum() / x.value.sum() * 100},
            {"group": "AER events not in the reportable set", "day_regions": len(aer.merge(rep, how="left", on=["date", "region"], indicator=True)
                                                                                 .query("_merge == 'left_only'"))}]
    for label in sorted(usable_labels(config)):
        s = lab[lab.causes.apply(lambda c: label in c)]
        rows.append({"group": f"AER attributed to {label}", "day_regions": len(s), "value_$m": s.value.sum() / 1e6,
                     "share_of_labelled_%": len(s) / len(lab) * 100, "value_share_of_labelled_%": s.value.sum() / lab.value.sum() * 100,
                     "precision": acc.loc[label, "precision"], "recall": acc.loc[label, "recall"]})
    s = lab[lab.causes.apply(len) == 0]
    rows.append({"group": "AER event, no usable label", "day_regions": len(s), "value_$m": s.value.sum() / 1e6,
                 "share_of_labelled_%": len(s) / len(lab) * 100, "value_share_of_labelled_%": s.value.sum() / lab.value.sum() * 100})
    for v in ("contributed", "did_not_contribute", "not_stated"):
        s = lab[lab.network_statement == v]
        rows.append({"group": f"AER statement on network outages: {v}", "day_regions": len(s), "value_$m": s.value.sum() / 1e6,
                     "share_of_labelled_%": len(s) / len(lab) * 100, "value_share_of_labelled_%": s.value.sum() / lab.value.sum() * 100})
    lab = lab.assign(fy="FY" + (pd.to_datetime(lab.date).dt.year + (pd.to_datetime(lab.date).dt.month >= 7)).astype(str))
    xfy = x.assign(fy="FY" + (pd.to_datetime(x.date).dt.year + (pd.to_datetime(x.date).dt.month >= 7)).astype(str))
    for fy, g in xfy.groupby("fy"):
        lg = g[g.labelled]
        rows.append({"group": f"coverage {fy}", "day_regions": len(g), "value_$m": g.value.sum() / 1e6,
                     "share_of_reportable_%": len(lg) / len(g) * 100,
                     "value_share_of_reportable_%": lg.value.sum() / g.value.sum() * 100 if g.value.sum() else float("nan")})
    out = pd.DataFrame(rows).assign(config=config)
    out.to_csv(OUT if config == PRIMARY else OUT.with_name(f"aer_decade_causes__{config}.csv"), index=False)
    with pd.option_context("display.width", 250, "display.max_columns", 20, "display.max_rows", 100):
        print(out.round(2).to_string(index=False))
    return out


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else PRIMARY)
