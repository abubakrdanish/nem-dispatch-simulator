"""Score rungs on a set of intervals: python -m nemsim.evaluate r1 --set stage1

Prints the accuracy statistics overall and broken down by region, interconnector state,
constraint state and price class, and writes the region-interval rows and the report under
results/ (gitignored: the rows are AEMO figures).
"""
from __future__ import annotations

import argparse
import json
import time

import duckdb
import pandas as pd

from nemsim.config import ROOT, path
from nemsim.harness import BREAKDOWNS, evaluate, exclusions, summarise
from nemsim.intervals import SETS
from nemsim.rungs import LADDER
from nemsim.rungs.base import Rung


def report(result: pd.DataFrame, rung: Rung, set_name: str, seconds: float) -> str:
    fmt = lambda df: df.to_markdown(floatfmt=".1f")  # noqa: E731
    lines = [f"## {rung.key}: {rung.name}", "", rung.summary, "",
             f"Set `{set_name}`: {SETS[set_name].description}. Target: {result.attrs['target'].upper()}. "
             f"Ran in {seconds:.1f} s; median solve {result.attrs['solve_ms_median'] or 0:.2f} ms per interval.", "",
             "Region-intervals by outcome: " + ", ".join(f"{k} {v}" for k, v in exclusions(result).items()), "",
             "### Overall", "", fmt(summarise(result)), ""]
    for title, column in BREAKDOWNS.items():
        lines += [f"### By {title}", "", fmt(summarise(result, column)), ""]
    return "\n".join(lines)


def run(rung: Rung, set_name: str, target: str, con: duckdb.DuckDBPyConnection) -> tuple[pd.DataFrame, str]:
    start = time.monotonic()
    result = evaluate(con, rung, SETS[set_name].intervals(), target=target)
    text = report(result, rung, set_name, time.monotonic() - start)
    out = ROOT / "results"
    out.mkdir(exist_ok=True)
    stem = f"{set_name}__{rung.key}__{target}"
    result.to_csv(out / f"{stem}.csv", index=False)
    if getattr(rung, "coverage", None):   # rung 4: which invoked constraints were applied, and why not
        (out / f"{stem}__coverage.json").write_text(json.dumps(dict(rung.coverage), indent=1))
    (out / f"{stem}.md").write_text(text)
    return result, text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("rungs", nargs="+", choices=sorted(LADDER))
    parser.add_argument("--set", default="stage1", choices=sorted(SETS))
    parser.add_argument("--target", default="rrp", choices=["rrp", "rop"])
    args = parser.parse_args()
    with duckdb.connect(str(path("warehouse")), read_only=True) as con:
        for key in args.rungs:
            print(run(LADDER[key], args.set, args.target, con)[1])


if __name__ == "__main__":
    main()
