"""Find and cache every AER report on high electricity prices: python -m nemsim.aer.collect sitemap | pages | pdfs | status

Under the National Electricity Rules (clause 3.13.7) the AER reports on each instance of the spot price
exceeding $5,000/MWh. Until 2023 it published one report per event; from then, one per quarter. Those are
the documents collected here. The broader quarterly market reports are not: they discuss prices but are not
event reports, and counting them would double-count events.

How they are found: the AER's own sitemap (robots.txt allows it; site search and faceted listings are
disallowed, so neither is used). Every sitemap URL whose path names a $5,000 or significant-price report
is fetched, and every PDF it links is cached.

Politeness: one request at a time, at least 3 seconds apart. User agent "nem-dispatch-simulator/0.1": the
AER's firewall drops any request whose user agent carries the project's parenthesised comment (tested
2026-11-16: the bare name and version get 200, the full string gets a reset), so the comment is left off for
this host. Every response is cached under data/raw/aer (gitignored) and never fetched again.
"""
from __future__ import annotations

import json
import re
import sys
import time
from html import unescape
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests

from nemsim.config import ROOT

RAW = ROOT / "data" / "raw" / "aer"
UA = "nem-dispatch-simulator/0.1"
GAP_S = 3.0
SITE = "https://www.aer.gov.au"
INDEX = RAW / "index.json"

# A page path is a candidate if it names a high-price event report.
# "Significant price variation" and the 2012 "significant price report" pages are gas-market reports.
PAGE_PATTERN = re.compile(r"(prices?-above-5000|5000-report|significant-electricity-prices|significant-price-report-"
                          r"(january|april|july|october)-[a-z]+-20(2[3-9]))", re.I)
# A linked file is kept if it is a document and its name or link text says high prices.
DOC_EXT = (".pdf", ".docx", ".doc")


class Fetcher:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.last = 0.0
        self.requests = 0
        self.challenges = 0

    def get(self, url: str, dest: Path) -> Path | None:
        """Fetch url to dest unless dest exists. Returns dest, or None on a non-200 or a bot challenge.

        The AER's CDN sometimes answers 200 with a bot-challenge page (a script asking the browser to solve a
        proof-of-work). That is a refusal, not the page: it is never solved, never cached as the page, and
        counts as not fetched. A challenge cached by an earlier run is set aside as <file>.challenge."""
        if dest.exists() and is_challenge(dest.read_bytes()[:4096]):
            dest.rename(dest.with_name(dest.name + ".challenge"))
        if dest.exists():
            return dest
        wait = self.last + GAP_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.requests += 1
        try:
            r = self.s.get(url, timeout=(15, 120))
        except requests.RequestException as exc:
            print(f"  error {url}: {type(exc).__name__}", flush=True)
            return None
        finally:
            self.last = time.monotonic()
        if r.status_code != 200:
            print(f"  {r.status_code} {url}", flush=True)
            return None
        if is_challenge(r.content[:4096]) or (dest.suffix.lower() in DOC_EXT and not real_document(r.content[:8])):
            self.challenges += 1
            print(f"  challenge {url}", flush=True)
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(r.content)
        tmp.rename(dest)
        return dest


def is_challenge(head: bytes) -> bool:
    return b"bm-verify" in head or b"_sec/verify" in head


def real_document(head: bytes) -> bool:
    return head.startswith(b"%PDF") or head.startswith(b"PK")      # PDF, or DOCX (a zip)


def _slug(url: str) -> str:
    p = urlparse(url)
    s = (p.path.strip("/") + ("_" + p.query if p.query else "")).replace("/", "__")
    return re.sub(r"[^A-Za-z0-9._,%-]", "_", s)[:180] or "root"


def sitemap(f: Fetcher) -> list[str]:
    idx = f.get(f"{SITE}/sitemap.xml", RAW / "sitemap" / "index.xml")
    pages = re.findall(r"<loc>([^<]+)</loc>", idx.read_text())
    urls = []
    for i, p in enumerate(pages, 1):
        part = f.get(unescape(p), RAW / "sitemap" / f"page_{i:03d}.xml")
        if part:
            urls += [unescape(u) for u in re.findall(r"<loc>([^<]+)</loc>", part.read_text())]
    return urls


# Not energy spot-price event reports: FCAS prices, gas markets, consultation material, news items.
EXCLUDE = re.compile(r"(fcas|sttm|gas|hub|submission|guideline|consultation|/news/|/industry/|review|issues-paper"
                     r"|decision|draft|errata|infringement)", re.I)


def candidates(urls: list[str]) -> list[str]:
    keep = set()
    for u in urls:
        path = unquote(urlparse(u).path)
        if (PAGE_PATTERN.search(path) and not EXCLUDE.search(path) and not path.lower().endswith(DOC_EXT)
                and re.search(r"^/(documents|publications)/", path)):
            keep.add(u)
    return sorted(keep)


def links(html: str, base: str) -> list[tuple[str, str]]:
    """(url, link text) for every linked document on a page."""
    out = []
    for href, text in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S | re.I):
        u = urljoin(base, unescape(href))
        if urlparse(u).path.lower().endswith(DOC_EXT) and urlparse(u).netloc.endswith("aer.gov.au"):
            out.append((u, re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescape(text))).strip()))
    return out


def page_meta(html: str) -> dict:
    title = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    date = (re.search(r'property="article:published_time" content="([^"]+)"', html)
            or re.search(r'<time[^>]+datetime="([^"]+)"', html))
    return {"title": re.sub(r"\s+", " ", unescape(title.group(1))).replace(" | Australian Energy Regulator (AER)", "").strip()
            if title else None, "published": date.group(1)[:10] if date else None}


def run(step: str) -> None:
    f = Fetcher()
    index = json.loads(INDEX.read_text()) if INDEX.exists() else {}
    if step in ("sitemap", "all"):
        urls = sitemap(f)
        cand = candidates(urls)
        index["sitemap_urls"] = len(urls)
        index["pages"] = {u: index.get("pages", {}).get(u, {}) for u in cand}
        print(f"sitemap: {len(urls)} URLs, {len(cand)} candidate report pages")
    if step in ("pages", "all"):
        for i, u in enumerate(index["pages"], 1):
            p = f.get(u, RAW / "pages" / f"{_slug(u)}.html")
            meta = {"fetched": bool(p)}
            if p:
                html = p.read_text(errors="replace")
                meta.update(page_meta(html), docs=links(html, u))
            index["pages"][u] = meta
            if i % 25 == 0:
                print(f"  {i}/{len(index['pages'])} pages, {f.requests} requests", flush=True)
                INDEX.write_text(json.dumps(index, indent=1, sort_keys=True))
    if step in ("pdfs", "all"):
        docs = {}
        for u, m in index["pages"].items():
            # A page often links the same report as PDF and DOCX: take the PDF, the DOCX only if there is none.
            linked = m.get("docs", [])
            pdfs = [x for x in linked if urlparse(x[0]).path.lower().endswith(".pdf")]
            for d, text in (pdfs or [x for x in linked if urlparse(x[0]).path.lower().endswith(".docx")]):
                docs.setdefault(d, {"pages": [], "link_text": text, "title": m.get("title"),
                                    "published": m.get("published")})["pages"].append(u)
        for i, (d, m) in enumerate(sorted(docs.items()), 1):
            name = unquote(urlparse(d).path.rsplit("/", 1)[-1])
            dest = RAW / "docs" / re.sub(r"[^A-Za-z0-9._,$()&%-]", "_", name)
            got = f.get(d, dest)
            m.update(file=str(dest.relative_to(ROOT)) if got else None, bytes=dest.stat().st_size if got else None)
            if i % 25 == 0:
                print(f"  {i}/{len(docs)} documents, {f.requests} requests", flush=True)
        index["docs"] = docs
    RAW.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps(index, indent=1, sort_keys=True))
    print(f"{f.requests} requests made this run, {f.challenges} answered with a bot challenge")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "all")
