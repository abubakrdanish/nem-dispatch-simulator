"""Fixed bids (stage 8): a unit with FIXEDLOAD > 0 is offered as exactly that MW at the price floor."""
from datetime import datetime

import duckdb
import pytest

from nemsim import inputs
from tests.fixtures.synthetic import INTERVALS, build

T = datetime.fromisoformat(INTERVALS[0])


@pytest.fixture
def con(tmp_path, monkeypatch):
    path = build(tmp_path / "w.duckdb")
    with duckdb.connect(str(path)) as c:
        c.execute("update raw.bidperoffer_d set fixedload = 250 where duid = 'COAL1' and interval_datetime = ?", [T])
    c = duckdb.connect(str(path), read_only=True)
    yield c
    c.close()


def test_switch_off_leaves_the_offers_as_bid(con, monkeypatch):
    monkeypatch.setattr(inputs, "FIXED_BIDS", False)
    o = inputs.offers(con, T, T)
    assert sorted(o[o.duid == "COAL1"].mw) == [200, 300]


def test_switch_on_offers_the_fixed_mw_at_the_floor(con, monkeypatch):
    monkeypatch.setattr(inputs, "FIXED_BIDS", True)
    o = inputs.offers(con, T, T)
    c = o[o.duid == "COAL1"]
    assert list(c.mw) == [250] and list(c.price) == [inputs.FLOOR]
    assert c.raw_price.iloc[0] == pytest.approx(inputs.FLOOR * 0.95)        # COAL1's TLF x DLF
    other = inputs.offers(con, datetime.fromisoformat(INTERVALS[1]), datetime.fromisoformat(INTERVALS[1]))
    assert sorted(other[other.duid == "COAL1"].mw) == [200, 300]             # no fixed bid there
