"""The ladder. Each rung adds one layer of realism and is scored by the same harness.

To add a rung: subclass nemsim.rungs.base.Rung in a new module, declare its inputs, and add an
instance here. Nothing in the harness changes.
"""
from nemsim.rungs.base import Rung
from nemsim.rungs.r1_bare_merit_order import BareMeritOrder
from nemsim.rungs.r2_dispatch_availability import DispatchAvailability
from nemsim.rungs.r3a_regional_flows_at_actual import RegionalFlowsAtActual
from nemsim.rungs.r3b_flows_solved import FlowsSolved
from nemsim.rungs.r4_constraints import Constraints

LADDER: dict[str, Rung] = {r.key: r for r in [BareMeritOrder(), DispatchAvailability(), RegionalFlowsAtActual(), FlowsSolved(), Constraints()]}

