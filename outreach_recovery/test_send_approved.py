import unittest
from concurrent.futures import ThreadPoolExecutor
from email.parser import BytesParser
from email.policy import default
import base64
import test_generation as fixtures
from test_delivery import FakeProvider
from outreach_recovery.send_approved import ReplySender,confirmed,terminal_text
from outreach_recovery.crm_handoff import register_route
from outreach_recovery.gmail_inbound import ingest_message
from outreach_recovery.review_repository import ReviewRepository

@unittest.skipUnless(fixtures.DSN,'Requires PostgreSQL')
class SendTests(unittest.TestCase):
    sql=fixtures.GenerationDatabaseTests.sql
    ingest=fixtures.GenerationDatabaseTests.ingest
    def setUp(self):
        fixtures.GenerationDatabaseTests.setUp(self)
        self.email=self.mailbox+'@test.example'
        with self.repo.connection() as c,c.cursor() as q:
            register_route(q,self.conversation,portal_id='123',contact_id='456',contact_email='prospect@example.test',mailbox_email=self.email,reviewer='Rui',approval_reference='test')
            q.execute("INSERT INTO outreach_pilot.crm_inbound_headers VALUES(%s,'first','prospect@example.test',%s,'Subject')",(self.mailbox,self.email))
            # Existing fixture has no MIME ID, so create a proper new inbound reply.
        from datetime import datetime,timezone
        parsed=dict(mailbox_id=self.mailbox,gmail_message_id='proper-reply',gmail_thread_id='generation',mime_message_id='<latest@example.test>',reply_ids=['<prior@example.test>'],body='A reply',received_at=datetime.now(timezone.utc),sender='prospect@example.test',recipient=self.email,subject='Re: Subject')
        message=ingest_message(self.repo,parsed)[1]
        job=self.sql('SELECT id FROM outreach_pilot.reply_jobs WHERE triggering_message_id=%s',(message,))[0][0]
        claim=self.repo.claim(job_id=job)
        self.draft=self.repo.save(claim,'Thank you, this is a reviewed response.')
        self.sender=ReplySender(fixtures.DSN);self.gmail=FakeProvider()
    def execute(self,preview=None):
        preview=preview or self.sender.prepare(self.draft)
        return self.sender.execute(preview,confirmed_digest=preview['digest'],reviewer='Rui',gmail=self.gmail)
    def test_preview_does_not_approve_or_send(self):
        preview=self.sender.prepare(self.draft)
        self.assertIsNone(self.sender.operation(self.draft));self.assertEqual(self.gmail.calls,0)
        self.assertEqual(self.sql('SELECT state FROM outreach_pilot.drafts WHERE id=%s',(self.draft,))[0][0],'pending_review')
        mime=BytesParser(policy=default).parsebytes(base64.urlsafe_b64decode(preview['payload']['raw_mime']))
        self.assertEqual(str(mime['In-Reply-To']),'<latest@example.test>')
        self.assertEqual(str(mime['References']),'<prior@example.test> <latest@example.test>')
        self.assertEqual(str(mime['To']),'prospect@example.test')
        self.assertEqual(str(mime['Subject']),'Re: Subject')
    def test_success_queues_crm_and_repeat_never_sends_twice(self):
        self.assertEqual(self.execute()['state'],'completed');self.assertEqual(self.execute()['outcome'],'already_sent')
        self.assertEqual(self.gmail.calls,1)
        self.assertEqual(self.sql("SELECT count(*) FROM outreach_pilot.delivery_operations WHERE draft_id=%s AND kind='hubspot_log'",(self.draft,))[0][0],1)
    def test_repeated_concurrent_previews_reuse_mime(self):
        with ThreadPoolExecutor(2) as pool:values=list(pool.map(lambda _:self.sender.prepare(self.draft),range(2)))
        self.assertEqual(values[0]['digest'],values[1]['digest'])
    def test_concurrent_confirmations_send_once(self):
        preview=self.sender.prepare(self.draft)
        with ThreadPoolExecutor(2) as pool:
            futures=[pool.submit(self.execute,preview) for _ in range(2)]
            for f in futures:f.result()
        self.assertEqual(self.gmail.calls,1)
    def test_changed_digest_blocks_approval(self):
        preview=self.sender.prepare(self.draft)
        with self.assertRaises(ValueError):self.sender.execute(preview,confirmed_digest='wrong',reviewer='Rui',gmail=self.gmail)
        self.assertIsNone(self.sender.operation(self.draft));self.assertEqual(self.gmail.calls,0)
    def test_new_reply_after_preview_prevents_send(self):
        preview=self.sender.prepare(self.draft);self.ingest('newer')
        with self.assertRaises(ValueError):self.execute(preview)
        self.assertEqual(self.gmail.calls,0)
    def test_review_only_approval_cannot_become_send(self):
        ReviewRepository(fixtures.DSN).decide(self.draft,'approved','Rui')
        with self.assertRaises(ValueError):self.sender.prepare(self.draft)
        self.assertEqual(self.gmail.calls,0)
    def test_existing_envelope_is_not_silently_replaced(self):
        preview=self.sender.prepare(self.draft)
        with self.assertRaises(ValueError):self.sender.delivery.prepare(self.draft,recipient='prospect@example.test',subject='Changed',hubspot_portal_id='123',hubspot_contact_id='456',in_reply_to='<latest@example.test>')
        self.assertEqual(self.sender.prepare(self.draft)['digest'],preview['digest'])
    def test_uncertain_send_reconciles_after_new_reply_without_resend(self):
        self.gmail.uncertain=True;preview=self.sender.prepare(self.draft)
        self.assertEqual(self.execute(preview)['state'],'reconciliation_required')
        self.ingest('newer')
        self.sql("UPDATE outreach_pilot.delivery_operations SET next_attempt_at=clock_timestamp()-interval '1 second' WHERE draft_id=%s AND kind='gmail_send'",(self.draft,),False)
        self.assertEqual(self.execute(preview)['state'],'completed');self.assertEqual(self.gmail.calls,1);self.assertEqual(self.gmail.lookups,1)

class ConfirmationTests(unittest.TestCase):
    def test_default_no_and_noninteractive(self):
        for response in ('','n','yes'):
            self.assertFalse(confirmed({'digest':'a'},None,interactive=True,input_fn=lambda _:response))
        self.assertFalse(confirmed({'digest':'a'},None,interactive=False,input_fn=lambda _:self.fail('must not prompt')))
        self.assertTrue(confirmed({'digest':'a'},None,interactive=True,input_fn=lambda _:'y'))
    def test_noninteractive_requires_exact_digest(self):
        self.assertTrue(confirmed({'digest':'a'},'a',interactive=False))
        with self.assertRaises(ValueError):confirmed({'digest':'a'},'wrong',interactive=False)
    def test_terminal_controls_cannot_execute(self):
        text=terminal_text('Hello\x1b[2J\u202eend')
        self.assertNotIn('\x1b',text);self.assertNotIn('\u202e',text)
