-- Apply once after 004, with workers stopped.
BEGIN;
ALTER TABLE outreach_pilot.drafts DROP CONSTRAINT drafts_state_check;
ALTER TABLE outreach_pilot.drafts ADD CONSTRAINT drafts_state_check
CHECK (state IN ('pending_review','approved','rejected','superseded','sent'));

CREATE TABLE outreach_pilot.review_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    draft_id uuid NOT NULL UNIQUE REFERENCES outreach_pilot.drafts(id),
    decision text NOT NULL CHECK (decision IN ('approved','rejected','superseded')),
    reviewer text NOT NULL CHECK (reviewer ~ '[^[:space:]]' AND length(reviewer)<=200),
    reason text NOT NULL DEFAULT '' CHECK (length(reason)<=2000),
    draft_version bigint NOT NULL,
    observed_version bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER immutable_review_events BEFORE UPDATE OR DELETE ON outreach_pilot.review_events
FOR EACH ROW EXECUTE FUNCTION outreach_pilot.block_snapshot_mutation();

CREATE FUNCTION outreach_pilot.freeze_draft_content() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id,NEW.conversation_id,NEW.triggering_message_id,NEW.version_snapshot,NEW.body)
        IS DISTINCT FROM (OLD.id,OLD.conversation_id,OLD.triggering_message_id,OLD.version_snapshot,OLD.body) THEN
        RAISE EXCEPTION 'Draft content is immutable; create a new revision instead';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER freeze_draft_content BEFORE UPDATE ON outreach_pilot.drafts
FOR EACH ROW EXECUTE FUNCTION outreach_pilot.freeze_draft_content();

CREATE FUNCTION outreach_pilot.review_draft(p_draft uuid,p_decision text,p_reviewer text,p_reason text DEFAULT '')
RETURNS text LANGUAGE plpgsql AS $$
DECLARE target uuid; current_version bigint; d outreach_pilot.drafts%ROWTYPE; final_state text;
BEGIN
    IF p_decision IS NULL OR p_decision NOT IN ('approved','rejected') THEN
        RAISE EXCEPTION 'Expected approved or rejected' USING ERRCODE='22023';
    END IF;
    IF p_reviewer IS NULL OR p_reviewer !~ '[^[:space:]]' OR length(p_reviewer)>200
       OR p_reason IS NULL OR length(p_reason)>2000 THEN
        RAISE EXCEPTION 'Invalid reviewer or rejection reason' USING ERRCODE='22023';
    END IF;
    SELECT conversation_id INTO target FROM outreach_pilot.drafts WHERE id=p_draft;
    IF NOT FOUND THEN RETURN 'not_found'; END IF;
    -- Same lock order as ingestion: conversation, then draft. Never reverse it.
    SELECT version_counter INTO current_version FROM outreach_pilot.conversations WHERE id=target FOR UPDATE;
    SELECT * INTO d FROM outreach_pilot.drafts WHERE id=p_draft FOR UPDATE;
    IF NOT FOUND THEN RETURN 'not_found'; END IF;
    IF d.state='superseded' THEN RETURN 'superseded'; END IF;
    IF d.state IN ('pending_review','approved') AND d.version_snapshot<>current_version THEN
        UPDATE outreach_pilot.drafts SET state='superseded' WHERE id=d.id;
        INSERT INTO outreach_pilot.review_events(draft_id,decision,reviewer,reason,draft_version,observed_version)
        VALUES(d.id,'superseded',p_reviewer,'Conversation changed before review',d.version_snapshot,current_version)
        ON CONFLICT(draft_id) DO NOTHING;
        RETURN 'superseded';
    END IF;
    IF d.state=p_decision AND EXISTS(SELECT 1 FROM outreach_pilot.review_events WHERE draft_id=d.id AND decision=p_decision) THEN
        RETURN 'already_' || p_decision;
    END IF;
    IF d.state<>'pending_review' THEN RETURN 'conflict'; END IF;
    UPDATE outreach_pilot.drafts SET state=p_decision WHERE id=d.id;
    INSERT INTO outreach_pilot.review_events(draft_id,decision,reviewer,reason,draft_version,observed_version)
    VALUES(d.id,p_decision,p_reviewer,CASE WHEN p_decision='rejected' THEN p_reason ELSE '' END,d.version_snapshot,current_version);
    RETURN p_decision;
END;
$$;
COMMIT;
