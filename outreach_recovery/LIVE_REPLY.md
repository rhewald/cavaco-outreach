# Controlled live reply milestone

The approved test email was accepted by Gmail and its recipient replied. Gmail
rewrote the MIME Message-ID. `gmail_inbound.verify_accepted` now fetches the
persisted Gmail provider ID, verifies authenticated mailbox, SENT label, thread,
and approved content, allowing only the MIME Message-ID to differ. Migration 008
records the delivered ID separately without changing approved payloads or history.

This does **not** solve a crash that loses the send response before its Gmail ID
is persisted. Those sends remain reconciliation-only and eventually require manual
inspection. A MIME search miss never authorizes a resend. Do not loosen matching
to subject/body alone.

`gmail_inbound` fetches one explicit message, checks sender/recipient headers and
thread, extracts plain text (including multipart alternatives), and invokes the
existing atomic ingestion function. Header checks are routing checks, not proof
of sender authentication. HTML-only/malformed/oversized content fails closed.
This is a supervised pull, not a Gmail Pub/Sub notification service.

The controlled test's reply is ingested and has one pending generation job. Its
context contains only internal-test facts, not production product claims.
To generate a review-only draft with a locally entered API key:

```sh
cd /Users/ruihewald/Documents/cavaco-outreach
/private/tmp/cavaco-outreach-py312/bin/python -m outreach_recovery.pilot_reply --prompt-key --review
```

The key is hidden while entered and used in process memory. This command targets
only this pilot job, never sends email, and opens the persistent pilot review queue
at http://127.0.0.1:8766/reviews. The earlier 8765 page is a separate demo database.
Repeated ingestion deduplicates the same Gmail message. The script has fixed IDs
for this authorized test and is not a general inbox poller. No delivery envelope
is attached to the generated reply, so review approval alone cannot dispatch it.

Verified offline: 128 tests pass, including changed Message-ID content checks,
inbound parsing, and existing transactional/concurrency tests.
