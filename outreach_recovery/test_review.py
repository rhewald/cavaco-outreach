import re
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from fastapi.testclient import TestClient
from outreach_recovery.review_app import create_app
from outreach_recovery.review_repository import ReviewRepository
import test_generation as fixtures


@unittest.skipUnless(fixtures.DSN and fixtures.psycopg2,"Requires disposable PostgreSQL")
class ReviewTests(unittest.TestCase):
    sql=fixtures.GenerationDatabaseTests.sql
    ingest=fixtures.GenerationDatabaseTests.ingest

    def setUp(self):
        fixtures.GenerationDatabaseTests.setUp(self)
        job=self.repo.claim()
        self.draft=self.repo.save(job,'Thank you. <script>alert("untrusted")</script>')
        self.reviews=ReviewRepository(fixtures.DSN)
        self.client=TestClient(create_app(self.reviews,'Local Reviewer'),base_url='http://127.0.0.1:8765')
        self.addCleanup(self.client.close)

    def state(self):
        return self.sql('SELECT state FROM outreach_pilot.drafts WHERE id=%s',(self.draft,))[0][0]

    def events(self):
        return self.sql('SELECT decision,reviewer,reason FROM outreach_pilot.review_events WHERE draft_id=%s',(self.draft,))

    def decide(self,decision='approved',reason=''):
        return self.reviews.decide(self.draft,decision,'Local Reviewer',reason)

    def test_competing_approvals_are_idempotent(self):
        barrier=Barrier(8)
        def act(_):
            barrier.wait(timeout=10)
            return self.decide()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(act,range(8)))
        self.assertEqual(results.count('approved'),1)
        self.assertEqual(results.count('already_approved'),7)
        self.assertEqual(self.events(),[('approved','Local Reviewer','')])

    def test_approve_reject_race_has_one_winner(self):
        barrier=Barrier(2)
        def act(decision):
            barrier.wait(timeout=10)
            return self.decide(decision)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(act,['approved','rejected']))
        self.assertEqual(results.count('conflict'),1)
        self.assertEqual(len(self.events()),1)
        self.assertEqual(self.events()[0][0],self.state())

    def test_new_reply_racing_approval_always_supersedes(self):
        barrier=Barrier(2)
        def approve():
            barrier.wait(timeout=10)
            return self.decide()
        def inbound():
            barrier.wait(timeout=10)
            return self.ingest('racing-reply')
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(approve); b=pool.submit(inbound)
            self.assertIn(a.result(),('approved','superseded'))
            self.assertEqual(b.result()[0],'ingested')
        self.assertEqual(self.state(),'superseded')
        self.assertLessEqual(len(self.events()),1)

    def test_explicit_version_check_catches_stale_draft(self):
        self.sql('UPDATE outreach_pilot.conversations SET version_counter=version_counter+1 WHERE id=%s',(self.conversation,),False)
        self.assertEqual(self.decide(),'superseded')
        self.assertEqual(self.state(),'superseded')
        self.assertEqual(self.events()[0][0],'superseded')

    def test_rejection_is_terminal_and_keeps_original_reason(self):
        self.assertEqual(self.decide('rejected','Wrong tone'),'rejected')
        self.assertEqual(self.decide('rejected','Changed reason'),'already_rejected')
        self.assertEqual(self.decide(),'conflict')
        self.ingest('after-rejection')
        self.assertEqual(self.state(),'rejected')
        self.assertEqual(self.events(),[('rejected','Local Reviewer','Wrong tone')])

    def test_audit_failure_rolls_back_decision(self):
        self.sql("""CREATE FUNCTION outreach_pilot.test_fail_review_event() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'test audit failure'; END; $$;
            CREATE TRIGGER test_fail_review_event BEFORE INSERT ON outreach_pilot.review_events
            FOR EACH ROW EXECUTE FUNCTION outreach_pilot.test_fail_review_event();""",fetch=False)
        try:
            with self.assertRaises(fixtures.psycopg2.errors.RaiseException):
                self.decide()
            self.assertEqual(self.state(),'pending_review')
            self.assertEqual(self.events(),[])
        finally:
            self.sql('DROP TRIGGER test_fail_review_event ON outreach_pilot.review_events; DROP FUNCTION outreach_pilot.test_fail_review_event();',fetch=False)

    def test_review_and_draft_content_are_immutable(self):
        self.decide()
        for statement in ('UPDATE outreach_pilot.review_events SET reviewer=\'other\' WHERE draft_id=%s',
                          'DELETE FROM outreach_pilot.review_events WHERE draft_id=%s',
                          'UPDATE outreach_pilot.drafts SET body=\'changed\' WHERE id=%s'):
            with self.assertRaises(fixtures.psycopg2.errors.RaiseException):
                self.sql(statement,(self.draft,),False)

    def form(self):
        page=self.client.get(f'/reviews/{self.draft}')
        token=re.search(r'name="csrf" value="([^"]+)"',page.text).group(1)
        return {'csrf':token}

    def test_ui_escapes_content_and_displays_pinned_facts(self):
        page=self.client.get(f'/reviews/{self.draft}')
        self.assertEqual(page.status_code,200)
        self.assertNotIn('<script>',page.text)
        self.assertIn('&lt;script&gt;',page.text)
        self.assertIn('Example Seller',page.text)
        self.assertIn('Untrusted research',page.text)
        self.assertIn('frame-ancestors',page.headers['content-security-policy'])
        self.assertEqual(page.headers['cache-control'],'no-store')

    def test_valid_form_approves_as_server_reviewer(self):
        result=self.client.post(f'/reviews/{self.draft}/approve',data=self.form(),headers={'origin':'http://127.0.0.1:8765'})
        self.assertEqual(result.status_code,200)
        self.assertIn('Recorded decision',result.text)
        self.assertEqual(self.events(),[('approved','Local Reviewer','')])
        self.assertNotIn('Approve draft</button>',result.text)

    def test_cross_origin_missing_token_and_spoofed_identity_are_blocked(self):
        for data,origin in ((self.form(),'https://evil.example'),({},'http://127.0.0.1:8765'),
                            ({**self.form(),'reviewer':'Someone Else'},'http://127.0.0.1:8765')):
            response=self.client.post(f'/reviews/{self.draft}/approve',data=data,headers={'origin':origin,'content-type':'application/x-www-form-urlencoded'})
            self.assertIn(response.status_code,(400,403))
        self.assertEqual(self.state(),'pending_review')
        self.assertEqual(self.events(),[])
        self.assertEqual(self.client.get('/reviews',headers={'host':'evil.example'}).status_code,400)

    def test_stale_form_returns_conflict(self):
        form=self.form()
        self.ingest('new-reply')
        response=self.client.post(f'/reviews/{self.draft}/approve',data=form,headers={'origin':'http://127.0.0.1:8765'})
        self.assertEqual(response.status_code,409)
        self.assertIn('new reply arrived',response.text)
        self.assertEqual(self.events(),[])

    def test_reject_form_and_unknown_draft(self):
        response=self.client.post(f'/reviews/{self.draft}/reject',data={**self.form(),'reason':'Unsupported claim'},headers={'origin':'http://127.0.0.1:8765'})
        self.assertEqual(response.status_code,200)
        self.assertIn('Unsupported claim',response.text)
        self.assertEqual(self.state(),'rejected')
        self.assertEqual(self.client.get('/reviews/'+str(uuid4())).status_code,404)

    def test_oversized_form_has_no_side_effect(self):
        response=self.client.post(f'/reviews/{self.draft}/approve',content='x'*17000,
             headers={'origin':'http://127.0.0.1:8765','content-type':'application/x-www-form-urlencoded'})
        self.assertEqual(response.status_code,413)
        self.assertEqual(self.state(),'pending_review')

    def test_queue_context_search_and_filters(self):
        self.sql("INSERT INTO outreach_pilot.review_contacts(conversation_id,display_name,company_name,source) VALUES(%s,%s,%s,'test')",(self.conversation,'Alice <script>','Example Ltd'),False)
        result=self.reviews.queue(q='Example Ltd')
        self.assertTrue(any(str(d['id'])==str(self.draft) for d in result['drafts']))
        row=next(d for d in result['drafts'] if str(d['id'])==str(self.draft))
        self.assertEqual(row['kind'],'reply')
        self.assertGreater(row['message_count'],0)
        self.assertEqual(self.reviews.queue(q='Example Ltd',kind='initial')['total'],0)
        self.assertEqual(self.reviews.queue(q='Example Ltd',mailbox='absent@example.test')['total'],0)
        page=self.client.get('/reviews?q=Example+Ltd&sort=company')
        self.assertEqual(page.status_code,200)
        self.assertIn('Alice &lt;script&gt;',page.text)
        self.assertNotIn('Alice <script>',page.text)
        self.assertIn('Example Ltd',page.text)
        self.assertEqual(self.client.get('/reviews?sort=invalid').status_code,400)
        self.assertEqual(self.client.get('/reviews?kind=invalid').status_code,400)
        self.assertEqual(self.reviews.queue(q='no-such-prospect-928736')['total'],0)
        self.assertEqual(self.state(),'pending_review')

    def test_company_filter_and_date_order(self):
        self.sql("INSERT INTO outreach_pilot.review_contacts(conversation_id,display_name,company_name,source) VALUES(%s,'Person','Filter Co','test')",(self.conversation,),False)
        rows=self.reviews.queue(company='name:Filter Co')['drafts']
        self.assertIn(str(self.draft),[str(d['id']) for d in rows])
        self.assertNotIn(str(self.draft),[str(d['id']) for d in self.reviews.queue(company='missing:')['drafts']])
        self.assertEqual(self.reviews.queue(company='name:Missing Co 937')['total'],0)
        for sort,reverse in [('oldest',False),('newest',True)]:
            dates=[d['activity_at'] for d in self.reviews.queue(sort=sort)['drafts'] if d['activity_at']]
            self.assertEqual(dates,sorted(dates,reverse=reverse))
        page=self.client.get('/reviews?company=name%3AFilter+Co&sort=newest')
        self.assertEqual(page.status_code,200)
        self.assertIn('value="name:Filter Co" selected',page.text)
        self.assertIn('Date · newest first',page.text)

    def bulk_preview(self):
        return self.client.post('/reviews/bulk-preview',data={'csrf':self.form()['csrf'],'draft_id':str(self.draft)},headers={'origin':'http://127.0.0.1:8765'})

    def test_bulk_confirmation_approval_and_repeat(self):
        preview=self.bulk_preview()
        self.assertEqual(preview.status_code,200)
        self.assertEqual(self.state(),'pending_review')
        token=re.search(r'name="selection" value="([^"]+)"',preview.text).group(1)
        data={'csrf':self.form()['csrf'],'selection':token}
        for _ in range(2):
            response=self.client.post('/reviews/bulk-approve',data=data,headers={'origin':'http://127.0.0.1:8765'})
            self.assertEqual(response.status_code,200)
        self.assertEqual(self.state(),'approved')
        self.assertEqual(len(self.events()),1)

    def test_bulk_stale_and_tampered_confirmation(self):
        preview=self.bulk_preview()
        token=re.search(r'name="selection" value="([^"]+)"',preview.text).group(1)
        csrf=self.form()['csrf']
        bad=self.client.post('/reviews/bulk-approve',data={'csrf':csrf,'selection':token+'bad'},headers={'origin':'http://127.0.0.1:8765'})
        self.assertEqual(bad.status_code,400)
        self.assertEqual(self.state(),'pending_review')
        self.ingest('bulk-new-reply')
        result=self.client.post('/reviews/bulk-approve',data={'csrf':csrf,'selection':token},headers={'origin':'http://127.0.0.1:8765'})
        self.assertEqual(result.status_code,200)
        self.assertNotEqual(self.state(),'approved')
        self.assertIn('Skipped:',result.text)

    def test_bulk_requires_selection_and_csrf(self):
        for data,status in [({'csrf':self.form()['csrf']},400),({'draft_id':str(self.draft),'csrf':'wrong'},403)]:
            response=self.client.post('/reviews/bulk-preview',data=data,headers={'origin':'http://127.0.0.1:8765'})
            self.assertEqual(response.status_code,status)
        self.assertEqual(self.state(),'pending_review')

    def test_bulk_multiple_drafts_partial_result(self):
        second=self.sql("INSERT INTO outreach_pilot.drafts(conversation_id,triggering_message_id,version_snapshot,body) SELECT conversation_id,NULL,version_snapshot,'Second full draft' FROM outreach_pilot.drafts WHERE id=%s RETURNING id",(self.draft,))[0][0]
        csrf=self.form()['csrf']
        preview=self.client.post('/reviews/bulk-preview',data={'csrf':csrf,'draft_id':[str(self.draft),str(second)]},headers={'origin':'http://127.0.0.1:8765'})
        self.assertEqual(preview.status_code,200)
        tokens=re.findall(r'name="selection" value="([^"]+)"',preview.text)
        self.assertEqual(len(tokens),2)
        self.reviews.decide(second,'rejected','Local Reviewer')
        result=self.client.post('/reviews/bulk-approve',data={'csrf':csrf,'selection':tokens},headers={'origin':'http://127.0.0.1:8765'})
        self.assertEqual(result.status_code,200)
        self.assertEqual(self.state(),'approved')
        self.assertIn('Skipped:',result.text)
        self.assertEqual(self.sql('SELECT state FROM outreach_pilot.drafts WHERE id=%s',(second,))[0][0],'rejected')
