-- Apply after inbound.sql (and optional 002). Apply once, with old workers stopped.
BEGIN;
ALTER TABLE outreach_pilot.reply_jobs DROP CONSTRAINT reply_jobs_state_check;
ALTER TABLE outreach_pilot.reply_jobs
    ADD CONSTRAINT reply_jobs_state_check CHECK (state IN
        ('pending','processing','completed','superseded','dead_letter')),
    ADD COLUMN job_type text NOT NULL DEFAULT 'GENERATE_SALESGPT_DRAFT'
        CHECK (job_type = 'GENERATE_SALESGPT_DRAFT'),
    ADD COLUMN lease_token uuid,
    ADD COLUMN lease_until timestamptz,
    ADD COLUMN attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    ADD COLUMN max_attempts integer NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 20),
    ADD COLUMN next_attempt_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN last_error text,
    ADD COLUMN context_snapshot jsonb,
    ADD COLUMN completed_by_token uuid,
    ADD COLUMN created_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
-- No generation side effects exist for abandoned legacy processing jobs.
UPDATE outreach_pilot.reply_jobs SET state='pending' WHERE state='processing';
ALTER TABLE outreach_pilot.reply_jobs ADD CONSTRAINT processing_requires_lease
    CHECK (state <> 'processing' OR (lease_token IS NOT NULL AND lease_until IS NOT NULL));
CREATE INDEX reply_jobs_poll ON outreach_pilot.reply_jobs(next_attempt_at,created_at)
    WHERE state IN ('pending','processing');

CREATE TABLE outreach_pilot.seller_profiles (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    facts jsonb NOT NULL CHECK (jsonb_typeof(facts)='object' AND facts <> '{}'::jsonb),
    approved_by text NOT NULL CHECK (btrim(approved_by) <> ''),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE outreach_pilot.conversation_contexts (
    revision bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES outreach_pilot.conversations(id),
    seller_profile_id uuid NOT NULL REFERENCES outreach_pilot.seller_profiles(id),
    prospect_research text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX conversation_context_latest ON outreach_pilot.conversation_contexts(conversation_id,revision DESC);

CREATE FUNCTION outreach_pilot.block_snapshot_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Append-only ledger: create a new record instead'; END;
$$;
CREATE TRIGGER immutable_messages BEFORE UPDATE OR DELETE ON outreach_pilot.messages
    FOR EACH ROW EXECUTE FUNCTION outreach_pilot.block_snapshot_mutation();
CREATE TRIGGER immutable_seller BEFORE UPDATE OR DELETE ON outreach_pilot.seller_profiles
    FOR EACH ROW EXECUTE FUNCTION outreach_pilot.block_snapshot_mutation();
CREATE TRIGGER immutable_research BEFORE UPDATE OR DELETE ON outreach_pilot.conversation_contexts
    FOR EACH ROW EXECUTE FUNCTION outreach_pilot.block_snapshot_mutation();

CREATE FUNCTION outreach_pilot.freeze_job_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.context_snapshot IS NOT NULL AND NEW.context_snapshot IS DISTINCT FROM OLD.context_snapshot THEN
        RAISE EXCEPTION 'Claimed context snapshot is immutable';
    END IF;
    IF (NEW.conversation_id,NEW.triggering_message_id,NEW.version_snapshot)
       IS DISTINCT FROM (OLD.conversation_id,OLD.triggering_message_id,OLD.version_snapshot) THEN
        RAISE EXCEPTION 'Job identity is immutable';
    END IF;
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END;
$$;
CREATE TRIGGER freeze_reply_job BEFORE UPDATE ON outreach_pilot.reply_jobs
    FOR EACH ROW EXECUTE FUNCTION outreach_pilot.freeze_job_snapshot();

CREATE FUNCTION outreach_pilot.claim_reply_job(p_lease_seconds integer DEFAULT 120, p_job uuid DEFAULT NULL)
RETURNS SETOF outreach_pilot.reply_jobs LANGUAGE plpgsql AS $$
DECLARE
    c outreach_pilot.conversations%ROWTYPE;
    j outreach_pilot.reply_jobs%ROWTYPE;
    snapshot jsonb;
    sweep integer;
BEGIN
    IF p_lease_seconds NOT BETWEEN 1 AND 3600 OR p_lease_seconds IS NULL THEN
        RAISE EXCEPTION 'Lease must be 1..3600 seconds';
    END IF;
    -- Conversation before job matches ingestion/publication. Never lock a job
    -- first and then wait for its conversation. SKIP LOCKED avoids busy threads.
    FOR sweep IN 1..100 LOOP
        SELECT c0.* INTO c FROM outreach_pilot.conversations c0
        JOIN LATERAL (
            SELECT q.id,q.next_attempt_at FROM outreach_pilot.reply_jobs q
            WHERE q.conversation_id=c0.id AND (p_job IS NULL OR q.id=p_job)
              AND ((q.state='pending' AND q.next_attempt_at <= clock_timestamp())
                   OR (q.state='processing' AND q.lease_until <= clock_timestamp()))
            ORDER BY q.next_attempt_at,q.created_at,q.id LIMIT 1
        ) ready ON true
        ORDER BY ready.next_attempt_at,c0.id
        LIMIT 1 FOR UPDATE OF c0 SKIP LOCKED;
        IF NOT FOUND THEN RETURN; END IF;
        SELECT q.* INTO j FROM outreach_pilot.reply_jobs q
        WHERE q.conversation_id=c.id AND (p_job IS NULL OR q.id=p_job)
          AND ((q.state='pending' AND q.next_attempt_at <= clock_timestamp())
               OR (q.state='processing' AND q.lease_until <= clock_timestamp()))
        ORDER BY q.next_attempt_at,q.created_at,q.id
        LIMIT 1 FOR UPDATE SKIP LOCKED;
        IF NOT FOUND THEN RETURN; END IF;
        IF j.version_snapshot <> c.version_counter THEN
            UPDATE outreach_pilot.reply_jobs SET state='superseded',lease_token=NULL,lease_until=NULL WHERE id=j.id;
            CONTINUE;
        END IF;
        IF j.attempts >= j.max_attempts THEN
            UPDATE outreach_pilot.reply_jobs SET state='dead_letter',lease_token=NULL,lease_until=NULL,
                last_error='Generation lease expired; retry budget exhausted' WHERE id=j.id;
            CONTINUE;
        END IF;
        snapshot := j.context_snapshot;
        IF snapshot IS NULL THEN
            -- One SELECT uses one MVCC snapshot. History is version ordered,
            -- not timestamp ordered: late-arriving mail still has a fixed place.
            SELECT jsonb_build_object(
                'job_id',j.id,'conversation_id',c.id,'target_conversation_version',j.version_snapshot,
                'seller_profile_id',s.id,'seller_facts',s.facts,'research_revision',r.revision,
                'untrusted_prospect_research',r.prospect_research,
                'history',coalesce((SELECT jsonb_agg(jsonb_build_object(
                    'message_id',m.id,'direction',m.direction,'body',m.body_text,
                    'version',m.conversation_version) ORDER BY m.conversation_version,m.id)
                    FROM outreach_pilot.messages m WHERE m.conversation_id=c.id
                      AND m.conversation_version <= j.version_snapshot AND m.id <> j.triggering_message_id),'[]'::jsonb),
                'triggering_reply',(SELECT jsonb_build_object('message_id',m.id,'body',m.body_text,
                    'version',m.conversation_version) FROM outreach_pilot.messages m
                    WHERE m.id=j.triggering_message_id AND m.conversation_id=c.id
                      AND m.direction='inbound' AND m.conversation_version=j.version_snapshot)
            ) INTO snapshot
            FROM outreach_pilot.conversation_contexts r
            JOIN outreach_pilot.seller_profiles s ON s.id=r.seller_profile_id
            WHERE r.conversation_id=c.id ORDER BY r.revision DESC LIMIT 1;
            IF snapshot IS NULL OR snapshot->'triggering_reply' = 'null'::jsonb THEN
                UPDATE outreach_pilot.reply_jobs SET state='dead_letter',lease_token=NULL,lease_until=NULL,
                    last_error='Missing approved seller/context configuration or invalid triggering message' WHERE id=j.id;
                CONTINUE;
            END IF;
        END IF;
        RETURN QUERY UPDATE outreach_pilot.reply_jobs SET state='processing',attempts=attempts+1,
            lease_token=gen_random_uuid(),lease_until=clock_timestamp()+make_interval(secs=>p_lease_seconds),
            context_snapshot=snapshot WHERE id=j.id RETURNING *;
        RETURN;
    END LOOP;
END;
$$;

-- Remove the unfenced API so callers cannot accidentally bypass lease validation.
DROP FUNCTION outreach_pilot.save_reply_draft(uuid,text);
CREATE FUNCTION outreach_pilot.save_reply_draft(p_job uuid,p_token uuid,p_body text)
RETURNS uuid LANGUAGE plpgsql AS $$
DECLARE
    j outreach_pilot.reply_jobs%ROWTYPE;
    target uuid;
    current_version bigint;
    draft uuid;
BEGIN
    IF p_body IS NULL OR btrim(p_body)='' OR length(p_body)>20000 THEN
        RAISE EXCEPTION 'Draft must contain 1..20000 characters';
    END IF;
    SELECT conversation_id INTO target FROM outreach_pilot.reply_jobs WHERE id=p_job;
    IF NOT FOUND THEN RETURN NULL; END IF;
    SELECT version_counter INTO current_version FROM outreach_pilot.conversations WHERE id=target FOR UPDATE;
    SELECT * INTO j FROM outreach_pilot.reply_jobs WHERE id=p_job FOR UPDATE;
    IF j.version_snapshot <> current_version THEN RETURN NULL; END IF;
    IF j.state='completed' AND j.completed_by_token=p_token THEN
        SELECT id INTO draft FROM outreach_pilot.drafts WHERE triggering_message_id=j.triggering_message_id;
        RETURN draft;
    END IF;
    IF j.state <> 'processing' OR j.lease_token IS DISTINCT FROM p_token
       OR j.lease_until <= clock_timestamp() OR p_token IS NULL THEN RETURN NULL; END IF;
    INSERT INTO outreach_pilot.drafts(conversation_id,triggering_message_id,version_snapshot,body)
    VALUES (j.conversation_id,j.triggering_message_id,j.version_snapshot,p_body) RETURNING id INTO draft;
    UPDATE outreach_pilot.reply_jobs SET state='completed',completed_by_token=p_token,
        lease_token=NULL,lease_until=NULL,last_error=NULL WHERE id=p_job;
    RETURN draft;
END;
$$;

CREATE FUNCTION outreach_pilot.fail_reply_job(p_job uuid,p_token uuid,p_error text,p_permanent boolean DEFAULT false)
RETURNS boolean LANGUAGE plpgsql AS $$
DECLARE j outreach_pilot.reply_jobs%ROWTYPE; target uuid; current_version bigint;
BEGIN
    SELECT conversation_id INTO target FROM outreach_pilot.reply_jobs WHERE id=p_job;
    IF NOT FOUND THEN RETURN false; END IF;
    SELECT version_counter INTO current_version FROM outreach_pilot.conversations WHERE id=target FOR UPDATE;
    SELECT * INTO j FROM outreach_pilot.reply_jobs WHERE id=p_job FOR UPDATE;
    IF j.state <> 'processing' OR j.lease_token IS DISTINCT FROM p_token
       OR j.lease_until <= clock_timestamp() OR p_token IS NULL THEN RETURN false; END IF;
    UPDATE outreach_pilot.reply_jobs SET
        state=CASE WHEN j.version_snapshot <> current_version THEN 'superseded'
                   WHEN p_permanent OR j.attempts >= j.max_attempts THEN 'dead_letter' ELSE 'pending' END,
        lease_token=NULL,lease_until=NULL,last_error=left(p_error,1000),
        next_attempt_at=clock_timestamp()+make_interval(secs=>least(3600,5*power(2,j.attempts-1))::integer)
    WHERE id=p_job;
    RETURN true;
END;
$$;
COMMIT;
