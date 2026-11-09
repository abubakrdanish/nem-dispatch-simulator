from nemsim.intervals import CASES, SETS


def test_research_window_is_266_intervals():
    iv = SETS["stage1"].intervals()
    assert len(iv) == 266 and str(iv[0]) == "2025-09-01 04:05:00" and str(iv[-1]) == "2025-09-02 02:10:00"


def test_nine_cases_and_months():
    assert [c.number for c in CASES] == list(range(1, 10))
    assert len(SETS["2025-09"].intervals()) == 30 * 288
