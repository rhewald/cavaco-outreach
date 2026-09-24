# Gmail monitoring and local services

The monitor reads `sdr@cavaco.ai` history every 30 seconds, independent of read/unread labels. It fetches bodies only for threads enrolled by `crm_conversation_routes`. Authenticated profile checks pin the mailbox. Other mailbox history metadata is not retained.

Apply migration 011 after 010. Initial startup captures a history baseline before reading enrolled threads, then consumes changes since that baseline. This closes the concurrent-arrival gap. A changed enrollment set also triggers a scoped thread resync. A 404 expired history cursor or an invalid saved pagination token triggers the same recovery. Gmail's [history API](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.history/list) requires storing the final history ID only when there is no next-page token.

PostgreSQL stores the history cursor, page token, resync progress, mailbox lease, retries, and message dispositions. Cursor advancement follows ingestion. Ingestion, CRM handoff and monitor receipt share a transaction. Message processing is idempotent across crashes. Only the lease owner can checkpoint. Rate/network errors preserve progress and back off; numeric Retry-After is honored up to one hour. Each cycle handles up to 200 new enrolled messages and 20 pages/threads before yielding.

Deleted messages are recorded as unavailable. Sent messages are skipped. Invalid or unexpected-sender replies are quarantined in `gmail_monitor_messages` and require operator review. Nothing is marked read, labeled, deleted or sent by this service. CRM logging runs independently.

## Generation

After polling, the inbox service processes at most one due generation job scoped to its enrolled mailbox and an existing seller context. The existing process-isolated adapter, lease checks and stale-draft checks remain in force. Output stops at `pending_review`; the service cannot claim send jobs. Missing generation credentials do not consume attempts or stop inbox sync.

Save a key through hidden Terminal input (never put it in a plist or shell command argument):

```sh
cd /Users/ruihewald/Documents/cavaco-outreach
/Users/ruihewald/.local/share/cavaco-outreach/runtime/bin/python -m outreach_recovery.openai_local_auth
```

The protected credential file is `~/.config/cavaco-outreach/openai-token.json`, mode 0600, outside Git. The current model is gpt-4o-mini. The running service reads a newly saved key on its next cycle. Restart the inbox agent after rotating a key it has already loaded.

## Login services

A persistent Python 3.12 environment lives in `~/.local/share/cavaco-outreach/runtime`; services never use `/tmp` executables. The installer copies only the package into `~/.local/share/cavaco-outreach/service-code`, avoiding background access to the protected Documents directory. Re-run the installer after code updates to redeploy that snapshot.

```sh
python -m outreach_recovery.install_services \
  --python /Users/ruihewald/.local/share/cavaco-outreach/runtime/bin/python \
  --mailbox sdr@cavaco.ai --portal 47521149 --load
```

Two user LaunchAgents start at login and restart after crashes: `ai.cavaco.outreach.inbox` and `ai.cavaco.outreach.crm`. They are not system daemons: they pause during sleep and do not run before login. No secrets are embedded in the plist files. Logs are under `~/.local/share/cavaco-outreach/logs`, with private permissions and no message bodies. PostgreSQL data stays in the existing persistent pilot directory. The installer does not install the review UI as a service.

Inspect status:

```sh
launchctl print gui/$(id -u)/ai.cavaco.outreach.inbox
launchctl print gui/$(id -u)/ai.cavaco.outreach.crm
```

Stop an agent with `launchctl bootout gui/$(id -u)/LABEL`. Bootstrap its plist again to resume. For a controlled restart use `launchctl kickstart -k gui/$(id -u)/LABEL`. Inbox leases can defer the next poll by up to 120 seconds after a hard crash. CRM recovery never blindly recreates an ambiguous write.

## Validation

Run `python -m outreach_recovery.run_postgres_tests`. Tests cover paginated checkpoints, read replies, unrelated threads, duplicate events, crash replay, expired cursors, lease exclusion, backoff, bounded resync, transaction rollback, quarantine, portal scoping and review-only generation. A launchd restart check verifies service recovery without sending a new email. An actual Mac reboot has not been exercised.
