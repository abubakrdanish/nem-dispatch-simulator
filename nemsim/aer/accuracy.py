"""How well the model reads the AER: python -m nemsim.aer.accuracy [model-key]

The reference is data/aer_hand_coded.json: events coded by reading the documents, before any model output
for those documents was looked at. Each event lists the cause labels the document supports for it, each with
a verbatim quote, and the network-outage and rebid statements.

Two checks, kept apart because they catch different failures:

1. **Verbatim** (mechanical, every quote in the corpus, nemsim/aer/extract.py): is the quote in the source?
   Catches invention.
2. **Support** (human, the sample only): every label the model gave on a sample event, whose quote IS
   verbatim, was judged by reading it against the document: does the quote support this label for this event
   and period? The judgements are data/aer_judgements.json. A label with a real quote that says something the
   document does not is a **misattribution**. The misattribution rate is misattributed labels over verbatim
   labels, on the sample.

Precision and recall per label compare the model's verbatim labels with the reference. A model label the
judge found supported but the blind hand-coding missed is added to the reference ("adjudicated"); both the
blind and the adjudicated figures are reported, and the count of additions.

A label is **usable** only if, on the adjudicated sample, it has at least MIN_POSITIVES reference positives,
precision of at least MIN_PRECISION and recall of at least MIN_RECALL. Fixed before any comparison was run.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict

import pandas as pd

from nemsim.aer.extract import LABELS, PRIMARY
from nemsim.config import ROOT

HAND = ROOT / "data" / "aer_hand_coded.json"
JUDGED = ROOT / "data" / "aer_judgements.json"
MODEL_LABELS = ROOT / "results" / "aer_labels.json"
OUT = ROOT / "results" / "aer_accuracy.csv"
MIN_POSITIVES, MIN_PRECISION, MIN_RECALL = 3, 0.80, 0.60
# A threshold pass is not enough when the same hand-coded sample changes verdict
# across independent runs.  solar_output passed one reasoning-on run and failed
# the other, on only four positives, so it is excluded for instability rather
# than described as a threshold failure.
UNSTABLE_LABELS = {"solar_output"}


def _key(file: str, date: str, regions) -> tuple:
    return (file, date, tuple(sorted(regions)))


def match(model_events: list[dict], hand: list[dict]) -> dict:
    """Hand event key -> the model event for the same document and date whose regions overlap most."""
    by = defaultdict(list)
    for e in model_events:
        by[(e["file"], e["event_date"])].append(e)
    out = {}
    for h in hand:
        cands = [e for e in by[(h["file"], h["event_date"])] if set(e["regions"]) & set(h["regions"])]
        if cands:
            out[_key(h["file"], h["event_date"], h["regions"])] = max(
                cands, key=lambda e: len(set(e["regions"]) & set(h["regions"])))
    return out


def score(model_events: list[dict], hand: list[dict], judged: dict, extra: set | None = None) -> dict:
    """Per-label counts, the misattribution rate, and statement agreement.

    `extra` is the adjudicated additions to the reference: (event key, label) pairs any model gave with a quote
    the judge found supported though the blind coding missed them. One reference for every model."""
    extra = extra or set()
    m = match(model_events, hand)
    counts = {k: {"tp": 0, "fp": 0, "fn": 0, "tp_blind": 0, "fp_blind": 0, "added": 0} for k in LABELS}
    mis, verb, unjudged = [], 0, []
    stmt = {"network_outages": [0, 0], "rebid_set_price": [0, 0]}
    for h in hand:
        k = _key(h["file"], h["event_date"], h["regions"])
        ref = set(h["causes"]) | {lab for (kk, lab) in extra if kk == k}
        blind = set(h["causes"])
        e = m.get(k)
        got = {c["label"]: c["quote"] for c in e["causes"]} if e else {}
        for label, quote in got.items():
            verb += 1
            j = judged.get(f"{'|'.join(map(str, k))}|{label}")
            if j is None:
                unjudged.append((k, label))
                continue
            if not j["supported"]:
                mis.append({"event": k, "label": label, "quote": quote, "why": j.get("why", "")})
        for label in LABELS:
            in_ref, in_got, in_blind = label in ref, label in got, label in blind
            c = counts[label]
            c["tp_blind"] += in_blind and in_got
            c["fp_blind"] += in_got and not in_blind
            c["added"] += in_ref and not in_blind and in_got
            if in_ref and in_got:
                c["tp"] += 1
            elif in_got:
                c["fp"] += 1
            elif in_ref:
                c["fn"] += 1
        for f in stmt:
            stmt[f][1] += 1
            stmt[f][0] += bool(e) and e[f]["value"] == h[f]["value"]
    rows = []
    for label, c in counts.items():
        pos = c["tp"] + c["fn"]
        fn_blind = sum(1 for h in hand if label in h["causes"]) - c["tp_blind"]
        rows.append({"label": label, "reference_positives": pos, "model_labels": c["tp"] + c["fp"],
                     "precision": c["tp"] / (c["tp"] + c["fp"]) if c["tp"] + c["fp"] else float("nan"),
                     "recall": c["tp"] / pos if pos else float("nan"),
                     "precision_blind": c["tp_blind"] / (c["tp_blind"] + c["fp_blind"]) if c["tp_blind"] + c["fp_blind"] else float("nan"),
                     "recall_blind": c["tp_blind"] / (c["tp_blind"] + fn_blind) if c["tp_blind"] + fn_blind else float("nan"),
                     "added_by_adjudication": c["added"]})
    t = pd.DataFrame(rows)
    t["passes_thresholds"] = ((t.reference_positives >= MIN_POSITIVES) & (t.precision >= MIN_PRECISION)
                              & (t.recall >= MIN_RECALL))
    t["usable"] = t.passes_thresholds & ~t.label.isin(UNSTABLE_LABELS)
    return {"table": t, "events_matched": len(m), "events": len(hand), "verbatim_labels": verb,
            "misattributed": mis, "misattribution_rate": len(mis) / (verb - len(unjudged)) if verb - len(unjudged) else float("nan"),
            "unjudged": unjudged,
            "statements": {f: {"agree": a, "of": n} for f, (a, n) in stmt.items()}}


def load_hand() -> list[dict]:
    return json.loads(HAND.read_text())["events"]


def load_judged(model: str = PRIMARY) -> dict:
    return json.loads(JUDGED.read_text()).get(model, {}) if JUDGED.exists() else {}


def usable_labels(model: str = PRIMARY) -> set[str]:
    """Stable labels that passed the hand-coded sample thresholds for one configuration."""
    t = pd.read_csv(OUT)
    t = t[t.model == model]
    # Older generated CSVs predate the explicit instability column.  Apply the
    # exclusion here too, so rebuilding downstream results never depends on
    # whether that gitignored CSV has already been regenerated.
    return set(t[t.usable.astype(bool) & ~t.label.isin(UNSTABLE_LABELS)].label)


def additions(models: dict[str, list[dict]], hand: list[dict]) -> set:
    """(event key, label) pairs some model gave, judged supported, that the blind hand-coding lacks."""
    out = set()
    for name, events in models.items():
        m, judged = match(events, hand), load_judged(name)
        for h in hand:
            k = _key(h["file"], h["event_date"], h["regions"])
            for c in (m[k]["causes"] if k in m else []):
                if c["label"] not in h["causes"] and judged.get(f"{'|'.join(map(str, k))}|{c['label']}", {}).get("supported"):
                    out.add((k, c["label"]))
    return out


def report(models: dict[str, list[dict]]) -> pd.DataFrame:
    hand = load_hand()
    from nemsim.aer.extract import CONFIGS, model_events
    have = {n: e for n, e in model_events(tuple(n for n in CONFIGS if n not in models), quiet=True).items() if e}
    extra = additions({**have, **models}, hand)
    print(f"reference: {sum(len(h['causes']) for h in hand)} blind hand-coded labels, plus {len(extra)} added by adjudication")
    allt = []
    for name, events in models.items():
        s = score(events, hand, load_judged(name), extra)
        t = s["table"].assign(model=name)
        allt.append(t)
        print(f"\n== {name}: {s['events_matched']} of {s['events']} hand-coded events matched; "
              f"{s['verbatim_labels']} verbatim labels; misattributed {len(s['misattributed'])} "
              f"({s['misattribution_rate'] * 100:.1f}%); unjudged {len(s['unjudged'])}")
        print("   statements agree:", s["statements"])
        print(t.round(2).to_string(index=False))
        for x in s["misattributed"]:
            print("   MIS", x["event"][1], x["event"][2], x["label"], "-", x["why"])
    out = pd.concat(allt)
    out.to_csv(OUT, index=False)
    return out


if __name__ == "__main__":
    from nemsim.aer.extract import model_events
    report(model_events())
