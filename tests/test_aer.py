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


def test_run_with_an_empty_list_extracts_nothing(monkeypatch):
    """An empty selection once fell through to the whole corpus (2026-11-17)."""
    monkeypatch.setattr(extract, "documents", lambda: {"u": {"file": "f.pdf", "chars": 10}})
    called = []
    monkeypatch.setattr(extract, "extract", lambda *a, **k: called.append(1))
    out = extract.run([])
    assert called == [] and out["results"] == {}
