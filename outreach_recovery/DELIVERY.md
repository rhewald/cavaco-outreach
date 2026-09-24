# Cavaco Outreach: durable delivery milestone

Update: the Gmail REST adapter now exists and is tested offline. See
[GMAIL_ADAPTER.md](GMAIL_ADAPTER.md) for current scope. The milestone description
below records the original provider-neutral foundation; OAuth onboarding and live
verification remain pending.

Implemented: provider contracts, PostgreSQL outbox/repository, a bounded worker invocation,
and fake-provider acceptance tests against PostgreSQL. No live Gmail/HubSpot calls,
credentials, OAuth setup, provider reconciliation search, or background daemon are included.

## Migration and review

Stop workers, back up the persistent database, and apply `006_delivery_outbox.sql`
once after 005. The test runner and disposable review demo apply 006 automatically.
Existing review-only approvals are not retroactively queued for delivery.

A trusted service calls `DeliveryRepository.prepare(draft_id, recipient=...,
subject=..., hubspot_portal_id=..., hubspot_contact_id=..., in_reply_to=...,
references=...)` while the draft is current and pending review. This pins one immutable
envelope with sender from the mailbox record, exact draft body, recipient, subject,
thread headers, explicit CRM destination, and serialized MIME bytes. The Message-ID
is generated once as a UUID under the sender domain and persisted before approval.
A second prepare conflicts; changing an envelope requires a fresh draft/review workflow.
Only one plain recipient is supported in this pilot; no attachments, CC, or BCC.
The future ingestion adapter must supply original subject and valid reply headers.

The review page displays the envelope and sends its ID with approval. The database
locks conversation then draft, rechecks version and displayed envelope identity,
and commits the approval audit and Gmail operation together. A form loaded before
an envelope was attached fails closed. Repeated identical approval returns
`already_approved`; it does not create another operation or increment the version.
A draft without an envelope remains review-only, preserving the synthetic demo.

## State transitions

Each external action has its own row, unique on `(draft_id, kind)`:

- `pending -> processing -> completed`
- ambiguous mutation, expired processing lease, or inconclusive lookup:
  `processing -> reconciliation_required -> processing` (lookup only)
- stale approval before dispatch: `superseded`
- definite Gmail rejection: `rejected` (no automatic resend)
- definite HubSpot non-acceptance explicitly classified safe-to-retry:
  `processing -> pending` with backoff
- eight unresolved attempts: `manual_review`; no automatic reopening

Claims use short transactions and `FOR UPDATE SKIP LOCKED` in the consistent order
conversation, draft, operation. All provider calls happen after commit. `begin()`
rechecks approval/version and active lease immediately before committing the permanent
started marker. Repeated begin for one lease is blocked. An expired lease always
switches to reconciliation, even if the worker died before reaching the API.
A fresh claim, begun send, and result are separate append-only audit events.

Completion requires current token and unexpired lease. Gmail completion atomically
records the outbound message, advances conversation version, marks the draft sent,
and queues one independent HubSpot operation with Gmail identifiers. If this
transaction fails, the processing row remains recoverable. Repeated completion has
no additional effects. HubSpot completion never schedules a Gmail send.

If conversation activity arrives after begin-dispatch, the provider call cannot be
recalled. Record actual acceptance even if the draft has since become superseded.
Invalidate old generation work and record `delivery_followups` for operator handling;
automatic regeneration for this rare race is not included. Inspect these durable
follow-up records along with `manual_review` operations before a live pilot.

## Provider contract and recovery limits

`delivery_contracts.py` defines typed mutation and reconciliation outcomes.
`DeliveryWorker.run_once(kind, operation_id=None)` performs at most one network
operation. Providers must enforce their timeout (default 30 seconds, lease 120),
disable mutation retries, use the pinned mailbox/CRM portal, and verify matched
content and identity. The operation UUID is a tracking key, not a Gmail or HubSpot
idempotency guarantee. The SDK mapping must not treat an arbitrary 5xx as proof
that a request was rejected. Unknown exceptions and malformed outcomes reconcile.
No raw error bodies, email text, tokens, or prompts are stored in attempt events.

HubSpot reconciliation needs a verified, supported correlation mechanism before
live use. There is no assumption that MIME Message-ID is a searchable, uniquely
constrained HubSpot property. If a provider cannot reliably find the activity,
return unresolved (`NOT_FOUND_YET`) and eventually route to manual review; never
create again on that evidence alone. Gmail lookup absence is likewise inconclusive.

Backoff starts at 10 seconds and doubles, capped at one hour. Both lanes honor
`next_attempt_at`. An ambiguous HubSpot outage retries lookup, not activity creation.
Only a definitively rejected CRM create can enter the create retry lane.
No exactly-once external delivery claim is made. Database fencing cannot stop a
request already executing remotely, and Gmail acceptance is not proof of inbox delivery.
The current database owner remains trusted; database role hardening is future work.

## Verification and next step

Run `python -m outreach_recovery.run_postgres_tests` with the existing test environment.
Coverage includes concurrent approvals/claims, stale drafts, old lease completions,
delayed search visibility, HubSpot uncertainty/outages, actual subprocess exit after
simulated acceptance, completion rollback, immutable content, approval-envelope
binding, and inbound activity during sending. Providers are deterministic fakes.

Next: implement the authenticated Gmail adapter, verify its reconciliation against
a controlled mailbox, then implement HubSpot creation/correlation and run a supervised
single-lead pilot. A polling service, operational dashboard for unresolved work,
mailbox eligibility/suppression checks, and live notification ingestion are still needed.

## Controlled initial Gmail delivery

Apply `007_initial_outreach.sql` after 006 to allow a manually reviewed initial
email without a fabricated inbound message. The draft still references a real
conversation, snapshots its version, and follows the same approval/outbox locks.
For an explicitly Gmail-only test, pass `gmail_only=True` and both HubSpot IDs as
`None` to `prepare`. Normal delivery still requires explicit CRM identifiers.
Gmail-only completion records the sent message but does not enqueue CRM logging.

On 2026-09-24, the user-authorized test from sdr@cavaco.ai to rui@cavaco.ai
was accepted by Gmail through DeliveryWorker and committed as completed. This is
provider acceptance, not proof of recipient inbox placement. Local pilot records
are retained in ~/.local/share/cavaco-outreach/controlled-pilot/postgres.
The first post-send reconciliation search returned not_found_yet; this does not
permit a resend. Automated inbound notification processing remains unconnected.
