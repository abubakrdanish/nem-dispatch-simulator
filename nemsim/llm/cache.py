"""Model responses cached as JSON files, committed to the repository.

The cache is the evidence: every published number must rebuild from it with
no API key. A response is fetched from the model only to fill a miss, and
only when online mode is asked for explicitly (see nemsim.llm.extract).

Key = sha256 of {input_sha256, model, prompt_version, prompt_sha256}.
  input_sha256    the exact text sent as the document
  model           the model ID the API was called with
  prompt_version  a human-chosen string, bumped when a prompt changes
  prompt_sha256   hash of the system prompt, user template, schema and
                  generation settings. Editing a prompt without bumping its
                  version still misses, rather than returning answers from the
                  older prompt.

Layout: <cache_dir>/<prompt_version>/<model>/<key>.json, written once with
sorted keys and a fixed indent so diffs are stable. A rerun reads; it never
rewrites, so a committed cache stays byte-identical.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CACHE_FORMAT = 1


class CacheMiss(LookupError):
    """Offline mode found no cached response. Never falls back to a call."""


class CacheCorrupt(ValueError):
    """A cache file does not match the key it is stored under."""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class CacheKey:
    input_sha256: str
    model: str
    prompt_version: str
    prompt_sha256: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical(self.__dict__).encode("utf-8")).hexdigest()


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value)


class ResponseCache:
    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def path(self, key: CacheKey) -> Path:
        return self.directory / _slug(key.prompt_version) / _slug(key.model) / f"{key.digest}.json"

    def get(self, key: CacheKey) -> dict | None:
        path = self.path(key)
        if not path.exists():
            return None
        entry = json.loads(path.read_text(encoding="utf-8"))
        for field in ("input_sha256", "model", "prompt_version", "prompt_sha256"):
            if entry.get(field) != getattr(key, field):
                raise CacheCorrupt(f"{path}: {field} does not match its key")
        if entry.get("key") != key.digest:
            raise CacheCorrupt(f"{path}: stored key does not match")
        return entry

    def put(self, key: CacheKey, *, status: str, result: Any = None, failure: dict | None = None,
            raw_response: str | None, usage: dict | None, latency_s: float | None, attempts: int,
            source: dict | None = None) -> dict:
        if status not in ("ok", "failed"):
            raise ValueError(f"status must be ok or failed, not {status!r}")
        if status == "ok" and failure is not None or status == "failed" and result is not None:
            raise ValueError("a failed response carries no result; an ok one carries no failure")
        entry = {
            "cache_format": CACHE_FORMAT,
            "key": key.digest,
            **key.__dict__,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "status": status,
            "result": result,
            "failure": failure,
            "raw_response": raw_response,
            "usage": usage,
            "latency_s": round(latency_s, 2) if latency_s is not None else None,
            "attempts": attempts,
            "source": source or {},
        }
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(entry, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
        # Atomic: an interrupted run never leaves a half-written file that looks like a hit.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(tmp, 0o644)   # mkstemp makes 0600; these are ordinary repository files
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return entry
