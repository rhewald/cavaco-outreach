import unittest,base64
from pathlib import Path
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
import test_generation as fixtures
from test_delivery import FakeProvider
from outreach_recovery.dispatch_service import run_cycle,ScopedGmail,test_mode
from outreach_recovery.delivery_repository import DeliveryRepository
from outreach_recovery.review_repository import ReviewRepository
from outreach_recovery.install_services import configuration
from outreach_recovery.delivery_contracts import MutationState,ReconciliationState

class DispatchConfigTests(unittest.TestCase):
    def test_mode_fails_closed(self):
        self.assertTrue(test_mode('true'))
        self.assertFalse(test_mode('false'))
        for value in ('','0','False','TRUE'):
            with self.assertRaises(ValueError): test_mode(value)
    def test_plist_defaults_to_test(self):
        c=configuration('delivery',Path('/runtime/python'),Path('/code'),Path('/logs'),'sdr@cavaco.ai',47521149)
        self.assertEqual(c['EnvironmentVariables']['OUTREACH_TEST_MODE'],'true')
        self.assertIn('outreach_recovery.dispatch_service',c['ProgramArguments'])
        self.assertTrue(c['KeepAlive'])

@unittest.skipUnless(fixtures.DSN,'Requires disposable PostgreSQL')
class DispatchDatabaseTests(unittest.TestCase):
    sql=fixtures.GenerationDatabaseTests.sql
    def setUp(self):
        self.repo=DeliveryRepository(fixtures.DSN)
        self.conv=str(uuid4());self.draft=str(uuid4())
        with self.repo.connection() as conn,conn.cursor() as c:
            c.execute("INSERT INTO outreach_pilot.mailboxes(email) VALUES('sdr@cavaco.ai') ON CONFLICT(email) DO UPDATE SET email=excluded.email RETURNING id")
            self.mailbox=c.fetchone()[0]
            c.execute('INSERT INTO outreach_pilot.conversations(id,mailbox_id) VALUES(%s,%s)',(self.conv,self.mailbox))
            c.execute("INSERT INTO outreach_pilot.drafts(id,conversation_id,version_snapshot,body) VALUES(%s,%s,0,'Internal test only')",(self.draft,self.conv))
        self.envelope=self.repo.prepare(self.draft,recipient='rui@cavaco.ai',subject='Internal test',hubspot_portal_id='47521149',hubspot_contact_id='90197841728')
        ReviewRepository(fixtures.DSN).decide(self.draft,'approved','Rui',envelope_id=self.envelope)
        self.op,self.payload=self.sql("SELECT id,payload FROM outreach_pilot.delivery_operations WHERE draft_id=%s",(self.draft,))[0]
    def simulate_target(self):
        # Other approved fixtures may remain pending; process bounded cycles until ours is audited.
        for _ in range(100):
            run_cycle(self.repo,testing=True,provider=object())
            result=self.sql('SELECT outcome FROM outreach_pilot.delivery_simulations WHERE operation_id=%s',(self.op,))
            if result:return result[0][0]
        self.fail('Simulation not reached')
    def test_test_mode_never_mutates_real_delivery(self):
        self.assertEqual(self.simulate_target(),'simulated_completed')
        self.assertEqual(self.sql('SELECT state FROM outreach_pilot.delivery_operations WHERE id=%s',(self.op,))[0][0],'pending')
        self.assertEqual(self.sql('SELECT state FROM outreach_pilot.drafts WHERE id=%s',(self.draft,))[0][0],'approved')
        self.assertEqual(self.sql("SELECT count(*) FROM outreach_pilot.delivery_operations WHERE draft_id=%s AND kind='hubspot_log'",(self.draft,))[0][0],0)
        self.assertEqual(self.sql('SELECT count(*) FROM outreach_pilot.messages WHERE conversation_id=%s',(self.conv,))[0][0],0)
    def test_simulation_repeated_concurrent_cycles_one_record(self):
        with ThreadPoolExecutor(4) as pool: list(pool.map(lambda _:self.simulate_target(),range(4)))
        self.assertEqual(self.sql('SELECT count(*) FROM outreach_pilot.delivery_simulations WHERE operation_id=%s',(self.op,))[0][0],1)
    def test_allowlist_and_hidden_recipients_no_provider_calls(self):
        provider=FakeProvider();guard=ScopedGmail(provider)
        for payload in ({**self.payload,'to':'other@cavaco.ai'}, {**self.payload,'from':'rui@cavaco.ai'}, {**self.payload,'raw_mime':base64.urlsafe_b64encode(base64.urlsafe_b64decode(self.payload['raw_mime']).replace(b'To: rui@cavaco.ai',b'To: rui@cavaco.ai\r\nBcc: other@cavaco.ai')).decode()}):
            self.assertEqual(guard.send(payload,operation_id=str(self.op),timeout_seconds=1).state,MutationState.REJECTED)
            self.assertEqual(guard.reconcile(payload,operation_id=str(self.op),timeout_seconds=1).state,ReconciliationState.LOOKUP_FAILED)
        self.assertEqual((provider.calls,provider.lookups),(0,0))
    def test_simulated_stale_approval_is_blocked(self):
        self.sql('UPDATE outreach_pilot.conversations SET version_counter=1 WHERE id=%s',(self.conv,),False)
        self.assertEqual(self.simulate_target(),'blocked_stale')
    def test_live_wrapper_keeps_single_send_and_independent_crm(self):
        provider=FakeProvider()
        worker=__import__('outreach_recovery.delivery_worker',fromlist=['DeliveryWorker']).DeliveryWorker(self.repo,gmail=ScopedGmail(provider),hubspot=None)
        self.assertEqual(worker.run_once('gmail_send',self.op),'completed')
        self.assertEqual(worker.run_once('gmail_send',self.op),'idle')
        self.assertEqual(provider.calls,1)
        self.assertEqual(self.sql("SELECT count(*) FROM outreach_pilot.delivery_operations WHERE draft_id=%s AND kind='hubspot_log'",(self.draft,))[0][0],1)
