# Local human review pilot

Migration 005 adds terminal rejection, an append-only decision ledger, immutable
draft content, and `review_draft`. Approval is a recorded permission only: no
Gmail outbox or HubSpot call is created. No new model call is needed for review.

## Try the synthetic demo

```sh
python -m pip install -r outreach_recovery/requirements-test.txt
python -m outreach_recovery.review_app --demo --reviewer Rui
```

Open http://127.0.0.1:8765/reviews in a regular browser. The server uses synthetic
facts and a deterministic example draft; no API key is required. The temporary
database and decisions are discarded on shutdown. Press Ctrl+C to stop.
Use the literal 127.0.0.1 address, not localhost: Host and Origin checks use the
configured address and port exactly. `--port` can select another local port.

## Use a persistent pilot database

Apply `005_human_review.sql` once after migrations through 004, with workers stopped.
No migrations run automatically against operational databases. Set
`OUTREACH_DATABASE_URL` and `OUTREACH_REVIEWER`, then run:

```sh
python -m outreach_recovery.review_app
```

This milestone does not provision a permanent database, manage backups, or turn
on delivery. Existing draft-generation and ingestion functions remain in use.
Previously approved drafts without audit events are legacy records: they are not
retroactively attributed to anyone, and review actions cannot manufacture an
approval event for them. Review functions remain invoker-rights functions; the
runtime DB role must be able to read the displayed tables, update draft state,
lock conversations and insert review events. Production least-privilege role
provisioning and prevention of arbitrary direct state updates remain future work.

## Routes and decisions

- GET `/reviews`: pending drafts, paged in groups of 50.
- GET `/reviews/{uuid}`: current ordered conversation, pinned generation facts and
  research, immutable draft text, current state and recorded decision.
- POST `/reviews/{uuid}/approve`: approve a fresh pending draft.
- POST `/reviews/{uuid}/reject`: reject with an optional reason up to 2,000 characters.

The server chooses reviewer identity at startup. Forms cannot override it. All
mutations require same-origin requests and a cryptographically random per-process
form token. Restarting invalidates old forms. Responses disable caching, disallow
framing/scripts, and escape untrusted text. The launcher binds only to loopback
and disables forwarded-header trust. This is a single trusted-user local tool,
not authenticated multi-user access: other local processes/users may access it.
Do not expose it through a reverse proxy or public bind without real authentication
and a deployment-specific security design. The process token is not a login token.

The transaction locks conversation before draft, matching ingestion lock order.
It validates draft state/version, updates state and inserts the event in the same
transaction. Concurrent identical decisions return the existing result without
changing attribution or reason. Opposite decisions conflict. A stale pending draft
is superseded; a new reply after approval also supersedes the draft via ingestion.
Rejections remain terminal after later replies. Historical approval events remain
unchanged when a draft is superseded: current draft state controls eligibility.
At most one terminal review event exists per draft. Existing ingestion-driven
supersession does not create a new human review event. This is a decision ledger,
not a comprehensive log of all system transitions or blocked clicks.

Draft body and identity fields cannot be edited. The review page has no editing
or deletion route. A later revision workflow must create a fresh reviewable draft.
Delivery must recheck approval and version in its own transaction; this milestone
does not eliminate the time gap between review and a future Gmail API call.

## Verification

Run `python -m outreach_recovery.run_postgres_tests` with the test requirements.
Acceptance tests cover simultaneous approval clicks, approve/reject races,
new inbound vs approval, explicit version mismatch, terminal rejection, audit
rollback, immutable content, escaped HTML, CSRF and Origin/Host checks, spoofed
reviewer fields, oversized forms, and stale browser submissions.

The first live OpenAI smoke test was reported successful by the user on 2026-09-24:
model gpt-4o-mini, 339 total tokens, about 3.4 seconds, one pending-review draft.
The review demo uses its own synthetic draft rather than that cleaned-up database.

## Delivery extension (006)

Apply migration 006 before running the current review app. Prepared envelopes are
shown before approval and their IDs are checked atomically when creating a send
intent. Drafts without an envelope remain review-only. See [DELIVERY.md](DELIVERY.md).
