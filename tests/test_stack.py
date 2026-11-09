import numpy as np

from nemsim.stack import cap_bands, clear


def test_marginal_band_sets_price():
    c = clear(np.array([10, -5, 30]), np.array([100, 50, 100]), 120, -1000, 20300)
    assert (c.price, c.marginal, c.bracket_lo, c.bracket_hi, c.status) == (10, 0, 10, 30, "marginal")


def test_requirement_on_a_boundary_takes_the_band_that_reaches_it():
    assert clear(np.array([-5, 10, 30]), np.array([50, 100, 100]), 150, -1000, 20300).price == 10


def test_short_stack_prices_at_the_cap_and_no_need_at_the_floor():
    assert clear(np.array([10]), np.array([5]), 10, -1000, 20300).status == "short"
    assert clear(np.array([10]), np.array([5]), 10, -1000, 20300).price == 20300
    assert clear(np.array([10]), np.array([5]), 0, -1000, 20300).price == -1000


def test_price_is_clipped_to_floor_and_cap():
    # A band price below the floor after dividing by a loss factor above 1.
    assert clear(np.array([-1010]), np.array([10]), 5, -1000, 20300).price == -1000


def test_zero_mw_bands_are_ignored():
    assert clear(np.array([-50, 10]), np.array([0, 10]), 5, -1000, 20300).price == 10


def test_cap_bands_fills_each_unit_in_band_order():
    duid = np.array(["A", "A", "A", "B", "B"])
    band = np.array([3, 1, 2, 1, 2])
    mw = np.array([100, 100, 100, 50, 50])
    out = cap_bands(duid, band, mw, np.array([150, 150, 150, 500, 500]))
    # A keeps band 1 (100) and half of band 2 (50); band 3 is gone. B is under its limit.
    assert out.tolist() == [0, 100, 50, 50, 50]


def test_cap_bands_with_negative_limit_offers_nothing():
    assert cap_bands(np.array(["A"]), np.array([1]), np.array([10]), np.array([-3])).tolist() == [0]
