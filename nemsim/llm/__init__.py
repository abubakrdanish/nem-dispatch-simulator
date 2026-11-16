"""The LLM layer: read a document, return schema-valid JSON, cache everything.

    from nemsim.llm import PromptSpec, extract
    result = extract(spec, document_text, source={"doc": "...", "chunk": 0})

extract() is the only entry point extraction code needs. It:
  1. builds the cache key (input hash, model, prompt version, prompt hash);
  2. returns the cached response if there is one;
  3. on a miss, in OFFLINE mode (the default, and what CI runs), raises CacheMiss
     and never calls out;
  4. on a miss in ONLINE mode (NEMSIM_LLM_MODE=online, key in the environment),
     calls the model once, validates the JSON against the schema, and caches the
     result, or caches a failure if no valid answer came back.

What the result is for: the model reads documents and emits structured fields,
each checkable against a quoted span of the source. No model output is ever a
number in a result (see the README).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from nemsim import config
from nemsim.llm.cache import CacheKey, CacheMiss, ResponseCache, canonical, sha256_text
from nemsim.llm.client import (BASE_URL_ENV, BudgetExceeded, ChatClient, ClientConfig, GenerationTimeout,
                               PermanentError, ResponseError, TransientError)

MODE_ENV = "NEMSIM_LLM_MODE"

__all__ = ["PromptSpec", "Result", "extract", "default_client", "default_cache", "mode",
           "CacheMiss", "BudgetExceeded", "GenerationTimeout", "PermanentError", "ResponseError", "TransientError"]


@dataclass(frozen=True)
class PromptSpec:
    """A prompt, versioned. Bump `version` whenever anything here changes;
    the prompt hash in the cache key catches it if you forget, by missing."""
    version: str
    system: str
    user_template: str          # must contain {document}
    schema: dict
    temperature: float = 0.0
    max_tokens: int = 4096
    # Provider-specific request fields that change the answer, e.g. reasoning
    # controls (chat_template_kwargs, thinking_token_budget). Part of the hash.
    extra_body: dict = field(default_factory=dict)

    def __post_init__(self):
        if "{document}" not in self.user_template:
            raise ValueError("user_template must contain {document}")

    @property
    def sha256(self) -> str:
        return sha256_text(canonical({
            "system": self.system, "user_template": self.user_template, "schema": self.schema,
            "temperature": self.temperature, "max_tokens": self.max_tokens, "extra_body": self.extra_body,
        }))

    def messages(self, document: str) -> list[dict]:
        return [{"role": "system", "content": self.system},
                {"role": "user", "content": self.user_template.replace("{document}", document)}]


@dataclass
class Result:
    ok: bool
    data: Any                    # schema-valid object when ok, else None
    failure: dict | None         # {"kind", "detail"} when not ok
    from_cache: bool
    entry: dict = field(repr=False)


def mode() -> str:
    value = os.environ.get(MODE_ENV, "offline").strip().lower()
    if value not in ("offline", "online"):
        raise ValueError(f"{MODE_ENV} must be 'offline' or 'online', not {value!r}")
    return value


def llm_settings() -> dict:
    return config.settings().get("llm", {})


def default_cache() -> ResponseCache:
    return ResponseCache(config.path("llm_cache_dir"))


def default_client() -> ChatClient:
    s = llm_settings()
    cfg = ClientConfig(
        base_url=os.environ.get(BASE_URL_ENV) or s["base_url"],
        requests_per_minute=float(s.get("requests_per_minute", 10)),
        read_timeout_s=float(s.get("read_timeout_s", 120)),
        total_timeout_s=float(s.get("total_timeout_s", 1200)),
        max_calls=int(s.get("max_calls_per_run", 25)),
        max_total_tokens=int(s.get("max_tokens_per_run", 250_000)),
    )
    return ChatClient(cfg)


def _result(entry: dict, from_cache: bool) -> Result:
    ok = entry["status"] == "ok"
    return Result(ok=ok, data=entry["result"] if ok else None, failure=entry["failure"],
                  from_cache=from_cache, entry=entry)


def extract(spec: PromptSpec, document: str, *, model: str | None = None, cache: ResponseCache | None = None,
            client: ChatClient | None = None, source: dict | None = None, retry_failed: bool = False) -> Result:
    model = model or llm_settings()["model"]
    cache = cache or default_cache()
    key = CacheKey(input_sha256=sha256_text(document), model=model,
                   prompt_version=spec.version, prompt_sha256=spec.sha256)

    entry = cache.get(key)
    if entry is not None and not (retry_failed and entry["status"] == "failed"):
        return _result(entry, from_cache=True)

    if mode() != "online":
        raise CacheMiss(
            f"no cached response for prompt {spec.version!r}, model {model!r}, input {key.input_sha256[:12]} "
            f"(expected {cache.path(key)}). Offline mode never calls the model; "
            f"set {MODE_ENV}=online with the key in the environment to fill it."
        )

    client = client or default_client()
    try:
        value, completion, attempts = client.complete_json(
            model=model, messages=spec.messages(document), schema=spec.schema,
            temperature=spec.temperature, max_tokens=spec.max_tokens, extra_body=spec.extra_body)
    except ResponseError as exc:
        # The model answered but not usably: record it as a failure, never as data.
        c = exc.completion
        entry = cache.put(key, status="failed", failure={"kind": exc.kind, "detail": exc.detail},
                          raw_response=exc.raw, usage=c.usage if c else None, latency_s=c.latency_s if c else None,
                          attempts=getattr(exc, "attempts", 1), source=source)
        return _result(entry, from_cache=False)
    # TransientError, PermanentError, GenerationTimeout and BudgetExceeded propagate and are never cached.
    entry = cache.put(key, status="ok", result=value, raw_response=completion.text, usage=completion.usage,
                      latency_s=completion.latency_s, attempts=attempts, source=source)
    return _result(entry, from_cache=False)
