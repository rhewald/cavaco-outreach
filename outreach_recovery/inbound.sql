-- Standalone pilot migration, PostgreSQL 14+. Apply once to a fresh schema.
-- Deliberately isolated from the earlier, non-executable public-schema proposal.
BEGIN;
CREATE SCHEMA outreach_pilot;

CREATE TABLE outreach_pilot.mailboxes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email text NOT NULL UNIQUE
);
CREATE TABLE outreach_pilot.conversations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    mailbox_id uuid NOT NULL REFERENCES outreach_pilot.mailboxes(id),
    gmail_thread_id text, -- Unknown until the initial send succeeds.
    version_counter bigint NOT NULL DEFAULT 0 CHECK (version_counter >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (mailbox_id, id),
    UNIQUE (mailbox_id, gmail_thread_id)
);
CREATE TABLE outreach_pilot.messages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    mailbox_id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    gmail_message_id text NOT NULL,
    mime_message_id text, -- Inbound headers can be absent.
    direction text NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    body_text text NOT NULL,
    received_at timestamptz NOT NULL,
    conversation_version bigint NOT NULL,
    UNIQUE (mailbox_id, gmail_message_id),
    UNIQUE (conversation_id, id),
    UNIQUE (conversation_id, conversation_version),
    FOREIGN KEY (mailbox_id, conversation_id)
        REFERENCES outreach_pilot.conversations(mailbox_id, id)
);
CREATE INDEX message_mime_lookup
    ON outreach_pilot.messages(mailbox_id, mime_message_id);

-- Durable receipt even when a reply arrives before outbound reconciliation.
CREATE TABLE outreach_pilot.inbound_receipts (
    mailbox_id uuid NOT NULL REFERENCES outreach_pilot.mailboxes(id),
    gmail_message_id text NOT NULL,
    gmail_thread_id text,
    mime_message_id text,
    reply_ids text[] NOT NULL, -- Parsed In-Reply-To plus References IDs.
    body_text text NOT NULL,
    received_at timestamptz NOT NULL,
    state text NOT NULL DEFAULT 'unmatched'
        CHECK (state IN ('unmatched', 'ambiguous', 'ingested')),
    message_id uuid REFERENCES outreach_pilot.messages(id),
    PRIMARY KEY (mailbox_id, gmail_message_id)
);
CREATE TABLE outreach_pilot.reply_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL,
    triggering_message_id uuid NOT NULL UNIQUE,
    version_snapshot bigint NOT NULL,
    state text NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'processing', 'completed', 'superseded')),
    FOREIGN KEY (conversation_id, triggering_message_id)
        REFERENCES outreach_pilot.messages(conversation_id, id)
);
CREATE TABLE outreach_pilot.drafts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL,
    triggering_message_id uuid NOT NULL UNIQUE,
    version_snapshot bigint NOT NULL,
    body text NOT NULL CHECK (length(btrim(body)) > 0),
    state text NOT NULL DEFAULT 'pending_review'
        CHECK (state IN ('pending_review', 'approved', 'superseded', 'sent')),
    FOREIGN KEY (conversation_id, triggering_message_id)
        REFERENCES outreach_pilot.messages(conversation_id, id)
);

-- Inputs are a fetched Gmail message, NOT a Gmail Pub/Sub notification.
CREATE FUNCTION outreach_pilot.ingest_reply(
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
BEGIN
    IF p_gmail_message IS NULL OR btrim(p_gmail_message) = '' THEN
        RAISE EXCEPTION 'Gmail message ID required';
    END IF;
    -- Pilot uses mailbox-wide serialization for simple, deterministic lock order.
    -- All writers: mailbox -> conversation -> message/job/draft/outbox.
    PERFORM 1 FROM outreach_pilot.mailboxes WHERE id = p_mailbox FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Unknown mailbox'; END IF;

    INSERT INTO outreach_pilot.inbound_receipts (
        mailbox_id, gmail_message_id, gmail_thread_id, mime_message_id,
        reply_ids, body_text, received_at
    ) VALUES (
        p_mailbox, p_gmail_message, p_gmail_thread, p_mime_message,
        coalesce(p_reply_ids, ARRAY[]::text[]), p_body, p_received_at
    ) ON CONFLICT (mailbox_id, gmail_message_id) DO NOTHING;

    SELECT * INTO STRICT r FROM outreach_pilot.inbound_receipts
    WHERE mailbox_id = p_mailbox AND gmail_message_id = p_gmail_message;
    -- Retried notifications use the first durable receipt, not changed input.
    IF r.state = 'ingested' THEN
        RETURN QUERY SELECT 'duplicate'::text, m.id, m.conversation_version
        FROM outreach_pilot.messages m WHERE m.id = r.message_id;
        RETURN;
    END IF;

    SELECT array_agg(DISTINCT matched.id) INTO candidates FROM (
        SELECT c.id FROM outreach_pilot.conversations c
        WHERE c.mailbox_id = p_mailbox AND c.gmail_thread_id = r.gmail_thread_id
        UNION
        SELECT m.conversation_id FROM outreach_pilot.messages m
        WHERE m.mailbox_id = p_mailbox AND m.direction = 'outbound'
          AND m.mime_message_id = ANY(r.reply_ids)
    ) AS matched;

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
CREATE FUNCTION outreach_pilot.save_reply_draft(p_job uuid, p_body text)
RETURNS uuid LANGUAGE plpgsql AS $$
DECLARE
    j outreach_pilot.reply_jobs%ROWTYPE;
    target_mailbox uuid;
    current_version bigint;
    saved_draft uuid;
BEGIN
    SELECT c.mailbox_id INTO STRICT target_mailbox
    FROM outreach_pilot.reply_jobs q
    JOIN outreach_pilot.conversations c ON c.id = q.conversation_id
    WHERE q.id = p_job;
    PERFORM 1 FROM outreach_pilot.mailboxes WHERE id = target_mailbox FOR UPDATE;
    SELECT * INTO STRICT j FROM outreach_pilot.reply_jobs WHERE id = p_job;
    SELECT version_counter INTO STRICT current_version
    FROM outreach_pilot.conversations WHERE id = j.conversation_id FOR UPDATE;
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
