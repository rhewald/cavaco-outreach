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
