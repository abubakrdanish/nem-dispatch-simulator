"""End-to-end check of the LLM layer on one small document.

    python -m nemsim.llm.smoke                          # offline: reads the committed cache
    NEMSIM_LLM_MODE=online python -m nemsim.llm.smoke   # fills the cache (needs NVIDIA_API_KEY)

Not extraction: a minimal prompt that asks for claims with verbatim quotes,
then checks every quote occurs in the source. That is the property the real
extraction relies on: each field is checkable against a quoted span.
"""
from __future__ import annotations

import sys

from nemsim.config import ROOT
from nemsim.llm import PromptSpec, extract

DOCUMENT_PATH = ROOT / "tests" / "fixtures" / "llm_smoke_document.md"

SPEC = PromptSpec(
    version="smoke-v3",
    system=(
        "You extract claims from a document and return JSON only. Each claim must be supported by a quote "
        "copied character for character from the document: no paraphrase, no added or changed words, no "
        "ellipses. If you cannot quote it exactly, leave the claim out."
    ),
    user_template=(
        "List up to 8 claims the document makes about what caused the high prices in June 2025. "
        "Keep each claim under 200 characters and each quote under 400. "
        'Return {"claims": [{"claim": "<short plain statement>", "quote": "<exact span from the document>"}]}.'
        "\n\nDocument:\n<<<\n{document}\n>>>"
    ),
    schema={
        "type": "object", "required": ["claims"], "additionalProperties": False,
        "properties": {"claims": {"type": "array", "minItems": 1, "maxItems": 8, "items": {
            "type": "object", "required": ["claim", "quote"], "additionalProperties": False,
            "properties": {"claim": {"type": "string", "minLength": 1, "maxLength": 300},
                           "quote": {"type": "string", "minLength": 3, "maxLength": 600}}}}},
    },
    temperature=0.0,
    # A reasoning model spends most of its output thinking (87% on 2026-11-14).
    # smoke-v1 (max_tokens 4096) was truncated before the JSON finished;
    # smoke-v2 (16384, reasoning unbounded) was still generating at 20 minutes.
    # The hosted endpoint does not reliably accept a reasoning cap
    # (thinking_token_budget: accepted once, then "Unsupported parameter").
    # v3 turns reasoning off; whether reasoning helps accuracy is for the
    # hand-coded sample to decide, not this smoke test.
    max_tokens=4096,
    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
)


def unverifiable_quotes(document: str, claims: list[dict]) -> list[str]:
    """Quotes that do not occur verbatim in the document (whitespace normalised)."""
    flat = " ".join(document.split())
    return [c["quote"] for c in claims if " ".join(c["quote"].split()) not in flat]


def run() -> dict:
    document = DOCUMENT_PATH.read_text(encoding="utf-8")
    result = extract(SPEC, document, source={"document": str(DOCUMENT_PATH.relative_to(ROOT))})
    claims = result.data["claims"] if result.ok else []
    return {"result": result, "claims": claims, "unverifiable": unverifiable_quotes(document, claims)}


def main() -> int:
    out = run()
    entry = out["result"].entry
    print(f"model {entry['model']} | prompt {entry['prompt_version']} | status {entry['status']} | "
          f"{'cache hit' if out['result'].from_cache else 'called the model'} | usage {entry['usage']} | "
          f"latency {entry['latency_s']}s")
    for c in out["claims"]:
        print(f"- {c['claim']}\n    \"{c['quote']}\"")
    print(f"{len(out['claims'])} claims; {len(out['unverifiable'])} quotes not found verbatim in the source")
    return 0 if out["result"].ok and not out["unverifiable"] else 1


if __name__ == "__main__":
    sys.exit(main())
