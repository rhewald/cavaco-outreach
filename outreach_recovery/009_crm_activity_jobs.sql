BEGIN;
CREATE TABLE outreach_pilot.crm_activity_jobs (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 message_id uuid NOT NULL REFERENCES outreach_pilot.messages(id),
 portal_id text NOT NULL CHECK (portal_id ~ '^[0-9]+$'),
 contact_id text NOT NULL CHECK (contact_id ~ '^[0-9]+$'),
 payload jsonb NOT NULL,
 state text NOT NULL DEFAULT 'awaiting_approval' CHECK(state IN
 ('awaiting_approval','pending','processing','completed','reconciliation_required','rejected','manual_review')),
 approved_by text, approved_at timestamptz,
 lease_token uuid, lease_until timestamptz, started_at timestamptz,
 attempts integer NOT NULL DEFAULT 0,
 next_attempt_at timestamptz NOT NULL DEFAULT now(),
 provider_id text, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(message_id,portal_id),
 CHECK(state='awaiting_approval' OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)),
 CHECK(state<>'completed' OR provider_id IS NOT NULL),
 CHECK((state='processing')=(lease_token IS NOT NULL AND lease_until IS NOT NULL))
);
CREATE TABLE outreach_pilot.crm_activity_events (
 id bigserial PRIMARY KEY, job_id uuid NOT NULL REFERENCES outreach_pilot.crm_activity_jobs(id),
 event text NOT NULL, lease_token uuid, at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER immutable_crm_event BEFORE UPDATE OR DELETE ON outreach_pilot.crm_activity_events
 FOR EACH ROW EXECUTE FUNCTION outreach_pilot.block_snapshot_mutation();
CREATE FUNCTION outreach_pilot.freeze_crm_activity() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE m outreach_pilot.messages%ROWTYPE;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'CRM intent is immutable'; END IF;
 IF TG_OP='UPDATE' THEN
  IF (NEW.id,NEW.message_id,NEW.portal_id,NEW.contact_id,NEW.payload,NEW.created_at)
    IS DISTINCT FROM (OLD.id,OLD.message_id,OLD.portal_id,OLD.contact_id,OLD.payload,OLD.created_at)
  THEN RAISE EXCEPTION 'CRM payload is immutable'; END IF;
  IF OLD.approved_at IS NOT NULL AND (NEW.approved_at,NEW.approved_by) IS DISTINCT FROM (OLD.approved_at,OLD.approved_by)
  THEN RAISE EXCEPTION 'CRM approval is immutable'; END IF;
  IF OLD.state='completed' AND NEW IS DISTINCT FROM OLD THEN RAISE EXCEPTION 'CRM completion is immutable'; END IF;
 ELSE
  SELECT * INTO STRICT m FROM outreach_pilot.messages WHERE id=NEW.message_id;
  IF NEW.payload->>'body' IS DISTINCT FROM m.body_text OR NEW.payload->>'direction' IS DISTINCT FROM m.direction
   OR NEW.payload->>'hubspot_portal_id' IS DISTINCT FROM NEW.portal_id
   OR NEW.payload->>'hubspot_contact_id' IS DISTINCT FROM NEW.contact_id
   OR (NEW.payload->>'timestamp')::timestamptz IS DISTINCT FROM m.received_at
  THEN RAISE EXCEPTION 'CRM payload/source mismatch'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER immutable_crm_activity BEFORE INSERT OR UPDATE OR DELETE ON outreach_pilot.crm_activity_jobs
 FOR EACH ROW EXECUTE FUNCTION outreach_pilot.freeze_crm_activity();
CREATE INDEX crm_activity_due ON outreach_pilot.crm_activity_jobs(next_attempt_at)
 WHERE state IN ('pending','processing','reconciliation_required');
COMMIT;
