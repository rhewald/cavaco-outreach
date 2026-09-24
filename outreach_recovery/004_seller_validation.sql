-- Apply once after 003, with generation workers stopped. Existing audit rows remain intact.
BEGIN;
CREATE FUNCTION outreach_pilot.seller_facts_error(f jsonb) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE k text; p jsonb; plan jsonb; n integer := 0; names text[] := ARRAY[]::text[];
BEGIN
    IF jsonb_typeof(f) IS DISTINCT FROM 'object' THEN RETURN 'facts: expected an object'; END IF;
    IF f->'schema_version' IS DISTINCT FROM '1'::jsonb THEN RETURN 'schema_version: expected numeric version 1'; END IF;
    FOR k IN SELECT jsonb_object_keys(f) LOOP
        IF k <> ALL(ARRAY['schema_version','company_name','seller_name','product_description',
             'conversation_purpose','meeting_policy','allowed_claims','pricing']) THEN
            RETURN 'facts: unknown field';
        END IF;
    END LOOP;
    FOREACH k IN ARRAY ARRAY['company_name','seller_name','product_description','conversation_purpose','meeting_policy'] LOOP
        IF jsonb_typeof(f->k) IS DISTINCT FROM 'string'
           OR (f->>k) !~ '[^[:space:]]' OR length(f->>k)>5000 THEN
            RETURN k || ': expected nonblank string of at most 5000 characters';
        END IF;
    END LOOP;
    IF jsonb_typeof(f->'allowed_claims') IS DISTINCT FROM 'array' THEN
        RETURN 'allowed_claims: expected an array of strings (may be empty)';
    END IF;
    IF jsonb_array_length(f->'allowed_claims')>100 THEN RETURN 'allowed_claims: maximum 100 claims'; END IF;
    FOR p IN SELECT value FROM jsonb_array_elements(f->'allowed_claims') LOOP
        IF jsonb_typeof(p) IS DISTINCT FROM 'string' OR (p#>>'{}') !~ '[^[:space:]]'
           OR length(p#>>'{}')>5000 THEN RETURN 'allowed_claims: each claim must be a nonblank string of at most 5000 characters'; END IF;
    END LOOP;
    p := f->'pricing';
    IF jsonb_typeof(p) IS DISTINCT FROM 'object' THEN RETURN 'pricing: expected an object'; END IF;
    IF (p->>'mode') IS NULL OR (p->>'mode') NOT IN ('undisclosed','custom','fixed') THEN
        RETURN 'pricing.mode: expected undisclosed, custom, or fixed';
    END IF;
    IF p->>'mode' IN ('undisclosed','custom') THEN
        IF p - 'mode' <> '{}'::jsonb THEN RETURN 'pricing: custom/undisclosed pricing permits only mode'; END IF;
        RETURN NULL;
    END IF;
    IF p - ARRAY['mode','plans'] <> '{}'::jsonb THEN RETURN 'pricing: unknown field'; END IF;
    IF jsonb_typeof(p->'plans') IS DISTINCT FROM 'array' THEN RETURN 'pricing.plans: expected an array'; END IF;
    IF jsonb_array_length(p->'plans') NOT BETWEEN 1 AND 100 THEN RETURN 'pricing.plans: expected 1..100 plans'; END IF;
    FOR plan IN SELECT value FROM jsonb_array_elements(p->'plans') LOOP
        n := n+1;
        IF jsonb_typeof(plan) IS DISTINCT FROM 'object' THEN RETURN format('pricing.plans[%s]: expected an object',n-1); END IF;
        IF plan - ARRAY['name','currency','amount_minor','billing_interval'] <> '{}'::jsonb THEN RETURN format('pricing.plans[%s]: unknown field',n-1); END IF;
        IF jsonb_typeof(plan->'name') IS DISTINCT FROM 'string' OR (plan->>'name') !~ '[^[:space:]]'
           OR length(plan->>'name')>200 THEN RETURN format('pricing.plans[%s].name: expected nonblank string up to 200 characters',n-1); END IF;
        IF lower(btrim(plan->>'name')) = ANY(names) THEN RETURN 'pricing.plans: duplicate plan name'; END IF;
        names := array_append(names,lower(btrim(plan->>'name')));
        -- Explicit pilot currency allowlist; all use two decimal minor units.
        IF (plan->>'currency') IS NULL OR (plan->>'currency') NOT IN ('USD','EUR','GBP') THEN RETURN format('pricing.plans[%s].currency: expected USD, EUR, or GBP',n-1); END IF;
        IF jsonb_typeof(plan->'amount_minor') IS DISTINCT FROM 'number' THEN RETURN format('pricing.plans[%s].amount_minor: expected nonnegative integer',n-1); END IF;
        IF (plan->>'amount_minor')::numeric < 0 OR (plan->>'amount_minor')::numeric > 100000000000
           OR trunc((plan->>'amount_minor')::numeric) <> (plan->>'amount_minor')::numeric THEN
            RETURN format('pricing.plans[%s].amount_minor: expected integer from 0 to 100000000000',n-1);
        END IF;
        IF (plan->>'billing_interval') IS NULL OR (plan->>'billing_interval') NOT IN ('one_time','month','year') THEN
            RETURN format('pricing.plans[%s].billing_interval: expected one_time, month, or year',n-1);
        END IF;
    END LOOP;
    RETURN NULL;
END;
$$;

CREATE FUNCTION outreach_pilot.validate_seller_profile_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE reason text;
BEGIN
    reason := outreach_pilot.seller_facts_error(NEW.facts);
    IF reason IS NOT NULL THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE=reason; END IF;
    IF NEW.approved_by IS NULL OR NEW.approved_by !~ '[^[:space:]]' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='approved_by: expected nonblank reviewer identity';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER validate_seller_profile BEFORE INSERT ON outreach_pilot.seller_profiles
FOR EACH ROW EXECUTE FUNCTION outreach_pilot.validate_seller_profile_insert();

CREATE FUNCTION outreach_pilot.validate_context_seller() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE reason text;
BEGIN
    SELECT outreach_pilot.seller_facts_error(facts) INTO reason
    FROM outreach_pilot.seller_profiles WHERE id=NEW.seller_profile_id;
    IF reason IS NOT NULL THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE=reason; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER validate_context_seller BEFORE INSERT ON outreach_pilot.conversation_contexts
FOR EACH ROW EXECUTE FUNCTION outreach_pilot.validate_context_seller();

CREATE OR REPLACE FUNCTION outreach_pilot.claim_reply_job(p_lease_seconds integer DEFAULT 120, p_job uuid DEFAULT NULL)
RETURNS SETOF outreach_pilot.reply_jobs LANGUAGE plpgsql AS $$
DECLARE
    c outreach_pilot.conversations%ROWTYPE;
    j outreach_pilot.reply_jobs%ROWTYPE;
    snapshot jsonb;
    sweep integer;
    validation_error text;
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
        validation_error := outreach_pilot.seller_facts_error(snapshot->'seller_facts');
        IF validation_error IS NOT NULL THEN
            UPDATE outreach_pilot.reply_jobs SET state='dead_letter',lease_token=NULL,lease_until=NULL,
                last_error='Invalid seller profile: ' || validation_error WHERE id=j.id;
            CONTINUE;
        END IF;
        RETURN QUERY UPDATE outreach_pilot.reply_jobs SET state='processing',attempts=attempts+1,
            lease_token=gen_random_uuid(),lease_until=clock_timestamp()+make_interval(secs=>p_lease_seconds),
            context_snapshot=snapshot WHERE id=j.id RETURNING *;
        RETURN;
    END LOOP;
END;
$$;

COMMIT;
