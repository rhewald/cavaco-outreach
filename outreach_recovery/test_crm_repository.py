import unittest
from concurrent.futures import ThreadPoolExecutor
import test_generation as fixtures
from outreach_recovery.crm_repository import CRMRepository,payload_digest
from outreach_recovery.delivery_worker import DeliveryWorker
from outreach_recovery.delivery_contracts import MutationResult,MutationState,ReconciliationResult,ReconciliationState

class Provider:
    def __init__(self): self.calls=0;self.lookups=0;self.crash=False;self.hidden=False;self.reject=False
    def create_email(self,*args,**kwargs):
        self.calls+=1
        if self.crash: raise SystemExit("crash after acceptance")
        return MutationResult(MutationState.REJECTED,safe_to_retry=True) if self.reject else MutationResult(MutationState.ACCEPTED,"789")
    def reconcile(self,*args,**kwargs):
        self.lookups+=1
        return ReconciliationResult(ReconciliationState.NOT_FOUND_YET) if self.hidden else ReconciliationResult(ReconciliationState.FOUND,"789")

@unittest.skipUnless(fixtures.DSN,"Requires disposable PostgreSQL")
class CRMTests(unittest.TestCase):
    sql=fixtures.GenerationDatabaseTests.sql
    ingest=fixtures.GenerationDatabaseTests.ingest
    def setUp(self):
        fixtures.GenerationDatabaseTests.setUp(self)
        self.crm=CRMRepository(fixtures.DSN)
        self.args=dict(portal_id="123",contact_id="456",sender="prospect@example.test",recipient="sdr@example.test",subject="Re: test")
        self.intent=self.crm.prepare(self.message,**self.args)
        self.provider=Provider();self.worker=DeliveryWorker(self.crm,gmail=None,hubspot=self.provider)
    def approve(self): return self.crm.approve(self.intent["id"],reviewer="Rui",digest=payload_digest(self.intent["payload"]))
    def run_worker(self): return self.worker.run_once("hubspot_log",self.intent["id"])
    def expire(self):
        self.sql("UPDATE outreach_pilot.crm_activity_jobs SET next_attempt_at=clock_timestamp()-interval '1 second',lease_until=CASE WHEN lease_token IS NOT NULL THEN clock_timestamp()-interval '1 second' END WHERE id=%s",(self.intent["id"],),False)
    def test_approval_required_and_happy_path(self):
        self.assertEqual(self.run_worker(),"idle");self.assertEqual(self.provider.calls,0)
        self.approve();self.assertEqual(self.run_worker(),"completed")
        self.assertEqual(self.run_worker(),"idle");self.assertEqual(self.provider.calls,1)
    def test_duplicate_prepare_and_approval(self):
        self.assertEqual(self.crm.prepare(self.message,**self.args)["id"],self.intent["id"])
        with ThreadPoolExecutor(2) as pool: values=list(pool.map(lambda _:self.approve(),range(2)))
        self.assertEqual(sorted(values),[False,True])
        self.assertEqual(self.sql("SELECT count(*) FROM outreach_pilot.crm_activity_events WHERE job_id=%s AND event='approved'",(self.intent["id"],))[0][0],1)
    def test_concurrent_workers_single_create(self):
        self.approve()
        with ThreadPoolExecutor(2) as pool: values=list(pool.map(lambda _:self.run_worker(),range(2)))
        self.assertEqual(sorted(values),["completed","idle"]);self.assertEqual(self.provider.calls,1)
    def test_expired_completion_rejected(self):
        self.approve();claim=self.crm.claim(kind="hubspot_log",operation_id=self.intent["id"])
        self.crm.begin(claim);self.expire()
        self.assertFalse(self.crm.complete(claim,"789"))
        self.assertEqual(self.run_worker(),"completed");self.assertEqual(self.provider.calls,0)
    def test_crash_and_visibility_delay_never_recreate(self):
        self.approve();self.provider.crash=True
        with self.assertRaises(SystemExit):self.run_worker()
        self.expire();self.provider.hidden=True
        self.assertEqual(self.run_worker(),"reconciliation_required")
        self.expire();self.provider.hidden=False
        self.assertEqual(self.run_worker(),"completed");self.assertEqual(self.provider.calls,1)
    def test_definitive_rejection_can_retry(self):
        self.approve();self.provider.reject=True
        self.assertEqual(self.run_worker(),"rejected")
        self.expire();self.provider.reject=False
        self.assertEqual(self.run_worker(),"completed");self.assertEqual(self.provider.calls,2)
    def test_payload_immutable_and_preview_digest_checked(self):
        with self.assertRaises(ValueError):self.crm.approve(self.intent["id"],reviewer="Rui",digest="wrong")
        with self.assertRaises(Exception):self.sql("UPDATE outreach_pilot.crm_activity_jobs SET payload=jsonb_set(payload,'{body}','\"changed\"') WHERE id=%s",(self.intent["id"],),False)
    def test_manual_review_after_bounded_reconciliation(self):
        self.approve();self.provider.crash=True
        with self.assertRaises(SystemExit):self.run_worker()
        self.provider.hidden=True
        for _ in range(7):self.expire();self.run_worker()
        self.assertEqual(self.sql("SELECT state FROM outreach_pilot.crm_activity_jobs WHERE id=%s",(self.intent["id"],))[0][0],"manual_review")
        self.assertEqual(self.provider.calls,1)
