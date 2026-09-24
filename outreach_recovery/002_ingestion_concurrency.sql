-- Upgrade an existing outreach_pilot schema created by inbound.sql.
BEGIN;
-- Inputs are a fetched Gmail message, NOT a Gmail Pub/Sub notification.
CREATE OR REPLACE FUNCTION outreach_pilot.ingest_reply(
    p_mailbox uuid, p_gmail_message text, p_gmail_thread text,
    p_mime_message text, p_reply_ids text[], p_body text,
    p_received_at timestamptz
) RETURNS TABLE(outcome text, message_id uuid, conversation_version bigint)
LANGUAGE plpgsql AS $$
DECLARE
    r outreach_pilot.inbound_receipts%ROWTYPE;
    candidates uuid[];
    selected_conversation uuid;
    saved_message uuid;
    new_version bigint;
    thread_conversation uuid;
BEGIN
    IF p_gmail_message IS NULL OR btrim(p_gmail_message) = '' THEN
        RAISE EXCEPTION 'Gmail message ID required';
    END IF;
    -- Receipt uniqueness serializes deliveries of this message only. Independent
    -- conversations in the same mailbox remain concurrent. The FK checks mailbox.

    INSERT INTO outreach_pilot.inbound_receipts (
        mailbox_id, gmail_message_id, gmail_thread_id, mime_message_id,
        reply_ids, body_text, received_at
    ) VALUES (
        p_mailbox, p_gmail_message, p_gmail_thread, p_mime_message,
        coalesce(p_reply_ids, ARRAY[]::text[]), p_body, p_received_at
    ) ON CONFLICT (mailbox_id, gmail_message_id) DO NOTHING;

    SELECT * INTO STRICT r FROM outreach_pilot.inbound_receipts
    WHERE mailbox_id = p_mailbox AND gmail_message_id = p_gmail_message
    FOR UPDATE;
    -- Also recognize messages already recorded by an earlier ingestion path.
    SELECT m.id, m.conversation_version INTO saved_message, new_version
    FROM outreach_pilot.messages m
    WHERE m.mailbox_id = p_mailbox AND m.gmail_message_id = p_gmail_message;
    IF FOUND THEN
        UPDATE outreach_pilot.inbound_receipts
        SET state = 'ingested', message_id = saved_message
        WHERE mailbox_id = p_mailbox AND gmail_message_id = p_gmail_message;
        RETURN QUERY SELECT 'duplicate'::text, saved_message, new_version;
        RETURN;
    END IF;
    -- Retried notifications use the first durable receipt, not changed input.
    IF r.state = 'ingested' THEN
        RETURN QUERY SELECT 'duplicate'::text, m.id, m.conversation_version
        FROM outreach_pilot.messages m WHERE m.id = r.message_id;
        RETURN;
    END IF;

    -- Tier 1: Internet reply headers, scoped to this authenticated mailbox.
    -- Multiple header matches to one conversation are fine; competing matches
    -- remain ambiguous. Never hide conflicting evidence with the fallback.
    SELECT array_agg(DISTINCT m.conversation_id) INTO candidates
    FROM outreach_pilot.messages m
    WHERE m.mailbox_id = p_mailbox AND m.direction = 'outbound'
      AND m.mime_message_id = ANY(r.reply_ids);
    SELECT c.id INTO thread_conversation FROM outreach_pilot.conversations c
    WHERE c.mailbox_id = p_mailbox AND c.gmail_thread_id = r.gmail_thread_id;
    -- Tier 2: Gmail's thread mapping when no known outbound header matches.
    IF coalesce(cardinality(candidates), 0) = 0 AND thread_conversation IS NOT NULL THEN
        candidates := ARRAY[thread_conversation];
    ELSIF cardinality(candidates) = 1 AND thread_conversation IS NOT NULL
          AND candidates[1] <> thread_conversation THEN
        candidates := array_append(candidates, thread_conversation);
    END IF;

    IF coalesce(cardinality(candidates), 0) <> 1 THEN
        UPDATE outreach_pilot.inbound_receipts
        SET state = CASE WHEN coalesce(cardinality(candidates), 0) = 0
                         THEN 'unmatched' ELSE 'ambiguous' END
        WHERE mailbox_id = p_mailbox AND gmail_message_id = p_gmail_message;
        RETURN QUERY SELECT CASE WHEN coalesce(cardinality(candidates), 0) = 0
                            THEN 'unmatched' ELSE 'ambiguous' END,
                            NULL::uuid, NULL::bigint;
        RETURN;
    END IF;
    selected_conversation := candidates[1];
    -- Hold the conversation lock through message, version, and job publication.
    PERFORM 1 FROM outreach_pilot.conversations
    WHERE mailbox_id = p_mailbox AND id = selected_conversation FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Conversation changed during resolution; retry ingestion'
            USING ERRCODE = '40001';
    END IF;
    UPDATE outreach_pilot.conversations
    SET version_counter = version_counter + 1, updated_at = now()
    WHERE id = selected_conversation
    RETURNING version_counter INTO new_version;

    INSERT INTO outreach_pilot.messages (
        mailbox_id, conversation_id, gmail_message_id, mime_message_id,
        direction, body_text, received_at, conversation_version
    ) VALUES (
        p_mailbox, selected_conversation, r.gmail_message_id, r.mime_message_id,
        'inbound', r.body_text, r.received_at, new_version
    ) RETURNING id INTO saved_message;

    UPDATE outreach_pilot.drafts SET state = 'superseded'
    WHERE conversation_id = selected_conversation
      AND version_snapshot < new_version AND state IN ('pending_review', 'approved');
    UPDATE outreach_pilot.reply_jobs SET state = 'superseded'
    WHERE conversation_id = selected_conversation
      AND version_snapshot < new_version AND state IN ('pending', 'processing');
    INSERT INTO outreach_pilot.reply_jobs (
        conversation_id, triggering_message_id, version_snapshot
    ) VALUES (selected_conversation, saved_message, new_version);
    UPDATE outreach_pilot.inbound_receipts
    SET state = 'ingested', message_id = saved_message
    WHERE mailbox_id = p_mailbox AND gmail_message_id = p_gmail_message;
    RETURN QUERY SELECT 'ingested'::text, saved_message, new_version;
END;
$$;

-- Call after generating text OUTSIDE a database transaction.
-- This version comparison prevents late model results from reviving stale drafts.
CREATE OR REPLACE FUNCTION outreach_pilot.save_reply_draft(p_job uuid, p_body text)
RETURNS uuid LANGUAGE plpgsql AS $$
DECLARE
    j outreach_pilot.reply_jobs%ROWTYPE;
    target_conversation uuid;
    current_version bigint;
    saved_draft uuid;
BEGIN
    SELECT conversation_id INTO STRICT target_conversation
    FROM outreach_pilot.reply_jobs WHERE id = p_job;
    -- Match the ingestion lock order: conversation before jobs/drafts. Read the
    -- job AFTER acquiring the lock so a waiting publisher sees supersession.
    SELECT version_counter INTO STRICT current_version
    FROM outreach_pilot.conversations WHERE id = target_conversation FOR UPDATE;
    SELECT * INTO STRICT j FROM outreach_pilot.reply_jobs WHERE id = p_job FOR UPDATE;
    IF current_version <> j.version_snapshot OR j.state = 'superseded' THEN
        UPDATE outreach_pilot.reply_jobs SET state = 'superseded' WHERE id = p_job;
        RETURN NULL;
    END IF;
    INSERT INTO outreach_pilot.drafts (
        conversation_id, triggering_message_id, version_snapshot, body
    ) VALUES (j.conversation_id, j.triggering_message_id, j.version_snapshot, p_body)
    ON CONFLICT (triggering_message_id) DO NOTHING;
    SELECT id INTO STRICT saved_draft FROM outreach_pilot.drafts
    WHERE triggering_message_id = j.triggering_message_id;
    UPDATE outreach_pilot.reply_jobs SET state = 'completed' WHERE id = p_job;
    RETURN saved_draft;
END;
$$;
COMMIT;
