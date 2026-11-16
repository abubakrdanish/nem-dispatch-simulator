"""The LLM layer, with no key and no network (conftest blocks sockets).

The HTTP session is replaced by FakeSession throughout; nothing here can reach
the API, and CI runs it without NVIDIA_API_KEY.
"""
from __future__ import annotations

import json
import logging
import math
import traceback

import pytest
import requests

from nemsim.llm import CacheMiss, PromptSpec, extract
from nemsim.llm.cache import CacheCorrupt, CacheKey, ResponseCache, sha256_text
from nemsim.llm.client import (BudgetExceeded, ChatClient, ClientConfig, GenerationTimeout, PermanentError,
                               ResponseError, TransientError)
from nemsim.llm.schema import SchemaError, parse_json, validate

KEY = "nvapi-SENTINEL-must-never-appear-0123456789"
MODEL = "test/model"
SCHEMA = {"type": "object", "required": ["claims"], "additionalProperties": False,
          "properties": {"claims": {"type": "array", "minItems": 1, "items": {
              "type": "object", "required": ["claim", "quote"], "additionalProperties": False,
              "properties": {"claim": {"type": "string", "minLength": 1},
                             "quote": {"type": "string", "minLength": 1}}}}}}
SPEC = PromptSpec(version="test-v1", system="Return JSON.", user_template="Document:\n{document}", schema=SCHEMA)
GOOD = {"claims": [{"claim": "the sky is blue", "quote": "sky is blue"}]}


class FakeResponse:
    """A response as requests returns it with stream=True: JSON for errors,
    server-sent event lines for a 200."""

    def __init__(self, status=200, body=None, headers=None, lines=None, raise_after=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self._lines = lines or []
        self._raise_after = raise_after
        self.closed = False

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body

    def iter_lines(self):
        for i, line in enumerate(self._lines):
            if self._raise_after is not None and i == self._raise_after:
                raise requests.exceptions.ChunkedEncodingError("connection broken")
            yield line

    def close(self):
        self.closed = True


def sse(event: dict) -> bytes:
    return b"data: " + json.dumps(event).encode()


def ok(content: str, finish="stop", tokens=100, usage=True, chunks=3):
    """A streamed 200: reasoning first, then the content split across chunks."""
    lines = [sse({"choices": [{"delta": {"reasoning_content": "thinking..."}, "finish_reason": None}]}), b""]
    size = max(1, -(-len(content) // chunks))
    for i in range(0, len(content), size):
        lines.append(sse({"choices": [{"delta": {"content": content[i:i + size]}, "finish_reason": None}]}))
    lines.append(sse({"choices": [{"delta": {}, "finish_reason": finish}]}))
    if usage:
        lines.append(sse({"choices": [], "usage": {"prompt_tokens": tokens - 10, "completion_tokens": 10,
                                                   "total_tokens": tokens}}))
    lines.append(b"data: [DONE]")
    return FakeResponse(200, lines=lines)


class FakeSession:
    """Plays back a script of responses or exceptions; records every call."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None, stream=False):
        self.calls.append({"url": url, "headers": headers, "json": json, "stream": stream, "timeout": timeout})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def client(*script, **cfg):
    clock = Clock()
    config = ClientConfig(base_url="https://example.invalid/v1", jitter=False, **cfg)
    session = FakeSession(*script)
    return ChatClient(config, api_key=KEY, session=session, clock=clock.now, sleep=clock.sleep), session, clock


@pytest.fixture
def cache(tmp_path):
    return ResponseCache(tmp_path / "llm_cache")


# ------------------------------------------------------------------ schema

def test_schema_accepts_valid_and_rejects_invalid():
    validate(GOOD, SCHEMA)
    for bad in ({}, {"claims": []}, {"claims": [{"claim": "x"}]}, {"claims": [{"claim": "x", "quote": "y", "z": 1}]},
                {"claims": "x"}):
        with pytest.raises(SchemaError):
            validate(bad, SCHEMA)


def test_parse_json_rejects_nan_and_prose():
    with pytest.raises(SchemaError):
        parse_json('{"confidence": NaN}')
    with pytest.raises(SchemaError):
        parse_json('Sure! Here is the JSON: {"a": 1}')
    assert parse_json('<think>hmm</think>\n```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(SchemaError):
        validate(math.inf, {"type": "number"})


def test_unsupported_schema_keyword_is_an_error():
    with pytest.raises(SchemaError, match="unsupported"):
        validate("x", {"type": "string", "pattern": "^x$"})


# ------------------------------------------------------------------ cache key

def test_cache_key_changes_with_every_component():
    base = dict(input_sha256=sha256_text("doc"), model=MODEL, prompt_version="v1", prompt_sha256=SPEC.sha256)
    digests = {CacheKey(**base).digest}
    for field, value in [("input_sha256", sha256_text("doc2")), ("model", "other/model"),
                         ("prompt_version", "v2"), ("prompt_sha256", "0" * 64)]:
        digests.add(CacheKey(**{**base, field: value}).digest)
    assert len(digests) == 5


def test_editing_a_prompt_without_bumping_the_version_still_misses():
    edited = PromptSpec(version=SPEC.version, system=SPEC.system + " Be brief.",
                        user_template=SPEC.user_template, schema=SPEC.schema)
    assert edited.sha256 != SPEC.sha256


def test_extra_body_is_sent_and_changes_the_prompt_hash():
    spec = PromptSpec(version="test-v1", system=SPEC.system, user_template=SPEC.user_template, schema=SCHEMA,
                      extra_body={"thinking_token_budget": 2048})
    assert spec.sha256 != SPEC.sha256
    c, session, _ = client(ok(json.dumps(GOOD)))
    c.complete(model=MODEL, messages=[], extra_body=spec.extra_body)
    assert session.calls[0]["json"]["thinking_token_budget"] == 2048
    with pytest.raises(ValueError):
        c.complete(model=MODEL, messages=[], extra_body={"model": "other"})


def test_cache_detects_a_file_under_the_wrong_key(cache):
    key = CacheKey(sha256_text("doc"), MODEL, "v1", SPEC.sha256)
    cache.put(key, status="ok", result=GOOD, raw_response="{}", usage={}, latency_s=1.0, attempts=1)
    path = cache.path(key)
    entry = json.loads(path.read_text())
    entry["model"] = "tampered"
    path.write_text(json.dumps(entry))
    with pytest.raises(CacheCorrupt):
        cache.get(key)


# ------------------------------------------------------------------ offline mode

def test_offline_cache_miss_raises_and_never_calls(cache, monkeypatch):
    monkeypatch.setenv("NEMSIM_LLM_MODE", "offline")
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    c, session, _ = client()
    with pytest.raises(CacheMiss, match="Offline mode never calls"):
        extract(SPEC, "a document", model=MODEL, cache=cache, client=c)
    assert session.calls == []


def test_offline_is_the_default(cache, monkeypatch):
    monkeypatch.delenv("NEMSIM_LLM_MODE", raising=False)
    with pytest.raises(CacheMiss):
        extract(SPEC, "a document", model=MODEL, cache=cache)


def test_offline_hit_returns_cached_result_without_a_client(cache, monkeypatch):
    monkeypatch.setenv("NEMSIM_LLM_MODE", "online")
    c, _, _ = client(ok(json.dumps(GOOD)))
    first = extract(SPEC, "a document", model=MODEL, cache=cache, client=c)
    assert first.ok and not first.from_cache

    monkeypatch.setenv("NEMSIM_LLM_MODE", "offline")
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    again = extract(SPEC, "a document", model=MODEL, cache=cache)
    assert again.ok and again.from_cache and again.data == GOOD


# ------------------------------------------------------------------ online behaviour (mocked)

def test_online_caches_tokens_model_version_and_time(cache, monkeypatch):
    monkeypatch.setenv("NEMSIM_LLM_MODE", "online")
    c, _, _ = client(ok(json.dumps(GOOD), tokens=321))
    result = extract(SPEC, "a document", model=MODEL, cache=cache, client=c, source={"doc": "x"})
    entry = result.entry
    assert entry["model"] == MODEL and entry["prompt_version"] == "test-v1"
    assert entry["usage"]["total_tokens"] == 321 and entry["created_at"]
    assert entry["source"] == {"doc": "x"}


def test_schema_failure_is_recorded_as_failure_never_as_data(cache, monkeypatch):
    monkeypatch.setenv("NEMSIM_LLM_MODE", "online")
    bad = json.dumps({"claims": [{"claim": "no quote"}]})
    c, session, _ = client(ok(bad), ok(bad))   # first try plus one retry
    result = extract(SPEC, "a document", model=MODEL, cache=cache, client=c)
    assert not result.ok and result.data is None
    assert result.failure["kind"] == "invalid"
    assert result.entry["result"] is None
    assert len(session.calls) == 2


def test_malformed_then_valid_is_retried_once(monkeypatch):
    c, session, _ = client(ok("not json"), ok(json.dumps(GOOD)))
    value, _, attempts = c.complete_json(model=MODEL, messages=[], schema=SCHEMA)
    assert value == GOOD and attempts == 2


def test_usage_recorded_is_the_sum_over_every_attempt(cache, monkeypatch):
    """smoke-v1 recorded only its last attempt's tokens and understated the burn."""
    monkeypatch.setenv("NEMSIM_LLM_MODE", "online")
    c, _, _ = client(ok('{"claims": [', finish="length", tokens=4900), ok(json.dumps(GOOD), tokens=4300))
    result = extract(SPEC, "a document", model=MODEL, cache=cache, client=c)
    assert result.ok
    assert result.entry["usage"]["total_tokens"] == 9200
    assert [u["total_tokens"] for u in result.entry["usage"]["per_attempt"]] == [4900, 4300]


def test_refusal_and_truncation_are_response_errors():
    c, _, _ = client(ok("", finish="content_filter"), ok("", finish="content_filter"))
    with pytest.raises(ResponseError) as exc:
        c.complete_json(model=MODEL, messages=[], schema=SCHEMA)
    assert exc.value.kind == "refusal"
    c, _, _ = client(ok('{"claims": [', finish="length"), ok('{"claims": [', finish="length"))
    with pytest.raises(ResponseError) as exc:
        c.complete_json(model=MODEL, messages=[], schema=SCHEMA)
    assert exc.value.kind == "truncated"


def test_transient_errors_retry_with_backoff_and_honour_retry_after():
    c, session, clock = client(FakeResponse(429, {"title": "Too Many Requests"}, headers={"Retry-After": "7"}),
                               FakeResponse(503, {"title": "Unavailable"}),
                               requests.ConnectionError("reset"),
                               ok(json.dumps(GOOD)))
    completion = c.complete(model=MODEL, messages=[])
    assert completion.attempts == 4 and len(session.calls) == 4
    assert 7.0 in clock.sleeps            # Retry-After honoured
    assert 4.0 in clock.sleeps and 8.0 in clock.sleeps   # 2 * 2**1, 2 * 2**2 backoff


def test_error_detail_is_kept_but_account_ids_are_masked():
    body = {"status": 404, "title": "Not Found",
            "detail": "Function 'abc-123': Not found for account 'ACCT-SECRETISH'"}
    c, _, _ = client(FakeResponse(404, body))
    with pytest.raises(PermanentError) as exc:
        c.complete(model=MODEL, messages=[])
    assert "Not found for account" in str(exc.value) and "ACCT-SECRETISH" not in str(exc.value)
    assert "abc-123" not in str(exc.value)


def test_permanent_errors_are_not_retried():
    c, session, _ = client(FakeResponse(401, {"title": "Unauthorized"}))
    with pytest.raises(PermanentError):
        c.complete(model=MODEL, messages=[])
    assert len(session.calls) == 1


def test_transient_retries_are_bounded():
    c, session, _ = client(*[FakeResponse(500, {"title": "x"})] * 3, transient_retries=2)
    with pytest.raises(TransientError):
        c.complete(model=MODEL, messages=[])
    assert len(session.calls) == 3


def test_transient_errors_are_never_cached(cache, monkeypatch):
    monkeypatch.setenv("NEMSIM_LLM_MODE", "online")
    c, _, _ = client(*[FakeResponse(500, {"title": "x"})] * 5)
    with pytest.raises(TransientError):
        extract(SPEC, "a document", model=MODEL, cache=cache, client=c)
    assert not list(cache.directory.rglob("*.json"))


def test_rate_limit_spaces_request_starts():
    c, _, clock = client(*[ok(json.dumps(GOOD))] * 3, requests_per_minute=6)
    for _ in range(3):
        c.complete(model=MODEL, messages=[])
    assert clock.sleeps == [10.0, 10.0]


def test_call_cap_stops_a_runaway_loop():
    c, session, _ = client(*[ok(json.dumps(GOOD))] * 10, max_calls=3)
    for _ in range(3):
        c.complete(model=MODEL, messages=[])
    with pytest.raises(BudgetExceeded):
        c.complete(model=MODEL, messages=[])
    assert len(session.calls) == 3


def test_token_cap_stops_further_calls():
    c, session, _ = client(*[ok(json.dumps(GOOD), tokens=600)] * 5, max_total_tokens=1000)
    c.complete(model=MODEL, messages=[])
    c.complete(model=MODEL, messages=[])
    with pytest.raises(BudgetExceeded):
        c.complete(model=MODEL, messages=[])
    assert len(session.calls) == 2


# ------------------------------------------------------------------ streaming

def test_streamed_reply_is_reassembled_and_reasoning_is_not_kept():
    c, session, _ = client(ok(json.dumps(GOOD), tokens=3417, chunks=5))
    completion = c.complete(model=MODEL, messages=[])
    assert json.loads(completion.text) == GOOD and "thinking" not in completion.text
    assert completion.usage["total_tokens"] == 3417 and completion.finish_reason == "stop"
    assert session.calls[0]["stream"] is True
    assert session.calls[0]["json"]["stream_options"] == {"include_usage": True}


def test_still_generating_at_the_total_cap_is_not_retried():
    """282 s was seen for 400 words. Past the cap tokens were spent, so retrying would spend them again."""
    slow = ok(json.dumps(GOOD))
    c, session, clock = client(slow, ok(json.dumps(GOOD)), total_timeout_s=60)
    lines = list(slow._lines)

    def creeping():
        for line in lines:
            clock.t += 25          # 25 s between chunks: under the read timeout, over the total cap
            yield line
    slow.iter_lines = creeping
    with pytest.raises(GenerationTimeout):
        c.complete(model=MODEL, messages=[])
    assert len(session.calls) == 1 and slow.closed


def test_interrupted_stream_is_transient_and_retried():
    c, session, _ = client(FakeResponse(200, lines=ok("x")._lines, raise_after=2), ok(json.dumps(GOOD)))
    completion = c.complete(model=MODEL, messages=[])
    assert json.loads(completion.text) == GOOD and len(session.calls) == 2


def test_missing_usage_is_estimated_high_so_the_token_cap_still_counts():
    c, _, _ = client(ok(json.dumps(GOOD), usage=False))
    completion = c.complete(model=MODEL, messages=[{"role": "user", "content": "x" * 500}])
    assert completion.usage["estimated_total_tokens"] > 500
    assert c.usage.total_tokens == completion.usage["estimated_total_tokens"]


# ------------------------------------------------------------------ the key never leaks

def _everything_visible(exc: BaseException, caplog) -> str:
    return "\n".join([str(exc), repr(exc), "".join(traceback.format_exception(exc)), caplog.text])


@pytest.mark.parametrize("failure", [
    requests.ConnectionError(f"failed to connect with Authorization: Bearer {KEY}"),
    FakeResponse(401, {"title": f"Unauthorized {KEY}", "detail": f"key {KEY} rejected"}),
    FakeResponse(500, {"title": f"oops {KEY}"}),
])
def test_key_never_appears_in_errors_tracebacks_or_logs(failure, caplog):
    caplog.set_level(logging.DEBUG)
    c, _, _ = client(*[failure] * 6, transient_retries=1)
    with pytest.raises(Exception) as exc:
        c.complete(model=MODEL, messages=[])
    assert KEY not in _everything_visible(exc.value, caplog)
    assert KEY not in repr(c) and KEY not in repr(c._key) and KEY not in str(vars(c))


def test_missing_key_error_names_the_variable_not_a_value(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    with pytest.raises(PermanentError, match="NVIDIA_API_KEY is not set"):
        ChatClient(ClientConfig(base_url="https://example.invalid/v1"))


def test_key_is_sent_only_in_the_authorization_header():
    c, session, _ = client(ok(json.dumps(GOOD)))
    c.complete(model=MODEL, messages=[{"role": "user", "content": "hi"}])
    call = session.calls[0]
    assert call["headers"]["Authorization"] == f"Bearer {KEY}"
    assert KEY not in json.dumps(call["json"]) and KEY not in call["url"]


# ------------------------------------------------------------------ the committed cache rebuilds offline

def test_committed_smoke_entry_rebuilds_offline_with_no_key(monkeypatch):
    """The repository's own cache answers the smoke document with no key and no
    network, and every quote in it occurs verbatim in the source."""
    monkeypatch.setenv("NEMSIM_LLM_MODE", "offline")
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    from nemsim.llm import smoke

    out = smoke.run()
    assert out["result"].from_cache and out["result"].ok
    assert out["claims"] and out["unverifiable"] == []
