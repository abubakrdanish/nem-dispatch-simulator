"""Document text, cached beside the file: python -m nemsim.aer.text

PDFs are read with pypdf (the optional `aer` extra); .docx with the standard library's zipfile. The text is
normalised the same way everywhere it is compared (NFKC, whitespace collapsed), so a quote checks against
exactly what the model was sent. A document that yields almost no text (a scanned image) is unreadable.
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
import zipfile
from pathlib import Path

from nemsim.config import ROOT

RAW = ROOT / "data" / "raw" / "aer"
MIN_CHARS = 500       # fewer than this from a whole document means it is an image, not text


def normalise(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-").replace("­", "")
    return re.sub(r"\s+", " ", s).strip()


def _pdf(p: Path) -> str:
    from pypdf import PdfReader
    r = PdfReader(str(p))
    return "\n\n".join(f"[page {i}]\n{pg.extract_text() or ''}" for i, pg in enumerate(r.pages, 1))


def _docx(p: Path) -> str:
    with zipfile.ZipFile(p) as z:
        xml = z.read("word/document.xml").decode("utf8", "replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    return re.sub(r"<[^>]+>", "", xml)


def text(p: Path) -> str | None:
    """The document's text, normalised; None if unreadable. Cached as <file>.txt."""
    cache = p.with_suffix(p.suffix + ".txt")
    if cache.exists():
        t = cache.read_text()
        return t or None
    try:
        raw = _pdf(p) if p.suffix.lower() == ".pdf" else _docx(p) if p.suffix.lower() == ".docx" else ""
    except Exception as exc:      # a damaged file is unreadable, recorded, not fatal
        print(f"  unreadable {p.name}: {type(exc).__name__}", flush=True)
        raw = ""
    t = normalise(raw)
    t = t if len(re.sub(r"\[page \d+\]", "", t).strip()) >= MIN_CHARS else ""
    cache.write_text(t)
    return t or None


def run() -> None:
    ix = json.loads((RAW / "index.json").read_text())
    ok = bad = 0
    for d, m in ix["docs"].items():
        if not m.get("file"):
            continue
        t = text(ROOT / m["file"])
        m["chars"] = len(t) if t else 0
        ok, bad = ok + bool(t), bad + (not t)
    (RAW / "index.json").write_text(json.dumps(ix, indent=1, sort_keys=True))
    print(f"{ok} readable, {bad} unreadable")


if __name__ == "__main__":
    run()
