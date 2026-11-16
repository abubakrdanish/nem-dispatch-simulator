"""OpenAI-compatible chat client: retries, rate limit, per-run budget, and a
key that never appears in a log, an exception or a traceback.

Failure kinds are kept apart because they need different handling:
  TransientError  network error, timeout, 429, 5xx: retried with backoff
  PermanentError  any other HTTP error (bad key, unknown model, bad request): not retried
  ResponseError   HTTP 200 but no usable answer: refusal, truncation, empty,
                  not JSON, or JSON that fails the schema. Retried a little,
                  then recorded as a failure. Never treated as data.
  BudgetExceeded  the per-run call or token cap is reached: stop, do not retry
  GenerationTimeout  the model was still generating at the total time cap. Not
                  retried: tokens were spent, and a retry would spend them again.

Responses are streamed. A reasoning model can take minutes to answer (282 s for
a 400-word document on 2026-11-14), and a non-streamed request sends nothing
until it finishes, so a read timeout would really be a cap on total generation
time. Streamed, the read timeout means "silent for this long" and the total is
capped separately.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from nemsim.llm.schema import SchemaError, parse_json, validate

logger = logging.getLogger(__name__)

KEY_ENV = "NVIDIA_API_KEY"
BASE_URL_ENV = "NVIDIA_BASE_URL"
TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class LLMError(Exception):
    """Base class. Messages are built only from safe parts and scrubbed again."""


class TransientError(LLMError):
    pass


class PermanentError(LLMError):
    pass


class BudgetExceeded(LLMError):
    pass


class GenerationTimeout(LLMError):
    pass


class ResponseError(LLMError):
    def __init__(self, kind: str, detail: str, *, raw: str | None = None, completion: "Completion | None" = None):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
        self.raw = raw
        self.completion = completion


class _Secret:
    """Holds the key. Its repr and str are masked, so it cannot leak through
    a debugger, a log of locals, or an f-string by accident."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<secret>"

    __str__ = __repr__


@dataclass
class ClientConfig:
    base_url: str
    requests_per_minute: float = 10.0       # well under the ~40/min reported for the trial
    read_timeout_s: float = 120.0           # longest silence allowed while streaming (17 s seen)
    total_timeout_s: float = 1200.0         # cap on one generation; 282 s seen for 400 words
    transient_retries: int = 4
    malformed_retries: int = 1              # one more try on refusal / bad JSON / schema failure
    backoff_base_s: float = 2.0
    backoff_max_s: float = 60.0
    jitter: bool = True
    max_calls: int = 25                     # hard cap per client, i.e. per run
    max_total_tokens: int = 250_000         # hard cap per run, prompt + completion


@dataclass
class Completion:
    text: str
    usage: dict
    latency_s: float
    finish_reason: str | None
    attempts: int


def total_usage(per_attempt: list[dict]) -> dict:
    """Token counts summed over attempts, with each attempt's own report kept."""
    def total(field: str) -> int:
        return sum(int(u.get(field) or 0) for u in per_attempt)
    reasoning = sum(int((u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0) for u in per_attempt)
    return {"total_tokens": total("total_tokens") or total("estimated_total_tokens"),
            "prompt_tokens": total("prompt_tokens"), "completion_tokens": total("completion_tokens"),
            "reasoning_tokens": reasoning, "per_attempt": per_attempt}


@dataclass
class Usage:
    calls: int = 0
    total_tokens: int = 0
    by_model: dict = field(default_factory=dict)


class ChatClient:
    def __init__(self, config: ClientConfig, *, api_key: str | None = None, session=None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        key = api_key if api_key is not None else os.environ.get(KEY_ENV, "")
        if not key:
            raise PermanentError(f"{KEY_ENV} is not set; online mode needs it in the environment")
        self._key = _Secret(key)
        self.config = config
        self.session = session or requests.Session()
        self._clock = clock
        self._sleep = sleep
        self._last_start: float | None = None
        self.usage = Usage()

    def __repr__(self) -> str:
        return f"ChatClient(base_url={self.config.base_url!r}, calls={self.usage.calls})"

    # ------------------------------------------------------------ safety

    def _scrub(self, text: str) -> str:
        key = self._key.reveal()
        return text.replace(key, "<redacted>") if key else text

    def _raise(self, cls, message: str, **kwargs):
        # `from None`: the original exception (which may carry request objects)
        # is not chained into the traceback.
        raise cls(self._scrub(message), **kwargs) from None

    # ------------------------------------------------------------ pacing and budget

    def _pace(self) -> None:
        gap = 60.0 / self.config.requests_per_minute
        if self._last_start is not None:
            wait = self._last_start + gap - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._last_start = self._clock()

    def _check_budget(self) -> None:
        if self.usage.calls >= self.config.max_calls:
            raise BudgetExceeded(f"call cap reached ({self.config.max_calls} per run)")
        if self.usage.total_tokens >= self.config.max_total_tokens:
            raise BudgetExceeded(f"token cap reached ({self.usage.total_tokens} of {self.config.max_total_tokens})")

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), self.config.backoff_max_s)
            except ValueError:
                pass
        delay = min(self.config.backoff_base_s * 2 ** attempt, self.config.backoff_max_s)
        return delay + (random.uniform(0, self.config.backoff_base_s) if self.config.jitter else 0.0)

    # ------------------------------------------------------------ one HTTP exchange

    def _post_once(self, body: dict) -> tuple[dict, float]:
        self._check_budget()
        self._pace()
        self.usage.calls += 1
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {self._key.reveal()}", "Accept": "text/event-stream"}
        start = self._clock()
        try:
            response = self.session.post(url, headers=headers, json=body, stream=True,
                                         timeout=(10, self.config.read_timeout_s))
        except requests.Timeout:
            self._raise(TransientError, f"no response within {self.config.read_timeout_s}s")
        except requests.RequestException as exc:
            self._raise(TransientError, f"network error ({type(exc).__name__})")

        if response.status_code != 200:
            message = f"HTTP {response.status_code}" + self._error_summary(response)
            if response.status_code in TRANSIENT_STATUS:
                err = TransientError(self._scrub(message))
                err.retry_after = response.headers.get("Retry-After")
                raise err from None
            self._raise(PermanentError, message)

        try:
            data = self._read_stream(response, start)
        except (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError) as exc:
            self._raise(TransientError, f"stream interrupted ({type(exc).__name__})")
        finally:
            response.close()
        return data, self._clock() - start

    def _error_summary(self, response) -> str:
        """Title and detail of an error body, shortened, with the key scrubbed and
        account or function identifiers masked (NVIDIA's 404s name the account)."""
        try:
            body = response.json()
        except ValueError:
            return ""
        if not isinstance(body, dict):
            return ""
        parts = [str(body.get(k, "")) for k in ("title", "detail", "message") if body.get(k)]
        if isinstance(body.get("error"), dict):
            parts.append(str(body["error"].get("message", "")))
        text = re.sub(r"(?i)\b(account|function)\s+'[^']*'", r"\1 '<masked>'", " | ".join(parts))
        return f": {text[:240]}" if text else ""

    def _read_stream(self, response, start: float) -> dict:
        """Server-sent events into the shape of a non-streamed reply. Reasoning
        text is counted but not kept: usage records its tokens, and the cache
        stores the answer."""
        content: list[str] = []
        finish_reason = None
        usage: dict = {}
        for line in response.iter_lines():
            if self._clock() - start > self.config.total_timeout_s:
                self._raise(GenerationTimeout,
                            f"still generating after {self.config.total_timeout_s:.0f}s; not retried")
            if not line or not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                break
            try:
                event = json.loads(payload)
            except ValueError:
                self._raise(TransientError, "malformed stream event")
            if event.get("usage"):
                usage = event["usage"]
            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    content.append(delta["content"])
                finish_reason = choice.get("finish_reason") or finish_reason
        return {"choices": [{"message": {"content": "".join(content)}, "finish_reason": finish_reason}],
                "usage": usage}

    def complete(self, *, model: str, messages: list[dict], temperature: float = 0.0,
                 max_tokens: int = 4096, json_mode: bool = True, extra_body: dict | None = None) -> Completion:
        body: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature,
                                "max_tokens": max_tokens, "stream": True,
                                "stream_options": {"include_usage": True}}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        for name, value in (extra_body or {}).items():
            if name in body:
                raise ValueError(f"extra_body may not override {name!r}")
            body[name] = value

        attempt = 0
        while True:
            try:
                data, latency = self._post_once(body)
                break
            except TransientError as exc:
                if attempt >= self.config.transient_retries:
                    raise
                delay = self._backoff(attempt, getattr(exc, "retry_after", None))
                logger.warning("LLM transient failure (%s); retry %d in %.1fs", exc, attempt + 1, delay)
                self._sleep(delay)
                attempt += 1

        usage = data.get("usage") or {}
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        tokens = int(usage.get("total_tokens") or 0)
        if not tokens:
            # No usage reported: the cap must still count something. One token
            # per character overstates text several times over, which covers the
            # reasoning tokens a stream does not show.
            tokens = len(json.dumps(messages)) + len(message.get("content") or "")
            usage = {**usage, "estimated_total_tokens": tokens}
        self.usage.total_tokens += tokens
        self.usage.by_model[model] = self.usage.by_model.get(model, 0) + tokens
        return Completion(text=message.get("content") or "", usage=usage, latency_s=latency,
                          finish_reason=choice.get("finish_reason"), attempts=attempt + 1)

    def complete_json(self, *, model: str, messages: list[dict], schema: dict, temperature: float = 0.0,
                      max_tokens: int = 4096, extra_body: dict | None = None) -> tuple[Any, Completion, int]:
        """A schema-valid object, its completion, and the number of attempts.
        Raises ResponseError (carrying the last raw reply) if no attempt is valid."""
        last: ResponseError | None = None
        attempts = 0
        per_attempt: list[dict] = []
        for _ in range(self.config.malformed_retries + 1):
            completion = self.complete(model=model, messages=messages, temperature=temperature,
                                       max_tokens=max_tokens, extra_body=extra_body)
            attempts += completion.attempts
            per_attempt.append(completion.usage)
            # Record what every attempt cost, not just the last one.
            completion.usage = total_usage(per_attempt)
            try:
                if completion.finish_reason == "content_filter":
                    raise ResponseError("refusal", "finish_reason=content_filter")
                if completion.finish_reason == "length":
                    raise ResponseError("truncated", f"hit max_tokens={max_tokens}")
                value = parse_json(completion.text)
                validate(value, schema)
                return value, completion, attempts
            except SchemaError as exc:
                last = ResponseError("invalid", str(exc), raw=completion.text, completion=completion)
            except ResponseError as exc:
                last = ResponseError(exc.kind, exc.detail, raw=completion.text, completion=completion)
            logger.warning("LLM response unusable (%s)", last)
        last.attempts = attempts
        raise last
