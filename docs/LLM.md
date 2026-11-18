# The LLM layer

`nemsim/llm` lets later stages ask a language model to read a document and return structured JSON. Its first use is the AER cause labels (below).

## The rule it exists to keep

**Every published number rebuilds with no API key.** Every model response is cached as JSON under `data/llm_cache/` and committed. The model is called only to fill a cache miss, and only when online mode is asked for explicitly. CI has no key, runs offline, and fails on any miss.

No model-derived number appears in a result without its measured accuracy beside it, and no model output is ever a price, a flow or a rung input. The model emits fields, each carrying a span quoted verbatim from the source, so every field can be checked against the document. Extraction accuracy is measured against a hand-coded sample before any extracted field is used.

## Using it

```python
from nemsim.llm import PromptSpec, extract
result = extract(spec, document_text, source={"doc": "...", "chunk": 0})
if result.ok:
    result.data      # schema-valid object
else:
    result.failure   # {"kind": "invalid" | "truncated" | "refusal", "detail": ...}
```

| Mode | How | On a cache miss |
|---|---|---|
| **offline** (default) | nothing to set; CI sets `NEMSIM_LLM_MODE=offline` explicitly | raises `CacheMiss`, naming the expected file. Never calls out |
| **online** | `NEMSIM_LLM_MODE=online`, with `NVIDIA_API_KEY` (and optionally `NVIDIA_BASE_URL`) in the environment | calls the model once, validates, caches |

`python -m nemsim.llm.smoke` runs the end-to-end check on `tests/fixtures/llm_smoke_document.md`.

## The cache

**Key** = sha256 of:

| Part | Why |
|---|---|
| `input_sha256` | sha256 of the exact text sent as the document |
| `model` | the model ID the API was called with |
| `prompt_version` | a string bumped whenever the prompt changes |
| `prompt_sha256` | hash of the system prompt, user template, schema, temperature, `max_tokens` and `extra_body` (e.g. reasoning on/off) |

Changing the prompt invalidates the cache. Editing a prompt without bumping its version **still misses**, because the prompt hash changes; it never returns answers from an older prompt.

**Layout:** `data/llm_cache/<prompt_version>/<model>/<key>.json`, one file per response. Each file is written once (atomically, sorted keys) and never rewritten by a rerun, so a committed cache stays byte-identical.

**Each entry records:**
- the model, prompt version and both hashes
- `created_at` (UTC)
- `status`: `ok` or `failed`
- `result` (only when ok) or `failure` (only when failed)
- the model's raw answer text
- usage summed over every attempt, with each attempt's own report
- latency, attempt count, and caller-supplied `source` metadata

**Never stored:** the input text (raw documents stay gitignored; the hash identifies them) and the model's reasoning text (its token count is in `usage`).

**What gets cached:**
- **Cached:** a reply that never validates is recorded as `failed`, never as data.
- **Never cached:** network errors, HTTP errors, a generation that hits the time cap, and the per-run budget being reached. These propagate.
- **Re-asking:** `extract(..., retry_failed=True)` re-asks the model about a recorded failure.

## The client

- **Key:** read from `NVIDIA_API_KEY` only and held in an object whose repr is masked. It's sent only in the `Authorization` header. Error messages are built from the status code and a shortened error title/detail, with the key scrubbed and NVIDIA's account and function identifiers masked. The original exception is not chained into the traceback. Tests assert the key never appears in an error, a traceback or a log, and they fail if either protection is removed.
- **Streaming.** Responses are streamed. A reasoning model can take minutes, and a non-streamed request sends nothing until it finishes, so its read timeout is really a cap on total generation time. Streamed, `read_timeout_s` (120) means "silent this long" and `total_timeout_s` (1200) caps one generation. Hitting the total cap is **not retried**: tokens were spent, and a retry spends them again.
- **Failure kinds:**
  - **transient** (network, 408/409/425/429/5xx): retried with exponential backoff, honouring `Retry-After`
  - **permanent** (other 4xx): not retried
  - **response** (refusal, truncation, empty, not JSON, fails the schema): one more try, then recorded as a failure
  - **budget**: stop
- **Rate limit:** request starts are spaced at `requests_per_minute` (default 10; the trial reportedly allows about 40 per model).
- **Hard caps per run:** `max_calls_per_run` (25) and `max_tokens_per_run` (250,000). If a stream reports no usage, tokens are over-estimated rather than counted as zero.
- **Schema validation:** a small JSON-Schema subset in `nemsim/llm/schema.py`, since CI's lock has no `jsonschema`. It rejects `NaN`/`Infinity` and unsupported keywords. The model is asked for JSON mode (`response_format: json_object`); NVIDIA's docs document no schema-constrained mode, so the schema is enforced here.

Settings live under `llm:` in `config/settings.yml`.

## Model choice (2026-11-14)

**Availability.** The endpoint's own `/models` lists 83 models. The list gives no context window or limits, and listing does not mean available: `moonshotai/kimi-k2.6` is listed but returns 404 for this account.

**Chosen: `nvidia/nemotron-3-ultra-550b-a55b`, with reasoning off** (`chat_template_kwargs: {"enable_thinking": false}`). *Superseded for the AER labels on 2026-11-18: measured on the hand-coded sample, reasoning on was far more accurate and is now used (below).*
- It is available on this account and is the largest model NVIDIA hosts natively.
- **Context:** 262,144 tokens native per NVIDIA's NIM docs; the model card says up to 1M. That is enough for a whole AER report in one call.
- JSON mode works.
- With reasoning off, the smoke document took 2.8 s and 1,248 tokens, with all 8 quotes verbatim.

**Reasoning is a cost and accuracy question to settle on the hand-coded sample, not here.** With reasoning on (the default):
- The smoke document took **282 s** and 3,417 tokens, **87%** of the output being reasoning.
- One run was **still generating at 20 minutes**.
- `max_tokens=4096` truncated the reasoning before the JSON finished.
- The hosted endpoint does not reliably accept a reasoning cap: `thinking_token_budget` was accepted once, then rejected as "Unsupported parameter".

**Runner-up: `mistralai/mistral-large-4-1t-a52b`.** Context is 1M native (NVIDIA's hosted page says 262K). It took 123 s for a one-word reply on this endpoint, which looks like queueing. It's worth an A/B on the hand-coded sample.

**One finding the smoke test already shows.** One of the 8 claims attributes June 2025 to network limitations, quoting a verbatim span that lists them. The same document says they belonged to April and May, and another of its claims says so. A verbatim quote does not make a correct reading. This is why accuracy is measured against hand-coded labels.

## Quota and burn rate

- **No quota signal.** No response carried rate-limit or quota headers (200, 400 and 404 alike), and no body reported remaining credit. Burn has to be read from the account page on build.nvidia.com.
- **Token use for setting this up:**
  - **Measured:** about 14,000 tokens across the probes, the streamed diagnosis, smoke-v1 (two attempts) and smoke-v3.
  - **Not measured:** four non-streamed attempts that timed out at 300 s, and smoke-v2, stopped at the 20-minute cap. Their server-side token use is unknown; at the observed ~9 tokens/s it could be 10–25k more.

## Expected cache size

An entry stores the answer, not the input or the reasoning, so its size depends on how much the model returns. smoke-v3 (8 short claims) is 4.2 KB; a failed entry is 3.4 KB. Assume **4–20 KB per entry** for real extraction output.

**Required set: the 17 sample months.** These fall in 9 quarters:
- **Q1 2020:** the AER's separate event reports for 4 January, 30 January and 31 January 2020, about 3–4 reports.
- **Q3 2024 to Q4 2025:** six quarterly "Electricity prices above $5,000/MWh" reports.
- **Q1 and Q2 2026:** two quarterly "Significant electricity prices" reports.

That's about **11–12 documents**.

| Chunking | Entries | Size per prompt version |
|---|---|---|
| Whole report per call | 11–12 | **about 45–240 KB** |
| By event section (3–10 per report) | 35–120 | **about 0.15–2.4 MB** |

**Decade (separate, optional).**
- From 2023 the AER publishes one quarterly report; before that, one report per event. The earliest seen so far is January 2010, and 2021–2022 have many.
- I found no complete index, so the count is an estimate: about 50–225 documents for 2016–2026. That's **about 0.2–4.5 MB** whole-report, or **up to about 45 MB** by event section, per prompt version.

Each prompt version keeps its own entries unless old ones are deleted, so iterating on a prompt multiplies these figures.

## First real use: the AER cause labels (2026-11-17, re-run 2026-11-18)

**Finding: on this task, reasoning on is worth its cost.** The same model, with the same prompt and the same 142 AER reports, misattributes a quarter as many labels with reasoning on. The full tables are in section 19 of [the results](stage2_results.md).

| Configuration | Prompt version | Misattributed labels, 30 hand-coded events | Labels passing the thresholds | Tokens | Time |
|---|---|---:|---:|---:|---:|
| nemotron, **reasoning on, whole corpus (published labels)** | `aer-v3` | **4.7%** (6 of 128) | 9 | **2,886,859** for 142 reports (1,898,038 prompt, 988,821 completion of which 791,857 reasoning) | ≈ 4.8 h of generation; ≈ 2.9 h wall clock, partly 4 in parallel |
| nemotron, reasoning off, whole corpus (comparison) | `aer-v2` | 18.0% (23 of 128) | 6 | 2,096,730 for 142 reports (1,891,343 prompt, 205,387 completion) | ≈ 0.6 h of generation |
| nemotron, reasoning on, sample only (first comparison) | `aer-v2` | 3.9% (5 of 128) | 9 | 485,969 for 17 reports | ≈ 36 min |
| mistral-large-4, sample only | `aer-v2` | 0 of 17, on 2 reports only | — | 240,913 (125,000 on the sample) | 5–10 min a report |

- **What reasoning fixes.** Reasoning off files network elements (Basslink, Directlink, lines) as plant outages, *system normal* constraints as network outages, constraints that set the price as violations, and "rebidding did not contribute" as a rebid. Reasoning on makes none of the network-outage errors (0 of 15, against 4 of 16) and rescues five of the seven labels reasoning off failed. Its remaining errors are an unplanned outage called planned, a negated rebid, a constraint read as low solar output, and FCAS-market rebids.
- **What it costs.** Prompt tokens are the same either way (the report is the prompt). Completion tokens are 4.8 times as many, 80% of them reasoning, so the whole run costs 1.38 times as much. The slowest report took 589 s (64 s with reasoning off); no generation hit the 1,200 s cap.
- **The verbatim rate falls slightly** (89.0% against 91.8% of quotes, ignoring whitespace). Reasoning on quotes more often across PDF line breaks and table cells. This is the mechanical check; the misattribution rate is the one that matters.
- **Run to run.** The two reasoning-on runs over the same 17 sample reports give 3.9% and 4.7%, and solar output passes the threshold in one and not the other. A label measured on 4 positives is a coin toss at the threshold; treat it so.
- **It changed a published conclusion.** The cause-conditional test went from "holds" to "consistent, not shown" (README). Label quality was not a detail.

**What reproducing it takes.** About 2.9 million tokens and, at one report at a time, about 5 hours, or under 1.5 hours with four workers (`python -m nemsim.aer.extract run-v3 k 4`). The run stops itself before spending past 3.5 million tokens: before each call a worker takes a file lock, adds the tokens already cached under `aer-v3` to the worst cases other workers have reserved and its own report's worst case (two attempts, each with the full prompt and the 65,536-token completion allowance), and calls only if that stays within the cap. A call that fails without a cache entry is charged its worst case in `results/aer_uncached_charges.json`. Nobody needs to do this to rebuild the results: the cache is committed.

**Other notes from the runs.**
- **Mistral cannot have reasoning switched off on this endpoint.** `chat_template_kwargs: {"enable_thinking": false}` is accepted and ignored, and 90–94% of its output is reasoning, so it was stopped after two sample reports.
- **aer-v1 was abandoned.** Its quote limit (700 characters) and interval-text limit (200) failed 2 of its first 5 documents. aer-v2 and aer-v3 allow 2,000 and 600.
- **Five reports fail in each run.** Four from 2005–2008 fail in both because they name the old Snowy region, which the schema does not allow; with reasoning on, the 2 February 2006 report fails the same way. The 13 January 2017 report, which failed twice with reasoning off on an over-long constraint ID, extracts with reasoning on.
- **The trial accepted four concurrent requests** without a single refusal. One request failed with a network error early in the run and succeeded on retry.
