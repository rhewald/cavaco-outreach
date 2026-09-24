# Pilot inbound transactions

`inbound.sql` is a standalone, apply-once PostgreSQL 14+ migration in the
`outreach_pilot` schema. It is not an incremental migration of the earlier pasted
DDL and is not yet connected to `worker.py`. Keep existing application tables
unchanged while evaluating it in a disposable database.

## Ingest a fetched reply

Execute this parameterized block through the PostgreSQL driver:

```sql
BEGIN;
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

The transaction serializes writers per mailbox, stores a durable receipt,
matches the conversation within that mailbox, increments the version once,
inserts the message, supersedes stale work, and queues generation. Duplicate
Gmail message IDs return the original version without incrementing or queuing.
Ambiguous or unmatched messages remain stored without changing any conversation.
Retry unmatched receipts after outbound reconciliation; ambiguous receipts require
review of conflicting associations. No Gmail or HubSpot request runs inside SQL.

This lock scope is deliberately conservative for the single-lead pilot. All
future writers must acquire locks in this order: mailbox, conversation, then
messages/jobs/drafts/outbox. Direct table writes bypass this application contract;
restrict production roles to vetted functions when the service is implemented.

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

PostgreSQL tooling was not available when these files were written, so these SQL
checks have not been executed. Real concurrent-session and rollback tests remain
required before deployment. The existing Python simulation is not SQL validation.

## Integration work outside this transaction

- Register the pilot conversation and confirmed outbound message before normal
  reply handling; increment outbound versions through the same lock discipline.
- Store enrichment in prospect/research records, not in the email message ledger.
- Synchronize HubSpot using durable outbox intents, rather than simultaneous
  uncoordinated database/API writes.
- Persist Gmail history batches durably before advancing the synchronization
  cursor. Acknowledge notifications only after durable intake or job registration.
- Bind the pilot to one explicit prospect and mailbox. Review approval creates
  the immutable send intent; this migration does not grant sending permission.
- A first outbound draft has no triggering inbound message. It requires a
  separate initial-outreach path; this schema covers reply drafts only.
