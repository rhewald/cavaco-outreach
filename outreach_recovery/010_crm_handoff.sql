BEGIN;
-- Routing is separate from untrusted email text and pinned to an approved contact.
CREATE TABLE outreach_pilot.crm_conversation_routes (
 conversation_id uuid PRIMARY KEY REFERENCES outreach_pilot.conversations(id),
 portal_id text NOT NULL CHECK(portal_id ~ '^[0-9]+$'),
 contact_id text NOT NULL CHECK(contact_id ~ '^[0-9]+$'),
 contact_email text NOT NULL, mailbox_email text NOT NULL,
 approved_by text NOT NULL CHECK(length(btrim(approved_by))>0),
 approval_reference text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER immutable_crm_route BEFORE UPDATE OR DELETE ON outreach_pilot.crm_conversation_routes
 FOR EACH ROW EXECUTE FUNCTION outreach_pilot.block_snapshot_mutation();
-- Persist headers even when the first reply beats send completion/contact binding.
CREATE TABLE outreach_pilot.crm_inbound_headers (
 mailbox_id uuid NOT NULL REFERENCES outreach_pilot.mailboxes(id),
 gmail_message_id text NOT NULL, sender text NOT NULL, recipient text NOT NULL,
 subject text NOT NULL,
 PRIMARY KEY(mailbox_id,gmail_message_id)
);
CREATE TRIGGER immutable_crm_headers BEFORE UPDATE OR DELETE ON outreach_pilot.crm_inbound_headers
 FOR EACH ROW EXECUTE FUNCTION outreach_pilot.block_snapshot_mutation();
COMMIT;
