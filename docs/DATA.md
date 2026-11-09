# Data on disk: what is needed, what it costs, and what can be thrown away

A clone of this repository has none of the data. Everything below is gitignored and has to be rebuilt from AEMO's public archive (nemweb.com.au) and from a checkout of [nem-market-explainer](https://github.com/abubakrdanish/nem-market-explainer). No accounts or API keys are needed. Every request to AEMO goes through `nemsim/http_client.py`: one request at a time, at least 3 seconds apart, with the project's user agent (`config/settings.yml`).

**Footprint as of 2026-11-08, before any deletion:** **10.1 GB** inside the repository (raw 4.60 GB in `data/raw/aemo/`, 0.40 GB in `data/raw/aemo_prices/`, warehouses 4.08 + 0.10 GB, results 0.88 GB), plus the explainer's 2.1 GB read in place. **After stage 6b (2026-11-09): 8.6 GB**, with seventeen months loaded instead of ten (raw 0.11 GB in `data/raw/aemo/`, 0.40 GB in `data/raw/aemo_prices/`, warehouses 6.73 + 0.11 GB, results 1.02 GB). Details at the end of this file.

## What lives where

| Location | What | Size | Files | Cost to rebuild | Disposable? |
|---|---|---:|---:|---|---|
| `data/raw/aemo/` dispatch months: `BIDPEROFFER_D`, `BIDDAYOFFER_D`, `DISPATCHLOAD`, `DISPATCHCONSTRAINT` | The evaluation months, one zip per table-month, exactly as AEMO publishes them. **All deleted after validation on 2026-11-09**; the warehouse holds them | 4,499 MB for the original ten, 2,804 MB for the seven spike months; now 0 | 0 | **About 16 minutes** for all ten (984 s of downloading across four runs; 25–50 s per file) | **Yes, once the month is loaded and validated** (below) |
| `data/raw/aemo/` constraint definitions: `GENCONDATA`, `SPDCONNECTIONPOINTCONSTRAINT`, `SPDINTERCONNECTORCONSTRAINT`, `SPDREGIONCONSTRAINT` | The equation versions the seventeen months invoke, from the 148 months they took effect in, 2010-11 to 2026-05 (135 for the original ten; 13 more for the spike months) | about 101 MB | 552 | **37 minutes and 667 rate-limited requests**, including HTTP range requests into 1.9 GB whole-month archives for 2012–2014 | **No. Keep permanently.** Small, and the slowest thing in the project to rebuild |
| `data/raw/aemo/` standing tables: `LOSSMODEL`, `LOSSFACTORMODEL`, `INTERCONNECTORCONSTRAINT` | Interconnector loss model and ratings, full version history, from the 2025-09 file | 0.2 MB | 3 | Seconds | Not worth deleting |
| `data/raw/aemo_prices/` | `DISPATCHPRICE`, `DISPATCHREGIONSUM`, `TRADINGPRICE` for July 2016 to June 2021 (the decade measurement; later months are read from the explainer) | 399.7 MB | 180 | 14 minutes, 240 requests | Could be, but it is outside the categories cleared for deletion, so it is kept |
| `warehouse/sim.duckdb` | The evaluation warehouse: every table the rungs and harness read | 6.73 GB (4.08 GB with ten months) | 1 | About 5 minutes to load from raw, plus the definitions | **No.** It is the working copy, and once raw months are deleted it is the only local copy |
| `warehouse/prices.duckdb` | Decade prices and demand, four columns per table | 100 MB | 1 | 46 s from the raw files | No |
| `results/` | Scored region-intervals, reports, audits, validation | 1.02 GB | about 200 | Re-running the rungs, hours | No |
| `results/web/` | The frontend's caches: `prices.parquet` (every scored price in one file, rebuilt automatically when a result file is newer) and `detail/<day>` (modelled flows for precomputed days) | 50 MB | | Seconds for the price cache; about 30 s a day for flows | **Yes**: derived, and rebuilt on demand |
| `../nem-market-explainer/data/raw/aemo/` | Another project's downloads: `DISPATCHPRICE`, `DISPATCHREGIONSUM`, `DISPATCHINTERCONNECTORRES`, `TRADINGPRICE`, `DISPATCH_UNIT_SCADA` 2021-07 to 2026-09, `DUDETAILSUMMARY` and others | 2.1 GB | 319 | That project's own downloader | **Not this project's to delete.** Read in place, never written |

## When a dispatch month's raw files may be deleted

Only after `python -m nemsim.validate <month>` returns **pass**. It checks three things:

1. **Files:** every planned file for the month is recorded in `meta.loaded_files`. The loader already refuses any file whose row count differs from its closing `END OF REPORT` record.
2. **Intervals:** `DISPATCHLOAD`, `DISPATCHCONSTRAINT` and `BIDPEROFFER_D` each cover every 5-minute interval. A month's bid file holds its trading days, which run 04:05 to 04:00, so the bid check is shifted four hours.
3. **The 1 MW rebuild:** for every applied energy constraint-interval, the left-hand side rebuilt from the loaded unit dispatch, link flows and constraint definitions matches the LHS AEMO reports within 1 MW, in at least 99.9% of constraint-intervals. This ties four tables together, so a truncated or mis-parsed file shows up here.

**Results on the ten months (`results/validation.csv`):** eight pass, every one at 100.0% within 1 MW. Two show an **interval gap** instead:

- **2025-09:** `DISPATCHLOAD` has 8,400 of 8,640 intervals. The missing 20 hours are missing from AEMO's own file: the loader read every record the file declares, and on the intervals that are present the rebuild matches 100.0%.
- **2024-11:** `BIDPEROFFER_D` has no offers for the four intervals 13:30–13:45 on 19 November 2024. Searching AEMO's 3.2 GB uncompressed file directly finds no rows for them, and the dispatch price holds at exactly $76.00013 across those intervals.

In both cases the gap is in AEMO's published file, so the warehouse holds everything the file holds, and a re-download would restore nothing.

**The two gaps, recorded so that nothing later mistakes them for an ingest fault:**

| Month | Table | Missing | Intervals | Highest dispatch price in the gap (any region) | $300+ region-intervals in the gap |
|---|---|---|---:|---:|---:|
| 2025-09 | `DISPATCHLOAD` | 30 September 2025 04:05 to 1 October 2025 00:00, one contiguous block (the month's last trading day) | 240 | $177.70 | 0 |
| 2024-11 | `BIDPEROFFER_D` | 19 November 2024 13:30 to 13:45 | 4 | $88.70 | 0 |

So neither gap touches the spike band. Any rung that needs the missing table cannot be scored on those intervals; the harness excludes them as `no_inputs` (checked: all 1,200 region-intervals of the 2025-09 gap for rung 2, all 20 of the 2024-11 gap for rung 1).

**The deletion rule was about ingest integrity, not source completeness.** A month whose only failing check is a gap shown to be in AEMO's own file may be deleted like a passing month. On 2026-11-09 both gap months were deleted with the eight passing months, by agreement.

**What to do with a month that fails outright:** keep its raw files, find which check failed, and re-download only the failing table (the downloader skips everything else). If the re-downloaded file matches the old one byte for byte, the fault is in the ingest; fix it, reload the month (`delete from raw.<table> where _source_file = ...` and the matching `meta.loaded_files` row), and validate again. Never delete a failing month's raw files.

## Where the resume state lives

Three records say what has already been done, and none of them is inside a directory that gets cleaned:

| Record | Where | What it skips |
|---|---|---|
| `meta.loaded_files` | inside `warehouse/sim.duckdb` | **Loading:** a file already recorded is not loaded again. **Downloading:** since stage 6b, `nemsim.download` and `nemsim.add_month` also skip any file recorded here, whether or not its zip is still on disk. Before that change, the downloader decided only by whether the zip existed, so deleting raw months would have made the next `python -m nemsim.download` fetch all 4.5 GB again |
| `config/definition_months.txt` and `config/definition_months_unavailable.txt` | in the repository | The 148 definition months already fetched, and the 28 whole-month archives (2006-05 to 2008-08) that AEMO does not publish, so they are not asked for again. With the definitions loaded, `python -m nemsim.definitions` finds no missing versions and makes **0 requests**: that is the "second run makes zero requests" property, and it does not depend on any raw file |
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

## Stage 6b: what was deleted, what was added, and the footprint after

**Deleted on 2026-11-09, with agreement, each file by name** (`results/deleted_raw_stage6b.json` lists every one with its size). Before deleting, each file had to be recorded in `meta.loaded_files`, and its size on disk had to equal the compressed size recorded when it was loaded.

- The 40 dispatch zips of the original ten months: 4,499.2 MB. That is the 32 files (3,634 MB) of the eight passing months, and the 8 files (865 MB) of the two months whose only fault is the gap in AEMO's own file (above).
- The dispatch zips of the seven spike months, by `nemsim.add_month` after each passed validation: 2020-01 (7 files, 249.8 MB, including its own price, demand and flow tables), 2024-08 (411.2 MB), 2024-12 (434.4 MB), 2025-01 (430.6 MB), 2025-02 (384.3 MB), 2025-05 (437.2 MB) and 2025-07 (457.2 MB). Each of the last six was 4 files.

**Kept:** the constraint definitions, the standing tables, `data/raw/aemo_prices/`, both warehouses, all results, and everything in the explainer's directory.

**Validation of the seven** (`results/validation.csv`): all pass. 2020-01 is at 99.987% within 1 MW, and the other six at 100.0%. The first run of the six failed at 37–46%. That was a fault in `nemsim.add_month`, not the data: it loaded only the tables a month lists, and the price, demand and flow tables those months take from the explainer were never loaded. Without the flows, every constraint with an interconnector term fails the rebuild. The rule held, so no raw file was deleted on a fail. `add_month` now loads the explainer's monthly tables as `nemsim.ingest` does, and the re-run passed.

**Definitions that cannot be fetched.** Three equation versions took effect in 2008, before AEMO's monthly archive begins. Rung 4 reports these constraints as "no definition" and leaves them out:

- `T_V_NIL_BL1`, a Basslink constraint, effective 2008-05-26. It is invoked in every interval of 2020-01 and binds in 2.
- `SVML_000` and `VSML_000`, Murraylink limits, effective 2008-08-05. They are invoked on 16 December 2024, 13:05 to 16:00 (36 intervals). `SVML_000` binds in all 36, 4 of which have a region at $300 or more (the highest SA or Victorian price is $399).

The walk back to find them made 28 HEAD requests for unpublished archives (2006-05 to 2008-08). Those archives are now recorded in `config/definition_months_unavailable.txt`, so a second run makes 0 requests (checked).

**January 2020's older layout.** Bid files before 3 June 2024 have no `DIRECTION`. It is filled from each unit's registration. A scheduled load offers LOAD, except Wholesale Demand Response units (dispatch type LOAD, subtype WDR), which offer GEN. Everything else offers GEN. The WDR exception was found by testing the rule against the post-IESS offers that do carry `DIRECTION`. 27 WDR units offer GEN, and a rule on dispatch type alone would have called them LOAD. WDR began in October 2021, so January 2020 has none. With the exception, the rule matches every one of the 471 one-direction units in the loaded post-IESS offers (`tests/test_direction.py`). 2,711,207 offer rows were filled for 2020-01.

**Memory**, as the maximum resident set of the `add_month` process (macOS `ru_maxrss`), with DuckDB capped at 4 GB and spilling to `warehouse/duckdb_tmp/`:

| Step | Peak over the seven months |
|---|---:|
| Load | 4.94 GB (2024-12) |
| Validate | 5.95 GB (2025-05) |
| Rung 4, per month (`/usr/bin/time -l`) | 1.74 GB |

For comparison, the original bulk load peaked at about 8 GB and the original validation run at 7.9 GB. The memory cap holds DuckDB's own buffers to 4 GB, and the rest is Python and pandas.

**Footprint after:**

| Location | Before stage 6b | After |
|---|---:|---:|
| `data/raw/aemo/` | 4.60 GB | 0.11 GB |
| `data/raw/aemo_prices/` | 0.40 GB | 0.40 GB |
| `warehouse/sim.duckdb` | 4.08 GB | 6.73 GB |
| `warehouse/prices.duckdb` | 0.10 GB | 0.11 GB |
| `results/` | 0.88 GB | 1.02 GB |
| Repository total | 10.1 GB | 8.6 GB |

**Resume state, checked after every deletion.** `python -m nemsim.download --limit-mb 0` plans 0 files. `python -m nemsim.ingest` loads nothing. `python -m nemsim.definitions` makes 0 requests.
