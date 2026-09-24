BEGIN;
CREATE TABLE outreach_pilot.gmail_monitor_state (
 mailbox_id uuid PRIMARY KEY REFERENCES outreach_pilot.mailboxes(id),
 history_id text, page_token text,
 route_fingerprint text NOT NULL DEFAULT '',
 resync_routes jsonb, resync_index integer NOT NULL DEFAULT 0,
 resync_history_id text,
 lease_token uuid, lease_until timestamptz,
 failures integer NOT NULL DEFAULT 0,
 next_poll_at timestamptz NOT NULL DEFAULT now(),
 last_success_at timestamptz, last_error text,
 CHECK((lease_token IS NULL)=(lease_until IS NULL))
);
CREATE TABLE outreach_pilot.gmail_monitor_messages (
 mailbox_id uuid NOT NULL REFERENCES outreach_pilot.mailboxes(id),
 gmail_message_id text NOT NULL, thread_id text NOT NULL,
 outcome text NOT NULL CHECK(outcome IN ('ingested','duplicate','quarantined','sent','deleted')),
 recorded_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(mailbox_id,gmail_message_id)
);
CREATE TRIGGER immutable_monitor_message BEFORE UPDATE OR DELETE ON outreach_pilot.gmail_monitor_messages
 FOR EACH ROW EXECUTE FUNCTION outreach_pilot.block_snapshot_mutation();
COMMIT;
