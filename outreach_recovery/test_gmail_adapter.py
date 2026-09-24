import base64
import unittest
from email.message import EmailMessage
from email.policy import SMTP
from uuid import uuid4
import httpx
from outreach_recovery.gmail_adapter import GmailAdapter
from outreach_recovery.delivery_contracts import MutationState, ReconciliationState

class GmailAdapterTests(unittest.TestCase):
    def setUp(self):
        self.mailbox=str(uuid4()); self.operation=str(uuid4()); self.calls=[]
        self.message=EmailMessage(policy=SMTP)
        for key,value in {'From':'seller@example.test','To':'buyer@example.test','Subject':'Re: Hello',
            'Message-ID':'<stable@example.test>','In-Reply-To':'<inbound@example.test>',
            'References':'<inbound@example.test>'}.items(): self.message[key]=value
        self.message.set_content('Hello buyer.\nCafé — thanks!')
        self.payload=dict(mailbox_id=self.mailbox,**{'from':'seller@example.test','to':'buyer@example.test'},
            subject='Re: Hello',body='Hello buyer.\nCafé — thanks!',mime_message_id='<stable@example.test>',
            raw_mime=base64.urlsafe_b64encode(self.message.as_bytes()).decode(),thread_id='thread1')
        self.send_status=200; self.profile_email='seller@example.test'; self.send_body={'id':'sent1','threadId':'thread1'}
        self.items=[{'id':'sent1'}]; self.detail_override={}; self.pages=0
        self.raise_send=False; self.lookup_status=200; self.token_calls=0
        self.adapter=GmailAdapter(mailbox_id=self.mailbox,email='seller@example.test',token_provider=self.token,
                                  transport=httpx.MockTransport(self.handle))
    def token(self,*,timeout_seconds):
        self.token_calls+=1
        self.assertGreater(timeout_seconds,0)
        return 'test-only-access-token'
    def handle(self,request):
        self.calls.append(request)
        self.assertEqual(request.url.host,'gmail.googleapis.com')
        self.assertEqual(request.headers['Authorization'],'Bearer test-only-access-token')
        if request.url.path.endswith('/profile'):
            return httpx.Response(200,json={'emailAddress':self.profile_email})
        if request.url.path.endswith('/send'):
            if self.raise_send: raise httpx.ReadTimeout('private raw content',request=request)
            return httpx.Response(self.send_status,json=self.send_body)
        if request.url.path.endswith('/messages'):
            self.pages+=1
            return httpx.Response(self.lookup_status,json={'messages':self.items})
        return httpx.Response(200,json=dict(id=request.url.path.split('/')[-1],threadId='thread1',labelIds=['SENT'],raw=self.payload['raw_mime'],**self.detail_override))
    def send(self):
        return self.adapter.send(self.payload,operation_id=self.operation,timeout_seconds=5)
    def reconcile(self):
        return self.adapter.reconcile(self.payload,operation_id=self.operation,timeout_seconds=5)
    def posts(self): return [r for r in self.calls if r.method=='POST']
    def test_send_preserves_exact_pinned_payload(self):
        import json
        self.assertEqual(self.send().state,MutationState.ACCEPTED)
        self.assertEqual(json.loads(self.posts()[0].content),{'raw':self.payload['raw_mime'],'threadId':'thread1'})
        self.assertEqual(len(self.posts()),1)
    def test_initial_send_omits_thread_id(self):
        import json
        self.payload['thread_id']=None
        self.assertEqual(self.send().state,MutationState.ACCEPTED)
        self.assertNotIn('threadId',json.loads(self.posts()[0].content))
    def test_wrong_authenticated_mailbox_never_posts(self):
        self.profile_email='other@example.test'
        self.assertEqual(self.send().state,MutationState.REJECTED)
        self.assertEqual(self.posts(),[])
    def test_mismatched_envelope_rejected_before_authentication(self):
        for key,value in [('body','Changed'),('to','other@example.test'),('subject','Changed'),('mailbox_id',str(uuid4())),('mime_message_id','<other@example.test>')]:
            with self.subTest(key=key):
                original=self.payload[key]; self.payload[key]=value
                self.assertEqual(self.send().state,MutationState.REJECTED)
                self.payload[key]=original
        self.assertEqual(self.calls,[])
    def test_duplicate_header_and_hidden_recipient_rejected(self):
        raw=base64.urlsafe_b64decode(self.payload['raw_mime'])
        for header in (b'To: attacker@example.test\r\n',b'Bcc: attacker@example.test\r\n'):
            self.payload['raw_mime']=base64.urlsafe_b64encode(header+raw).decode()
            self.assertEqual(self.send().state,MutationState.REJECTED)
        self.assertEqual(self.posts(),[])
    def test_transient_and_redirect_outcomes_never_retry_mutation(self):
        for status in (302,408,409,500,502,503,504):
            with self.subTest(status=status):
                self.calls=[]; self.send_status=status
                self.assertEqual(self.send().state,MutationState.UNCERTAIN)
                self.assertEqual(len(self.posts()),1)
    def test_explicit_rejections_are_terminal(self):
        for status in (400,401,403,404,413,422,429):
            self.calls=[]; self.send_status=status
            result=self.send()
            self.assertEqual(result.state,MutationState.REJECTED)
            self.assertFalse(result.safe_to_retry)
            self.assertEqual(len(self.posts()),1)
    def test_timeout_is_sanitized_and_uncertain(self):
        self.raise_send=True
        result=self.send()
        self.assertEqual(result.state,MutationState.UNCERTAIN)
        self.assertNotIn('private',repr(result)); self.assertEqual(len(self.posts()),1)
    def test_incomplete_or_conflicting_acceptance_is_uncertain(self):
        for body in ({},{'id':'sent1'},{'id':'sent1','threadId':'wrong'}):
            self.send_body=body
            self.assertEqual(self.send().state,MutationState.UNCERTAIN)
    def test_lookup_verifies_full_raw_message(self):
        result=self.reconcile()
        self.assertEqual(result.state,ReconciliationState.FOUND)
        self.assertEqual(result.provider_id,'sent1'); self.assertEqual(self.posts(),[])
        request=[r for r in self.calls if r.url.path.endswith('/messages')][0]
        self.assertEqual(request.url.params['q'],'rfc822msgid:stable@example.test')
        self.assertEqual(request.url.params['labelIds'],'SENT')
    def test_empty_lookup_is_not_found_yet(self):
        self.items=[]
        self.assertEqual(self.reconcile().state,ReconciliationState.NOT_FOUND_YET)
        self.assertEqual(self.posts(),[])
    def test_multiple_matching_messages_are_not_silent_success(self):
        self.items=[{'id':'sent1'},{'id':'sent2'}]
        self.assertEqual(self.reconcile().state,ReconciliationState.LOOKUP_FAILED)
    def test_conflicting_search_hit_fails_closed(self):
        # Use a separate transport to avoid duplicate keyword fixture fields.
        for changes in ({'labelIds':['INBOX']},{'threadId':'wrong'},{'raw':base64.urlsafe_b64encode(b'bad mime').decode()}):
            def handler(request):
                if request.url.path.endswith('/profile'): return httpx.Response(200,json={'emailAddress':self.profile_email})
                if request.url.path.endswith('/messages'): return httpx.Response(200,json={'messages':[{'id':'sent1'}]})
                data=dict(id='sent1',threadId='thread1',labelIds=['SENT'],raw=self.payload['raw_mime']); data.update(changes)
                return httpx.Response(200,json=data)
            self.adapter.transport=httpx.MockTransport(handler)
            self.assertEqual(self.reconcile().state,ReconciliationState.LOOKUP_FAILED)
    def test_pagination_consumes_all_pages_before_success(self):
        def handler(request):
            self.calls.append(request)
            if request.url.path.endswith('/profile'): return httpx.Response(200,json={'emailAddress':self.profile_email})
            if request.url.path.endswith('/messages'):
                if 'pageToken' not in request.url.params: return httpx.Response(200,json={'messages':[],'nextPageToken':'next'})
                return httpx.Response(200,json={'messages':[{'id':'sent1'}]})
            return httpx.Response(200,json=dict(id='sent1',threadId='thread1',labelIds=['SENT'],raw=self.payload['raw_mime']))
        self.adapter.transport=httpx.MockTransport(handler)
        self.assertEqual(self.reconcile().state,ReconciliationState.FOUND)
        self.assertEqual(len([r for r in self.calls if r.url.path.endswith('/messages')]),2)
    def test_lookup_failure_is_not_absence(self):
        self.lookup_status=503
        self.assertEqual(self.reconcile().state,ReconciliationState.LOOKUP_FAILED)
    def test_expired_budget_and_missing_token_never_send(self):
        self.assertEqual(self.adapter.send(self.payload,operation_id=self.operation,timeout_seconds=0).state,MutationState.REJECTED)
        self.adapter.token_provider=lambda **_: ''
        self.assertEqual(self.send().state,MutationState.REJECTED)
        self.assertEqual(self.calls,[])

# Exercise the real repository/worker boundary with mocked HTTP, not just adapter calls.
import test_delivery as delivery_fixtures
import test_generation as generation_fixtures

@unittest.skipUnless(generation_fixtures.DSN and generation_fixtures.psycopg2,'Requires disposable PostgreSQL')
class GmailDatabaseTests(unittest.TestCase):
    sql=delivery_fixtures.DeliveryTests.sql
    ingest=delivery_fixtures.DeliveryTests.ingest
    approve=delivery_fixtures.DeliveryTests.approve
    due=delivery_fixtures.DeliveryTests.due
    state=delivery_fixtures.DeliveryTests.state
    crm_id=delivery_fixtures.DeliveryTests.crm_id
    def setUp(self):
        delivery_fixtures.DeliveryTests.setUp(self)
        self.http_posts=0; self.stored_raw=None; self.status=200
        self.worker.gmail=GmailAdapter(mailbox_id=self.mailbox,email=self.mailbox+'@test.example',
            token_provider=lambda **_: 'offline-token',transport=httpx.MockTransport(self.handle))
    def handle(self,request):
        import json
        if request.url.path.endswith('/profile'):
            return httpx.Response(200,json={'emailAddress':self.mailbox+'@test.example'})
        if request.method=='POST':
            self.http_posts+=1; self.stored_raw=json.loads(request.content)['raw']
            return httpx.Response(self.status,json={'id':'gmail-confirmed','threadId':'generation'})
        if request.url.path.endswith('/messages'):
            return httpx.Response(200,json={'messages':[{'id':'gmail-confirmed'}]})
        return httpx.Response(200,json={'id':'gmail-confirmed','threadId':'generation','labelIds':['SENT'],'raw':self.stored_raw})
    def test_approved_pinned_mime_reaches_mock_api_and_enqueues_crm(self):
        self.approve()
        self.assertEqual(self.worker.run_once('gmail_send',self.operation),'completed')
        self.assertIsNotNone(self.crm_id()); self.assertEqual(self.http_posts,1)
    def test_ambiguous_http_response_reconciles_without_second_post(self):
        self.approve(); self.status=503
        self.assertEqual(self.worker.run_once('gmail_send',self.operation),'reconciliation_required')
        self.due()
        self.assertEqual(self.worker.run_once('gmail_send',self.operation),'completed')
        self.assertEqual(self.http_posts,1); self.assertIsNotNone(self.crm_id())
