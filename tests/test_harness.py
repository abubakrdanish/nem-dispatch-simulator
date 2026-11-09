from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from nemsim.harness import constraint_region, evaluate, summarise
from nemsim.inputs import REGIONS, Window
from nemsim.rungs import LADDER
from nemsim.rungs.base import Rung
from tests.fixtures.synthetic import INTERVALS

T = [datetime.fromisoformat(t) for t in INTERVALS]


@pytest.fixture(scope="module")
def r1(warehouse):
    return evaluate(warehouse, LADDER["r1"], T)


def test_rung_one_clears_the_stack_by_hand(r1):
    # Interval 1 needs 500 MW: 450 MW at the floor (COAL1 band 1, WIND1 band 1 at -882 / (0.9 x 0.98)),
    # then COAL1 band 2 at 38 / 0.95 = 40. COAL1's band 2 is cut to 100 MW by its MAXAVAIL of 400.
    first = r1[r1.interval == T[0]]
    assert first.price.round(6).unique().tolist() == [40.0]
    assert first.marginal_duid.unique().tolist() == ["COAL1"]
    # Interval 2 needs 600 MW: COAL1's capped band 2 runs out at 550, so GAS1 at 80 sets the price.
    assert r1[r1.interval == T[1]].price.round(6).unique().tolist() == [80.0]


def test_exclusions_keep_their_reason(r1):
    third = r1[r1.interval == T[2]].set_index("region").excluded
    assert third["TAS1"] == "suspended"            # suspension is per region and outranks the rest
    assert (third.drop("TAS1") == "no_inputs").all()
    assert r1[r1.interval == T[2]].price.isna().all()


def test_scores_by_hand(r1):
    s = summarise(r1).iloc[0]
    # Errors: interval 1 [0, 5, 50, 0, 40], interval 2 [40, 35, 90, 40, 0].
    assert s.region_intervals == 10
    assert s["within_$1"] == pytest.approx(30)
    assert s["within_$5"] == pytest.approx(40)
    assert s["within_$20"] == pytest.approx(40)
    assert s.median_abs_error == pytest.approx(37.5)
    assert s.mean_abs_error == pytest.approx(30)
    assert s.mean_error == pytest.approx(21)


def test_breakdowns(r1):
    links = r1[r1.interval == T[0]].set_index("region").link_at_limit
    assert links.to_dict() == {"NSW1": False, "QLD1": False, "SA1": True, "TAS1": False, "VIC1": True}
    binding = r1[r1.interval == T[0]].set_index("region").constraint_binding
    # Q>NIL_TEST and N>COAL_CAP bind (non-zero marginal value); F_ is FCAS; N>NIL_SLACK does not bind.
    assert binding.to_dict() == {"NSW1": True, "QLD1": True, "SA1": False, "TAS1": False, "VIC1": False}
    by_region = summarise(r1, "region")
    assert by_region.loc["VIC1", "within_$1"] == pytest.approx(50)
    assert by_region.loc["TAS1", "region_intervals"] == 2


def test_rop_target(warehouse):
    assert summarise(evaluate(warehouse, LADDER["r1"], T, target="rop")).iloc[0].region_intervals == 10


class Oracle(Rung):
    """A new rung needs nothing but this class. It also proves the answer is out of reach."""
    key, name, summary = "oracle", "Oracle", "tries to read the actual price"
    uses = frozenset({"demand"})

    def solve(self, x):
        assert x.offers is None and x.units is None, "inputs it did not ask for must not be loaded"
        assert not any("rrp" in f.columns for f in (x.demand,)), "the actual price must never be an input"
        return pd.DataFrame({"region": list(REGIONS), "price": 40.0})


def test_a_new_rung_plugs_in_unchanged(warehouse):
    result = evaluate(warehouse, Oracle(), T)
    s = summarise(result).iloc[0]
    # Demand exists in all three intervals, so only the suspended region-interval is excluded.
    assert s.region_intervals == 14
    assert result.excluded.value_counts().to_dict() == {"suspended": 1}
    assert "bracket_contains_%" not in summarise(result)


def test_unknown_input_is_refused(warehouse):
    with pytest.raises(ValueError, match="unknown inputs"):
        Window(warehouse, T[0], T[1], frozenset({"actual_price"}))


def test_offer_price_uses_tlf_times_dlf(warehouse):
    offers = Window(warehouse, T[0], T[0], frozenset({"offers"})).at(T[0]).offers
    wind = offers[(offers.duid == "WIND1") & (offers.band == 1)].iloc[0]
    assert wind.price == pytest.approx(-882 / (0.9 * 0.98))


@pytest.mark.parametrize("cid, region", [
    ("Q>NIL_TEST", "QLD1"), ("N^^V_NIL_1", "NSW1"), ("NC_Q_ULPBESS1", "QLD1"), ("NSA_S_TB3_40", "SA1"),
    ("C_V_MREHA1_200_G", "VIC1"), ("#CPP_3_E", "QLD1"), ("$CPP_3", "QLD1"), ("#R033668_001_RAMP_V", None),
    ("F_MAIN+TEST", None), ("I_6F_SN_150", None)])
def test_constraint_region(cid, region):
    assert constraint_region(cid, {"CPP_3": "QLD1", "CPP": "NSW1"}) == region


def test_rung_two_caps_wind_at_its_forecast(warehouse):
    r2 = evaluate(warehouse, LADDER["r2"], T)
    # WIND1 is cut from 150 MW to its 50 MW forecast. Interval 1 needs 500 MW: 350 at the floor,
    # COAL1's 100 MW at 40 reaches 450, so GAS1's band at 80 sets the price. Interval 2 needs 600:
    # GAS1's second band at 150 sets it.
    assert r2[r2.interval == T[0]].price.round(6).unique().tolist() == [80.0]
    assert r2[r2.interval == T[1]].price.round(6).unique().tolist() == [150.0]


def test_dispatch_outcomes_are_withheld_from_units(warehouse):
    units = Window(warehouse, T[0], T[0], frozenset({"units"})).at(T[0]).units
    assert {"availability", "initialmw", "uigf"} <= set(units.columns)
    assert not {"totalcleared", "semidispatchcap"} & set(units.columns)


def test_rung_3b_separates_a_region_behind_full_links(warehouse):
    r3 = evaluate(warehouse, LADDER["r3b"], T).set_index(["interval", "region"]).price
    # SA1 needs 100 MW with 50 MW of wind and 40 MW of import capacity, so it is short and at the cap.
    # The mainland meets 400 MW plus 40 MW of exports: COAL1's floor band and WIND1 reach 350 (COAL1
    # must stay above 250 MW), COAL1's 40-dollar band reaches 450, so GAS1 at 80 sets the price.
    assert r3[T[0], "SA1"] == pytest.approx(20300)
    for region in ("NSW1", "QLD1", "TAS1", "VIC1"):
        assert r3[T[0], region] == pytest.approx(80)
    # Interval 2: mainland needs 520 MW, past GAS1's first band, so its band at 150 sets the price.
    assert r3[T[1], "NSW1"] == pytest.approx(150)


def test_lp_price_is_the_marginal_offer():
    from nemsim import lp
    s = lp.solve(["A", "B"], np.array(["A", "A", "B"]), np.array([10.0, 30.0, 50.0]), np.array([100.0, 100.0, 100.0]),
                 np.array(["u1", "u2", "u3"]), {}, {"A": 50.0, "B": 120.0}, [("AB", "A", "B", 100.0, 100.0)],
                 -1000, 20300)
    # B takes 100 MW from A (link full), so A clears on u2 at 30 and B on its own u3 at 50.
    assert s.prices == pytest.approx({"A": 30.0, "B": 50.0})
    assert s.flows["AB"] == pytest.approx(100.0)


def test_relative_and_dollar_weighted_scores_by_hand(r1):
    s = summarise(r1).iloc[0]
    # Within 10% of the actual price: NSW1 and TAS1 in interval 1, VIC1 in interval 2.
    assert s["within_10%"] == pytest.approx(30)
    # Weights are |price x demand|. Hits within $5: NSW1, QLD1, TAS1 in interval 1 (40, 45, 40 x 100 MW)
    # and VIC1 in interval 2 (80 x 120 MW): 22,100 of 47,300.
    assert s["within_$5_by_$"] == pytest.approx(22100 / 47300 * 100)
    bands = summarise(r1, "price_class")
    assert set(bands.index) == {"a. negative", "b. $0-50", "c. $50-300"}


def test_oracle_bound_picks_the_best_single_price():
    from nemsim.ceiling import oracle_hits
    rows = pd.DataFrame({"interval": [1] * 5 + [2] * 5, "region": list(REGIONS) * 2,
                         "rrp": [40, 44, 49, 100, -10, 10, 10, 10, 10, 10]})
    hits = oracle_hits(rows).set_index(["interval", "region"]).oracle_hit
    # Interval 1: a price of 44.5 or so reaches 40, 44 and 49 (three regions), no price reaches more.
    assert hits.loc[1].sum() == 3 and not hits[1, "TAS1"] and not hits[1, "VIC1"]
    assert hits.loc[2].all()


def test_rung_3a_clears_each_region_on_its_own_requirement(warehouse):
    r = evaluate(warehouse, LADDER["r3a"], T).set_index(["interval", "region"]).price
    # The fixture's net interchange is 0, so each region meets its own 100 MW. NSW1 has COAL1:
    # 300 MW at the floor covers it, so NSW1 clears at the floor-band price (-950 / 0.95 = -1000).
    # QLD1 and TAS1 have no offers, so they are short and at the cap. SA1 has 50 MW of wind against
    # 100 MW, also short. VIC1 has GAS1: 100 MW at 80 meets 100 MW exactly.
    assert r[T[0], "NSW1"] == pytest.approx(-1000)
    assert r[T[0], "QLD1"] == pytest.approx(20300)
    assert r[T[0], "SA1"] == pytest.approx(20300)
    assert r[T[0], "VIC1"] == pytest.approx(80)


def test_lp_losses_scale_the_far_region_price():
    from nemsim import lp
    # A sells at 10; B has no supply and needs 100 MW over a link with 10% marginal losses, all charged
    # to A (share 1). One more MW in B needs 1.1 MW from A, so B's price is 11.
    s = lp.solve(["A", "B"], np.array(["A"]), np.array([10.0]), np.array([500.0]), np.array(["u1"]), {},
                 {"A": 0.0, "B": 100.0}, [("AB", "A", "B", 300.0, 300.0)], -1000, 20300, [(0.0, 0.1, 1.0)])
    assert s.prices["A"] == pytest.approx(10.0)
    assert s.prices["B"] == pytest.approx(11.0)
    assert s.flows["AB"] == pytest.approx(100.0)


def test_lp_generic_constraint_holds_a_cheap_unit_back():
    from nemsim import lp
    # One region, cheap u1 at 10 and dear u2 at 50, 100 MW of demand. A constraint caps u1 at 60 MW,
    # so u2 is marginal and sets the price; without it u1 would set 10.
    args = (["A"], np.array(["A", "A"]), np.array([10.0, 50.0]), np.array([100.0, 100.0]),
            np.array(["u1", "u2"]), {}, {"A": 100.0}, [], -1000, 20300)
    assert lp.solve(*args).prices["A"] == pytest.approx(10.0)
    capped = lp.solve(*args, constraints=[lp.Constraint({"u1": 1.0}, {}, "<=", 60.0, 1e6, "CAP_U1")])
    assert capped.prices["A"] == pytest.approx(50.0)
    assert capped.dispatch.tolist() == pytest.approx([60.0, 40.0])
    assert capped.violated == []


def test_rung_4_applies_invoked_constraints_and_skips_fcas(warehouse):
    m = LADDER["r4"]
    r4 = evaluate(warehouse, m, T).set_index(["interval", "region"]).price
    # As in the 3b test, but N>COAL_CAP holds COAL1 to 260 MW. The mainland needs 440: COAL1 260 + WIND1 50
    # = 310, GAS1's first band reaches 410, so its band at 150 sets the price. SA1 is still short.
    assert r4[T[0], "NSW1"] == pytest.approx(150)
    assert r4[T[0], "SA1"] == pytest.approx(20300)
    assert m.coverage["FCAS terms"] >= 1 and m.coverage["applied"] >= 3


def test_rung_4_never_reads_solve_outputs(warehouse):
    from nemsim.inputs import Window
    x = Window(warehouse, T[0], T[0], frozenset({"constraints"})).at(T[0])
    assert not {"marginalvalue", "lhs", "violationdegree"} & set(x.constraints.columns)
