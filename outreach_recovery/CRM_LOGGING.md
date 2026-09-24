# HubSpot email activity logging

`hubspot_adapter.HubSpotAdapter` implements the existing provider contract. It logs CRM email activities, never sends mail. The adapter pins the HubSpot account, verifies the contact email, and associates each activity using email-to-contact type 198. HTTP mutation retries and redirects are disabled.

Existing outbound `DeliveryRepository` jobs can use this adapter. For already-sent Gmail-only messages and received replies, apply `009_crm_activity_jobs.sql` after migration 008 and use `CRMRepository`. This separate lane does not change the immutable original delivery envelope or schedule Gmail sends.

`prepare()` freezes a message body/direction/timestamp and its approved routing and contact association. `approve()` requires the SHA256 digest of that payload and a reviewer. Claims exclude unapproved records. Concurrent preparation and approval are idempotent; an immutable source message may have only one job per portal. Events are append-only. Lost leases cannot complete a job.

Before creating an activity, the adapter searches for the same subject, timestamp, direction, body, sender, recipient and contact association. A single verified match is reused. Multiple matches or an incomplete search remain unresolved. Search indexing delay means a negative result is not proof of absence. Following an ambiguous mutation or expired processing lease, the worker performs only reconciliation, never another create. After eight attempts it requires manual review. Confirmed rate-limit rejection can retry with backoff; 5xx and transport failures after mutation starts are uncertain. There is no cross-system exactly-once guarantee.

## Controlled pilot

The pilot command is restricted to the two previously authorized internal test messages. Its default mode prepares local intents and runs read-only HubSpot lookups. It writes an exact local preview, including original quoted text and signature, without changing CRM records:

```sh
python -m outreach_recovery.pilot_crm
```

After the user approves the displayed two records and their association, run the same command with `--approve DIGEST`, using the digest from that preview. This releases only those two jobs. The HubSpot access token is loaded from the protected local credential file; the client secret is not needed. Do not store credentials in the repository.

Read back the returned activity IDs and contact associations before reporting live write success. If reconciliation remains unresolved, investigate in HubSpot; do not reset the job or blindly recreate the record.

Offline verification: `python -m outreach_recovery.run_postgres_tests`. Live CRM writes remain untested until the approved pilot runs.
