import unittest
import httpx
from outreach_recovery.hubspot_auth import verify, REQUIRED

class HubSpotAuthTests(unittest.TestCase):
    def check(self, portal=47521149, scopes=None, status=200):
        calls=[]
        def handle(request):
            calls.append(request)
            if request.url.path.endswith('access-token-info'):
                return httpx.Response(200,json={'hubId':portal,'appId':36316861,'scopes':list(REQUIRED if scopes is None else scopes)})
            self.assertEqual(request.method,'GET')
            self.assertEqual(request.headers['Authorization'],'Bearer test-token')
            return httpx.Response(status,json={'results':[]})
        return verify('test-token',47521149,httpx.MockTransport(handle))
    def test_expected_account_and_reads(self):
        self.assertTrue(self.check()['email_read_verified'])
    def test_wrong_account_rejected(self):
        with self.assertRaises(ValueError): self.check(portal=42)
    def test_missing_scope_rejected(self):
        with self.assertRaises(ValueError): self.check(scopes=['crm.objects.contacts.read'])
    def test_read_failure_rejected(self):
        with self.assertRaises(ValueError): self.check(status=403)

    def test_introspection_404_falls_back_to_verified_account(self):
        paths=[]
        def handler(request):
            paths.append(request.url.path)
            if request.url.path.endswith('access-token-info'):
                return httpx.Response(404)
            if request.url.path.endswith('/details'):
                return httpx.Response(200,json={'portalId':47521149})
            return httpx.Response(200,json={'results':[]})
        result=verify('test-token',47521149,httpx.MockTransport(handler))
        self.assertFalse(result['required_scopes_verified'])
        self.assertFalse(result['write_access_tested'])
        self.assertTrue(result['contact_read_verified'])
        self.assertIn('/account-info/v3/details',paths)

    def test_fallback_never_accepts_wrong_portal_or_invalid_token(self):
        for status,portal in [(200,42),(401,47521149),(403,47521149)]:
            def handler(request):
                if request.url.path.endswith('access-token-info'):
                    return httpx.Response(404)
                return httpx.Response(status,json={'portalId':portal})
            with self.assertRaises(ValueError):
                verify('test-token',47521149,httpx.MockTransport(handler))
