"""Real PostgreSQL, fake providers: no external keys or network access."""
import re
import os
import multiprocessing
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from fastapi.testclient import TestClient
import test_generation as fixtures
from outreach_recovery.delivery_contracts import MutationResult, MutationState, ReconciliationResult, ReconciliationState
from outreach_recovery.delivery_repository import DeliveryRepository
from outreach_recovery.delivery_worker import DeliveryWorker
from outreach_recovery.review_repository import ReviewRepository
from outreach_recovery.review_app import create_app

class FakeProvider:
    def __init__(self):
        self.calls=0; self.lookups=0; self.accepted={}; self.crash=False; self.uncertain=False
        self.hidden=False; self.failure=False; self.reject=False; self.callback=None
    def send(self,payload,*,operation_id,timeout_seconds):
        self.calls+=1
        if self.callback: self.callback()
        if self.reject: return MutationResult(MutationState.REJECTED,safe_to_retry=True)
        key='provider-'+operation_id
        self.accepted[operation_id]=key
        if self.crash: raise SystemExit('Simulated process death after provider acceptance')
        if self.uncertain: return MutationResult(MutationState.UNCERTAIN)
        return MutationResult(MutationState.ACCEPTED,key,'generation')
    create_email=send
    def reconcile(self,payload,*,operation_id,timeout_seconds):
        self.lookups+=1
        if self.failure: raise TimeoutError('Private transport error must not be persisted')
        if self.hidden or operation_id not in self.accepted:
            return ReconciliationResult(ReconciliationState.NOT_FOUND_YET)
        return ReconciliationResult(ReconciliationState.FOUND,self.accepted[operation_id],'generation')

class ExitAfterAcceptance:
    def __init__(self, pipe): self.pipe=pipe
    def send(self, payload, *, operation_id, timeout_seconds):
        self.pipe.send((operation_id,'accepted-before-process-exit'))
        self.pipe.close()
        os._exit(17)

def crash_worker_process(dsn, operation_id, pipe):
    worker=DeliveryWorker(DeliveryRepository(dsn),gmail=ExitAfterAcceptance(pipe),hubspot=None)
    worker.run_once('gmail_send',operation_id)

@unittest.skipUnless(fixtures.DSN and fixtures.psycopg2,'Requires disposable PostgreSQL')
class DeliveryTests(unittest.TestCase):
    sql=fixtures.GenerationDatabaseTests.sql
    ingest=fixtures.GenerationDatabaseTests.ingest
    def setUp(self):
        fixtures.GenerationDatabaseTests.setUp(self)
        self.draft=self.repo.save(self.repo.claim(),'Thank you for your interest.')
        self.delivery=DeliveryRepository(fixtures.DSN)
        self.reviews=ReviewRepository(fixtures.DSN)
        self.envelope=self.delivery.prepare(self.draft,recipient='prospect@example.test',subject='Re: Example',
            hubspot_portal_id='123',hubspot_contact_id='456',in_reply_to='<first@example.test>')
        self.gmail=FakeProvider(); self.crm=FakeProvider()
        self.worker=DeliveryWorker(self.delivery,gmail=self.gmail,hubspot=self.crm)
    def approve(self):
        result=self.reviews.decide(self.draft,'approved','Rui',envelope_id=self.envelope)
        rows=self.sql("SELECT id FROM outreach_pilot.delivery_operations WHERE draft_id=%s AND kind='gmail_send'",(self.draft,))
        self.operation=rows[0][0] if rows else None
        return result
    def state(self,op=None):
        return self.sql('SELECT state FROM outreach_pilot.delivery_operations WHERE id=%s',(op or self.operation,))[0][0]
    def due(self,op=None):
        self.sql("UPDATE outreach_pilot.delivery_operations SET next_attempt_at=clock_timestamp()-interval '1 second',lease_until=CASE WHEN lease_token IS NOT NULL THEN clock_timestamp()-interval '1 second' END WHERE id=%s",(op or self.operation,),False)
    def crm_id(self):
        return self.sql("SELECT id FROM outreach_pilot.delivery_operations WHERE draft_id=%s AND kind='hubspot_log'",(self.draft,))[0][0]
    def test_happy_path_two_independent_operations_and_one_message(self):
        self.approve()
        self.assertEqual(self.worker.run_once('gmail_send',self.operation),'completed')
        self.assertEqual(self.worker.run_once('hubspot_log',self.crm_id()),'completed')
        self.assertEqual(self.state(),'completed')
        self.assertEqual(self.state(self.crm_id()),'completed')
        self.assertEqual(self.sql('SELECT state FROM outreach_pilot.drafts WHERE id=%s',(self.draft,))[0][0],'sent')
        self.assertEqual(self.sql("SELECT count(*) FROM outreach_pilot.messages WHERE conversation_id=%s AND direction='outbound'",(self.conversation,))[0][0],1)
        self.assertEqual((self.gmail.calls,self.crm.calls),(1,1))
    def test_double_approval_creates_exactly_one_intent(self):
        barrier=Barrier(6)
        def approve(_):
            barrier.wait(timeout=10)
            return self.reviews.decide(self.draft,'approved','Rui',envelope_id=self.envelope)
        with ThreadPoolExecutor(max_workers=6) as pool: results=list(pool.map(approve,range(6)))
        self.assertEqual(results.count('approved'),1)
        self.assertEqual(results.count('already_approved'),5)
        self.assertEqual(self.sql('SELECT count(*) FROM outreach_pilot.delivery_operations WHERE draft_id=%s',(self.draft,))[0][0],1)
    def test_stale_before_claim_never_sends(self):
        self.approve(); self.ingest('new')
        self.assertEqual(self.worker.run_once('gmail_send',self.operation),'idle')
        self.assertEqual(self.state(),'superseded'); self.assertEqual(self.gmail.calls,0)
    def test_stale_after_claim_before_begin_never_sends(self):
        self.approve(); claim=self.delivery.claim(kind='gmail_send',operation_id=self.operation)
        self.ingest('new')
        self.assertFalse(self.delivery.begin(claim)); self.assertEqual(self.state(),'superseded')
    def test_expired_completion_fenced_and_recovery_never_sends(self):
        self.approve(); old=self.delivery.claim(kind='gmail_send',operation_id=self.operation)
        self.assertTrue(self.delivery.begin(old)); self.due()
        new=self.delivery.claim(kind='gmail_send',operation_id=self.operation)
        self.assertTrue(new['recovery'])
        self.assertFalse(self.delivery.complete(old,'stale'))
        self.assertFalse(self.delivery.begin(new))
        self.assertFalse(self.sql("SELECT id FROM outreach_pilot.delivery_operations WHERE kind='hubspot_log' AND draft_id=%s",(self.draft,)))
    def test_delayed_search_visibility_stays_reconciliation_only(self):
        self.approve(); self.gmail.uncertain=True; self.gmail.hidden=True
        self.assertEqual(self.worker.run_once('gmail_send',self.operation),'reconciliation_required')
        self.assertEqual(self.worker.run_once('gmail_send',self.operation),'idle')
        self.due(); self.worker.run_once('gmail_send',self.operation)
        self.assertEqual(self.state(),'reconciliation_required')
        self.gmail.hidden=False; self.due(); self.worker.run_once('gmail_send',self.operation)
        self.assertEqual(self.state(),'completed'); self.assertEqual(self.gmail.calls,1)
    def test_ambiguous_hubspot_creation_reconciles_without_duplicate(self):
        self.approve(); self.worker.run_once('gmail_send',self.operation)
        crm=self.crm_id(); self.crm.uncertain=True
        self.worker.run_once('hubspot_log',crm)
        self.assertEqual(self.state(crm),'reconciliation_required')
        self.due(crm); self.worker.run_once('hubspot_log',crm)
        self.assertEqual(self.state(crm),'completed'); self.assertEqual((self.gmail.calls,self.crm.calls),(1,1))
    def test_process_death_after_acceptance_recovers(self):
        self.approve()
        ctx=multiprocessing.get_context('spawn')
        receive,send=ctx.Pipe(duplex=False)
        process=ctx.Process(target=crash_worker_process,args=(fixtures.DSN,self.operation,send))
        process.start(); send.close()
        try:
            self.assertTrue(receive.poll(15),'Child never reached simulated provider acceptance')
            operation_id,provider_id=receive.recv()
            process.join(10)
            self.assertEqual(process.exitcode,17)
            self.gmail.accepted[operation_id]=provider_id
            self.assertEqual(self.state(),'processing'); self.due()
            self.worker.run_once('gmail_send',self.operation)
            self.assertEqual(self.state(),'completed'); self.assertEqual(self.gmail.calls,0)
        finally:
            if process.is_alive(): process.terminate(); process.join(5)
            receive.close()
    def test_hubspot_outage_leaves_gmail_complete_and_backs_off(self):
        self.approve(); self.worker.run_once('gmail_send',self.operation)
        crm=self.crm_id(); self.crm.uncertain=True; self.crm.failure=True
        self.worker.run_once('hubspot_log',crm)
        for _ in range(2):
            self.due(crm); self.worker.run_once('hubspot_log',crm)
            self.assertEqual(self.worker.run_once('hubspot_log',crm),'idle')
        self.assertEqual(self.state(),'completed'); self.assertEqual(self.state(crm),'reconciliation_required')
        self.assertEqual((self.gmail.calls,self.crm.calls),(1,1))
    def test_safe_crm_rejection_can_retry_but_uncertain_cannot(self):
        self.approve(); self.worker.run_once('gmail_send',self.operation)
        crm=self.crm_id(); self.crm.reject=True
        self.worker.run_once('hubspot_log',crm)
        self.assertEqual(self.state(crm),'pending'); self.due(crm); self.crm.reject=False
        self.worker.run_once('hubspot_log',crm)
        self.assertEqual(self.state(crm),'completed'); self.assertEqual(self.gmail.calls,1)
    def test_repeated_begin_and_completion_have_no_duplicate_effects(self):
        self.approve(); claim=self.delivery.claim(kind='gmail_send',operation_id=self.operation)
        self.assertTrue(self.delivery.begin(claim)); self.assertFalse(self.delivery.begin(claim))
        self.assertTrue(self.delivery.complete(claim,'confirmed'))
        self.assertFalse(self.delivery.complete(claim,'confirmed'))
        self.assertEqual(self.sql('SELECT version_counter FROM outreach_pilot.conversations WHERE id=%s',(self.conversation,))[0][0],2)
    def test_provider_call_holds_no_database_locks(self):
        self.approve()
        def check():
            self.sql('SELECT id FROM outreach_pilot.conversations WHERE id=%s FOR UPDATE NOWAIT',(self.conversation,))
            self.sql('SELECT id FROM outreach_pilot.delivery_operations WHERE id=%s FOR UPDATE NOWAIT',(self.operation,))
        self.gmail.callback=check
        self.assertEqual(self.worker.run_once('gmail_send',self.operation),'completed')
    def test_competing_workers_claim_once(self):
        self.approve(); barrier=Barrier(5)
        def claim(_):
            barrier.wait(timeout=10)
            return self.delivery.claim(kind='gmail_send',operation_id=self.operation)
        with ThreadPoolExecutor(max_workers=5) as pool: claims=list(pool.map(claim,range(5)))
        self.assertEqual(sum(c is not None for c in claims),1)
    def test_immutable_payload_envelope_and_audit(self):
        self.approve(); self.delivery.claim(kind='gmail_send',operation_id=self.operation)
        for query,args in (
            ("UPDATE outreach_pilot.delivery_operations SET payload='{}' WHERE id=%s",(self.operation,)),
            ("DELETE FROM outreach_pilot.delivery_operations WHERE id=%s",(self.operation,)),
            ("UPDATE outreach_pilot.delivery_envelopes SET payload='{}' WHERE id=%s",(self.envelope,)),
            ("DELETE FROM outreach_pilot.delivery_attempts WHERE operation_id=%s",(self.operation,))):
            with self.assertRaises(fixtures.psycopg2.errors.RaiseException): self.sql(query,args,False)
    def test_missing_envelope_confirmation_blocks_unseen_delivery(self):
        self.assertEqual(self.reviews.decide(self.draft,'approved','Rui'),'conflict')
        self.assertEqual(self.sql('SELECT state FROM outreach_pilot.drafts WHERE id=%s',(self.draft,))[0][0],'pending_review')
    def test_review_page_shows_envelope_and_approval_queues_it(self):
        with TestClient(create_app(self.reviews,'Rui'),base_url='http://127.0.0.1:8765') as client:
            page=client.get(f'/reviews/{self.draft}')
            self.assertIn('prospect@example.test',page.text); self.assertIn('Re: Example',page.text)
            csrf=re.search(r'name="csrf" value="([^"]+)"',page.text).group(1)
            response=client.post(f'/reviews/{self.draft}/approve',data={'csrf':csrf,'envelope_id':self.envelope},headers={'origin':'http://127.0.0.1:8765'})
            self.assertEqual(response.status_code,200)
            self.assertEqual(self.sql('SELECT count(*) FROM outreach_pilot.delivery_operations WHERE draft_id=%s',(self.draft,))[0][0],1)
    def test_attempt_limit_routes_unresolved_to_manual(self):
        self.approve(); self.gmail.uncertain=True; self.gmail.hidden=True
        for _ in range(8):
            self.worker.run_once('gmail_send',self.operation); self.due()
        self.assertEqual(self.state(),'manual_review'); self.assertEqual(self.gmail.calls,1)
    def test_outbox_failure_rolls_back_approval_and_audit(self):
        self.sql("""CREATE FUNCTION outreach_pilot.test_fail_delivery() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'injected'; END; $$;
        CREATE TRIGGER test_fail_delivery BEFORE INSERT ON outreach_pilot.delivery_operations FOR EACH ROW EXECUTE FUNCTION outreach_pilot.test_fail_delivery();""",fetch=False)
        try:
            with self.assertRaises(fixtures.psycopg2.errors.RaiseException): self.approve()
            self.assertEqual(self.sql('SELECT state FROM outreach_pilot.drafts WHERE id=%s',(self.draft,))[0][0],'pending_review')
            self.assertFalse(self.sql('SELECT id FROM outreach_pilot.review_events WHERE draft_id=%s',(self.draft,)))
        finally:
            self.sql('DROP TRIGGER test_fail_delivery ON outreach_pilot.delivery_operations; DROP FUNCTION outreach_pilot.test_fail_delivery();',fetch=False)

    def test_completion_failure_rolls_back_ledger_and_crm_intent(self):
        self.approve()
        self.sql("""CREATE FUNCTION outreach_pilot.test_fail_completion() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF NEW.event='accepted' THEN RAISE EXCEPTION 'injected'; END IF; RETURN NEW; END; $$;
        CREATE TRIGGER test_fail_completion BEFORE INSERT ON outreach_pilot.delivery_attempts FOR EACH ROW EXECUTE FUNCTION outreach_pilot.test_fail_completion();""",fetch=False)
        try:
            with self.assertRaises(fixtures.psycopg2.errors.RaiseException): self.worker.run_once('gmail_send',self.operation)
            self.assertEqual(self.state(),'processing')
            self.assertEqual(self.sql('SELECT version_counter FROM outreach_pilot.conversations WHERE id=%s',(self.conversation,))[0][0],1)
            self.assertFalse(self.sql("SELECT id FROM outreach_pilot.delivery_operations WHERE draft_id=%s AND kind='hubspot_log'",(self.draft,)))
        finally:
            self.sql('DROP TRIGGER test_fail_completion ON outreach_pilot.delivery_attempts; DROP FUNCTION outreach_pilot.test_fail_completion();',fetch=False)
        self.due(); self.worker.run_once('gmail_send',self.operation)
        self.assertEqual(self.state(),'completed'); self.assertEqual(self.gmail.calls,1)

    def test_started_gmail_cannot_be_reopened_by_pending_status(self):
        self.approve(); self.gmail.uncertain=True
        self.worker.run_once('gmail_send',self.operation)
        with self.assertRaises(fixtures.psycopg2.errors.RaiseException):
            self.sql("UPDATE outreach_pilot.delivery_operations SET state='pending' WHERE id=%s",(self.operation,),False)
        self.due()
        self.worker.run_once('gmail_send',self.operation)
        self.assertEqual(self.gmail.calls,1); self.assertEqual(self.state(),'completed')

    def test_inbound_during_send_preserves_actual_provider_outcome(self):
        self.approve(); self.gmail.callback=lambda: self.ingest('arrived-during-send')
        self.worker.run_once('gmail_send',self.operation)
        self.assertEqual(self.state(),'completed')
        self.assertEqual(self.sql('SELECT version_counter FROM outreach_pilot.conversations WHERE id=%s',(self.conversation,))[0][0],3)
        self.assertEqual(self.sql('SELECT reason FROM outreach_pilot.delivery_followups WHERE operation_id=%s',(self.operation,))[0][0],'conversation_changed_during_send')
        self.assertEqual(self.sql('SELECT state FROM outreach_pilot.drafts WHERE id=%s',(self.draft,))[0][0],'sent')
