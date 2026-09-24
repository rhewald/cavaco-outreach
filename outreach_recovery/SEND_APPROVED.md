# Explicit reply approval and send

The local `sdr` command lives beside the persistent Python runtime. With that directory on PATH:

```sh
sdr send-approved DRAFT_ID
```

Full path: `~/.local/share/cavaco-outreach/runtime/bin/sdr`. Default database is the persistent pilot; set `OUTREACH_DATABASE_URL` for a different deployment.

The command derives the recipient, account/contact association, subject, In-Reply-To and References from the enrolled conversation and verified triggering reply. It prepares one immutable delivery envelope and displays sender, recipient, subject and exact draft body. Only an explicit `y` at `Approve and send? [y/N]:` approves and dispatches. Blank input and noninteractive stdin do not approve. Terminal control characters are displayed as escaped text. `--preview` only prepares/displays the envelope and never approves or sends.

For an operator who has already reviewed and explicitly approved a preview, `--confirm-digest SHA256` binds noninteractive execution to that exact stored envelope. This option is not blanket authority for other drafts. Any newer reply blocks a fresh send. A review-only approval made before an envelope exists cannot silently become send approval; a new review draft is required.

Approval and durable intent use the existing transactional review function. Network delivery uses `DeliveryWorker` and the authenticated Gmail adapter. No direct send shortcut or general queue drain is introduced. Repeated completed commands return the original provider ID. Repeated pending/uncertain commands reuse the same operation and MIME bytes. Concurrent confirmations cannot create another operation.

Gmail refreshes expired access tokens through the existing OAuth provider. Ambiguous network/server outcomes require reconciliation, never automatic resend. Confirmed Gmail rejections remain terminal for operator investigation; this milestone does not enable blind Gmail mutation retries for rate limits. Reconciliation may remain unresolved if Gmail rewrote Message-ID before a crash; manual review is preferable to duplicate sending.

Confirmed acceptance records `sent`, message/thread IDs, and the independent HubSpot job in one transaction. The running CRM service handles that job, with its own retry/reconciliation rules. A Gmail acceptance is not proof of inbox placement; inspect the known accepted message and confirm receipt for the live pilot.

The existing UI detects a prepared envelope and displays its routing for review. The CLI remains the explicit dispatch entry point; no new send daemon is installed. Re-run `install_services` to update the service-code snapshot and install/update the CLI entry point.
