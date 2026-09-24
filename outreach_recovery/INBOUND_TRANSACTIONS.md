# Pilot inbound transactions

`inbound.sql` is a standalone, apply-once PostgreSQL 14+ migration in the
`outreach_pilot` schema. It is not an incremental migration of the earlier pasted
DDL and is not yet connected to `worker.py`. Keep existing application tables
unchanged while evaluating it in a disposable database.

## Ingest a fetched reply

Execute this parameterized block through the PostgreSQL driver:

```sql
BEGIN ISOLATION LEVEL READ COMMITTED;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '15s';
SELECT * FROM outreach_pilot.ingest_reply(
    %(mailbox_id)s::uuid,
    %(gmail_message_id)s,
    %(gmail_thread_id)s,
    %(mime_message_id)s,
    %(reply_ids)s::text[],
    %(body_text)s,
    %(received_at)s::timestamptz
);
COMMIT;
```

`reply_ids` contains parsed, consistently normalized In-Reply-To and References
Message-IDs. Preserve ID case and use the same representation for outbound data.
Use the authenticated mailbox identity, not a sender-supplied mailbox value.
Only call this for actual prospect replies after filtering sent mail, drafts,
bounces, and automatic messages. Gmail Pub/Sub carries a mailbox history cursor;
fetch the Gmail message before invoking this function.

The transaction locks the receipt for one Gmail message, stores its durable input,
tries outbound MIME header matching first and Gmail thread matching second,
locks the selected conversation, increments its version once,
inserts the message, supersedes stale work, and queues generation. Duplicate
Gmail message IDs return the original version without incrementing or queuing.
Ambiguous or unmatched messages remain stored without changing any conversation.
Retry unmatched receipts after outbound reconciliation; ambiguous receipts require
review of conflicting associations. No Gmail or HubSpot request runs inside SQL.

Unrelated conversations in the same mailbox can proceed concurrently. Ingestion
locks receipt -> conversation -> jobs/drafts. Draft publication locks conversation
before re-reading the job. Other writers must follow that order and never acquire
an inbound receipt lock while holding a conversation lock. Process one reply per
transaction; retry the entire transaction on deadlock, serialization failure, or
lock timeout with bounded backoff. Stored conversation identity/header mappings
must not be reassigned concurrently with ingestion.

The PostgreSQL function contains no COMMIT. Its caller owns the BEGIN/COMMIT
boundary; exceptions roll back the receipt, message, version and job together.
A content hash is not a substitute for `(mailbox_id, gmail_message_id)` identity:
two distinct messages may contain identical text. Gmail history entries must be
expanded into individual fetched messages before invoking the function.

Direct table writes bypass this application contract; restrict production roles
to vetted functions when the service is implemented. This is not the completed
production repository or a guarantee of perfect concurrency under arbitrary writers.

### Caller parameters

| Parameter | Source |
| --- | --- |
| mailbox_id | Internal UUID mapped to the authenticated Gmail account |
| gmail_message_id | Fetched Gmail message `id`, not Pub/Sub messageId/historyId |
| gmail_thread_id | Fetched Gmail message `threadId`, or NULL if unavailable |
| mime_message_id | Parsed RFC Message-ID, nullable |
| reply_ids | Parsed IDs from In-Reply-To and References, as a text array |
| body_text | Decoded reply text; filter auto-replies before this call |
| received_at | Gmail internalDate converted from epoch milliseconds to UTC |

The first stored receipt is retained across retries. A duplicate returns the
original message UUID and version. Unmatched/ambiguous outcomes return NULL IDs
and do not enqueue a draft job. Late matching can be retried using the same inputs.

## Publish a generated reply draft

Read history through the job's version snapshot, sorting by received_at and a
stable tie-breaker. Exclude the triggering message if passing it separately to
SalesGPT. Generate outside the transaction, then execute:

```sql
BEGIN;
SELECT outreach_pilot.save_reply_draft(%(job_id)s::uuid, %(body)s);
COMMIT;
```

A UUID means the draft exists in pending review (or was previously created).
NULL means the generation result is stale; do not present it for approval.
The UI must query the current draft state. New inbound messages supersede pending
and approved drafts, but cannot recall sends already started. The send repository
must recheck approval and the exact conversation version at its dispatch boundary.

## Verification

```sh
psql "$PILOT_DATABASE_URL" -v ON_ERROR_STOP=1 -f outreach_recovery/inbound.sql
psql "$PILOT_DATABASE_URL" -v ON_ERROR_STOP=1 -f outreach_recovery/inbound_checks.sql
```

The check script rolls back its fixtures and exercises duplicate ingestion,
missing inbound Message-ID, version increments, stale approval/generation,
duplicate draft results, early replies, and conflicting thread evidence.

Validated on PostgreSQL 16.2 with independent database connections, including
12 concurrent duplicate deliveries, 12 distinct simultaneous replies, mailbox
isolation, header/thread resolution, enqueue-failure rollback, lock timeout/retry,
unrelated conversation progress, and stale draft regression cases.

For an already initialized pilot schema, apply `002_ingestion_concurrency.sql`
instead of re-running `inbound.sql`. The upgrade replaces both functions without
recreating tables. Fresh databases only require `inbound.sql`.

To repeat the full disposable-database suite (Python 3.9–3.12 on a platform with
a pgserver wheel):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install pgserver==0.1.4 psycopg2-binary==2.9.12
.venv/bin/python -m outreach_recovery.run_postgres_tests
```

Alternatively install psycopg2-binary and set OUTREACH_TEST_DATABASE_URL to an
explicitly disposable database with inbound.sql applied, then run unittest
discovery. The test suite inserts fixtures and temporarily adds a failure trigger;
never point it at a production database. Integration tests skip without that URL.

## Integration work outside this transaction

- Register the pilot conversation and confirmed outbound message before normal
  reply handling; increment outbound versions through the same conversation-first lock discipline (no receipt lock needed).
- Store enrichment in prospect/research records, not in the email message ledger.
- Synchronize HubSpot using durable outbox intents, rather than simultaneous
  uncoordinated database/API writes.
- Persist Gmail history batches durably before advancing the synchronization
  cursor. Acknowledge notifications only after durable intake or job registration.
- Bind the pilot to one explicit prospect and mailbox. Review approval creates
  the immutable send intent; this migration does not grant sending permission.
- A first outbound draft has no triggering inbound message. It requires a
  separate initial-outreach path; this schema covers reply drafts only.
