# Durable draft generation

This phase provides a PostgreSQL-backed generation queue, a snapshot context
builder, and a Python executor. It generates drafts for review only. It does not
send Gmail messages, write HubSpot records, or approve its own drafts.

## Database setup

Stop old generation workers before upgrading. On a fresh database, apply
`inbound.sql` then `003_draft_generation.sql`. On the earlier pilot schema, apply
`002_ingestion_concurrency.sql` then `003_draft_generation.sql`. Migration 003
replaces the old unfenced two-argument `save_reply_draft` API. Do not apply 002
again after 003; that would restore the obsolete API.

The application must populate `seller_profiles` with human-approved facts and
associate a profile with each conversation through `conversation_contexts`.
These tables are append-only: new facts/research require new rows. Store product
facts, allowed claims, prices, meeting policy, seller identity, and desired
conversation outcome in `facts`; do not place prospect instructions there.
`approved_by` records who reviewed the facts; it is not an authorization system.
Only a trusted operator/configuration path should be able to insert these rows.

A generation job without a configured profile/context goes to `dead_letter`
instead of inventing seller details. Provision context before enabling the poller.

## Claim, build, generate, save

1. `claim_reply_job(lease_seconds, optional_job_id)` locks a ready conversation
   with `FOR UPDATE SKIP LOCKED`, then its job. Pending/due jobs and expired
   generation leases are eligible. Other conversations remain available.
2. The claim checks the current conversation version, increments attempts, assigns
   a fresh token, and captures `context_snapshot` on the first claim. Subsequent
   attempts reuse the snapshot. Commit and close the connection immediately.
3. `build_prompt` assembles approved seller facts as system instructions, with
   research and conversation data in a separate user message. The triggering
   reply appears exactly once. Delimiters/roles do not guarantee injection immunity;
   the model has no delivery tools and all outputs require review.
4. A bounded model subprocess performs generation with no open worker database
   transaction. The default deadline is 60 seconds and the lease is 120 seconds.
5. `save_reply_draft(job_id, lease_token, body)` locks conversation then job and
   checks current version, state, lease token, and wall-clock lease expiry. It
   inserts the review draft and marks completion in one transaction. Invalidated
   or late output returns NULL. Repeated completion with the same completion
   token returns the original draft without changing its content.

`target_conversation_version` in the JSON is the job's `version_snapshot`.
History uses versions <= that target, sorted by version and message ID. The
triggering inbound message is separated from history. Ingestion version order,
not provider timestamps, defines the durable sequence. The first claim pins
seller/research revisions as well; this is not a retroactive seller snapshot from
the time the email originally arrived. Changed seller facts do not silently alter
an already claimed job. Operators must supersede affected work when a policy
change should invalidate existing drafts.

The worker rejects oversized context instead of silently dropping messages:
100,000 input characters and 20,000 output characters by default. The input limit
is a character bound, not a provider token count. Configure the model adapter's
token budget and further checks for the chosen model.

## Run the worker

Install `psycopg2-binary==2.9.12` in your runtime. Supply a trusted, importable
model adapter implementing this contract:

```python
def generate(messages: list, *, timeout_seconds: float) -> str:
    # Call your configured LLM / SalesGPT adapter here, with provider timeout.
    # Return only a nonempty email body. Never send email or mutate the CRM.
    # Raise generation.PermanentModelError for invalid credentials/configuration
    # or other non-retryable provider errors. Other exceptions are retried.
    ...
```

The provider adapter and its credentials are explicit runtime configuration;
there is no default model or sample sales identity. No live SalesGPT/provider
connection is installed or validated by this phase. Subprocess imports require
the adapter to be on the worker's Python path. The adapter is trusted local code,
not executable text from a prospect. Do not run test model fixtures in production.

```sh
export OUTREACH_DATABASE_URL='postgresql://...'
python -m outreach_recovery.generation --generator your_adapter:generate --once
python -m outreach_recovery.generation --generator your_adapter:generate
```

`--timeout`, `--lease-seconds`, and `--poll-seconds` configure runtime bounds.
The lease must exceed the model deadline by at least 30 seconds. No heartbeat is
needed for this bounded design; if the worker stalls past its lease, its result
is rejected. SIGTERM/SIGINT stop new claims and let the bounded current attempt
finish. Poller database errors are retried after the polling interval.

## Failure and recovery

- Transient model errors/timeouts return a still-owned job to pending with
  exponential backoff: 5, 10, 20, 40 seconds, capped at one hour. Defaults allow
  five attempts. Database scheduling, not worker sleeps, controls retry eligibility.
- Worker death leaves a processing lease. After expiry, another worker may
  regenerate the draft. Generation can therefore incur duplicate model calls;
  lease/version fencing prevents duplicate review drafts. This policy is separate
  from Gmail delivery, where ambiguous sends must never automatically resend.
- Invalid/oversized context, invalid output, permanent model errors, and exhausted
  attempts enter `dead_letter`. That is the review queue, not a discarded record.
- Raw provider exceptions and prompts are not included in persisted error text or
  worker logs. Trusted model adapters must also avoid logging sensitive prompts.
- A database error after model completion leaves recovery to the lease mechanism.
  A completed transaction remains completed even if the response was lost.

Inspect actionable failures:

```sql
SELECT id, conversation_id, attempts, max_attempts, last_error, updated_at
FROM outreach_pilot.reply_jobs
WHERE state = 'dead_letter'
ORDER BY updated_at;
```

After correcting a retryable configuration problem, an operator can grant more
attempts without erasing attempt history. Lock conversation then job, require the
current version to match, raise `max_attempts` (hard cap 20), and set pending/due.
There is deliberately no automatic dead-letter replay and no external alerting
integration yet. Stale jobs require work for the new conversation version.

## Verification and remaining boundaries

Run `python -m outreach_recovery.run_postgres_tests` in the documented test
environment. It creates an isolated PostgreSQL cluster, applies the migrations,
executes the tests, and stops/deletes the cluster. The suite covers real competing
claims, lost leases, model deadlines, new replies during generation, snapshot
stability, no model-time DB locks, retry scheduling, and dead-letter routing.

The caller must restrict access to the database functions and configuration
tables for a deployed service. The schema uses invoker privileges, not a complete
application-role security model. External model quality, live SalesGPT dependency
compatibility, approval UI, observability alerts, CRM sync, and Gmail delivery
remain separate integration work. This worker is not deployed as a background
service by installing these files.

## Seller profile validation (migration 004)

Apply `004_seller_validation.sql` after 003, with generation workers stopped.
New profile inserts and context associations are validated in PostgreSQL, including
writes made outside Python. `GenerationRepository.create_seller_profile(facts,
approved_by)` returns the profile UUID or raises `InvalidContext` with a field-level
error. Failed writes roll back. No values from rejected facts are echoed in errors.

See `seller_profile.example.json` for the version 1 shape. It contains test-only
placeholder facts; replace and review them before use. Required nonblank strings:
`company_name`, `seller_name`, `product_description`, `conversation_purpose`, and
`meeting_policy`. Each allows at most 5,000 characters. `allowed_claims` is an
array of up to 100 nonblank strings (5,000 characters each); an empty array means
no additional approved claims. Unknown keys and unsupported schema versions fail.

Pricing is explicit:

- `{"mode":"undisclosed"}`: no price is approved for disclosure.
- `{"mode":"custom"}`: custom quotation; no numeric price is approved.
- `{"mode":"fixed","plans":[{"name":"Example","currency":"EUR",
  "amount_minor":15000,"billing_interval":"month"}]}`: a declared plan.

Fixed pricing requires 1..100 plans with unique names (case-insensitive after
space trimming), amounts from 0 to 100000000000 in whole minor units, and
`one_time`, `month`, or `year` billing. The pilot currency allowlist is USD/EUR/GBP,
all with 100 minor units per major unit: 15000 EUR minor units means EUR 150.
Other currencies or pricing models require an explicit schema extension.

Existing append-only profiles are preserved, not silently rewritten. Audit them:

```sql
SELECT id, outreach_pilot.seller_facts_error(facts) AS error
FROM outreach_pilot.seller_profiles
WHERE outreach_pilot.seller_facts_error(facts) IS NOT NULL;
```

Create reviewed replacement profiles and append new conversation contexts. New
context associations cannot point at invalid legacy profiles. Claiming a job
validates its pinned facts, including snapshots from before migration 004;
invalid facts route to dead-letter with a specific error before a model call.
An already-pinned invalid snapshot cannot be rewritten or repaired by a retry;
resolve it through an explicit reviewed replacement-job workflow (not supplied).
Existing inbound ingestion still records and queues replies even if configuration
is absent; the claim boundary stops generation. This avoids losing incoming mail.

Validation enforces structure, not factual truth or authorization. `approved_by`
remains an audit attribution, and human review of business claims is still needed.
