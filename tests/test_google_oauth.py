"""No real grants/keys: all browser, storage and revoke effects use injected fakes."""
import copy
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from finance_mcp.contract import ContractError
from finance_mcp.google_oauth import (app_config, login, disconnect, stored_credentials,
    READ_SCOPE, TOKEN_URI, AUTH_URI, TokenStore, profile_name)

APP={'installed':dict(client_id='123-example.apps.googleusercontent.com',
                      client_secret='public-desktop-app-value',auth_uri=AUTH_URI,token_uri=TOKEN_URI)}
TOKEN=dict(client_id=APP['installed']['client_id'],client_secret=APP['installed']['client_secret'],
           token_uri=TOKEN_URI,refresh_token='synthetic-refresh',token='synthetic-access',scopes=[READ_SCOPE])
class Store:
    def __init__(self,raw=None):self.raw=raw
    def read(self):return self.raw
    def write(self,v):self.raw=v
    def delete(self):self.raw=None
class Credentials:
    scopes=[READ_SCOPE];granted_scopes=[READ_SCOPE];refresh_token='synthetic-refresh'
    def to_json(self):return json.dumps(TOKEN)
class Flow:
    credentials=Credentials()
    options={}; local={}
    @classmethod
    def from_client_config(cls,app,**kwargs):
        cls.options=kwargs;return cls()
    def run_local_server(self,**kwargs):
        type(self).local=kwargs;return self.credentials

@unittest.skipUnless(importlib.util.find_spec("google_auth_oauthlib"), "Install extra sheets for OAuth tests")
class OAuthTests(unittest.TestCase):
    def test_app_uses_only_registered_google_endpoints(self):
        with TemporaryDirectory() as tmp:
            p=Path(tmp)/'app.json';p.write_text(json.dumps(APP))
            self.assertEqual(app_config(p),APP)
            bad=copy.deepcopy(APP);bad['installed']['token_uri']='https://evil.test'
            p.write_text(json.dumps(bad))
            with self.assertRaises(ContractError):app_config(p)
    def test_login_pkce_loopback_offline_and_secure_save(self):
        store=Store()
        with patch('finance_mcp.google_oauth.app_config',return_value=APP),patch('builtins.print'):
            result=login(store=store,flow_class=Flow)
        self.assertEqual(result['status'],'connected')
        self.assertTrue(Flow.options['autogenerate_code_verifier'])
        self.assertEqual(Flow.options['scopes'],[READ_SCOPE])
        self.assertEqual(Flow.local['host'],'127.0.0.1')
        self.assertEqual(Flow.local['port'],0)
        self.assertEqual(Flow.local['timeout_seconds'],180)
        self.assertEqual(Flow.local['access_type'],'offline')
        self.assertEqual(json.loads(store.raw)['scopes'],[READ_SCOPE])
    def test_denied_login_does_not_replace_saved_grant_or_leak(self):
        class Denied(Flow):
            def run_local_server(self,**kwargs):raise RuntimeError('private-code-and-token')
        store=Store('old')
        with patch('finance_mcp.google_oauth.app_config',return_value=APP),patch('builtins.print'):
            with self.assertRaises(ContractError) as c:login(store=store,flow_class=Denied)
        self.assertNotIn('private-code',str(c.exception));self.assertEqual(store.raw,'old')
    def test_expanded_scope_and_no_refresh_rejected(self):
        class BadCredentials(Credentials):granted_scopes=[READ_SCOPE,'https://www.googleapis.com/auth/drive']
        class Bad(Flow):credentials=BadCredentials()
        store=Store('old')
        with patch('finance_mcp.google_oauth.app_config',return_value=APP),patch('builtins.print'):
            with self.assertRaises(ContractError):login(store=store,flow_class=Bad)
        self.assertEqual(store.raw,'old')
    def test_saved_profile_cannot_redirect_tokens_or_expand_access(self):
        for field,value in [('client_id','foreign-client'),('token_uri','https://evil.test'),
                            ('scopes',['https://www.googleapis.com/auth/drive'])]:
            obj=dict(TOKEN);obj[field]=value
            with self.assertRaises(ContractError):stored_credentials(store=Store(json.dumps(obj)),app=APP)
        credentials=stored_credentials(store=Store(json.dumps(TOKEN)),app=APP)
        self.assertEqual(credentials.refresh_token,TOKEN['refresh_token'])
    def test_disconnect_revokes_then_deletes_local_grant(self):
        store=Store(json.dumps(TOKEN))
        class HTTP:
            def post(self,url,**kwargs):
                self.url=url;self.kwargs=kwargs;return SimpleNamespace(status_code=200)
        http=HTTP();result=disconnect(store=store,app=APP,session=http)
        self.assertEqual(result['status'],'disconnected');self.assertIsNone(store.raw)
        self.assertEqual(http.url,'https://oauth2.googleapis.com/revoke')
        self.assertEqual(http.kwargs['data']['token'],TOKEN['refresh_token'])
        self.assertFalse(http.kwargs['allow_redirects'])
    def test_failed_revoke_does_not_claim_disconnect(self):
        store=Store(json.dumps(TOKEN))
        class HTTP:
            def post(self,*a,**kw):return SimpleNamespace(status_code=503)
        with self.assertRaises(ContractError):disconnect(store=store,app=APP,session=HTTP())
        self.assertIsNotNone(store.raw)
    def test_real_google_library_generates_pkce_and_rejects_wrong_state(self):
        from urllib.parse import urlsplit, parse_qs
        from google_auth_oauthlib.flow import InstalledAppFlow
        from oauthlib.oauth2 import MismatchingStateError
        flow=InstalledAppFlow.from_client_config(APP,scopes=[READ_SCOPE],autogenerate_code_verifier=True)
        flow.redirect_uri='http://127.0.0.1:12345/'
        url,state=flow.authorization_url(access_type='offline',prompt='consent')
        query=parse_qs(urlsplit(url).query)
        self.assertEqual(urlsplit(url).netloc,'accounts.google.com')
        self.assertEqual(query['code_challenge_method'],['S256'])
        self.assertTrue(query['code_challenge'][0])
        with patch.object(flow.oauth2session,'post',side_effect=AssertionError('no network')):
            with self.assertRaises(MismatchingStateError):
                flow.fetch_token(authorization_response='https://localhost/?state=wrong&code=fake')
    def test_plaintext_store_rejected_and_no_secrets_in_errors(self):
        class Plaintext: pass
        try:import keyring
        except ImportError:self.skipTest('sheets extra required')
        with patch('keyring.get_keyring',return_value=Plaintext()):
            with self.assertRaises(ContractError):TokenStore('default')
        with self.assertRaises(ContractError):profile_name('../other')

if __name__=='__main__':unittest.main()
