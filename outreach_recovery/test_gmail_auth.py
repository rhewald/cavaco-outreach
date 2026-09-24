import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import httpx
from outreach_recovery.gmail_auth import (AuthenticationError, SCOPES, TOKEN_URI,
    OAuthTokenProvider, connect, private_path, save_credentials, verify_mailbox)

class GmailAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.token=self.root/'token.json'
        self.creds=Mock()
        self.creds.token='offline-access'; self.creds.refresh_token='offline-refresh'
        self.creds.granted_scopes=SCOPES; self.creds.scopes=SCOPES
        self.creds.to_json.return_value=json.dumps(dict(token='offline-access',refresh_token='offline-refresh',
            client_id='offline-client',client_secret='offline-secret',token_uri=TOKEN_URI,scopes=SCOPES))
        self.client=self.root/'client.json'
        self.client.write_text(json.dumps({'installed':{'client_id':'offline-client','client_secret':'offline-secret',
            'auth_uri':'https://accounts.google.com/o/oauth2/auth','token_uri':TOKEN_URI}}))
    def test_private_atomic_storage(self):
        save_credentials(self.token,self.creds)
        self.assertEqual(self.token.stat().st_mode&0o777,0o600)
        self.assertEqual(json.loads(self.token.read_text())['token'],'offline-access')
    def test_shared_or_symlink_token_rejected(self):
        self.token.write_text('{}'); self.token.chmod(0o644)
        with self.assertRaises(AuthenticationError): private_path(self.token)
        link=self.root/'link.json'; link.symlink_to(self.token)
        with self.assertRaises(AuthenticationError): private_path(link)
    def test_repository_storage_rejected(self):
        (self.root/'.git').mkdir()
        with self.assertRaises(AuthenticationError): save_credentials(self.token,self.creds)
    def test_verify_is_read_only_and_checks_exact_mailbox(self):
        calls=[]
        def handle(request):
            calls.append(request)
            return httpx.Response(200,json={'emailAddress':'sdr@cavaco.ai'})
        transport=httpx.MockTransport(handle)
        self.assertEqual(verify_mailbox('offline','sdr@cavaco.ai',transport=transport),'sdr@cavaco.ai')
        with self.assertRaises(AuthenticationError): verify_mailbox('offline','other@cavaco.ai',transport=transport)
        self.assertTrue(all(r.method=='GET' and r.url.path.endswith('/profile') for r in calls))
    def test_connect_checks_pkce_scopes_and_identity_before_save(self):
        with patch('google_auth_oauthlib.flow.InstalledAppFlow.from_client_config') as factory, patch('outreach_recovery.gmail_auth.verify_mailbox',return_value='sdr@cavaco.ai') as verify:
            factory.return_value.run_local_server.return_value=self.creds
            self.assertEqual(connect(self.client,self.token,'sdr@cavaco.ai'),'sdr@cavaco.ai')
            self.assertTrue(factory.call_args.kwargs['autogenerate_code_verifier'])
            args=factory.return_value.run_local_server.call_args.kwargs
            self.assertEqual(args['host'],'127.0.0.1'); self.assertEqual(args['port'],0)
            verify.assert_called_once_with('offline-access','sdr@cavaco.ai')
        self.assertTrue(self.token.exists())
    def test_wrong_account_or_partial_consent_never_saved(self):
        with patch('google_auth_oauthlib.flow.InstalledAppFlow.from_client_config') as factory, patch('outreach_recovery.gmail_auth.verify_mailbox',side_effect=AuthenticationError('Mismatch')):
            factory.return_value.run_local_server.return_value=self.creds
            with self.assertRaises(AuthenticationError): connect(self.client,self.token,'sdr@cavaco.ai')
            self.assertFalse(self.token.exists())
            self.creds.granted_scopes=[SCOPES[0]]
            with self.assertRaises(AuthenticationError): connect(self.client,self.token,'sdr@cavaco.ai')
            self.assertFalse(self.token.exists())
    def test_untrusted_auth_endpoint_rejected(self):
        self.client.write_text(json.dumps({'installed':{'auth_uri':'https://evil.example','token_uri':TOKEN_URI}}))
        with self.assertRaises(AuthenticationError): connect(self.client,self.token,'sdr@cavaco.ai')
        self.assertFalse(self.token.exists())
    def test_refresh_and_cached_token_boundary(self):
        save_credentials(self.token,self.creds)
        with patch('google.oauth2.credentials.Credentials.from_authorized_user_info',return_value=self.creds):
            self.creds.valid=True
            provider=OAuthTokenProvider(self.token)
            self.assertEqual(provider(timeout_seconds=5),'offline-access')
            self.creds.refresh.assert_not_called()
            self.creds.valid=False
            self.assertEqual(provider(timeout_seconds=5),'offline-access')
            self.creds.refresh.assert_called_once()
    def test_errors_do_not_expose_token_data(self):
        save_credentials(self.token,self.creds)
        with patch('google.oauth2.credentials.Credentials.from_authorized_user_info',side_effect=ValueError('SECRET-CONTENT')):
            with self.assertRaises(AuthenticationError) as result: OAuthTokenProvider(self.token)(timeout_seconds=5)
            self.assertNotIn('SECRET-CONTENT',str(result.exception))
