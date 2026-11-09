# Data on disk: what is needed, what it costs, and what can be thrown away

A clone of this repository has none of the data. Everything below is gitignored and has to be rebuilt from AEMO's public archive (nemweb.com.au) and from a checkout of [nem-market-explainer](https://github.com/abubakrdanish/nem-market-explainer). No accounts or API keys are needed. Every request to AEMO goes through `nemsim/http_client.py`: one request at a time, at least 3 seconds apart, with the project's user agent (`config/settings.yml`).

**Footprint as of 2026-11-08, before any deletion:** **10.1 GB** inside the repository (raw 4.60 GB in `data/raw/aemo/`, 0.40 GB in `data/raw/aemo_prices/`, warehouses 4.08 + 0.10 GB, results 0.88 GB), plus the explainer's 2.1 GB read in place. The footprint after cleaning is recorded at the end of this file once it has been done.

## What lives where

| Location | What | Size | Files | Cost to rebuild | Disposable? |
|---|---|---:|---:|---|---|
| `data/raw/aemo/` dispatch months: `BIDPEROFFER_D`, `BIDDAYOFFER_D`, `DISPATCHLOAD`, `DISPATCHCONSTRAINT` | The ten evaluation months, one zip per table-month, exactly as AEMO publishes them | 4,499 MB | 40 | **About 16 minutes** for all ten (984 s of downloading across four runs; 25–50 s per file) | **Yes, once the month is loaded and validated** (below) |
| `data/raw/aemo/` constraint definitions: `GENCONDATA`, `SPDCONNECTIONPOINTCONSTRAINT`, `SPDINTERCONNECTORCONSTRAINT`, `SPDREGIONCONSTRAINT` | The equation versions the ten months invoke, from the 135 months they took effect in, 2012-07 to 2026-05 | 100.8 MB | 506 | **37 minutes and 667 rate-limited requests**, including HTTP range requests into 1.9 GB whole-month archives for 2012–2014 | **No. Keep permanently.** Small, and the slowest thing in the project to rebuild |
| `data/raw/aemo/` standing tables: `LOSSMODEL`, `LOSSFACTORMODEL`, `INTERCONNECTORCONSTRAINT` | Interconnector loss model and ratings, full version history, from the 2025-09 file | 0.2 MB | 3 | Seconds | Not worth deleting |
| `data/raw/aemo_prices/` | `DISPATCHPRICE`, `DISPATCHREGIONSUM`, `TRADINGPRICE` for July 2016 to June 2021 (the decade measurement; later months are read from the explainer) | 399.7 MB | 180 | 14 minutes, 240 requests | Could be, but it is outside the categories cleared for deletion, so it is kept |
| `warehouse/sim.duckdb` | The evaluation warehouse: every table the rungs and harness read | 4.08 GB | 1 | About 5 minutes to load from raw, plus the definitions | **No.** It is the working copy, and once raw months are deleted it is the only local copy |
| `warehouse/prices.duckdb` | Decade prices and demand, four columns per table | 100 MB | 1 | 46 s from the raw files | No |
| `results/` | Scored region-intervals, reports, audits, validation | 875 MB | 170 | Re-running the rungs, hours | No |
| `../nem-market-explainer/data/raw/aemo/` | Another project's downloads: `DISPATCHPRICE`, `DISPATCHREGIONSUM`, `DISPATCHINTERCONNECTORRES`, `TRADINGPRICE`, `DISPATCH_UNIT_SCADA` 2021-07 to 2026-09, `DUDETAILSUMMARY` and others | 2.1 GB | 319 | That project's own downloader | **Not this project's to delete.** Read in place, never written |

## When a dispatch month's raw files may be deleted

Only after `python -m nemsim.validate <month>` returns **pass**. It checks three things:

1. **Files:** every planned file for the month is recorded in `meta.loaded_files`. The loader already refuses any file whose row count differs from its closing `END OF REPORT` record.
2. **Intervals:** `DISPATCHLOAD`, `DISPATCHCONSTRAINT` and `BIDPEROFFER_D` each cover every 5-minute interval. A month's bid file holds its trading days, which run 04:05 to 04:00, so the bid check is shifted four hours.
3. **The 1 MW rebuild:** for every applied energy constraint-interval, the left-hand side rebuilt from the loaded unit dispatch, link flows and constraint definitions matches the LHS AEMO reports within 1 MW, in at least 99.9% of constraint-intervals. This ties four tables together, so a truncated or mis-parsed file shows up here.

**Results on the ten months (`results/validation.csv`):** eight pass, every one at 100.0% within 1 MW. Two show an **interval gap** instead:

- **2025-09:** `DISPATCHLOAD` has 8,400 of 8,640 intervals. The missing 20 hours are missing from AEMO's own file: the loader read every record the file declares, and on the intervals that are present the rebuild matches 100.0%.
- **2024-11:** `BIDPEROFFER_D` has no offers for the four intervals 13:30–13:45 on 19 November 2024. Searching AEMO's 3.2 GB uncompressed file directly finds no rows for them, and the dispatch price holds at exactly $76.00013 across those intervals.

In both cases the gap is in AEMO's published file, so the warehouse holds everything the file holds. Deleting the raw zip loses nothing that a re-download would restore, but the rule is that only passing months are deleted, so these two stay until that is decided explicitly.

**What to do with a month that fails outright:** keep its raw files, find which check failed, and re-download only the failing table (the downloader skips everything else). If the re-downloaded file matches the old one byte for byte, the fault is in the ingest; fix it, reload the month (`delete from raw.<table> where _source_file = ...` and the matching `meta.loaded_files` row), and validate again. Never delete a failing month's raw files.

## Where the resume state lives

Three records say what has already been done, and none of them is inside a directory that gets cleaned:

| Record | Where | What it skips |
|---|---|---|
| `meta.loaded_files` | inside `warehouse/sim.duckdb` | **Loading:** a file already recorded is not loaded again. **Downloading:** since stage 6b, `nemsim.download` and `nemsim.add_month` also skip any file recorded here, whether or not its zip is still on disk. Before that change, the downloader decided only by whether the zip existed, so deleting raw months would have made the next `python -m nemsim.download` fetch all 4.5 GB again |
| `config/definition_months.txt` | in the repository | The 135 definition months already fetched. With the definitions loaded, `python -m nemsim.definitions` finds no missing versions and makes **0 requests**: that is the "second run makes zero requests" property, and it does not depend on any raw file |
| The zips themselves | `data/raw/aemo/` | Before loading, a zip on disk is not downloaded again |

So deleting a validated month's zips is safe: `python -m nemsim.download` will not fetch it again, and `python -m nemsim.ingest` skips a table-month whose files are gone if the warehouse records it as loaded (it is an error only if it was never loaded). Deleting `warehouse/sim.duckdb` would lose all three kinds of state at once; that is why it is never deleted.

## Rebuilding from nothing

```
python -m nemsim.download --limit-mb 4700   # dispatch months, about 16 minutes; refuses plans over 1,000 MB without the flag
python -m nemsim.ingest                     # about 5 minutes; reads the explainer's price, demand and flow files in place
python -m nemsim.definitions                # 135 months, 667 requests, 101 MB, about 37 minutes
python -m nemsim.validate                   # every month: files, intervals, the 1 MW rebuild
python -m nemsim.decade fetch               # 400 MB, about 14 minutes (July 2016 to June 2021)
python -m nemsim.decade load                # under a minute, into warehouse/prices.duckdb
```

To add or rebuild one month at a time, keeping memory and disk low:

```
python -m nemsim.add_month 2024-12 --delete-raw
```

It downloads the month, loads it with DuckDB capped at 4 GB and spilling to `warehouse/duckdb_tmp/`, fetches any missing constraint definitions, validates, and only on a pass deletes that month's dispatch zips, each by name. It appends what it did, with peak memory and disk, to `results/add_month_log.jsonl`.

## What cannot be rebuilt cheaply

AEMO's monthly archive carries the final-offer tables (`BIDPEROFFER_D`, `BIDDAYOFFER_D`) only up to February 2021 and from August 2024. For March 2021 to July 2024 it has only the full rebid history: 4.3 GB compressed for February 2024 and 7.4 GB for May 2024, against about 0.15 GB for a final-offer month. This project never downloads the rebid history, so those months, which include the 2022 energy crisis and 62% of the decade's $300+ settled value, cannot be evaluated here.
