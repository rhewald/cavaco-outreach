# Direct OpenAI draft adapter

The adapter implements `generate(messages, *, timeout_seconds) -> str` using
OpenAI Responses. It receives two text messages (seller instructions and untrusted
conversation data) and returns a draft body. It never calls Gmail or HubSpot.

## Install and configure

From the repository root, use Python 3.9+ and an isolated virtual environment:

```sh
python3 -m venv .venv
.venv/bin/pip install -r outreach_recovery/requirements-test.txt
```

Runtime dependencies are pinned in `requirements.txt`; `requirements-test.txt`
adds the disposable PostgreSQL runner. Supply `OPENAI_API_KEY` privately through
the process environment. Set `OUTREACH_OPENAI_MODEL` explicitly; the approved
smoke-test model is `gpt-4o-mini` (or use the user's alternative `gpt-4o`).
`.env` files are not automatically loaded. Never commit credentials.
`OUTREACH_OPENAI_MAX_OUTPUT_TOKENS` defaults to 2048, allowed range 64..8192.

## Single live smoke test

Run this in your own terminal; the API key prompt hides what you type and does
not put it in shell history. This makes one potentially billable request:

```sh
.venv/bin/python -m outreach_recovery.smoke_openai --live --model gpt-4o-mini --prompt-key
```

If the key is already in the environment, omit `--prompt-key`. The runner creates
its own disposable PostgreSQL database, applies migrations through 004, seeds a
synthetic mailbox and approved example seller facts, ingests one synthetic reply,
and targets only its new job. It does not read `OUTREACH_DATABASE_URL`. The runner
executes one attempt and never polls retries. Success means exactly one completed
job and a persisted `pending_review` draft. It prints a summary and the synthetic
draft for human inspection; the database is temporary, not a permanent pilot store.
The normal automated suite uses mocked HTTP and never calls the live provider.

## Operational worker

Provision a database through migration 004 and inject its DSN as
`OUTREACH_DATABASE_URL`, plus the key and explicit model. Start with a specific job:

```sh
.venv/bin/python -m outreach_recovery.generation \
  --generator outreach_recovery.openai_adapter:generate \
  --once --job-id YOUR_JOB_UUID
```

Omit `--job-id` and `--once` only when ready to poll the queue continuously.
Missing local configuration stops before claiming. Credentials/model access cannot
be proven without a provider request: HTTP 401/403/404 dead-letter the claimed job
and halt this worker so it does not consume additional jobs with broken credentials.
Other 4xx rejections are permanent for the job; 408/409/429 and 5xx or transport
failures use the queue's bounded retry/backoff. Incomplete, refused, malformed,
nontext/tool, oversized or internal-marker output is never saved as a draft.
A 429 caused by quota exhaustion currently uses the same bounded retry policy as
other 429 responses; it does not distinguish billing quota from temporary limits.

The SDK uses `max_retries=0`; the parent process enforces the 60-second deadline
and the worker uses a 120-second lease. The API request uses no tools, no streaming,
`store=False`, and disabled input truncation. The configured output-token cap
bounds generation size; the existing character limit bounds assembled input.
There is no additional remote token-count call. Model configuration is process
configuration, not part of the pinned seller/message snapshot; changing a model
between worker restarts may change retry outputs. Use explicit versioned model
IDs if operational reproducibility requires it.

## Logging and boundaries

Allowlisted telemetry includes model, job UUID/attempt, request ID when available,
duration, result category, status code, and token counts. SDK HTTP/debug logging
is suppressed by the adapter; raw provider errors, prompts, credentials and draft
bodies are not logged. The smoke test alone prints its synthetic draft for review.
Metrics are operational logs, not a durable billing ledger; a killed subprocess
may not emit its final metrics or know the provider's eventual completion state.

Subprocess isolation enforces deadlines; it is not a security sandbox. Trusted
Python adapters inherit the process environment. The model has no enabled tools;
prompt separation and validation do not prove factual truth or injection immunity.
Human approval remains mandatory. Generation retries can duplicate provider compute
and cost, while PostgreSQL fencing prevents stale draft publication.

## Verification

```sh
.venv/bin/python -m outreach_recovery.run_postgres_tests
```

Tests exercise the official SDK through HTTP mock transport, subprocess error
propagation, no-retry behavior, configuration guards, output rejection, metadata
redaction, real PostgreSQL publication, and single-job smoke-test selection.
A passing mocked suite is not a successful live smoke test.

Official references:
- [Responses API](https://developers.openai.com/api/reference/python/resources/responses/methods/create)
- [OpenAI Python SDK](https://developers.openai.com/api/docs/libraries)
- [GPT-4o mini](https://developers.openai.com/api/docs/models/gpt-4o-mini)
