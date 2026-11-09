"""What a rung is, to the harness.

A rung declares the inputs it uses and returns one price per region for one interval. That is
the whole contract. The harness loads only the declared inputs, never passes the actual price,
and scores every rung with the same code, so adding a rung is a new subclass and nothing else.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

import pandas as pd

from nemsim.inputs import IntervalInputs

# Columns a rung may return. `region` and `price` are required; the rest are diagnostics the
# harness keeps and summarises when present.
OUTPUT_COLUMNS = ("region", "price", "marginal_duid", "bracket_lo", "bracket_hi", "status")


class Rung(ABC):
    key: ClassVar[str]               # short id used on the command line and in result files
    name: ClassVar[str]
    summary: ClassVar[str]           # one line: what it models and what it holds at actual
    uses: ClassVar[frozenset[str]]   # keys of nemsim.inputs.INPUTS

    @abstractmethod
    def solve(self, x: IntervalInputs) -> pd.DataFrame:
        """One row per region with at least `region` and `price` ($/MWh)."""
