"""AEMO MMSDM monthly archive: find and download table files, then load them into DuckDB.

The patterns follow nem-market-explainer's ingestion/aemo.py: read each month's listing,
save files unchanged, check each file's closing record count, and read columns by name
because AEMO adds columns between table versions.

Timestamps stay as published: naive NEM market time (AEST, UTC+10 all year). For dispatch
tables SETTLEMENTDATE is the end of the 5-minute interval. In BIDPEROFFER_D and
BIDDAYOFFER_D, SETTLEMENTDATE is the trading day and INTERVAL_DATETIME is the interval end.
"""
from __future__ import annotations

import html
import logging
import re
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

import duckdb

from nemsim.http_client import PoliteClient

log = logging.getLogger(__name__)


def month_url(archive_url: str, month: date) -> str:
    return urljoin(archive_url, f"{month:%Y}/MMSDM_{month:%Y_%m}/MMSDM_Historical_Data_SQLLoader/DATA/")


def table_links(listing: str, table: str) -> list[tuple[str, str, int | None]]:
    """(href, part, size in bytes) for one table in a month's DATA listing.

    Up to 2024-07 files are PUBLIC_DVD_<TABLE>_<YYYYMM>010000.zip; from 2024-08 they are
    PUBLIC_ARCHIVE#<TABLE>#FILE<nn>#<YYYYMM>010000.zip, and large tables run over several parts.
    """
    links = []
    for size, href in re.findall(r'(?:(\d+)\s+)?<A HREF="([^"]+\.zip)"', listing, re.I):
        name = html.unescape(href).replace("%23", "#").rsplit("/", 1)[-1]
        nbytes = int(size) if size else None
        if re.fullmatch(rf"PUBLIC_DVD_{table}_\d{{12}}\.zip", name):
            links.append((href, "01", nbytes))
        elif m := re.fullmatch(rf"PUBLIC_ARCHIVE#{table}#FILE(\d\d)#\d{{12}}\.zip", name):
            links.append((href, m.group(1), nbytes))
    return sorted(links, key=lambda link: link[1])


def saved_name(table: str, month: date, part: str) -> str:
    return f"{table.lower()}_{month:%Y%m}_{part}.zip"


def download(client: PoliteClient, archive_url: str, plan: dict[date, list[str]], directory: Path,
             limit_bytes: int, required: bool = True, done: frozenset[str] = frozenset()) -> list[dict]:
    """Fetch every planned table-month not yet saved. Refuses to start if the plan exceeds limit_bytes.

    A file counts as fetched if it is on disk or its name is in `done`, the files the warehouse has
    already loaded (meta.loaded_files). That is the resume state once raw files are deleted after
    ingest: without it, a later run would fetch every deleted file again.
    With required=False a table missing from a month's listing is skipped instead of being an error
    (older months do not always carry every definition table).
    """
    directory.mkdir(parents=True, exist_ok=True)
    todo = []
    for month, tables in sorted(plan.items()):
        listing = client.get(month_url(archive_url, month)).decode()
        for table in tables:
            links = table_links(listing, table)
            if not links:
                if not required:
                    continue
                raise RuntimeError(f"{table} is not in the {month:%Y-%m} archive listing")
            for href, part, size in links:
                target = directory / saved_name(table, month, part)
                if not target.exists() and target.name not in done:
                    todo.append((table, month, urljoin(archive_url, href), target, size or 0))
    total = sum(t[4] for t in todo)
    log.info("%d files to fetch, %.1f MB", len(todo), total / 1e6)
    if total > limit_bytes:
        raise RuntimeError(f"Planned download is {total / 1e6:.0f} MB, over the {limit_bytes / 1e6:.0f} MB limit. "
                           "Raise --limit-mb only after agreeing it.")
    report = []
    for table, month, url, target, _ in todo:
        start = time.monotonic()
        size = client.download(url, target)
        seconds = time.monotonic() - start
        log.info("%s %s: %.1f MB in %.0f s", table, f"{month:%Y-%m}", size / 1e6, seconds)
        report.append({"table": table, "month": f"{month:%Y-%m}", "bytes": size, "seconds": round(seconds, 1)})
    return report


# ---------------------------------------------------------------------------------------------
# Loading

TIMESTAMP_FORMAT = "%Y/%m/%d %H:%M:%S"


@dataclass(frozen=True)
class Table:
    name: str
    columns: dict[str, str]          # AEMO column -> DuckDB type
    where: str = "true"              # filter applied while loading, in terms of the raw text columns
    optional: frozenset[str] = field(default_factory=frozenset)   # may be absent in older layouts

    @property
    def raw_table(self) -> str:
        return f"raw.{self.name.lower()}"


def _select_list(table: Table, present: set[str]) -> str:
    parts = []
    for column, kind in table.columns.items():
        if column not in present:
            parts.append(f"null::{'varchar' if kind == 'varchar' else kind} as {column.lower()}")
            continue
        value = f"nullif(\"{column}\", '')"
        if kind == "timestamp":
            value = f"strptime({value}, '{TIMESTAMP_FORMAT}')"
        elif kind != "varchar":
            value = f"{value}::{kind}"
        parts.append(f"{value} as {column.lower()}")
    return ", ".join(parts)


def _header(csv_path: Path) -> tuple[list[str], int]:
    """Column names from the I record, and the record count from the closing C record."""
    with csv_path.open("rb") as f:
        head = f.read(65536).decode()
        f.seek(max(0, csv_path.stat().st_size - 200))
        tail = f.read().decode()
    header = next(line for line in head.splitlines() if line.startswith("I,")).strip().split(",")
    if not all(re.fullmatch(r"[A-Z0-9_]+", c) for c in header[4:]):
        raise ValueError(f"{csv_path.name}: unexpected column names in {header}")
    total = re.search(r'C,"END OF REPORT",(\d+)\s*$', tail)
    if not total:
        raise ValueError(f"{csv_path.name}: no END OF REPORT record, so the file is incomplete")
    return ["record", "category", "report", "version", *header[4:]], int(total.group(1))


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("create schema if not exists raw")
    con.execute("create schema if not exists meta")
    con.execute("""create table if not exists meta.loaded_files (file varchar primary key, tablename varchar,
        rows_in_file bigint, rows_loaded bigint, compressed_bytes bigint, seconds double, loaded_at timestamp)""")


def load_file(con: duckdb.DuckDBPyConnection, table: Table, path: Path) -> dict:
    """Load one saved zip in one transaction, unless it is already loaded. Returns a summary."""
    done = con.execute("select rows_loaded from meta.loaded_files where file = ?", [path.name]).fetchone()
    if done:
        return {"file": path.name, "skipped": True, "rows_loaded": done[0]}
    start = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(path) as archive:
        [member] = archive.namelist()
        csv_path = Path(archive.extract(member, tmp))
        names, total = _header(csv_path)
        missing = [c for c in table.columns if c not in names and c not in table.optional]
        if missing:
            raise ValueError(f"{path.name}: columns {missing} not in this file's {table.name} layout")
        columns = "{" + ", ".join(f"'{n}': 'VARCHAR'" for n in names) + "}"
        # strict_mode is off because the opening C record has more fields than the table; the
        # record count check below catches any rows the reader would otherwise drop.
        con.execute(f"""create or replace temp table incoming as select * from read_csv(?, header=false,
            auto_detect=false, delim=',', quote='"', escape='"', null_padding=true, strict_mode=false,
            parallel=false, columns={columns})""", [str(csv_path)])
        read, headers = con.execute(
            "select count(*), count(*) filter (where record = 'I') from incoming").fetchone()
        if read != total:
            raise ValueError(f"{path.name}: read {read} records but the file says {total}")
        if headers != 1:
            raise ValueError(f"{path.name}: {headers} I records; expected one table layout per file")
        select = _select_list(table, set(names))
        con.execute("begin")
        try:
            con.execute(f"create table if not exists {table.raw_table} as select {select}, "
                        "''::varchar as _source_file from incoming where false")
            loaded = con.execute(f"insert into {table.raw_table} select {select}, ? from incoming "
                                 f"where record = 'D' and ({table.where})", [path.name]).fetchone()[0]
            seconds = time.monotonic() - start
            con.execute("insert into meta.loaded_files values (?, ?, ?, ?, ?, ?, now())",
                        [path.name, table.name, read - 3, loaded, path.stat().st_size, seconds])
            con.execute("commit")
        except Exception:
            con.execute("rollback")
            raise
        finally:
            con.execute("drop table if exists incoming")
    return {"file": path.name, "rows_in_file": read - 3, "rows_loaded": loaded,
            "compressed_mb": round(path.stat().st_size / 1e6, 1), "seconds": round(seconds, 1)}


def files_for(directory: Path, table: str, month: date | None) -> list[Path]:
    """Saved zips for one table-month, newest snapshot per part. month=None: the newest month's file.

    This project saves <table>_<YYYYMM>_<part>.zip; the explainer saves
    <table>_<YYYYMM>_<part>_<YYYYMMDD>.zip and can hold several snapshots of a republished month.
    """
    pattern = re.compile(rf"{table.lower()}_(\d{{6}})_(\d\d)(?:_(\d{{8}}))?\.zip")
    found = {}
    for p in directory.glob(f"{table.lower()}_*.zip"):
        if m := pattern.fullmatch(p.name):
            key = (m.group(1), m.group(2))
            if key not in found or (m.group(3) or "") > found[key][0]:
                found[key] = (m.group(3) or "", p)
    if not found:
        return []
    wanted = f"{month:%Y%m}" if month else max(k[0] for k in found)
    return [p for (mon, _), (_, p) in sorted(found.items()) if mon == wanted]


def loaded_files(warehouse: Path) -> frozenset[str]:
    """Names of every raw file the warehouse has loaded: the resume state for downloads."""
    if not warehouse.exists():
        return frozenset()
    with duckdb.connect(str(warehouse), read_only=True) as con:
        has = con.execute("select count(*) from information_schema.tables where table_schema = 'meta' "
                          "and table_name = 'loaded_files'").fetchone()[0]
        return frozenset(r[0] for r in con.execute("select file from meta.loaded_files").fetchall()) if has else frozenset()
