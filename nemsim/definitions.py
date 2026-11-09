"""Fetch only the constraint definitions rung 4 needs: python -m nemsim.definitions

DISPATCHCONSTRAINT names, for every invoked constraint in every interval, the version of its equation
in force (GENCONID_EFFECTIVEDATE, GENCONID_VERSIONNO). The equations are in four monthly archive
tables (GENCONDATA, SPDCONNECTIONPOINTCONSTRAINT, SPDINTERCONNECTORCONSTRAINT, SPDREGIONCONSTRAINT),
and each monthly file holds the versions created that month, which can be years before the sample.

So instead of fetching every month back to 2012, this walks backwards. It tries each needed version's
effective month first; for versions still missing, it steps back one month at a time; it stops when
nothing is missing or MAX_MONTHS_BACK is reached. Every file goes through the same polite client and
the same idempotent loader as the monthly tables, so an interrupted run resumes. The months fetched
are written to config/definition_months.txt, and that file is what a later run reads.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date

import duckdb

from nemsim.config import ROOT, path, settings
from nemsim.http_client import PoliteClient
from nemsim.mmsdm import download, ensure_schema, files_for, load_file
from nemsim.tables import TABLES

log = logging.getLogger(__name__)

DEFINITION_TABLES = ["GENCONDATA", "SPDCONNECTIONPOINTCONSTRAINT", "SPDINTERCONNECTORCONSTRAINT",
                     "SPDREGIONCONSTRAINT"]
MAX_MONTHS_BACK = 24
MONTHS_FILE = ROOT / "config" / "definition_months.txt"


def _month(d) -> date:
    return date(d.year, d.month, 1)


def _back(m: date) -> date:
    return date(m.year - (m.month == 1), 12 if m.month == 1 else m.month - 1, 1)


def missing_versions(con) -> list[tuple]:
    """Versions referenced by an invoked constraint whose GENCONDATA row is not loaded yet."""
    have = "exists (select 1 from information_schema.tables where table_schema = 'raw' and table_name = 'gencondata')"
    loaded = con.execute(f"select {have}").fetchone()[0]
    sql = """select distinct d.constraintid, d.genconid_effectivedate, d.genconid_versionno
             from raw.dispatchconstraint d where d.intervention = 0 and d.genconid_effectivedate is not null"""
    if loaded:
        sql += """ and not exists (select 1 from raw.gencondata g where g.genconid = d.constraintid
                   and g.effectivedate = d.genconid_effectivedate and g.versionno = d.genconid_versionno)"""
    return con.execute(sql).fetchall()


WHOLE_ZIP_UNTIL = date(2014, 12, 1)   # up to here the archive holds each month only as one ~1.9 GB zip


def fetch_whole_zip_month(client: PoliteClient, archive_url: str, month: date) -> list[dict]:
    """Pull the definition tables out of a whole-month zip by range requests, saving each like a folder file."""
    from nemsim.remote_zip import central_directory, fetch_member, find
    url = f"{archive_url}{month:%Y}/MMSDM_{month:%Y_%m}.zip"
    _, entries = central_directory(client, url)
    out = []
    for table in DEFINITION_TABLES:
        target = path("raw_dir") / f"{table.lower()}_{month:%Y%m}_01.zip"
        if target.exists():
            continue
        for e in find(entries, table):
            data = fetch_member(client, url, e)
            target.write_bytes(data)
            out.append({"table": table, "month": f"{month:%Y-%m}", "bytes": len(data), "via": "range"})
    return out


def load_months(con, months: list[date]) -> list[dict]:
    out = []
    for m in months:
        for name in DEFINITION_TABLES:
            for f in files_for(path("raw_dir"), name, m):
                out.append({"table": name, **load_file(con, TABLES[name], f)})
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cfg = settings()
    client = PoliteClient(cfg["nemweb"]["user_agent"], cfg["nemweb"]["min_request_gap_seconds"])
    start = time.monotonic()
    fetched: set[date] = set(date.fromisoformat(line.strip() + "-01") for line in MONTHS_FILE.read_text().split()
                             if line.strip()) if MONTHS_FILE.exists() else set()
    report, downloaded, steps = [], [], []
    with duckdb.connect(str(path("warehouse"))) as con:
        ensure_schema(con)
        load_months(con, sorted(fetched))
        # Each version is tried at its effective month, then one month earlier per round.
        offset = 0
        while True:
            missing = missing_versions(con)
            steps.append({"round": offset, "missing_versions": len(missing)})
            if not missing or offset > MAX_MONTHS_BACK:
                break
            want = set()
            for _, eff, _ in missing:
                m = _month(eff)
                for _ in range(offset):
                    m = _back(m)
                want.add(m)
            want -= fetched
            if want:
                plan = {m: DEFINITION_TABLES for m in sorted(want) if m > WHOLE_ZIP_UNTIL}
                downloaded += download(client, cfg["nemweb"]["archive_url"], plan, path("raw_dir"),
                                       int(200e6), required=False)
                for m in sorted(w for w in want if w <= WHOLE_ZIP_UNTIL):
                    downloaded += fetch_whole_zip_month(client, cfg["nemweb"]["archive_url"], m)
                fetched |= want
                report += load_months(con, sorted(want))
                MONTHS_FILE.write_text("\n".join(f"{m:%Y-%m}" for m in sorted(fetched)) + "\n")
            offset += 1
        con.execute("checkpoint")
        still = missing_versions(con)
    print(json.dumps({"months_fetched": len(fetched), "requests": client.requests, "files_downloaded": len(downloaded),
                      "mb_downloaded": round(sum(d["bytes"] for d in downloaded) / 1e6, 1),
                      "rounds": steps, "versions_still_missing": len(still),
                      "seconds": round(time.monotonic() - start, 1)}, indent=1))


if __name__ == "__main__":
    main()
