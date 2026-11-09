# NEM dispatch simulator: stage 1 research

Research only. No application code was written and nothing was committed. Checked on 2026-10-20.

> **Corrections, 2026-11-08.** Building stage 2 against the data found errors in this document. They are corrected in place below. Each is marked **[Corrected 2026-11-08]**, says what the text said before, and points to the evidence in [stage2_results.md](stage2_results.md), sections 2, 10 and 11. In short:
>
> 1. **Loss factors.** Band prices are scaled by TLF × DLF (the marginal loss factor), not by TLF alone. Divide by TLF × DLF. The TLF-only rule misprices embedded units, 77 of 433 offering generators on 2025-09-01, by up to about $70/MWh at the floor.
> 2. **The 28% figure** comes from regional stacks with AEMO's unit limits and interconnector flows fixed at actual. It is not a plain merit order, which scores 0% within $5.
> 3. **"No unit bounds"** in section 4 still capped each offer at its declared availability (MAXAVAIL).
> 4. **Test case 4.** Heywood is at its export limit; its limits are not crossed.
> 5. **Energy offer directions** are GEN and LOAD only. BIDIRECTIONAL appears on FCAS offers.
> 6. **The demand definition**, left open in section 3, is settled: TOTALDEMAND = DISPATCHABLEGENERATION − DISPATCHABLELOAD − NETINTERCHANGE.
> 7. **The appendix scripts** no longer exist; the session scratchpad was cleared. The four simple models of section 4 are rebuilt in `nemsim/reproduce.py`, and they reproduce this document's figures to within about a point.
>
> Nothing else was changed. Claims not listed here were either confirmed (see `python -m nemsim.cases`) or not tested.

## How to read this document

Every factual claim carries one of four labels:

| Label | Meaning |
|---|---|
| **[V-data]** | Verified today against the warehouse, the raw files on disk, or a file sampled from NEMWEB. The query or script is listed in the appendix. |
| **[V-doc]** | Verified today against a document I read directly, with the source numbered in [Sources](#sources). |
| **[J]** | My judgement or inference. It may be well founded, but I did not verify it. |
| **[U]** | Unverified. I could not check it and say what is missing. |

**Short answer.** Reproducing AEMO's price is feasible, and an open-source tool (nempy) has already shown it can be done to within a few dollars. It cannot be done with a plain merit-order stack. On a real day of bids, a plain merit order got 0% of region-intervals within $5 of the actual price. Even with AEMO's unit limits added and interconnector flows fixed at their actual values, regional stacks got only 28%, and network constraints bind in nearly every interval. **[Corrected 2026-11-08]** This sentence said the 28% was "the simple stack"; it is the best of the four simple variants in section 4, not a plain merit order (stage2_results.md, section 11). A two-week build can deliver a five-region linear program with real bids, unit limits, interconnectors and losses, plus an evaluation that measures how much error each missing piece adds. It cannot also deliver FCAS, the interface and the map.

---

## 1. What the warehouse already holds

The source is `nem-market-explainer/warehouse/nem.duckdb` (3.3 GB), opened read-only. **[V-data]**

### Tables this project would use

| Need | Table | Rows | Coverage (interval end, NEM time) |
|---|---|---:|---|
| 5-minute regional price, flags | `raw.aemo_dispatchprice` | 2,779,355 | 2021-07-01 00:05 to 2026-10-01 00:00 |
| Settlement price | `raw.aemo_tradingprice` | 2,651,520 | 2021-07-01 00:30 to 2026-10-01 00:00 |
| Demand, available generation, net interchange, regional UIGF | `raw.aemo_dispatchregionsum` | 2,779,355 | 2021-07-01 00:05 to 2026-10-01 00:00 |
| Interconnector flow, losses, calculated limits | `raw.aemo_dispatchinterconnectorres` | 3,335,226 | 2021-07-01 00:05 to 2026-10-01 00:00 |
| Unit output | `raw.aemo_dispatch_unit_scada` | 250,980,379 | 2021-07-01 00:05 to 2026-10-01 00:00 |
| Unit registration (region, type, dates) | `raw.aemo_dudetailsummary` | 23,361 | standing table; start dates 1998-10-25 to 2026-10-09 |
| Fuel and capacity | `raw.aemo_genunits`, `raw.aemo_dualloc` | 982; 3,068 | standing |
| Market price cap and floor | `raw.aemo_market_price_thresholds` | 19 | effective 1997-11-04 to 2026-07-01 |
| Region × interval mart | `marts.mart_region_interval` | 2,761,920 | 2021-07-01 to 2026-10-01 |
| Interconnector × interval mart | `marts.mart_interconnector_interval` | 3,314,304 | same |
| Daily mart, notable intervals, events | `mart_region_daily`, `mart_notable_intervals`, `mart_events` | 9,590; 31,056; 694 | events run to 2027-12-14 |

### Already on disk but not loaded

The raw zips under `data/raw/aemo/` hold many more columns than the pipeline reads. Several of them matter here. **[V-data]**, from the header rows of the 2025-01 files:

- **DISPATCHPRICE:** `ROP` (the price before overrides such as administered capping), RRP and ROP for all ten FCAS services (`RAISE1SECRRP` … `LOWERREGRRP`), and the `PRE_AP_*` prices.
- **DISPATCHREGIONSUM:** `DISPATCHABLELOAD`, `AGGREGATEDISPATCHERROR`, `CLEAREDSUPPLY`, the FCAS requirement and local-dispatch columns, and `SS_SOLAR_UIGF` / `SS_WIND_UIGF`.
- **DISPATCHINTERCONNECTORRES:** `MARGINALLOSS`, `EXPORTGENCONID` / `IMPORTGENCONID` (the binding constraint behind each limit), and `FCASEXPORTLIMIT` / `FCASIMPORTLIMIT`.
- **DUDETAILSUMMARY:** `CONNECTIONPOINTID`, `TRANSMISSIONLOSSFACTOR`, `MIN_ENERGY_PRICE` / `MAX_ENERGY_PRICE`, `MAX_RAMP_RATE_UP` / `DOWN`, and `SECONDARY_TLF`.

### Missing

| Missing | Archive table | Why it matters |
|---|---|---|
| Offers | `BIDDAYOFFER_D`, `BIDPEROFFER_D` | The offer stack itself |
| Unit dispatch solution | `DISPATCHLOAD` | Per-unit availability (including UIGF for wind and solar), initial MW, cleared MW, ramp rates used, FCAS enablement. This is both an input and the validation target |
| Network constraints | `DISPATCHCONSTRAINT`, `GENCONDATA`, `SPDCONNECTIONPOINTCONSTRAINT`, `SPDINTERCONNECTORCONSTRAINT`, `SPDREGIONCONSTRAINT`, `GENERICCONSTRAINTRHS` | Constraint coefficients, per-interval right-hand sides, which constraints bound |
| Interconnector loss model | `LOSSMODEL`, `LOSSFACTORMODEL` | Loss segments and demand coefficients |
| Administered pricing events | `APEVENT`, `APEVENTREGION` | Which region and which market was capped, and when |
| MNSP offers | `MNSP_BIDOFFERPERIOD`, `MNSP_DAYOFFER` | Basslink is offered as a market link |
| Rooftop PV | `ROOFTOP_PV_ACTUAL` | Needed later for weather sliders |
| Weather, unit coordinates | none in MMSDM | `DUDETAILSUMMARY` has no latitude or longitude **[V-data]**. Weather and location need sources outside AEMO's archive **[U]** |

---

## 2. Bid data

### It exists, and these are the tables

AEMO publishes every unit's actual price and quantity offers in the monthly MMSDM archive. **[V-data]**, from the 2025-09 listing and sampled files; **[V-doc]** [1][2].

| Table | Grain | What it holds |
|---|---|---|
| `BIDDAYOFFER_D` | unit × bid type × direction × trading day | `PRICEBAND1`–`PRICEBAND10`, `DAILYENERGYCONSTRAINT`, fast-start profile `T1`–`T4`, `MINIMUMLOAD`, `REBIDEXPLANATION`, `PARTICIPANTID` |
| `BIDPEROFFER_D` | unit × bid type × direction × 5-minute interval | `BANDAVAIL1`–`BANDAVAIL10`, `MAXAVAIL`, `FIXEDLOAD`, `ROCUP` / `ROCDOWN`, FCAS trapezium (`ENABLEMENTMIN/MAX`, `LOW/HIGHBREAKPOINT`), `PASAAVAILABILITY` |
| `BIDOFFERPERIOD` (from 2024-08), `BIDPEROFFER` (before) | every rebid version | Full rebid history: every offer a participant submitted |
| `BIDDAYOFFER` | every rebid version of the daily part | |

AEMO describes `BIDPEROFFER_D` as "the public summary of the energy and FCAS offers applicable in the Dispatch for the intervals identified" [2]. The `_D` tables are what a simulator needs. The full history is only needed to study rebidding.

### Granularity

All of the following is **[V-data]**, from a sample of 2025-09-01.

- **Ten price bands.** Prices are fixed for the trading day per unit, bid type and direction. Only the MW in each band changes interval by interval.
- **One row per unit, bid type, direction and 5-minute interval.** No duplicates were found in 432,158 rows. On that day the sample covered 524 units and about 1,500 rows per interval. About 433 units had an energy generation offer in each interval.
- **Eleven bid types:** `ENERGY` plus ten FCAS services (raise and lower for 1 second, 6 seconds, 60 seconds, 5 minutes and regulation).
- **Three directions:** `GEN`, `LOAD` and `BIDIRECTIONAL`, but energy offers use only `GEN` and `LOAD`. `BIDIRECTIONAL` appears only on FCAS bid types, and batteries offer energy as separate `GEN` and `LOAD` rows. **[Corrected 2026-11-08]** This said batteries offer both sides without noting that energy offers never use `BIDIRECTIONAL` (stage2_results.md, section 2). AEMO's web page for `BIDPEROFFER_D` does not list the `DIRECTION` column, but the file has it [2].
- **The September 2025 daily file holds 529 units and 241 participants.**
- **Band prices are stored multiplied by the unit's marginal loss factor, TLF × DLF**, so they must be divided by TLF × DLF to compare them with the regional price. **[Corrected 2026-11-08]** This said TLF alone. That holds only where DLF = 1, which is true of the four units below, all transmission-connected. Of the 136 units with DLF ≠ 1, 134 have `MINIMUM_ENERGY_PRICE` = −1,000 × TLF × DLF, and none has −1,000 × TLF. Example: WSTWYSF1 bids −859.31 = −1,000 × 0.922 × 0.932, where dividing by TLF alone gives −932. On 2025-09-01, 77 of 433 offering generators had DLF ≠ 1 (stage2_results.md, section 2). Original evidence: BW01's bands run from −963.7 to 19,563.11. Its 2025-26 TLF is 0.9637 and its DLF is 1, and `DUDETAILSUMMARY` gives its `MIN/MAX_ENERGY_PRICE` as exactly −963.7 and 19,563.11, which are the floor (−1,000) and cap (20,300) times 0.9637. The same pattern holds for ER01, HPR1 and TORRB1. **[V-data]** for the numbers. That AEMO intends a division is inferred **[J]**; none of the pages read states it.

### Size of one month (September 2025)

Compressed sizes come from the directory listing. Uncompressed sizes were read from each zip's central directory, which needs only its last 64 KB. **[V-data]**

| Table | Parts | Compressed | Uncompressed |
|---|---:|---:|---:|
| `BIDPEROFFER_D` | 1 | 151.6 MB | 3,419.7 MB |
| `BIDDAYOFFER_D` | 1 | 1.9 MB | 13.9 MB |
| `BIDOFFERPERIOD` (all rebids) | 44 | 12,177 MB | about 270 GB (estimated: two parts were measured at 21.7–22.4× compression) |
| `BIDDAYOFFER` (all rebids) | 1 | 209.9 MB | not measured |
| `DISPATCHLOAD` | 1 | 109.6 MB | 1,008.1 MB |
| `DISPATCHCONSTRAINT` | 1 | 174.3 MB | 1,530.1 MB |
| Constraint definitions and loss model (`GENCONDATA`, `SPD*`, `GENERICCONSTRAINTRHS`, `LOSSMODEL`, `LOSSFACTORMODEL`) | — | about 2 MB | small |

The tables keep growing. In September 2026 `BIDPEROFFER_D` was 184.4 MB and `BIDOFFERPERIOD` was 17.7 GB in 64 parts. **[V-data]**

**Do not download the rebid history.** A month of it is 12–18 GB compressed and about 270 GB uncompressed, and the simulator does not need it.

### The archive has a gap in the cheap tables

**This is the most important data finding.** `BIDPEROFFER_D` and `BIDDAYOFFER_D` are present in the archive up to February 2021 and from August 2024 onward. They are absent from every month from March 2021 to July 2024. I checked 2021-02, 2021-03, 2021-07, 2022-06, 2023-06, 2024-06, 2024-07, 2024-08, 2024-12, 2025-09 and 2026-09. **[V-data]**

For those 41 months, only the full-history `BIDPEROFFER` exists, at 0.7 GB (2021-03) to 7.5 GB (2024-07) compressed. The final bid would have to be rebuilt from it by picking the latest valid offer for each unit and interval. A third-party R package reports the same gap [8], and the listings agree.

**Consequence:** work in August 2024 to September 2026. This rules out cheap reproduction of June 2022, your existing project's best-studied event.

### Lag and confidentiality

- AEMO's data model says `BIDDAYOFFER` data "is confidential to the submitting participant until made public after 4am the next day", with visibility "Private; Public Next-Day" [1]. **[V-doc]**
- It says `BIDPEROFFER_D` "is public data" and "updates daily shortly after 4am" [2]. **[V-doc]**
- Every sampled file's header record reads `PUBLIC`. **[V-data]**
- Daily files: `Reports/Current/Bidmove_Complete` holds the last 60 days, about 9 MB a day. `Reports/Archive/Bidmove_Complete` holds 12 monthly bundles of 230–280 MB. **[V-data]**
- Monthly archive: published about two weeks after month end (your `data_sources.md`). The September 2025 files carry a creation date of 2025-10-08, and the listing shows them republished on 2026-04-18. **[V-data]**

### Does the copyright position match `docs/data_sources.md`?

**As far as I can verify, yes.** The bid tables come from the same MMSDM archive on the same host, and their files are marked `PUBLIC` in the same way. **[V-data]**

The copyright permissions page still returns HTTP 403 to scripted requests, so I could not read it directly, the same limitation `data_sources.md` records. **[U]** Search-engine copies show the same "any purpose … with accurate and appropriate attribution" wording. They also show an exclusion for confidential documents and reports commissioned by third parties [9]. **[U]**, not read directly.

Bids are confidential only until 4 am the next day, so that exclusion does not appear to cover archived bids. **[J]**

One difference is practical rather than legal. Bids carry `PARTICIPANTID` and free-text `REBIDEXPLANATION`, so anything published from them describes named firms' behaviour. **[J]** The existing policy of no redistribution, gitignored raw files and synthetic fixtures carries over unchanged. If you plan to publish anything that names a participant, AEMO's legal team is the only route to certainty.

---

## 3. How dispatch sets the price

### The core

NEMDE is a linear program. AEMO states its aim as "satisfying energy demand and all FCAS requirements using the least-cost combination of energy and FCAS bids, subject to technical limits" [3]. **[V-doc]**

The regional price (RRP) is the marginal cost of supplying one more MW at the region's reference node: the shadow price, or dual, of the regional balance constraint. nempy derives prices from "constraint shadow prices" [5] **[V-doc]**, and an independent paper writes out the same objective [7] **[V-doc]**.

When nothing binds, this dual equals the TLF-adjusted price of the marginal offer band. When something binds, it does not. **[J]**, standard LP theory.

### Each mechanism, and what ignoring it costs

| Mechanism | What it does | Can a simple model ignore it? | Error introduced (evidence) |
|---|---|---|---|
| **Merit order and marginal unit** | Cheapest offers fill demand. The partly dispatched band sets the price, divided by TLF | No; this is the model | — |
| **Unit availability (UIGF) and ramp limits** | Wind and solar are capped at AEMO's forecast (UIGF), not their offered MAXAVAIL. Every unit moves at most its ramp rate from its initial MW | **No.** This was the biggest single effect measured | Without these bounds, the 1 September 2025 regional stacks priced near the floor: 2.5% within $20, mean error −$643. With them: 44.7% within $20 **[V-data]**. DispatchLoad ramp rates are MW per hour and bound 99.87% of actual cleared moves **[V-data]** |
| **Interconnectors** | Six links carry power between regions, up to limits that already fold in network constraints | No | Since August 2024, at least one link is at its limit in 86% of intervals **[V-data]**. Since 2021, mainland prices differ by more than $20 and more than 20% in 62–69% of intervals each year **[V-data]**. A single NEM stack cannot produce that separation |
| **Losses** | Flows lose 4–15% on average. The marginal loss factor turns one region's price into the next region's | Partly: a fixed loss factor per link may do | Marginal loss factors differed from 1 by 1.6% (Basslink) to 19.8% (Murraylink) on average in September 2025, up to 59% **[V-data]**. Ignoring losses puts a systematic gap of that size into price differences between regions **[J]** |
| **Network (generic) constraints** | About 1,131 constraint equations are invoked each interval. A binding one adds its marginal value × coefficient to the price, and can hold cheap units off or expensive units on | **No**, especially in QLD1 and NSW1 | In 206 sampled intervals, about 4.8 non-FCAS constraints bound per interval, and at least one bound in 99.5% of intervals **[V-data]**. The most frequent were Queensland ones. On 1 September 2025, actual dispatch held more than 50 MW out of merit order in 64% of QLD1 intervals and 32% of NSW1 intervals **[V-data]**. **[Note 2026-11-08]** Over whole months, 7.3 (September 2025) and 8.0 (November 2025) non-FCAS constraints bound per interval, and at least one bound in 98.7% and 99.8% of intervals. The per-interval count is higher than this sample's 4.8, which may come from sampling or from which prefixes were counted (stage2_results.md, section 11) |
| **Constraint violation and over-constrained rerun** | Constraints carry penalty prices above the cap, so they are violated only after every offer is used. A separate rerun relaxes violated constraints to set the price | Yes, outside rare intervals | Rare: 0.12 constraints violated per sampled interval **[V-data]**. When it happens the price can pass the cap or floor [4] **[V-doc]** |
| **FCAS** | Ten services co-optimised with energy in the same solve. Trapezium and joint ramping constraints tie each unit's energy to its FCAS enablement [3] **[V-doc]** | Often, but not always | Most binding constraints are FCAS requirements (`F_` prefix: 12.8 of the 17.6 binding per interval) **[V-data]**. Batteries trade energy against FCAS, so ignoring FCAS errs where units are held for FCAS **[J]**. See the 16 August 2025 test case |
| **Fast-start profiles, daily energy limits, fixed loads** | Commitment rules and energy caps for some units | Yes, for a first version | Small and unit-specific **[J]** |
| **Price floor and cap** | −$1,000 floor. Cap $17,500 (2024-25), $20,300 (2025-26), $23,200 (2026-27) **[V-data]** | No, but trivial: clip to them | — |
| **Administered pricing** | If the 7-day sum of prices (2,016 intervals) passes the cumulative price threshold, prices are capped at $600 and floored at −$600. The threshold is $2,225,900 for 2026-27. Caps carry to connected regions through loss factors. They apply "after all other price modifiers" [6] **[V-doc]** | Yes: compare with ROP, the price before overrides, instead of RRP | The APC was $300 until 1 December 2022 and $600 since [6] **[V-doc]**. On 2025-08-17, SA1 FCAS RRP differed from ROP in 25 intervals **[V-data]** |
| **Market suspension and interventions** | Suspension pricing replaces dispatch prices. Interventions add a second dispatch run | Exclude suspended intervals; use the pricing run (`INTERVENTION = 0`) | A simulator cannot reproduce suspension prices from bids **[J]** |
| **Demand definition** | What exactly NEMDE balances: total demand, scheduled loads, aggregate dispatch error | No | **[Corrected 2026-11-08]** Settled, no longer **[U]**: `TOTALDEMAND = DISPATCHABLEGENERATION − DISPATCHABLELOAD − NETINTERCHANGE`, to 0.01 MW in every September 2025 region-interval but six. `CLEAREDSUPPLY` is not part of it (stage2_results.md, section 2) |

---

## 4. What a simple model can and cannot reproduce

### Measured on one real day

I ran a throwaway merit-order check on 1 September 2025, a spring weekday with prices from −$10 to $760. It covered 04:05 to 02:10 the next day: 266 intervals × 5 regions = 1,330 region-intervals. Inputs were real final bids, real TLFs, and DispatchLoad's availability, initial MW and ramp rates. **[V-data]** **[Corrected 2026-11-08]** Prices were divided by TLF, which is wrong for embedded units (see section 2). Re-run with TLF × DLF, the figures below move by at most 0.2 points. The script is listed in the appendix.

To isolate the offer stack, each region's demand was fixed at what AEMO actually dispatched there. This is generous: flows, FCAS and constraints are all held at their real outcomes.

| Model | Within $1 | Within $5 | Within $20 | Median abs error |
|---|---:|---:|---:|---:|
| One NEM stack, no unit bounds (each offer still capped at its MAXAVAIL) | 0% | 0% | 0% | $622 |
| One NEM stack, with availability and ramp bounds | — | 9.1% | 29.1% | $36.87 |
| Regional stacks, flows fixed at actual, no unit bounds (each offer still capped at its MAXAVAIL) | 0.6% | 0.9% | 2.5% | $367 |
| Regional stacks, flows fixed, with availability and ramp bounds | 20.8% | 28.1% | 44.7% | $26.29 |
| nempy with FCAS, constraints and losses (different sample, [7]) | 72.5% | 89.5% | 97.2% | $0.05 |

**[Corrected 2026-11-08]** "No unit bounds" in the table still capped each offer at its declared MAXAVAIL. Without that cap the one-stack median error is $1,146, not $622, because offered band MW sum to about 95 GW against about 52 GW of MAXAVAIL (stage2_results.md, section 10). The last row is the source of the "28%" figure. It is not a plain merit order: it is told the actual interconnector flows and applies AEMO's unit limits.

In the best simple variant, the RRP fell inside the bracket between the last dispatched band and the next band in 51% of region-intervals. **[V-data]**

Results varied by region **[V-data]**. **[Corrected 2026-11-08]** The "Bracket contains RRP" column did not reproduce. A rebuild gets NSW1 53%, QLD1 24%, SA1 57%, TAS1 67%, VIC1 57%, with the same 51% overall; the other two columns match to within a few points. Treat that column as unverified (stage2_results.md, section 10):

| Region | Within $5 | Median abs error | Bracket contains RRP |
|---|---:|---:|---:|
| TAS1 | 44% | $10 | 38% |
| VIC1 | 27% | $17 | 72% |
| SA1 | 24% | $31 | 85% |
| NSW1 | 29% | $34 | 41% |
| QLD1 | 16% | $43 | 18% |

The worst errors were QLD1 intervals priced by the stack near −$950 while the actual price was $109–$290. Those are constrained-off floor-priced units. **[V-data]**

**Caveats.** This is one day, in one season, with my own simplified clearing. The nempy row comes from a different paper and sample. **[J]**

### Honest expectation

A plain merit order will be within $5 of the real price in almost no intervals. With AEMO's unit limits and the actual interconnector flows supplied, regional stacks reach roughly a quarter within $5 and under half within $20. **[J]**, based on the measurement above. **[Corrected 2026-11-08]** This said "a plain merit order" reaches a quarter. Measured over three months in stage 2, a plain merit order gets 0.1% within $5 (stage2_results.md, section 4).

The idea that it "works on unconstrained intervals" breaks down because unconstrained intervals barely exist: a network constraint bound in 99.5% of sampled intervals. Even on intervals where no interconnector is at its limit, losses alone separate regional prices by several per cent. **[V-data]** for the frequencies, **[J]** for the conclusion.

### Where it fails worst

Worst to least bad **[J]**:

1. **Price separation by an intra-regional constraint.** Example: 2025-11-26 11:00, NSW1 at $20,300 while VIC1 was −$33.67. VNI was not at its limit, so the interconnector limits alone do not explain the gap.
2. **QLD1 generally**, where constraints hold large amounts of cheap generation off.
3. **Scarcity at or near the cap**, where small differences in availability flip the price between a few hundred dollars and the cap.
4. **Deep negative prices**, where many offers sit at about −$1,000 and the price is set by which floor-priced units are constrained off.
5. **FCAS-stressed regions**, such as SA1 in August 2025.

A full LP closes most of this gap, as the nempy result shows. The gap closes through constraints and losses, not through a better merit order.

---

## 5. Test cases

All are drawn from the warehouse, and all fall in August 2024 or later, where the final-bid tables exist. Times are interval end in NEM time. **[V-data]** for every price, flag and binding state below. Event causes are **[V-doc]** where cited.

| # | Interval | Regions and prices | Why it is interesting |
|---|---|---|---|
| 1 | **2026-05-14 14:05** (also 14:10) | Mainland mean $66.48, spread $1.37 across regions. No interconnector at a limit | **Ordinary control.** One of the few intervals with no link at its limit. If the model cannot get this one, nothing else matters |
| 2 | **2025-09-01**, whole day | NSW1 −$10 to $600, SA1 $6 to $760 | **Already measured** (section 4). A full ordinary day with an evening peak, useful for regression testing |
| 3 | **2025-11-26 11:00** (NSW1 at cap 11:00–11:15) | NSW1 $20,300, QLD1 $165.35, VIC1 −$33.67, SA1 −$29.44. QNI at limit, 782 MW flowing QLD→NSW. VNI not at limit | **Constraint binding.** A $20,000 gap between neighbours without VNI at its limit points to a constraint inside NSW. Interconnector limits alone will miss it |
| 4 | **2026-01-26 20:40** (SA1 at cap 20:35–20:55) | SA1 $20,300, VIC1 $88.99. Heywood at its export limit, 496 MW into SA (binding constraint `VS_600_TEST`). **[Corrected 2026-11-08]** This said "with crossed limits". The limits are 123 to 496 MW and are not crossed in any interval from 20:30 to 21:00; the positive import limit is a minimum flow into SA (stage2_results.md, section 11) | **Import-limited region at the cap.** Tests whether interconnector limits from the warehouse are enough when the binding element is the link itself |
| 5 | **2025-06-26 18:00** (≥$1,500 from 07:25 to 20:55) | NSW1 $11,704, VIC1 $11,340, SA1 $10,426, TAS1 $9,715, QLD1 $994.51. QNI at limit, QLD exporting 1,102 MW | **High-price winter scarcity** across four regions, with QLD separated. 52 intervals ≥$1,500 in NSW1, VIC1 and SA1 |
| 6 | **2024-10-07 13:00** (11:00–14:55, SA1 median −$439) | SA1 −$716.96, NSW1 −$47.57, VIC1 $34.81. Heywood (SA→VIC 197 MW) and Murraylink at limit | **Deep negative midday price** with SA1 export-limited. Alternative: VIC1 at the −$1,000 floor in 3 intervals, 13:10–15:55 on 2024-11-17 |
| 7 | **2025-08-16** (SA1 all day), then the administered period 2025-08-17 05:20 to 2025-08-24 04:00 | SA1 Lower 1-second FCAS averaged $3,142 and reached $20,300, while SA1 energy averaged $82 (max $346). `APEVENT` 204–210 cap all FCAS services in SA1 but not energy (`ENERGYAPFLAG = 0`) | **FCAS stress.** An energy-only model will look fine here, which is itself the lesson. The warehouse's `apc_code` is 0 throughout, so the energy mart cannot see this event |
| 8 | **2024-09-05 13:55–15:10**, all regions | `MARKETSUSPENDEDFLAG = 2`. AEMO suspended the market after an internal IT fault [10] | **Negative control.** Suspension pricing replaces dispatch. The simulator should flag these intervals and refuse to compare |
| 9 | **2024-11-27 15:50–16:45**, all regions; NSW1 $2,154 at 18:00 | Intervention runs present. RERT activated in NSW at 15:45, with directions to NSW bidirectional units [11] | **Intervention.** Tests that the right run (`INTERVENTION = 0`) is compared, and shows what directions do to the comparison |

---

## 6. Feasibility

### Speed on a MacBook

On this Apple M4 with 24 GB of RAM, HiGHS solved a synthetic LP of NEM size (4,506 variables, 1,105 constraints) in a median 8.3 ms, maximum 17 ms. After a 5% change in one region's demand, a warm re-solve took 0.9 ms. **[V-data]** The LP is synthetic, so real constraint structure may be slower. **[J]**

nempy reports 1.0–2.5 s per interval on a Xeon, including model building in pandas [5]. **[V-doc]**

My judgement is that the solve itself is never the bottleneck **[J]**:

- **A single interval with sliders is interactive** if the matrices are built once and only bounds and right-hand sides change.
- **A whole day (288 intervals) is a few seconds:** fine behind a button, too slow for live dragging.

So: live for one interval, batch for days and scenarios.

**Caveat for later stages.** A demand or weather slider holds every bid fixed (no rebidding) and holds every constraint right-hand side at its historical value. For small changes that is defensible. For large ones it is wrong in a way the price will not reveal. **[J]**

### Solver and libraries

| Option | Licence | Status here |
|---|---|---|
| **HiGHS via `highspy` 1.15.1** | MIT (PyPI) **[V-data]** | Installed and solved on this machine |
| `scipy.optimize.linprog(method="highs")`, scipy 1.18.1 | scipy licence (BSD-style; PyPI lists only the copyright line) **[V-data]** | Ran, and returns duals |
| nempy 3.0.3 | BSD-3-Clause (GitHub) **[V-data]**. Last push 2025-10-22 | Installs on Python 3.12 (fails on 3.14 while building pandas). Its pinned solver package `mip==1.16rc0` (EPL-2.0) imports, but CBC fails to load on this M4 (`NameError: cbclib`) **[V-data]** |

**Recommendation: HiGHS through `highspy`.** It is free, permissively licensed, fast, and gives duals directly. Treat nempy as a reference implementation to read rather than a dependency. **[J]**

### How much extra data

The test cases fall in 9 months: 2024-09, 2024-10, 2024-11, 2025-06, 2025-08, 2025-09, 2025-11, 2026-01 and 2026-05. Each month needs about 450–490 MB compressed **[V-data]** for September 2025 and September 2026:

| Table | Per month, compressed |
|---|---:|
| `BIDPEROFFER_D` | 150–185 MB |
| `BIDDAYOFFER_D` | about 2 MB |
| `DISPATCHLOAD` | 110–125 MB |
| `DISPATCHCONSTRAINT` | about 175 MB |
| Constraint definitions and loss tables | about 3 MB |

| | Estimate |
|---|---|
| Total download | about 4.3 GB **[J]**, from the measured monthly sizes |
| Download time | about 8 minutes at the measured 9 MB/s, or 10–20 minutes with polite gaps and variation **[J]** |
| Uncompressed per month | about 6 GB **[V-data]** for September 2025 |

Filter to the test days while loading rather than loading whole months.

The energy-only first version can skip `DISPATCHCONSTRAINT` and save about 1.6 GB. Extending to every month from August 2024 onward is about 26 months × 470 MB ≈ 12 GB. **[J]**

### The smallest version that shows something real

A five-region LP with **[J]**:

- real final energy offers, with prices divided by TLF × DLF; **[Corrected 2026-11-08]** was "divided by TLF";
- unit bounds from DispatchLoad (availability and ramp);
- six interconnectors bounded by AEMO's calculated limits, which are already in the warehouse;
- a fixed loss factor per link;
- prices taken from the regional duals.

Measure it against ROP and RRP on the nine test cases plus a random sample of intervals.

The point is not that it matches. It is a measured account of how much error each missing mechanism causes. That is real, nobody can fake it, and it reads well beside the existing project.

---

## 7. Repository

### For keeping it in `nem-market-explainer`

- The warehouse, ingestion code, raw-file conventions, quality checks and AEMO quirks are already there and tested. A second repository copies or depends on them.
- The explainer could later use simulator output, such as "the price was set by constraint X". One repository makes that easy.
- One data-sources document, licence position and attribution, instead of two that can drift apart.

### For a new repository

- **The story is different.** The existing README's story is about where a language model fails when explaining prices. A dispatch simulator is a numerical model of the market, with its own failure story about where a price model diverges from AEMO. Putting both in one repository blurs both.
- **The data is a different size.** Bids, DispatchLoad and constraints add about 6 GB uncompressed per month to a 3.3 GB warehouse. Mixing them in makes `dbt build` and ingestion of the existing project slower for no benefit to it.
- **The dependencies differ:** a solver and possibly a UI stack later, against Ollama and dbt.
- **The windows differ.** The explainer runs from July 2021. The simulator only works from August 2024 because of the bid gap.
- `nem-dispatch-simulator` already exists as an empty, untracked directory.

### Recommendation

**A new repository** **[J]**. It should:

- read the explainer's warehouse read-only for prices, demand, interconnectors and registration (DuckDB `ATTACH … (READ_ONLY)`);
- keep its own raw files and its own DuckDB file for bids, DispatchLoad and constraints;
- copy the ingestion patterns (polite client, record-count check, read columns by name) rather than importing them;
- link to the explainer in the README, and carry the same attribution and no-redistribution policy.

The cost is some duplicated code and a dependency on another repository's file path. Name that dependency explicitly in the configuration.

---

## Recommendation on scope

### Build in two weeks

**Week 1**

1. Ingest `BIDPEROFFER_D`, `BIDDAYOFFER_D`, `DISPATCHLOAD`, `LOSSMODEL` and `LOSSFACTORMODEL` for the 9 test-case months, filtered to the test days.
2. Load the extra columns already on disk: ROP, TLF, marginal loss factor, and binding constraint IDs.
3. Settle the demand definition first, before anything is built on it.
4. Build an evaluation harness that compares any model's price with ROP and RRP, by region and interval class.

**Week 2**

1. Build a ladder of models, each evaluated on the same intervals:
   1. one NEM stack;
   2. regional stacks with flows fixed;
   3. five-region LP with interconnector limits and losses;
   4. add generic constraints, with left-hand sides from the `SPD*` tables and right-hand sides from `DISPATCHCONSTRAINT`. This step only if time allows, and it is the most likely to slip. **[U]** I have not verified that the coefficients rebuild AEMO's published left-hand side.
2. Write up the error each rung removes, interval types where it still fails, and the nine test cases one by one.

### Leave out

- FCAS co-optimisation. Fix FCAS outcomes at actual if needed.
- Fast-start profiles, MNSP offers, the intervention run, and the rebid history.
- Sliders, weather, the map, and any counterfactual. They belong after the model is measured, and they bring their own caveat: bids would not respond to the change.

---

## Appendix: throwaway scripts and samples

**[Corrected 2026-11-08]** These scripts no longer exist: the session scratchpad was cleared before stage 2. The section 4 models are rebuilt in `nemsim/reproduce.py`.

All were outside both repositories, in the session scratchpad:
`/private/tmp/claude-501/-Users-abubakar-Code-nem-dispatch-simulator/f0f73bd0-c5aa-4790-a312-9adafdd8d50d/scratchpad/`

| Script | What it does |
|---|---|
| `scripts/inventory.py` | Warehouse tables, rows and dates (section 1) |
| `scripts/partial_unzip.py`, `scripts/zip_tail.py` | Inflate the start of a range-downloaded zip; read exact sizes from its tail (section 2) |
| `scripts/bid_grain.py` | Grain and bid types of the bid samples (section 2) |
| `scripts/merit_check.py` | The one-day merit-order check and out-of-merit measurement (sections 3–4) |
| `scripts/ramp_units.py` | Confirms DispatchLoad ramp rates are MW per hour (section 3) |
| `scripts/constraints.py` | Invoked and binding constraints per interval (section 3) |
| `scripts/test_cases.py`, `scripts/test_cases2.py` | Separation statistics and test intervals (sections 3, 5) |
| `scripts/lp_bench.py` | HiGHS timing (section 6) |

Downloads stayed small. Full files: `BIDDAYOFFER_D` (1.9 MB) and nine small standing or constraint tables (under 0.3 MB in total). Range samples: the first 4 MB of `BIDPEROFFER_D`, `BIDOFFERPERIOD`, `DISPATCHLOAD` and `DISPATCHCONSTRAINT`, one 8 MB range used only to time the download, and the last 64 KB of five zips. Everything is for September 2025. The scratchpad also holds two throwaway Python venvs.

## A side finding for `nem-market-explainer`

Two open questions in your existing `findings.md` can now be partly answered:

1. **The June 2022 $300 bunching.** AEMO's Guide to Administered Pricing says the APC was $300/MWh until it was raised to $600 on 1 December 2022 [6]. **[V-doc]** So $300 was the APC in force in June 2022.
2. **Which regions were capped.** `APEVENTREGION` lists June 2022 administered price events for NSW1, QLD1, SA1 and VIC1 (events 158–161 and 182), with energy flags set. **[V-data]** This could replace guessing from `APCFLAG` bits.

Nothing in that repository was changed.

## Sources

1. AEMO MMS Data Model, table BIDDAYOFFER: https://visualisations.aemo.com.au/aemo/nemweb/MMSDataModelReport/Electricity/MMS%20Data%20Model%20Report_files/MMS_18.htm
2. AEMO MMS Data Model, table BIDPEROFFER_D: https://visualisations.aemo.com.au/aemo/nemweb/MMSDataModelReport/Electricity/MMS%20Data%20Model%20Report_files/MMS_23.htm
3. AEMO, *FCAS Model in NEMDE*, version 3.0, effective 3 June 2024: https://aemo.com.au/-/media/files/electricity/nem/security_and_reliability/dispatch/policy_and_process/fcas-model-in-nemde.pdf
4. AEMO, *Constraint Relaxation Procedure* (ME_PD_03), version 3, 17 November 2017: https://www.aemo.com.au/-/media/Files/Electricity/NEM/Security_and_Reliability/Congestion-Information/2016/Constraint-Relaxation-Procedure.pdf
5. nempy documentation and README (UNSW CEEM): https://nempy.readthedocs.io/en/latest/intro.html and https://github.com/UNSW-CEEM/nempy
6. AEMO, *Guide to Administered Pricing*, version 1.5, effective 1 July 2026: https://www.aemo.com.au/-/media/files/electricity/nem/security_and_reliability/dispatch/policy_and_process/guide-to-administered-pricing.pdf
7. N. Eschenbaum, *Shared Bidding Algorithms and Competition: Evidence from Electricity Markets*, arXiv:2607.13002 v3, Appendix B: https://arxiv.org/abs/2607.13002
8. CRAN `aemo` R package manual (third party; reports the `BIDPEROFFER_D` gap): https://cran.r-project.org/web/packages/aemo/index.html
9. AEMO copyright permissions page (HTTP 403 to scripted requests; seen only through search results): https://www.aemo.com.au/privacy-and-legal-notices/copyright-permissions
10. AEMO, *Statement on market suspension*: https://www.aemo.com.au/newsroom/news-updates/statement-on-market-suspension, and WattClarity's copies of the market notices: https://wattclarity.com.au/?p=33609
11. AEMO, *Directions to NSW Bidirectional Units – 27 November 2024*: https://www.aemo.com.au/energy-systems/electricity/national-electricity-market-nem/nem-events-and-reports/market-event-reports/directions-to-nsw-bidirectional-units---27-november-2024, and AEMO RERT quarterly report Q4 2024: https://aemo.com.au/-/media/files/electricity/nem/emergency_management/rert/2025/rert-quarterly-report-q4-2024-ver-1.pdf

Data: contains data from the Australian Energy Market Operator (AEMO), nemweb.com.au. AEMO is the author of the data and has not reviewed or endorsed this project.
