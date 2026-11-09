"""Load the downloaded files, and the explainer's raw files for the same months, into this
project's DuckDB file: python -m nemsim.ingest

Files already loaded are skipped, so running it again adds nothing. The explainer's files are
only read.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date

import duckdb

from nemsim.config import path, settings
from nemsim.mmsdm import ensure_schema, files_for, load_file
from nemsim.tables import TABLES

log = logging.getLogger(__name__)


def plan(done: frozenset[str] = frozenset()) -> list[tuple[str, object]]:
    """(table, file) pairs to load, in order.

    A table-month whose raw files were deleted after a validated ingest is skipped when the warehouse
    records it as loaded (`done`, from meta.loaded_files); it is an error only if it was never loaded.
    """
    cfg = settings()
    own, explainer = path("raw_dir"), path("explainer_raw_dir")
    jobs = []
    for month_text, tables in cfg["downloads"].items():
        month = date.fromisoformat(month_text + "-01")
        for name in tables:
            found = files_for(own, name, month)
            if not found and any(f.startswith(f"{name.lower()}_{month:%Y%m}_") for f in done):
                continue
            if not found:
                raise FileNotFoundError(f"{name} {month_text} is not downloaded; run python -m nemsim.download")
            jobs += [(name, f) for f in found]
    for month_text in [*cfg["downloads"], *cfg["explainer_only_months"]]:
        month = date.fromisoformat(month_text + "-01")
        for name in cfg["explainer_monthly"]:
            if name in cfg["downloads"].get(month_text, []):   # fetched here: the explainer does not hold that month
                continue
            found = files_for(explainer, name, month)
            if not found:
                raise FileNotFoundError(f"{name} {month_text} is not in {explainer}")
            jobs += [(name, f) for f in found]
    for name in cfg["explainer_standing"]:
        found = files_for(explainer, name, None)
        if not found:
            raise FileNotFoundError(f"{name} is not in {explainer}")
        jobs += [(name, f) for f in found]
    return jobs


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    target = path("warehouse")
    target.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    report = []
    with duckdb.connect(str(target)) as con:
        ensure_schema(con)
        for name, file in plan(frozenset(r[0] for r in con.execute("select file from meta.loaded_files").fetchall())):
            result = load_file(con, TABLES[name], file)
            log.info("%s", result)
            report.append({"table": name, **result})
        con.execute("checkpoint")
    print(json.dumps({"files": report, "seconds": round(time.monotonic() - start, 1),
                      "warehouse_mb": round(target.stat().st_size / 1e6, 1)}, indent=1))


if __name__ == "__main__":
    main()
