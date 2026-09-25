# Persistent pilot delivery service

`ai.cavaco.outreach.worker` runs `outreach_recovery.dispatch_service --pilot --watch --poll-interval 5` under the current user's macOS LaunchAgent. It starts at login, restarts after exit (30-second launchd throttle), and pauses when the Mac sleeps. This is not a 24/7 cloud service.

Apply migration 014 after 013, then use the existing `install_services` installer. The installer always sets `OUTREACH_TEST_MODE=true`, including on reinstall. It installs the inbox, CRM and delivery services together and snapshots the current code. No credentials are written into plists.

## Test mode

Unset mode defaults to true; only exact `true`/`false` values are accepted. Test mode never initializes OAuth or live provider instances. Approved send intents are inspected and simulated Gmail/CRM results are written to `delivery_simulations`, a separate mock outbox. No live queue lease, draft state, conversation version, outbound ledger entry or real CRM job is changed. Reconciliation-state records are marked reconciliation-only, not simulated as successful sends. Repeated/concurrent cycles produce at most one simulation record per operation.

This mock outbox is a dry-run snapshot, not proof of delivery. Full lease/crash/recovery behavior is exercised by the disposable PostgreSQL integration tests with fake providers. The existing inbox and CRM agents retain their prior behavior; test mode applies to this new dispatcher.

## Live path (not enabled)

The live branch reuses `DeliveryWorker` and `DeliveryRepository`; no second live queue exists. It accepts only `sdr@cavaco.ai` → `rui@cavaco.ai` and validates actual raw MIME, rejecting additional recipients. Outside-scope new sends are definitively rejected before a network call. Outside-scope uncertain operations remain unresolved for operator inspection rather than being resent.

Only approved, current envelopes can begin sending. Expired leases and uncertain outcomes enter existing reconciliation. A missing search result never authorizes another send. Gmail may rewrite MIME Message-ID; unresolved searches can require manual reconciliation. Success queues the existing independent HubSpot job; CRM failure never causes Gmail resend. No deal stage changes are performed.

Live enablement requires a separately reviewed configuration change and user sign-off on the live batch. Do not flip mode merely because the test service is healthy: review all pending intents first. Test records do not consume live intents, so pending approved intents remain eligible when live mode is enabled.

## Operations

Logs: `~/.local/share/cavaco-outreach/logs/delivery.log` and `delivery.error.log`. Logs contain mode/outcome/counters, not email bodies or tokens. Cycle failures use bounded exponential delay, up to 300 seconds.

Validation: PostgreSQL tests cover provider acceptance followed by real child-process exit, stale-before/after-claim checks, concurrent workers, lease fencing, delayed visibility, CRM outages, simulated outbox isolation, repeated simulations, allowlist/MIME checks, and default test-mode plist configuration.
