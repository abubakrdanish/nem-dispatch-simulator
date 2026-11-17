"""Label each high-price event in an AER report by the causes the AER gives: python -m nemsim.aer.extract run | check

Every label must carry a verbatim quote from the document. `check` tests every quote mechanically against the
document text (normalised as in nemsim/aer/text.py) and drops any label whose quote is not found. Whether a
found quote actually supports its label, for the event claimed, is a human judgement measured on the
hand-coded sample (nemsim/aer/accuracy.py), not here.

The labels say what the AER attributed an event to, not what caused it.

Model output is cached under data/llm_cache (nemsim/llm), so `check` and everything downstream run offline.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from nemsim.aer.text import RAW, normalise, text
from nemsim.config import ROOT
from nemsim.llm import (BudgetExceeded, CacheMiss, GenerationTimeout, PermanentError, PromptSpec, TransientError,
                        default_client, extract)

OUT = ROOT / "results" / "aer_labels.json"

LABELS = {
    "demand": "high demand, or demand higher than forecast",
    "wind_output": "low wind generation, or wind lower than forecast",
    "solar_output": "low or falling solar generation (including sunset), or solar lower than forecast",
    "scheduled_plant_outage": "generating units unavailable on planned or scheduled outage",
    "unscheduled_plant_outage": "generating units that tripped, failed, or were forced out or derated unexpectedly",
    "network_outage": "a transmission or network element out of service (planned or unplanned), or a network "
                      "event such as a separation",
    "rebids": "participants rebidding capacity to higher prices or withdrawing it, or offering it at high prices",
    "battery_state_of_charge": "batteries or storage limited by their state of charge or energy",
    "ramp_rates": "generators limited by ramp rates, or slow-start plant unable to respond in time",
    "fcas_co_optimisation": "energy and FCAS competing for the same capacity, or FCAS requirements constraining energy",
    "constraint_violation": "a network constraint violated, or the price set by a constraint violation",
    "interconnector_limit": "imports limited by an interconnector reaching its limit or being constrained",
    "administered_pricing_or_intervention": "administered pricing, the cumulative price threshold, market "
                                            "suspension, directions or intervention",
}

# aer-v1 capped quotes at 700 and interval text at 200 characters; the model overran both on 2 of 5 documents
# (2026-11-17), so v2 allows 2,000 and 600. Length is checked again, verbatim, downstream.
_QUOTE = {"type": "string", "minLength": 3, "maxLength": 2000}
SCHEMA = {
    "type": "object", "required": ["events"], "additionalProperties": False,
    "properties": {"events": {"type": "array", "maxItems": 80, "items": {
        "type": "object", "additionalProperties": False,
        "required": ["event_date", "regions", "intervals", "peak_price", "causes", "constraints_named",
                     "interconnectors_named", "network_outages", "rebid_set_price"],
        "properties": {
            "event_date": {"type": "string", "minLength": 10, "maxLength": 10},
            "regions": {"type": "array", "minItems": 1, "items": {"enum": ["NSW1", "QLD1", "SA1", "TAS1", "VIC1"]}},
            "intervals": {"type": ["object", "null"], "additionalProperties": False, "required": ["text", "quote"],
                          "properties": {"text": {"type": "string", "maxLength": 600}, "quote": _QUOTE}},
            "peak_price": {"type": ["object", "null"], "additionalProperties": False, "required": ["value", "quote"],
                           "properties": {"value": {"type": "number"}, "quote": _QUOTE}},
            "causes": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["label", "quote"],
                "properties": {"label": {"enum": list(LABELS)}, "quote": _QUOTE}}},
            "constraints_named": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["id", "quote"],
                "properties": {"id": {"type": "string", "maxLength": 200}, "quote": _QUOTE}}},
            "interconnectors_named": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["name", "quote"],
                "properties": {"name": {"type": "string", "maxLength": 200}, "quote": _QUOTE}}},
            "network_outages": {"type": "object", "additionalProperties": False, "required": ["value", "quote"],
                                "properties": {"value": {"enum": ["contributed", "did_not_contribute", "not_stated"]},
                                               "quote": {"type": ["string", "null"], "maxLength": 2000}}},
            "rebid_set_price": {"type": "object", "additionalProperties": False, "required": ["value", "quote"],
                                "properties": {"value": {"enum": ["yes", "no", "not_stated"]},
                                               "quote": {"type": ["string", "null"], "maxLength": 2000}}},
        }}}},
}

_LABEL_LINES = "\n".join(f"- {k}: {v}" for k, v in LABELS.items())
SYSTEM = (
    "You read Australian Energy Regulator (AER) reports on electricity spot prices above $5,000/MWh in the "
    "National Electricity Market and record, event by event, what the AER says. You return JSON only.\n\n"
    "Rules:\n"
    "1. Every field that has a quote must be supported by a quote copied character for character from the "
    "document: no paraphrase, no ellipses, no joined fragments. If you cannot quote it exactly, leave it out.\n"
    "2. The quote must be about THIS event (this date and region). A sentence about another day, another "
    "region, another quarter or a general trend does not support a label for this event.\n"
    "3. A cause label means the AER presents that factor as contributing to this event's high price or to "
    "the tight supply-demand conditions behind it. A factor mentioned as normal, as absent, or as not "
    "contributing is not a cause.\n"
    "4. Record what the AER says, not your own view of the market.\n\n"
    f"Cause labels:\n{_LABEL_LINES}"
)
USER = (
    "List every high-price event in the document. An event is one trading day, in the region or regions the "
    "document reports together for that day. Skip events that are only about FCAS prices.\n\n"
    "For each event give:\n"
    "- event_date (YYYY-MM-DD) and regions (NSW1, QLD1, SA1, TAS1, VIC1)\n"
    "- intervals: the trading or dispatch intervals with high prices, if the document states them, with the quote; else null\n"
    "- peak_price: the highest price the document reports for the event in $/MWh, with the quote; else null\n"
    "- causes: one entry per cause label that applies, each with its quote (several labels may share a quote)\n"
    "- constraints_named: every network constraint ID the document names for this event, with the quote\n"
    "- interconnectors_named: every interconnector the document names as relevant to this event, with the quote\n"
    "- network_outages: \"contributed\" if the document says a network outage contributed, "
    "\"did_not_contribute\" if it says explicitly that none did (for example 'there were no network outages' "
    "or 'network outages did not contribute'), else \"not_stated\"; with the quote, or null when not_stated\n"
    "- rebid_set_price: \"yes\" if the document says a rebid set or caused the high price, \"no\" if it says "
    "explicitly that rebidding did not, else \"not_stated\"; with the quote, or null when not_stated\n\n"
    "Keep each quote under 400 characters: quote the one sentence that says it.\n"
    "Return exactly this shape (one object per event; arrays may be empty):\n"
    '{"events": [{"event_date": "2019-01-24", "regions": ["VIC1"], '
    '"intervals": {"text": "...", "quote": "..."} or null, '
    '"peak_price": {"value": 14500, "quote": "..."} or null, '
    '"causes": [{"label": "demand", "quote": "..."}], '
    '"constraints_named": [{"id": "...", "quote": "..."}], '
    '"interconnectors_named": [{"name": "...", "quote": "..."}], '
    '"network_outages": {"value": "not_stated", "quote": null}, '
    '"rebid_set_price": {"value": "not_stated", "quote": null}}]}'
    "\n\nDocument:\n<<<\n{document}\n>>>"
)

NO_REASONING = {"chat_template_kwargs": {"enable_thinking": False}}
SPEC = PromptSpec(version="aer-v2", system=SYSTEM, user_template=USER, schema=SCHEMA, temperature=0.0,
                  max_tokens=32768, extra_body=NO_REASONING)
# The comparison runs, on the hand-coded sample only.
SPEC_REASONING = PromptSpec(version="aer-v2", system=SYSTEM, user_template=USER, schema=SCHEMA, temperature=0.0,
                            max_tokens=65536, extra_body={})
MISTRAL = "mistralai/mistral-large-4-1t-a52b"


def documents() -> dict[str, dict]:
    """Readable documents from the collection index, keyed by URL."""
    ix = json.loads((RAW / "index.json").read_text())
    return {u: m for u, m in ix["docs"].items() if m.get("file") and m.get("chars")}


_NOSPACE = {}


def found(quote: str | None, doc: str) -> bool:
    """The quote occurs in the document, ignoring whitespace only.

    PDF text extraction inserts stray spaces inside words and before punctuation ("S ix", "f ailed",
    "Australia , materially"), and the model quotes the words as printed. So the check removes all
    whitespace from both sides and nothing else: a paraphrase, a changed word, or fragments joined with an
    ellipsis still fail. Decided after the first sample run showed the artefact (2026-11-17); the strict
    rate (whitespace runs collapsed, not removed) is reported beside it by `strict_found`."""
    if not quote:
        return False
    key = id(doc)
    if key not in _NOSPACE or _NOSPACE[key][0] is not doc:
        _NOSPACE[key] = (doc, re.sub(r"\s+", "", doc))
    return re.sub(r"\s+", "", normalise(quote)) in _NOSPACE[key][1]


def strict_found(quote: str | None, doc: str) -> bool:
    return bool(quote) and normalise(quote) in doc


def run(urls: list[str] | None = None, spec: PromptSpec = SPEC, model: str | None = None) -> dict:
    """Extract every document (or `urls`). Offline unless NEMSIM_LLM_MODE=online. Returns per-URL results."""
    docs = documents()
    todo = sorted(docs) if urls is None else list(urls)      # an empty list means none, never all
    client = None
    out, usage, missed, errors = {}, {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}, [], []
    for i, u in enumerate(todo, 1):
        t = text(ROOT / docs[u]["file"])
        try:
            if client is None:
                try:
                    client = default_client()
                except Exception:      # offline with no key: the cache is all we need
                    client = None
            r = extract(spec, t, model=model, client=client, source={"url": u, "file": docs[u]["file"]})
        except CacheMiss:
            missed.append(u)
            continue
        except BudgetExceeded as exc:
            print(f"  budget reached after {i - 1} documents: {exc}", flush=True)
            break
        except (GenerationTimeout, TransientError, PermanentError) as exc:     # never cached; recorded, not fatal
            errors.append((u, type(exc).__name__))
            print(f"  {i}/{len(todo)} {type(exc).__name__}  {Path(docs[u]['file']).name[:70]}", flush=True)
            continue
        out[u] = r
        if not r.from_cache:
            u_ = r.entry.get("usage") or {}
            usage["prompt_tokens"] += u_.get("prompt_tokens", 0) or 0
            usage["completion_tokens"] += u_.get("completion_tokens", 0) or 0
            usage["calls"] += 1
            print(f"  {i}/{len(todo)} {r.entry['status']} {u_.get('prompt_tokens')}+{u_.get('completion_tokens')} "
                  f"tokens {r.entry.get('latency_s')} s  {Path(docs[u]['file']).name[:70]}", flush=True)
    print(f"this run: {usage}; {len(out)} answered, {len(missed)} not in cache, {len(errors)} errors")
    return {"results": out, "usage": usage, "missed": missed, "errors": errors}


def checked(results: dict) -> tuple[list[dict], dict]:
    """Flatten model answers into events, dropping every label, name or statement whose quote is not verbatim."""
    docs = documents()
    events, stats = [], {"quotes": 0, "verbatim": 0, "strict_verbatim": 0, "failed_docs": 0}
    for u, r in results.items():
        if not r.ok:
            stats["failed_docs"] += 1
            continue
        doc = text(ROOT / docs[u]["file"])

        def ok_(q, doc=doc):
            stats["quotes"] += 1
            stats["strict_verbatim"] += strict_found(q, doc)
            hit = found(q, doc)
            stats["verbatim"] += hit
            return hit
        for k, e in enumerate(r.data["events"]):
            ev = {"url": u, "file": docs[u]["file"], "event_index": k, "event_date": e["event_date"],
                  "regions": sorted(set(e["regions"]))}
            for field in ("intervals", "peak_price"):
                v = e.get(field)
                if v:
                    ok = ok_(v["quote"])
                    ev[field] = v if ok else None
                else:
                    ev[field] = None
            ev["causes"], ev["dropped_causes"] = [], []
            for c in e["causes"]:
                ok = ok_(c["quote"])
                (ev["causes"] if ok else ev["dropped_causes"]).append(c)
            for field in ("constraints_named", "interconnectors_named"):
                keep = []
                for c in e[field]:
                    ok = ok_(c["quote"])
                    if ok:
                        keep.append(c)
                ev[field] = keep
            for field in ("network_outages", "rebid_set_price"):
                v = dict(e[field])
                if v["value"] not in ("not_stated",):
                    ok = ok_(v["quote"])
                    if not ok:
                        v = {"value": "not_stated", "quote": None, "dropped": e[field]}
                ev[field] = v
            events.append(ev)
    return events, stats


CONFIGS = {   # name -> (spec, model). Only the first labels the corpus; the others run on the sample.
    "nemotron": (SPEC, None),
    "nemotron-reasoning": (SPEC_REASONING, None),
    "mistral": (SPEC, MISTRAL),
}


def sample_urls() -> list[str]:
    from nemsim.aer.accuracy import load_hand
    return sorted({u for u, m in documents().items() if m["file"] in {h["file"] for h in load_hand()}})


def model_events(names=tuple(CONFIGS), quiet: bool = False) -> dict[str, list[dict]]:
    """Checked events on the hand-coded sample's documents, per configuration (offline, from the cache)."""
    out = {}
    for name in names:
        spec, model = CONFIGS[name]
        res = run(sample_urls(), spec=spec, model=model)
        if res["missed"]:
            print(f"{name}: {len(res['missed'])} sample documents not in the cache; scored on the rest")
        out[name] = checked(res["results"])[0]
    return out


def check() -> None:
    res = run()
    events, stats = checked(res["results"])
    OUT.write_text(json.dumps({"events": events, "stats": stats}, indent=1, sort_keys=True, default=str))
    print(f"{len(events)} events; {stats['verbatim']} of {stats['quotes']} quotes verbatim; "
          f"{stats['failed_docs']} documents with no valid answer; wrote {OUT}")


if __name__ == "__main__":
    {"run": lambda: run(), "check": check}[sys.argv[1] if len(sys.argv) > 1 else "check"]()
