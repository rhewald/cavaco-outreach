# Outreach crash recovery tests

Run from the repository root with Python 3.9+:

```sh
python3 -m unittest discover -s outreach_recovery -p 'test_*.py' -v
```

This isolated module supplies an executable delivery policy and fault-injection
tests. It is not a deployed service. It makes no real Gmail or HubSpot calls and
needs no credentials or third-party packages.

The repository fake survives simulated worker restarts. Gmail can accept a
message and then crash the worker, lose the response, or hide the sent message
from searches. Recovery never calls send. A persistent dispatch-started marker
must prevent all future automatic dispatch of that same intent, regardless of
status changes or lease expiry.

This intentionally trades automatic recovery for avoiding duplicate sends:
a crash after the marker commits but before the network call leaves an uncertain
operation requiring reconciliation or human review. An empty search result does
not authorize another send. MIME Message-ID is a correlation identifier, not
provider-enforced idempotency.

## Boundaries still requiring integration work

- Implement `Repository` using PostgreSQL transactions and test with independent
  database connections/processes. The in-memory fake does not prove SQL locking,
  durability, rollback, or atomicity under real concurrency.
- Implement the Gmail adapter with mailbox credential validation, immutable MIME,
  reply thread headers, candidate validation, pagination, and no implicit send
  retries. The fake does not validate real Gmail search behavior.
- Implement HubSpot logging/reconciliation independently; these tests only verify
  creation of a unique logging intent, not delivery to HubSpot.
- A standalone inbound migration and transaction blocks are now provided in
  [INBOUND_TRANSACTIONS.md](INBOUND_TRANSACTIONS.md). They have PostgreSQL 16.2 concurrency/rollback checks and still need service integration. Review workflow and SalesGPT
  integration remain outstanding. No live service is wired to this module.
- The begin-dispatch transaction is the authorization boundary. Changes after
  that commit cannot recall an already in-flight provider request.

## Blocking findings in the supplied revision

1. Reconciliation sets `failed`, but dispatch accepts `failed`, reopening sends.
2. Composite FKs reference `(mailbox_id, id)` without a corresponding unique
   constraint on conversations. The supplied migration cannot create those FKs.
3. Draft/message/conversation alignment remains unenforced; the draft conversation
   field has no FK. Version increments must also cover inbound ingestion.
4. Reconciliation ignores `next_attempt_at`, defeating its ten-minute backoff.
5. Success does not clear its token or require `processing`, so repeated calls
   can increment the conversation version repeatedly.
6. Message search only compares Message-ID; it does not verify sent status,
   sender, recipients, or expected content.
7. The payload freeze can be bypassed by resetting status to pending. Freeze
   permanent identity and content using an irreversible dispatch-started marker.
8. The HubSpot JSON argument uses nonexistent `psycopg2.営業json`; use
   `psycopg2.extras.Json` directly. The pasted code also has broken formatting.
9. The schema omits the outbox updated_at trigger; messages still reject missing
   inbound Message-ID headers, and first outbound conversations have no Gmail
   thread ID yet despite the required column.
10. The adapter accepts model_name without ensuring the model attribute accessed
    by SalesGPT, changes only returned metadata on invalid stage, does not remove
    END_OF_TURN/END_OF_CALL markers, and does not actually filter the triggering
    reply out of supplied history. Validate against the pinned SalesGPT version.
