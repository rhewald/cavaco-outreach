-- Allow explicitly reviewed initial emails without inventing inbound messages.
BEGIN;
ALTER TABLE outreach_pilot.drafts ALTER COLUMN triggering_message_id DROP NOT NULL;
ALTER TABLE outreach_pilot.drafts ADD CONSTRAINT drafts_conversation_fk
 FOREIGN KEY(conversation_id) REFERENCES outreach_pilot.conversations(id);
COMMIT;
