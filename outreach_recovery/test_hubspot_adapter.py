import json
import unittest
import httpx
from outreach_recovery.hubspot_adapter import HubSpotAdapter
from outreach_recovery.delivery_contracts import MutationState, ReconciliationState

P = dict(hubspot_portal_id="123",hubspot_contact_id="456",direction="outbound",
         **{"from":"sdr@example.test","to":"rui@example.test"},subject="Test",body="Original",timestamp="2026-09-24T19:00:00+00:00")

class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.creates=[]; self.code=201; self.existing=[]; self.portal=123; self.drop=False; self.association="456"
        self.adapter=HubSpotAdapter(portal_id="123",token_provider=lambda:"private",transport=httpx.MockTransport(self.handle))
    def handle(self, request):
        path=request.url.path
        if path=="/account-info/v3/details": return httpx.Response(200,json={"portalId":self.portal})
        if path=="/crm/v3/objects/contacts/456": return httpx.Response(200,json={"properties":{"email":"rui@example.test"}})
        if path.endswith("/search"): return httpx.Response(200,json={"results":self.existing,"total":len(self.existing)})
        if path=="/crm/v3/objects/emails" and request.method=="POST":
            self.creates.append(json.loads(request.content))
            if self.drop: raise httpx.ReadTimeout("secret should not escape")
            return httpx.Response(self.code,json={"id":"789"})
        return httpx.Response(200,json={"associations":{"contacts":{"results":[{"id":self.association}]}}})
    def create(self,p=None): return self.adapter.create_email(p or P,operation_id="op",timeout_seconds=10)
    def hit(self):
        return {"id":"789","properties":{"hs_email_text":"Original","hs_email_from_email":"sdr@example.test","hs_email_to_email":"rui@example.test"}}
    def test_create_association_and_original_content(self):
        self.assertEqual(self.create().provider_id,"789")
        self.assertEqual(len(self.creates),1)
        self.assertEqual(self.creates[0]["associations"][0]["types"][0]["associationTypeId"],198)
        self.assertEqual(self.creates[0]["properties"]["hs_email_text"],"Original")
    def test_inbound_direction_has_no_outbound_status(self):
        p=dict(P,direction="inbound"); p["from"],p["to"]=p["to"],p["from"]
        self.assertEqual(self.create(p).state,MutationState.ACCEPTED)
        self.assertEqual(self.creates[0]["properties"]["hs_email_direction"],"INCOMING_EMAIL")
        self.assertNotIn("hs_email_status",self.creates[0]["properties"])
    def test_wrong_portal_never_writes(self):
        self.portal=999
        self.assertEqual(self.create().state,MutationState.REJECTED); self.assertFalse(self.creates)
    def test_timeout_no_retry(self):
        self.drop=True
        self.assertEqual(self.create().state,MutationState.UNCERTAIN); self.assertEqual(len(self.creates),1)
    def test_server_error_is_uncertain(self):
        self.code=503
        self.assertEqual(self.create().state,MutationState.UNCERTAIN); self.assertEqual(len(self.creates),1)
    def test_rate_limit_definitive_retry(self):
        self.code=429
        self.assertTrue(self.create().safe_to_retry)
    def test_existing_exact_activity_no_create(self):
        self.existing=[self.hit()]
        self.assertEqual(self.create().provider_id,"789"); self.assertFalse(self.creates)
    def test_ambiguous_matches_do_not_create(self):
        self.existing=[self.hit(),self.hit()]
        self.assertEqual(self.create().state,MutationState.REJECTED); self.assertFalse(self.creates)
    def test_absence_reconciliation_never_creates(self):
        result=self.adapter.reconcile(P,operation_id="op",timeout_seconds=10)
        self.assertEqual(result.state,ReconciliationState.NOT_FOUND_YET); self.assertFalse(self.creates)
    def test_wrong_contact_not_a_verified_match(self):
        self.existing=[self.hit()]; self.association="999"
        self.assertEqual(self.adapter.reconcile(P,operation_id="op",timeout_seconds=10).state,ReconciliationState.NOT_FOUND_YET)

    def test_missing_provider_id_is_uncertain(self):
        original=self.handle
        def handler(request):
            if request.url.path=="/crm/v3/objects/emails":return httpx.Response(201,json={})
            return original(request)
        self.adapter.transport=httpx.MockTransport(handler)
        self.assertEqual(self.create().state,MutationState.UNCERTAIN)
    def test_existing_delivery_envelope_date(self):
        import base64
        p=dict(P);p.pop("timestamp")
        p["raw_mime"]=base64.urlsafe_b64encode(b"Date: Thu, 24 Sep 2026 19:00:00 +0000\r\n\r\nOriginal").decode()
        self.assertEqual(self.create(p).state,MutationState.ACCEPTED)
        self.assertEqual(self.creates[0]["properties"]["hs_timestamp"],"1790276400000")
