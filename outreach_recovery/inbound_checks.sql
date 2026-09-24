-- Run after inbound.sql in a disposable database. All fixtures roll back.
\set ON_ERROR_STOP on
BEGIN;
DO $$
DECLARE
    mailbox uuid;
    convo uuid;
    other_convo uuid;
    result record;
    first_job uuid;
    latest_job uuid;
    draft uuid;
BEGIN
    INSERT INTO outreach_pilot.mailboxes(email) VALUES ('pilot@example.test')
    RETURNING id INTO mailbox;
    INSERT INTO outreach_pilot.conversations(mailbox_id, gmail_thread_id)
    VALUES (mailbox, 'thread-1') RETURNING id INTO convo;

    SELECT * INTO result FROM outreach_pilot.ingest_reply(
        mailbox, 'reply-1', 'thread-1', NULL, ARRAY[]::text[], 'Interested', now());
    ASSERT result.outcome = 'ingested' AND result.conversation_version = 1;
    SELECT id INTO first_job FROM outreach_pilot.reply_jobs
    WHERE triggering_message_id = result.message_id;
    SELECT * INTO result FROM outreach_pilot.ingest_reply(
        mailbox, 'reply-1', 'thread-1', NULL, ARRAY[]::text[], 'Changed input', now());
    ASSERT result.outcome = 'duplicate' AND result.conversation_version = 1;
    ASSERT (SELECT body_text = 'Interested' FROM outreach_pilot.messages
            WHERE id = result.message_id);
    draft := outreach_pilot.save_reply_draft(first_job, 'First draft');
    UPDATE outreach_pilot.drafts SET state = 'approved' WHERE id = draft;

    SELECT * INTO result FROM outreach_pilot.ingest_reply(
        mailbox, 'reply-2', 'thread-1', '<reply2@test>', ARRAY[]::text[], 'More details', now());
    ASSERT result.conversation_version = 2;
    ASSERT (SELECT state = 'superseded' FROM outreach_pilot.drafts WHERE id = draft);
    ASSERT outreach_pilot.save_reply_draft(first_job, 'Late stale output') IS NULL;
    SELECT id INTO latest_job FROM outreach_pilot.reply_jobs
    WHERE triggering_message_id = result.message_id;
    draft := outreach_pilot.save_reply_draft(latest_job, 'Current draft');
    ASSERT outreach_pilot.save_reply_draft(latest_job, 'Duplicate output') = draft;

    SELECT * INTO result FROM outreach_pilot.ingest_reply(
        mailbox, 'early-reply', 'thread-later', NULL, ARRAY[]::text[], 'Early', now());
    ASSERT result.outcome = 'unmatched';
    INSERT INTO outreach_pilot.conversations(mailbox_id, gmail_thread_id)
    VALUES (mailbox, 'thread-later') RETURNING id INTO other_convo;
    SELECT * INTO result FROM outreach_pilot.ingest_reply(
        mailbox, 'early-reply', 'thread-later', NULL, ARRAY[]::text[], 'Early', now());
    ASSERT result.outcome = 'ingested' AND result.conversation_version = 1;

    -- Conflicting Gmail-thread and MIME-header matches must not select arbitrarily.
    INSERT INTO outreach_pilot.messages (
        mailbox_id, conversation_id, gmail_message_id, mime_message_id, direction,
        body_text, received_at, conversation_version
    ) VALUES (mailbox, other_convo, 'outbound-seed', '<other@test>',
              'outbound', 'Hello', now(), 2);
    UPDATE outreach_pilot.conversations SET version_counter = 2 WHERE id = other_convo;
    SELECT * INTO result FROM outreach_pilot.ingest_reply(
        mailbox, 'conflict', 'thread-1', NULL, ARRAY['<other@test>'], 'Ambiguous', now());
    ASSERT result.outcome = 'ambiguous';
    ASSERT (SELECT version_counter = 2 FROM outreach_pilot.conversations WHERE id = convo);
    ASSERT (SELECT count(*) = 3 FROM outreach_pilot.reply_jobs j
            JOIN outreach_pilot.conversations c ON c.id = j.conversation_id
            WHERE c.mailbox_id = mailbox);
END;
$$;
ROLLBACK;
