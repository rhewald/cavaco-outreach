-- Preserve actual delivered MIME IDs separately from immutable approved payloads.
BEGIN;
CREATE TABLE outreach_pilot.gmail_sent_observations (
 operation_id uuid PRIMARY KEY REFERENCES outreach_pilot.delivery_operations(id),
 provider_message_id text NOT NULL,
 provider_thread_id text NOT NULL,
 actual_mime_message_id text NOT NULL,
 verified_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER immutable_sent_observation BEFORE UPDATE OR DELETE ON outreach_pilot.gmail_sent_observations
 FOR EACH ROW EXECUTE FUNCTION outreach_pilot.block_snapshot_mutation();
COMMIT;
