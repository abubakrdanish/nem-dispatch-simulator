"""The AER cause labels (nemsim/aer): the quote check, the scorer and the verdict rule, on synthetic data.
No raw document and no API key is needed."""
from types import SimpleNamespace

import pandas as pd
import pytest

from nemsim.aer import accuracy, extract, test as cause_test
from nemsim.aer.text import normalise
from nemsim.llm.schema import SchemaError, validate

DOC = normalise("[page 1] The Heywood interconnector f ailed at 1.24 pm. Wind output was low, averaging 41 MW. "
                "Rebidding did not contribute to the high price.")


def test_quote_check_ignores_extraction_spaces_but_not_paraphrase():
    assert extract.found("The Heywood interconnector failed at 1.24 pm.", DOC)          # PDF put a space in "failed"
    assert not extract.strict_found("The Heywood interconnector failed at 1.24 pm.", DOC)
    assert not extract.found("The Heywood link failed at 1.24 pm.", DOC)                # a changed word
    assert not extract.found("The Heywood interconnector ... averaging 41 MW.", DOC)     # joined fragments
    assert not extract.found(None, DOC) and not extract.found("", DOC)


def _event(causes, net="not_stated", net_q=None):
    return {"event_date": "2020-01-31", "regions": ["SA1"], "intervals": None, "peak_price": None,
            "causes": causes, "constraints_named": [], "interconnectors_named": [],
            "network_outages": {"value": net, "quote": net_q}, "rebid_set_price": {"value": "not_stated", "quote": None}}


def test_schema_accepts_a_well_formed_answer_and_rejects_an_unknown_label():
    good = {"events": [_event([{"label": "wind_output", "quote": "Wind output was low, averaging 41 MW."}])]}
    validate(good, extract.SCHEMA)
    bad = {"events": [_event([{"label": "sunspots", "quote": "Wind output was low."}])]}
    with pytest.raises(SchemaError):
        validate(bad, extract.SCHEMA)


def test_checked_drops_labels_and_statements_whose_quote_is_not_in_the_document(monkeypatch):
    monkeypatch.setattr(extract, "documents", lambda: {"u": {"file": "f.pdf", "chars": len(DOC)}})
    monkeypatch.setattr(extract, "text", lambda p: DOC)
    data = {"events": [_event([{"label": "wind_output", "quote": "Wind output was low, averaging 41 MW."},
                               {"label": "demand", "quote": "Demand was a record."}],
                              net="contributed", net_q="Network outages contributed.")]}
    events, stats = extract.checked({"u": SimpleNamespace(ok=True, data=data)})
    e = events[0]
    assert [c["label"] for c in e["causes"]] == ["wind_output"]
    assert [c["label"] for c in e["dropped_causes"]] == ["demand"]
    assert e["network_outages"]["value"] == "not_stated"          # its quote is not in the document
    assert stats["quotes"] == 3 and stats["verbatim"] == 1


def test_scorer_separates_wrong_labels_from_misattributed_quotes():
    hand = [{"file": "f", "event_date": "2020-01-31", "regions": ["SA1"], "causes": {"network_outage": "q", "wind_output": "q"},
             "network_outages": {"value": "contributed"}, "rebid_set_price": {"value": "no"}}]
    model = [{"file": "f", "event_date": "2020-01-31", "regions": ["SA1"],
              "causes": [{"label": "network_outage", "quote": "a"}, {"label": "demand", "quote": "b"}],
              "network_outages": {"value": "contributed"}, "rebid_set_price": {"value": "not_stated"}}]
    k = "f|2020-01-31|('SA1',)"
    judged = {f"{k}|network_outage": {"supported": False, "why": "quote is about another day"},
              f"{k}|demand": {"supported": False, "why": "demand was normal"}}
    s = accuracy.score(model, hand, judged)
    t = s["table"].set_index("label")
    assert t.loc["network_outage", "precision"] == 1.0            # the label is right for the event...
    assert s["misattribution_rate"] == 1.0                        # ...but both quotes misattribute
    assert t.loc["demand", "precision"] == 0.0 and t.loc["wind_output", "recall"] == 0.0
    assert s["statements"]["network_outages"]["agree"] == 1 and s["statements"]["rebid_set_price"]["agree"] == 0


def test_solar_output_is_dropped_for_instability_even_when_it_passes_thresholds():
    hand = [{"file": f"f{i}", "event_date": f"2020-01-{i + 1:02d}", "regions": ["SA1"],
             "causes": {"solar_output": "q"}, "network_outages": {"value": "not_stated"},
             "rebid_set_price": {"value": "not_stated"}} for i in range(3)]
    model = [{"file": h["file"], "event_date": h["event_date"], "regions": h["regions"],
              "causes": [{"label": "solar_output", "quote": "q"}],
              "network_outages": {"value": "not_stated"}, "rebid_set_price": {"value": "not_stated"}}
             for h in hand]
    judged = {f"{h['file']}|{h['event_date']}|('SA1',)|solar_output": {"supported": True} for h in hand}
    row = accuracy.score(model, hand, judged)["table"].set_index("label").loc["solar_output"]
    assert row.passes_thresholds
    assert not row.usable


def test_a_label_the_blind_coding_missed_is_added_when_the_judge_finds_it_supported():
    hand = [{"file": "f", "event_date": "2020-01-31", "regions": ["SA1"], "causes": {},
             "network_outages": {"value": "not_stated"}, "rebid_set_price": {"value": "not_stated"}}]
    model = [{"file": "f", "event_date": "2020-01-31", "regions": ["SA1"], "causes": [{"label": "demand", "quote": "a"}],
              "network_outages": {"value": "not_stated"}, "rebid_set_price": {"value": "not_stated"}}]
    judged = {"f|2020-01-31|('SA1',)|demand": {"supported": True}}
    extra = {(("f", "2020-01-31", ("SA1",)), "demand")}      # what accuracy.additions finds for a supported, missed label
    s = accuracy.score(model, hand, judged, extra)
    row = s["table"].set_index("label").loc["demand"]
    assert row.precision == 1.0 and row.added_by_adjudication == 1 and row.precision_blind == 0.0


def test_verdict_needs_separated_intervals_to_say_the_prediction_holds():
    def table(a, a_lo, b, b_hi):
        return pd.DataFrame([{"population": "$5,000+", "group": "network-attributed", "r4 within $5": a, "r4 ci_lo": a_lo, "r4 ci_hi": a + 5},
                             {"population": "$5,000+", "group": "not network-attributed", "r4 within $5": b, "r4 ci_lo": b - 5, "r4 ci_hi": b_hi}])
    assert cause_test.verdict(table(40, 30, 20, 25)) == "holds"
    assert cause_test.verdict(table(40, 20, 20, 25)) == "consistent, not shown"
    assert cause_test.verdict(table(15, 10, 20, 25)) == "does not hold"


def test_exploratory_network_partition_is_mutually_exclusive_and_prioritised():
    assert cause_test._exploratory_cell({"network_outage", "interconnector_limit"}) == "network outage named"
    assert cause_test._exploratory_cell({"interconnector_limit", "constraint_violation"}) == "interconnector limit named, no outage"
    assert cause_test._exploratory_cell({"constraint_violation"}) == "constraint violation named, neither"
    assert cause_test._exploratory_cell({"demand"}) == "none of the three named"


def test_day_interval_resamples_whole_days_for_weighted_and_unweighted_measures():
    g = pd.DataFrame({"date": ["2020-01-01", "2020-01-01", "2020-01-02"],
                      "hit": [True, False, True], "weight": [1.0, 3.0, 2.0]})
    lo, hi = cause_test._day_interval(g, g.hit)
    low, hiw = cause_test._day_interval(g, g.weight * g.hit, g.weight)
    assert 50 <= lo <= hi <= 100
    assert 25 <= low <= hiw <= 100


def test_run_with_an_empty_list_extracts_nothing(monkeypatch):
    """An empty selection once fell through to the whole corpus (2026-11-17)."""
    monkeypatch.setattr(extract, "documents", lambda: {"u": {"file": "f.pdf", "chars": 10}})
    called = []
    monkeypatch.setattr(extract, "extract", lambda *a, **k: called.append(1))
    out = extract.run([])
    assert called == [] and out["results"] == {}


def test_stage_cap_halts_before_a_call_that_could_cross_it(monkeypatch, tmp_path):
    """The reasoning-on corpus run halts rather than spend past the cap the user set (2026-11-17)."""
    monkeypatch.setattr(extract, "RESERVATIONS", tmp_path / "res.json")
    monkeypatch.setattr(extract, "LEDGER", tmp_path / "ledger.json")
    monkeypatch.setattr(extract, "documents", lambda: {"a": {"file": "a.pdf", "chars": 10}, "b": {"file": "b.pdf", "chars": 10}})
    monkeypatch.setattr(extract, "text", lambda p: "x" * 3000)
    monkeypatch.setattr(extract, "is_cached", lambda *a: False)
    monkeypatch.setattr(extract, "default_client", lambda: None)
    called = []
    monkeypatch.setattr(extract, "extract", lambda *a, **k: called.append(1))
    wc = extract.worst_case(extract.SPEC_V3, "x" * 3000)
    monkeypatch.setattr(extract, "stage_spent", lambda *a: {"total": 1_000_000 - wc + 1})
    out = extract.run(spec=extract.SPEC_V3, stage_cap=1_000_000)
    assert called == [] and out["results"] == {}
    monkeypatch.setattr(extract, "stage_spent", lambda *a: {"total": 1_000_000 - wc})     # exactly fits: the call is made
    with pytest.raises(AttributeError):        # the stub returns None, so the run fails after the call, proving it was made
        extract.run(["a"], spec=extract.SPEC_V3, stage_cap=1_000_000)
    assert called == [1]


def test_worst_case_covers_a_retry_and_the_full_completion_allowance():
    assert extract.worst_case(extract.SPEC_V3, "") >= 2 * extract.SPEC_V3.max_tokens


def test_a_parallel_workers_reservation_counts_toward_the_cap(monkeypatch, tmp_path):
    import os
    monkeypatch.setattr(extract, "RESERVATIONS", tmp_path / "res.json")
    monkeypatch.setattr(extract, "documents", lambda: {"a": {"file": "a.pdf", "chars": 10}})
    monkeypatch.setattr(extract, "text", lambda p: "x" * 3000)
    monkeypatch.setattr(extract, "is_cached", lambda *a: False)
    monkeypatch.setattr(extract, "default_client", lambda: None)
    called = []
    monkeypatch.setattr(extract, "extract", lambda *a, **k: called.append(1))
    wc = extract.worst_case(extract.SPEC_V3, "x" * 3000)
    monkeypatch.setattr(extract, "stage_spent", lambda *a: {"total": 1_000_000 - wc})     # would fit on its own
    extract._save_reservations([{"pid": os.getppid(), "url": "other", "tokens": 1}])     # a live worker holds 1 token
    extract.run(spec=extract.SPEC_V3, stage_cap=1_000_000)
    assert called == []
    extract._save_reservations([{"pid": 2 ** 22 + 12345, "url": "other", "tokens": 1}])  # a dead worker's is dropped
    with pytest.raises(AttributeError):
        extract.run(spec=extract.SPEC_V3, stage_cap=1_000_000)
    assert called == [1] and extract._reservations() == []
