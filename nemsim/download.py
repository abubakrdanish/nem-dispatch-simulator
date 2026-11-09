"""Download the planned AEMO archive files: python -m nemsim.download [--limit-mb 1000]"""
from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import date

from nemsim.config import path, settings
from nemsim.http_client import PoliteClient
from nemsim.mmsdm import download, loaded_files


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit-mb", type=float, default=1000,
                        help="refuse to start if the files still to fetch exceed this (default 1000)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cfg = settings()
    plan = {date.fromisoformat(m + "-01"): tables for m, tables in cfg["downloads"].items()}
    client = PoliteClient(cfg["nemweb"]["user_agent"], cfg["nemweb"]["min_request_gap_seconds"])
    start = time.monotonic()
    report = download(client, cfg["nemweb"]["archive_url"], plan, path("raw_dir"), int(args.limit_mb * 1e6),
                      done=loaded_files(path("warehouse")))
    total = sum(r["bytes"] for r in report)
    print(json.dumps({"files": report, "total_mb": round(total / 1e6, 1),
                      "seconds": round(time.monotonic() - start, 1)}, indent=1))


if __name__ == "__main__":
    main()
