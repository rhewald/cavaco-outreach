import base64,copy,unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch
import test_generation as fixtures
from outreach_recovery.crm_handoff import register_route
from outreach_recovery.gmail_monitor import MonitorRepository,GmailMonitor,HistoryExpired,SyncError
from outreach_recovery.inbox_worker import EnrolledGenerationRepository,generate_once
from outreach_recovery.generation import GenerationWorker
from outreach_recovery.install_services import configuration

class Source:
    def __init__(self,email):
        self.email=email;self.pages={None:{'historyId':'120'}};self.raw_calls=[];self.history_calls=[]
        self.thread_items=[];self.deleted=False;self.expired=False;self.baseline_id='100';self.fail=False;self.bad_sender=False
    def baseline(self):return self.baseline_id
    def thread(self,thread):return {'id':thread,'messages':self.thread_items}
    def history(self,start,page):
        self.history_calls.append((start,page))
        if self.expired:self.expired=False;self.baseline_id='110';raise HistoryExpired()
        if self.fail:raise SyncError('provider_http_429',90)
        return copy.deepcopy(self.pages[page])
    def message(self,identifier):
        self.raw_calls.append(identifier)
        if self.deleted:return None
        m=EmailMessage();m['From']='bad@example.test' if self.bad_sender else 'prospect@example.test';m['To']=self.email
        m['Subject']='Re: test';m['Message-ID']='<'+identifier+'@example.test>';m.set_content('Reply '+identifier)
        return {'id':identifier,'threadId':'generation','labelIds':['INBOX'],
                'internalDate':str(int(datetime.now(timezone.utc).timestamp()*1000)),
                'raw':base64.urlsafe_b64encode(m.as_bytes()).decode()}
    def add(self,ids,page=None,next_page=None):
        self.pages[page]={'historyId':'120','history':[{'messagesAdded':[{'message':{'id':i,'threadId':'generation'}} for i in ids]}]}
        if next_page:self.pages[page]['nextPageToken']=next_page

@unittest.skipUnless(fixtures.DSN,'Requires PostgreSQL')
class MonitorTests(unittest.TestCase):
    sql=fixtures.GenerationDatabaseTests.sql
    ingest=fixtures.GenerationDatabaseTests.ingest
    def setUp(self):
        fixtures.GenerationDatabaseTests.setUp(self)
        self.email=self.mailbox+'@test.example'
        with self.repo.connection() as c,c.cursor() as q:
            register_route(q,self.conversation,portal_id='123',contact_id='456',contact_email='prospect@example.test',
                mailbox_email=self.email,reviewer='Rui',approval_reference='test')
        self.monitor_repo=MonitorRepository(fixtures.DSN,self.mailbox)
        self.source=Source(self.email);self.monitor=GmailMonitor(self.monitor_repo,self.source)
    def due(self,expire=False):
        self.sql("UPDATE outreach_pilot.gmail_monitor_state SET next_poll_at=clock_timestamp()-interval '1 second',lease_until=CASE WHEN %s THEN clock_timestamp()-interval '1 second' ELSE lease_until END WHERE mailbox_id=%s",(expire,self.mailbox),False)
    def state(self):return self.sql('SELECT history_id,page_token,last_error FROM outreach_pilot.gmail_monitor_state WHERE mailbox_id=%s',(self.mailbox,))[0]
    def test_initial_resync_and_history_ingest_read_message(self):
        self.source.thread_items=[{'id':'old','threadId':'generation'}];self.source.add(['new'])
        self.assertEqual(self.monitor.run_once(),'synced');self.assertEqual(self.state()[0],'120')
        self.assertEqual(self.source.raw_calls,['old','new'])
        self.assertEqual(self.sql("SELECT count(*) FROM outreach_pilot.crm_activity_jobs j JOIN outreach_pilot.messages m ON m.id=j.message_id WHERE m.conversation_id=%s",(self.conversation,))[0][0],2)
    def test_unrelated_thread_body_not_fetched(self):
        self.source.pages[None]={'historyId':'120','history':[{'messagesAdded':[{'message':{'id':'private','threadId':'unrelated'}}]}]}
        self.monitor.run_once();self.assertFalse(self.source.raw_calls)
    def test_pagination_checkpoints_without_advancing_start(self):
        self.source.add(['one'],next_page='p2');self.source.add(['two'],page='p2')
        self.monitor.max_pages=3 # thread resync, switch to history, first history page
        self.monitor.run_once();self.assertEqual(self.state()[:2],('100','p2'))
        self.due();self.monitor.run_once();self.assertEqual(self.state()[:2],('120',None))
        self.assertEqual(self.source.history_calls,[('100',None),('100','p2')])
    def test_budget_continues_same_page_without_duplicate(self):
        self.source.add(['one','two']);self.monitor.max_messages=1
        self.monitor.run_once();self.assertEqual(self.state()[0],'100')
        self.due();self.monitor.run_once();self.assertEqual(self.state()[0],'120')
        self.assertEqual(self.source.raw_calls,['one','two'])
    def test_expired_history_resyncs_threads(self):
        self.monitor.run_once();self.due();self.source.expired=True
        self.source.thread_items=[{'id':'recovered','threadId':'generation'}]
        self.assertEqual(self.monitor.run_once(),'synced');self.assertIn('recovered',self.source.raw_calls)
    def test_duplicate_history_only_ingests_once(self):
        self.source.add(['one','one']);self.monitor.run_once();self.due();self.monitor.run_once()
        self.assertEqual(self.source.raw_calls,['one'])
    def test_backoff_keeps_cursor(self):
        self.monitor.run_once();self.due();self.source.fail=True
        self.assertEqual(self.monitor.run_once(),'provider_http_429');self.assertEqual(self.state()[0],'120')
        self.assertIsNone(self.monitor_repo.claim())
        seconds=self.sql('SELECT extract(epoch FROM next_poll_at-clock_timestamp()) FROM outreach_pilot.gmail_monitor_state WHERE mailbox_id=%s',(self.mailbox,))[0][0]
        self.assertGreater(seconds,85)
    def test_only_one_monitor_claim(self):
        with ThreadPoolExecutor(2) as pool:claims=list(pool.map(lambda _:self.monitor_repo.claim(),range(2)))
        self.assertEqual(sum(c is not None for c in claims),1)
    def test_expired_lease_cannot_record_or_checkpoint(self):
        claim=self.monitor_repo.claim();self.due(expire=True)
        with self.assertRaises(SyncError):self.monitor_repo.record(claim,'one','generation','sent')
        with self.assertRaises(SyncError):self.monitor_repo.checkpoint(claim,history_id='999')
    def test_crash_after_ingestion_before_checkpoint_replays_safely(self):
        self.source.add(['one']);original=self.monitor_repo.checkpoint
        def crash(claim,**updates):
            if updates.get('history_id')=='120':raise SystemExit('process death')
            return original(claim,**updates)
        with patch.object(self.monitor_repo,'checkpoint',side_effect=crash):
            with self.assertRaises(SystemExit):self.monitor.run_once()
        self.due(expire=True);self.monitor.run_once();self.assertEqual(self.source.raw_calls,['one'])
    def test_ingestion_failure_rolls_back_monitor_receipt(self):
        self.source.add(['one'])
        with patch('outreach_recovery.gmail_inbound.ingest_in_transaction',side_effect=RuntimeError('failed')):
            # The monitor imports its own reference; patch that boundary.
            with patch('outreach_recovery.gmail_monitor.ingest_in_transaction',side_effect=RuntimeError('failed')):
                self.assertEqual(self.monitor.run_once(),'sync_failed')
        self.assertFalse(self.monitor_repo.seen('one'));self.assertEqual(self.state()[0],'100')
        self.due();self.monitor.run_once();self.assertTrue(self.monitor_repo.seen('one'))
    def test_sent_deleted_and_bad_sender_do_not_generate(self):
        self.source.thread_items=[{'id':'sent','threadId':'generation','labelIds':['SENT']}]
        self.source.add(['bad']);self.source.bad_sender=True;self.monitor.run_once()
        self.assertEqual(self.sql("SELECT outcome FROM outreach_pilot.gmail_monitor_messages WHERE mailbox_id=%s AND gmail_message_id='bad'",(self.mailbox,))[0][0],'quarantined')
        self.assertNotIn('sent',self.source.raw_calls)
        self.due();self.source.add(['deleted']);self.source.deleted=True;self.monitor.run_once()
        self.assertTrue(self.monitor_repo.seen('deleted'))
    def test_route_change_forces_resync(self):
        self.monitor.run_once();self.due();self.source.thread_items=[{'id':'missed','threadId':'generation'}]
        self.sql("UPDATE outreach_pilot.gmail_monitor_state SET route_fingerprint='old' WHERE mailbox_id=%s",(self.mailbox,),False)
        self.monitor.run_once();self.assertIn('missed',self.source.raw_calls)
    def test_generation_only_creates_review_draft(self):
        self.source.add(['one']);self.monitor.run_once()
        class Generator:
            timeout_seconds=1
            def generate(self,messages):return 'A reply for review.'
        worker=GenerationWorker(EnrolledGenerationRepository(fixtures.DSN,self.mailbox),Generator())
        self.assertEqual(worker.run_once(),'drafted')
        self.assertEqual(self.sql('SELECT state FROM outreach_pilot.drafts WHERE conversation_id=%s',(self.conversation,))[0][0],'pending_review')
        self.assertFalse(self.sql('SELECT 1 FROM outreach_pilot.delivery_operations WHERE conversation_id=%s',(self.conversation,)))
    def test_missing_key_does_not_claim_jobs(self):
        with patch.dict('os.environ',{},clear=True),patch('outreach_recovery.inbox_worker.DEFAULT_KEY',Path('/private/tmp/does-not-exist-key')):
            self.assertEqual(generate_once(EnrolledGenerationRepository(fixtures.DSN,self.mailbox)),'needs_openai_key')
        self.assertEqual(self.sql('SELECT attempts FROM outreach_pilot.reply_jobs WHERE id=%s',(self.job_id,))[0][0],0)

class ServiceTests(unittest.TestCase):
    def test_agents_use_absolute_paths_and_no_secrets_or_send_worker(self):
        for kind in ('crm','inbox'):
            cfg=configuration(kind,Path('/persistent/bin/python'),Path('/persistent/code'),Path('/persistent/logs'),'sdr@cavaco.ai',123)
            self.assertTrue(cfg['KeepAlive']);self.assertTrue(cfg['RunAtLoad'])
            text=str(cfg);self.assertNotIn('API_KEY',text);self.assertNotIn('TOKEN',text);self.assertNotIn('gmail_send',text)


class HistoryBoundaryTests(unittest.TestCase):
    def source(self,handler):
        import httpx
        from outreach_recovery.gmail_adapter import GmailAdapter
        from outreach_recovery.gmail_monitor import GmailHistorySource
        def handle(request):
            if request.url.path.endswith('/profile'):return httpx.Response(200,json={'emailAddress':'sdr@example.test','historyId':'100'})
            return handler(request)
        adapter=GmailAdapter(mailbox_id='00000000-0000-0000-0000-000000000001',email='sdr@example.test',
            token_provider=lambda **kw:'private',transport=httpx.MockTransport(handle))
        return GmailHistorySource(adapter)
    def test_history_404_and_stale_page_resync(self):
        import httpx
        for status,page in ((404,None),(400,'old')):
            source=self.source(lambda request:httpx.Response(status,json={}))
            with self.assertRaises(HistoryExpired):source.history('100',page)
    def test_numeric_retry_after(self):
        import httpx
        source=self.source(lambda request:httpx.Response(429,headers={'Retry-After':'120'}))
        with self.assertRaises(SyncError) as caught:source.history('100',None)
        self.assertEqual(caught.exception.retry_after,120)
    def test_query_uses_message_added_without_unread_filter(self):
        import httpx
        observed=[]
        def handle(request):observed.append(request);return httpx.Response(200,json={'historyId':'200'})
        self.source(handle).history('100',None)
        self.assertEqual(observed[0].url.params['historyTypes'],'messageAdded')
        self.assertNotIn('labelId',observed[0].url.params)
