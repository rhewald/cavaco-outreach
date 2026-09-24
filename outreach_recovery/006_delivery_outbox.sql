-- Apply once after 005, with workers stopped. No live provider is enabled.
BEGIN;
CREATE TABLE outreach_pilot.delivery_envelopes (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 draft_id uuid NOT NULL UNIQUE REFERENCES outreach_pilot.drafts(id),
 payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER immutable_delivery_envelope BEFORE UPDATE OR DELETE ON outreach_pilot.delivery_envelopes
 FOR EACH ROW EXECUTE FUNCTION outreach_pilot.block_snapshot_mutation();
CREATE FUNCTION outreach_pilot.guard_delivery_envelope() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target uuid; d outreach_pilot.drafts%ROWTYPE; c outreach_pilot.conversations%ROWTYPE; sender text;
BEGIN
 SELECT conversation_id INTO target FROM outreach_pilot.drafts WHERE id=NEW.draft_id;
 SELECT * INTO STRICT c FROM outreach_pilot.conversations WHERE id=target FOR UPDATE;
 SELECT * INTO STRICT d FROM outreach_pilot.drafts WHERE id=NEW.draft_id FOR UPDATE;
 SELECT email INTO sender FROM outreach_pilot.mailboxes WHERE id=c.mailbox_id;
 IF d.state<>'pending_review' OR d.version_snapshot<>c.version_counter THEN
   RAISE EXCEPTION 'Delivery envelope requires a current unreviewed draft';
 END IF;
 IF NOT (NEW.payload ?& ARRAY['from','to','subject','body','raw_mime','mime_message_id','mailbox_id','hubspot_portal_id','hubspot_contact_id'])
 OR NEW.payload->>'body' IS DISTINCT FROM d.body OR NEW.payload->>'from' IS DISTINCT FROM sender
 OR NEW.payload->>'mailbox_id' IS DISTINCT FROM c.mailbox_id::text
 OR NEW.payload->>'thread_id' IS DISTINCT FROM c.gmail_thread_id THEN
   RAISE EXCEPTION 'Delivery envelope does not match the draft and mailbox';
 END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER validate_delivery_envelope BEFORE INSERT ON outreach_pilot.delivery_envelopes
 FOR EACH ROW EXECUTE FUNCTION outreach_pilot.guard_delivery_envelope();
CREATE TABLE outreach_pilot.delivery_operations (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 draft_id uuid NOT NULL REFERENCES outreach_pilot.drafts(id),
 envelope_id uuid NOT NULL REFERENCES outreach_pilot.delivery_envelopes(id),
 conversation_id uuid NOT NULL,
 mailbox_id uuid NOT NULL,
 conversation_version bigint NOT NULL,
 kind text NOT NULL CHECK(kind IN ('gmail_send','hubspot_log')),
 depends_on uuid REFERENCES outreach_pilot.delivery_operations(id),
 payload jsonb NOT NULL,
 state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','processing','reconciliation_required','completed','rejected','superseded','manual_review')),
 lease_token uuid, lease_until timestamptz,
 attempts int NOT NULL DEFAULT 0 CHECK(attempts>=0),
 next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 started_at timestamptz, completed_at timestamptz,
 provider_id text, provider_thread_id text,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(draft_id,kind),
 FOREIGN KEY(mailbox_id,conversation_id) REFERENCES outreach_pilot.conversations(mailbox_id,id),
 CHECK((lease_token IS NULL)=(lease_until IS NULL)),
 CHECK(state<>'completed' OR (provider_id IS NOT NULL AND completed_at IS NOT NULL))
);
CREATE UNIQUE INDEX delivery_mime_unique ON outreach_pilot.delivery_operations(mailbox_id,(payload->>'mime_message_id')) WHERE kind='gmail_send';
CREATE INDEX delivery_due ON outreach_pilot.delivery_operations(next_attempt_at) WHERE state IN ('pending','reconciliation_required');
CREATE INDEX delivery_expired ON outreach_pilot.delivery_operations(lease_until) WHERE state='processing';
CREATE FUNCTION outreach_pilot.guard_delivery_operation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE d outreach_pilot.drafts%ROWTYPE; e outreach_pilot.delivery_envelopes%ROWTYPE; c outreach_pilot.conversations%ROWTYPE; parent outreach_pilot.delivery_operations%ROWTYPE;
BEGIN
 SELECT * INTO STRICT d FROM outreach_pilot.drafts WHERE id=NEW.draft_id;
 SELECT * INTO STRICT e FROM outreach_pilot.delivery_envelopes WHERE id=NEW.envelope_id;
 SELECT * INTO STRICT c FROM outreach_pilot.conversations WHERE id=d.conversation_id;
 IF e.draft_id<>d.id OR NEW.conversation_id<>d.conversation_id OR NEW.mailbox_id<>c.mailbox_id
 OR NEW.conversation_version<>d.version_snapshot THEN RAISE EXCEPTION 'Delivery identity mismatch'; END IF;
 IF NEW.kind='gmail_send' THEN
   IF NEW.payload IS DISTINCT FROM e.payload OR NEW.depends_on IS NOT NULL OR d.state<>'approved'
   OR NOT EXISTS(SELECT 1 FROM outreach_pilot.review_events WHERE draft_id=d.id AND decision='approved')
   THEN RAISE EXCEPTION 'Send requires the exact approved envelope'; END IF;
 ELSE
   SELECT * INTO STRICT parent FROM outreach_pilot.delivery_operations WHERE id=NEW.depends_on;
   IF parent.kind<>'gmail_send' OR parent.draft_id<>d.id OR parent.envelope_id<>e.id
   OR (NEW.payload - 'gmail_message_id' - 'gmail_thread_id') IS DISTINCT FROM e.payload
   THEN RAISE EXCEPTION 'CRM activity must depend on its own send'; END IF;
 END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER validate_delivery_operation BEFORE INSERT ON outreach_pilot.delivery_operations
 FOR EACH ROW EXECUTE FUNCTION outreach_pilot.guard_delivery_operation();
CREATE TABLE outreach_pilot.delivery_attempts (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 operation_id uuid NOT NULL REFERENCES outreach_pilot.delivery_operations(id),
 lease_token uuid NOT NULL,
 event text NOT NULL CHECK(event IN ('claimed_dispatch','claimed_reconcile','started','accepted','found','rejected','retry_rejected','uncertain','not_found_yet','lookup_failed','superseded','manual_review')),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX one_dispatch_per_lease ON outreach_pilot.delivery_attempts(operation_id,lease_token) WHERE event='started';
CREATE TRIGGER immutable_delivery_attempt BEFORE UPDATE OR DELETE ON outreach_pilot.delivery_attempts
 FOR EACH ROW EXECUTE FUNCTION outreach_pilot.block_snapshot_mutation();
CREATE TABLE outreach_pilot.delivery_followups (
 operation_id uuid PRIMARY KEY REFERENCES outreach_pilot.delivery_operations(id),
 reason text NOT NULL CHECK(reason='conversation_changed_during_send'),
 observed_version bigint NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER immutable_delivery_followup BEFORE UPDATE OR DELETE ON outreach_pilot.delivery_followups
 FOR EACH ROW EXECUTE FUNCTION outreach_pilot.block_snapshot_mutation();
CREATE FUNCTION outreach_pilot.freeze_delivery_operation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (NEW.id,NEW.draft_id,NEW.envelope_id,NEW.conversation_id,NEW.mailbox_id,NEW.conversation_version,NEW.kind,NEW.depends_on,NEW.payload)
 IS DISTINCT FROM (OLD.id,OLD.draft_id,OLD.envelope_id,OLD.conversation_id,OLD.mailbox_id,OLD.conversation_version,OLD.kind,OLD.depends_on,OLD.payload) THEN
  RAISE EXCEPTION 'Delivery intent is immutable';
 END IF;
 IF OLD.started_at IS NOT NULL AND NEW.started_at IS DISTINCT FROM OLD.started_at THEN
  RAISE EXCEPTION 'Dispatch-started marker is permanent';
 END IF;
 IF OLD.state IN ('completed','rejected','superseded','manual_review') AND NEW.state<>OLD.state THEN
  RAISE EXCEPTION 'Terminal delivery state cannot be reopened';
 END IF;
 IF NEW.state='pending' AND OLD.state<>'pending' AND NOT (OLD.state='processing' AND OLD.kind='hubspot_log') THEN
  RAISE EXCEPTION 'Uncertain operations cannot reenter dispatch';
 END IF;
 NEW.updated_at=clock_timestamp(); RETURN NEW;
END; $$;
CREATE TRIGGER immutable_delivery_content BEFORE UPDATE ON outreach_pilot.delivery_operations
 FOR EACH ROW EXECUTE FUNCTION outreach_pilot.freeze_delivery_operation();
CREATE TRIGGER no_delivery_delete BEFORE DELETE ON outreach_pilot.delivery_operations
 FOR EACH ROW EXECUTE FUNCTION outreach_pilot.block_snapshot_mutation();
ALTER FUNCTION outreach_pilot.review_draft(uuid,text,text,text) RENAME TO review_draft_without_delivery;
CREATE FUNCTION outreach_pilot.review_draft(p_draft uuid,p_decision text,p_reviewer text,p_reason text DEFAULT '',p_envelope uuid DEFAULT NULL)
RETURNS text LANGUAGE plpgsql AS $$
DECLARE target uuid; d outreach_pilot.drafts%ROWTYPE; c outreach_pilot.conversations%ROWTYPE;
 e outreach_pilot.delivery_envelopes%ROWTYPE; result text;
BEGIN
 SELECT conversation_id INTO target FROM outreach_pilot.drafts WHERE id=p_draft;
 IF NOT FOUND THEN RETURN 'not_found'; END IF;
 SELECT * INTO STRICT c FROM outreach_pilot.conversations WHERE id=target FOR UPDATE;
 SELECT * INTO STRICT d FROM outreach_pilot.drafts WHERE id=p_draft FOR UPDATE;
 SELECT * INTO e FROM outreach_pilot.delivery_envelopes WHERE draft_id=p_draft;
 -- Bind approval to the envelope actually displayed; a newly attached envelope
 -- cannot silently turn a previously loaded review-only form into a send approval.
 IF p_decision='approved' AND e.id IS DISTINCT FROM p_envelope THEN RETURN 'conflict'; END IF;
 result=outreach_pilot.review_draft_without_delivery(p_draft,p_decision,p_reviewer,p_reason);
 IF result='approved' AND e.id IS NOT NULL THEN
  INSERT INTO outreach_pilot.delivery_operations(draft_id,envelope_id,conversation_id,mailbox_id,conversation_version,kind,payload)
  VALUES(d.id,e.id,c.id,c.mailbox_id,d.version_snapshot,'gmail_send',e.payload);
 END IF;
 RETURN result;
END; $$;
COMMIT;
