"""Named sets of intervals to score, including the stage 1 research test cases.

Times are interval ends in NEM market time. Ranges are inclusive and step by five minutes.
Each test case carries the facts the research states about it, so `python -m nemsim.cases`
can check the research against the data before anything is scored on it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

STEP = timedelta(minutes=5)


@dataclass(frozen=True)
class IntervalSet:
    name: str
    description: str
    ranges: tuple[tuple[str, str], ...]

    def intervals(self) -> list[datetime]:
        out = set()
        for start, end in self.ranges:
            t, stop = datetime.fromisoformat(start), datetime.fromisoformat(end)
            while t <= stop:
                out.add(t)
                t += STEP
        return sorted(out)


@dataclass(frozen=True)
class Case(IntervalSet):
    number: int = 0
    why: str = ""
    # What the research says, as (interval, region, RRP) to the cent.
    prices: tuple[tuple[str, str, float], ...] = ()
    # Other stated facts, checked by name in nemsim/cases.py.
    claims: tuple[str, ...] = field(default=())


CASES = [
    Case("case1", "Ordinary control: mainland prices within $1.37, no interconnector at a limit",
         (("2026-05-14 14:05", "2026-05-14 14:10"),), number=1,
         why="If the model cannot get this one, nothing else matters",
         claims=("case1_mainland_mean_66.48_spread_1.37", "case1_no_link_at_limit")),
    Case("case2", "2025-09-01, the day the research measured",
         (("2025-09-01 00:05", "2025-09-02 00:00"),), number=2,
         why="A full ordinary day with an evening peak; regression test",
         claims=("case2_nsw_range_-10_600", "case2_sa_range_6_760")),
    Case("case3", "2025-11-26 NSW1 at the cap, 11:00-11:15",
         (("2025-11-26 11:00", "2025-11-26 11:15"),), number=3,
         why="Constraint binding inside NSW: interconnector limits alone will miss it",
         prices=(("2025-11-26 11:00", "NSW1", 20300), ("2025-11-26 11:00", "QLD1", 165.35),
                 ("2025-11-26 11:00", "VIC1", -33.67), ("2025-11-26 11:00", "SA1", -29.44)),
         claims=("case3_nsw_at_cap_11:00-11:15", "case3_qni_at_limit_782_qld_to_nsw", "case3_vni_not_at_limit")),
    Case("case4", "2026-01-26 SA1 at the cap, 20:35-20:55",
         (("2026-01-26 20:35", "2026-01-26 20:55"),), number=4,
         why="Import-limited region at the cap",
         prices=(("2026-01-26 20:40", "SA1", 20300), ("2026-01-26 20:40", "VIC1", 88.99)),
         claims=("case4_heywood_496_into_sa_crossed_limits",)),
    Case("case5", "2025-06-26 winter scarcity, 07:25-20:55",
         (("2025-06-26 07:25", "2025-06-26 20:55"),), number=5,
         why="High prices across four regions with QLD separated",
         prices=(("2025-06-26 18:00", "NSW1", 11704), ("2025-06-26 18:00", "VIC1", 11340),
                 ("2025-06-26 18:00", "SA1", 10426), ("2025-06-26 18:00", "TAS1", 9715),
                 ("2025-06-26 18:00", "QLD1", 994.51)),
         claims=("case5_qni_at_limit_qld_exporting_1102", "case5_52_intervals_ge_1500")),
    Case("case6", "2024-10-07 deep negative SA1, 11:00-14:55",
         (("2024-10-07 11:00", "2024-10-07 14:55"),), number=6,
         why="Deep negative midday price with SA1 export-limited",
         prices=(("2024-10-07 13:00", "SA1", -716.96), ("2024-10-07 13:00", "NSW1", -47.57),
                 ("2024-10-07 13:00", "VIC1", 34.81)),
         claims=("case6_sa_median_-439", "case6_heywood_and_murraylink_at_limit")),
    Case("case7", "2025-08-16 SA1 FCAS stress",
         (("2025-08-16 00:05", "2025-08-17 00:00"),), number=7,
         why="An energy-only model will look fine here, which is itself the lesson",
         claims=("case7_sa_energy_mean_82_max_346",)),
    Case("case8", "2024-09-05 market suspension, 13:55-15:10",
         (("2024-09-05 13:55", "2024-09-05 15:10"),), number=8,
         why="Negative control: the harness must refuse to score suspended intervals",
         claims=("case8_suspended_flag_2_all_regions",)),
    Case("case9", "2024-11-27 intervention, 15:50-16:45",
         (("2024-11-27 15:50", "2024-11-27 16:45"),), number=9,
         why="Tests that the pricing run (INTERVENTION = 0) is compared",
         claims=("case9_intervention_rows_present", "case9_nsw_2154_at_18:00")),
]

# Months with offers loaded (config/settings.yml). Each is a set of every interval in the month.
MONTHS = ("2024-10", "2024-11", "2025-06", "2025-08", "2025-09", "2025-11", "2025-12", "2026-01", "2026-02",
          "2026-05")
# Stage 6b: spike months added on purpose (selection rule in the README). Kept apart from MONTHS so that
# every earlier result, and every module that loops over MONTHS, still means the original ten.
SPIKE_MONTHS = ("2020-01", "2024-08", "2024-12", "2025-01", "2025-02", "2025-05", "2025-07")


def _month(m: str) -> IntervalSet:
    start = datetime.fromisoformat(m + "-01")
    end = (start + timedelta(days=32)).replace(day=1)
    return IntervalSet(m, f"Every interval in {start:%B %Y}", ((f"{start:%Y-%m-%d} 00:05", f"{end:%Y-%m-%d} 00:00"),))


SETS: dict[str, IntervalSet] = {s.name: s for s in [
    # The research ran 04:05 on 2025-09-01 to 02:10 the next day: 266 intervals, one trading
    # day cut short (probably where its 4 MB DISPATCHLOAD sample ran out).
    IntervalSet("stage1", "The stage 1 research window: 2025-09-01 04:05 to 2025-09-02 02:10, 266 intervals",
                (("2025-09-01 04:05", "2025-09-02 02:10"),)),
    *(_month(m) for m in MONTHS),
    *(_month(m) for m in SPIKE_MONTHS),
    IntervalSet("cases", "All nine stage 1 test cases", tuple(r for c in CASES for r in c.ranges)),
    *CASES,
]}
