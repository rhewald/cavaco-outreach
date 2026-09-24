import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from unittest.mock import patch
import test_generation as fixtures
import test_delivery as delivery_fixtures
from outreach_recovery.crm_handoff import register_route
from outreach_recovery.gmail_inbound import ingest_message
from outreach_recovery.crm_worker import run_cycle,PortalQueue
from outreach_recovery.crm_repository import CRMRepository
from outreach_recovery.delivery_worker import DeliveryWorker

@unittest.skipUnless(fixtures.DSN,"Requires PostgreSQL")
class HandoffTests(unittest.TestCase):
    sql=fixtures.GenerationDatabaseTests.sql
    ingest=fixtures.GenerationDatabaseTests.ingest
    def setUp(self):
        fixtures.GenerationDatabaseTests.setUp(self)
        self.email=self.mailbox+'@test.example'
        self.parsed=dict(mailbox_id=self.mailbox,gmail_message_id='actual-reply',gmail_thread_id='generation',
            mime_message_id='<reply@example.test>',reply_ids=[],body='Actual reply',received_at=datetime.now(timezone.utc),
            sender='prospect@example.test',recipient=self.email,subject='Re: Actual subject')
    def route(self):
        with self.repo.connection() as c,c.cursor() as q:
            register_route(q,self.conversation,portal_id='123',contact_id='456',contact_email='prospect@example.test',
                mailbox_email=self.email,reviewer='Rui',approval_reference='test authorization')
    def jobs(self):return self.sql("SELECT j.state,j.payload FROM outreach_pilot.crm_activity_jobs j JOIN outreach_pilot.messages m ON m.id=j.message_id WHERE m.conversation_id=%s",(self.conversation,))
    def test_registered_reply_queues_original_once(self):
        self.route();ingest_message(self.repo,self.parsed);ingest_message(self.repo,self.parsed)
        jobs=self.jobs();self.assertEqual(len(jobs),1);self.assertEqual(jobs[0][0],'pending')
        self.assertEqual(jobs[0][1]['body'],'Actual reply');self.assertEqual(jobs[0][1]['subject'],'Re: Actual subject')
    def test_reply_before_route_backfills(self):
        ingest_message(self.repo,self.parsed);self.assertFalse(self.jobs());self.route();self.assertEqual(len(self.jobs()),1)
    def test_route_ingestion_race(self):
        with ThreadPoolExecutor(2) as pool:
            futures=[pool.submit(self.route),pool.submit(ingest_message,self.repo,self.parsed)]
            for f in futures:f.result()
        self.assertEqual(len(self.jobs()),1)
    def test_changed_duplicate_preserves_first_headers(self):
        ingest_message(self.repo,self.parsed)
        changed=dict(self.parsed,sender='other@example.test',subject='changed',body='changed')
        ingest_message(self.repo,changed);self.route()
        self.assertEqual(self.jobs()[0][1]['subject'],'Re: Actual subject')
        self.assertEqual(self.jobs()[0][1]['body'],'Actual reply')
    def test_unmatched_or_wrong_sender_not_logged(self):
        self.route()
        result=ingest_message(self.repo,dict(self.parsed,gmail_thread_id='unknown'))
        self.assertEqual(result[0],'unmatched');self.assertFalse(self.jobs())
        ingest_message(self.repo,dict(self.parsed,gmail_message_id='wrong',sender='wrong@example.test'))
        self.assertFalse(self.jobs())
    def test_enqueue_failure_rolls_back_receipt_and_message(self):
        self.route()
        with patch('outreach_recovery.crm_handoff.handoff_ingested',side_effect=RuntimeError('rollback')):
            with self.assertRaises(RuntimeError):ingest_message(self.repo,self.parsed)
        self.assertFalse(self.sql("SELECT 1 FROM outreach_pilot.messages WHERE mailbox_id=%s AND gmail_message_id='actual-reply'",(self.mailbox,)))
        self.assertFalse(self.sql("SELECT 1 FROM outreach_pilot.crm_inbound_headers WHERE mailbox_id=%s",(self.mailbox,)))
    def test_portal_queue_does_not_claim_other_account(self):
        self.route();ingest_message(self.repo,self.parsed)
        queue=PortalQueue(CRMRepository(fixtures.DSN),'999')
        job_id=self.sql("SELECT j.id FROM outreach_pilot.crm_activity_jobs j JOIN outreach_pilot.messages m ON m.id=j.message_id WHERE m.conversation_id=%s",(self.conversation,))[0][0]
        self.assertIsNone(queue.claim(kind='hubspot_log',operation_id=job_id))
        self.assertEqual(self.jobs()[0][0],'pending')

@unittest.skipUnless(fixtures.DSN,"Requires PostgreSQL")
class OutboundHandoffTests(unittest.TestCase):
    sql=fixtures.GenerationDatabaseTests.sql
    ingest=fixtures.GenerationDatabaseTests.ingest
    setUp=delivery_fixtures.DeliveryTests.setUp
    approve=delivery_fixtures.DeliveryTests.approve
    crm_id=delivery_fixtures.DeliveryTests.crm_id
    def test_approved_send_creates_route_and_crm_operation(self):
        self.approve();self.assertEqual(self.worker.run_once('gmail_send',self.operation),'completed')
        self.assertEqual(self.sql('SELECT contact_id FROM outreach_pilot.crm_conversation_routes WHERE conversation_id=%s',(self.conversation,))[0][0],'456')
        # Use a real numeric response ID for the HubSpot lane contract.
        from outreach_recovery.delivery_contracts import MutationResult,MutationState
        self.crm.create_email=lambda *a,**k:MutationResult(MutationState.ACCEPTED,'789')
        self.assertEqual(self.worker.run_once('hubspot_log',self.crm_id()),'completed')
        self.assertEqual(self.gmail.calls,1)
    def test_crm_failure_does_not_resend_gmail(self):
        self.approve();self.worker.run_once('gmail_send',self.operation)
        self.crm.uncertain=True
        self.assertEqual(self.worker.run_once('hubspot_log',self.crm_id()),'reconciliation_required')
        self.assertEqual(self.worker.run_once('gmail_send',self.operation),'idle');self.assertEqual(self.gmail.calls,1)
    def test_unapproved_send_does_not_register_route(self):
        self.assertEqual(self.worker.run_once('gmail_send'),'idle')
        self.assertFalse(self.sql('SELECT 1 FROM outreach_pilot.crm_conversation_routes WHERE conversation_id=%s',(self.conversation,)))
