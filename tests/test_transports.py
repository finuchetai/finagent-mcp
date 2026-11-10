"""HTTP security boundary, OAuth JWT failures and the same financial tools; synthetic keys/data only."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient
from finance_mcp.contract import ContractError
from finance_mcp.demo import DemoAdapter
from finance_mcp.remote import OAuthJWTVerifier, create_http_server, create_http_app
from finance_mcp.plugin_package import build_plugin, public_source_files

ENV = {"FINANCE_MCP_PUBLIC_URL": "https://finance.example.test/mcp",
       "FINANCE_MCP_OAUTH_ISSUER": "https://issuer.example.test/",
       "FINANCE_MCP_JWKS_URL": "https://issuer.example.test/jwks",
       "FINANCE_MCP_ALLOWED_SUBJECTS": '["owner-1"]'}


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def token(self, **changes):
        claims = dict(iss=ENV['FINANCE_MCP_OAUTH_ISSUER'], aud=ENV['FINANCE_MCP_PUBLIC_URL'],
                      sub='owner-1', iat=int(time.time())-1, exp=int(time.time())+300,
                      scope='finance:read', client_id='test-client')
        claims.update(changes)
        return jwt.encode(claims, self.key, algorithm='RS256', headers={'kid': 'test-only'})

    def test_closed_configuration_and_https(self):
        for missing in ENV:
            env = dict(ENV);env.pop(missing)
            with self.assertRaises(ContractError):create_http_server(DemoAdapter(), environ=env)
        for field, value in [('FINANCE_MCP_PUBLIC_URL','http://finance.example.test/mcp'),
                ('FINANCE_MCP_PUBLIC_URL','https://finance.example.test/mcp?key=secret'),
                ('FINANCE_MCP_JWKS_URL','https://user:secret@issuer.example.test/jwks'),
                ('FINANCE_MCP_ALLOWED_SUBJECTS','[]'),('FINANCE_MCP_ALLOWED_SUBJECTS','"owner-1"')]:
            env=dict(ENV);env[field]=value
            with self.assertRaises(ContractError):create_http_server(DemoAdapter(), environ=env)

    def test_real_signature_and_all_claim_boundaries(self):
        v=OAuthJWTVerifier(ENV['FINANCE_MCP_OAUTH_ISSUER'],ENV['FINANCE_MCP_PUBLIC_URL'],
                           ENV['FINANCE_MCP_JWKS_URL'],('owner-1',))
        with patch.object(v.keys,'get_signing_key_from_jwt',return_value=SimpleNamespace(key=self.key.public_key())):
            good=asyncio.run(v.verify_token(self.token()));self.assertEqual(good.subject,'owner-1')
            for changes in [dict(iss='https://other.example.test/'),dict(aud='https://other.example.test/mcp'),
                            dict(sub='other-client'),dict(scope='other:read'),dict(exp=int(time.time())-100),
                            dict(client_id=''),dict(iat=int(time.time())+100)]:
                self.assertIsNone(asyncio.run(v.verify_token(self.token(**changes))),changes)
            bad=jwt.encode(dict(iss=ENV['FINANCE_MCP_OAUTH_ISSUER'],aud=ENV['FINANCE_MCP_PUBLIC_URL'],
                       sub='owner-1',iat=int(time.time())-1,exp=int(time.time())+300,
                       scope='finance:read',client_id='test-client'), self.other, algorithm='RS256')
            self.assertIsNone(asyncio.run(v.verify_token(bad)))
            self.assertIsNone(asyncio.run(v.verify_token('not-a-token')))
            self.assertIsNone(asyncio.run(v.verify_token('a'*20000)))

    def test_http_unauthorized_metadata_and_financial_tools(self):
        # The provider's network key lookup alone is mocked; signatures and HTTP auth are real.
        with patch('jwt.PyJWKClient.get_signing_key_from_jwt',return_value=SimpleNamespace(key=self.key.public_key())):
            app=create_http_app(DemoAdapter(),environ=ENV)
            with TestClient(app,base_url='https://finance.example.test') as client:
                r=client.post('/mcp',json={});self.assertEqual(r.status_code,401)
                self.assertIn('resource_metadata',r.headers['www-authenticate'])
                self.assertNotIn('Маяк',r.text)
                meta=client.get('/.well-known/oauth-protected-resource/mcp').json()
                self.assertEqual(meta['resource'],ENV['FINANCE_MCP_PUBLIC_URL'])
                self.assertEqual(meta['authorization_servers'],[ENV['FINANCE_MCP_OAUTH_ISSUER']])
                headers={'Authorization':'Bearer '+self.token(), 'Accept':'application/json, text/event-stream',
                         'MCP-Protocol-Version':'2025-11-25'}
                def call(method,params=None):
                    body={'jsonrpc':'2.0','id':1,'method':method}
                    if params is not None:body['params']=params
                    res=client.post('/mcp',headers=headers,json=body)
                    self.assertEqual(res.status_code,200,res.text)
                    return res.json()
                init=call('initialize',{'protocolVersion':'2025-11-25','capabilities':{},
                         'clientInfo':{'name':'acceptance','version':'1'}})
                self.assertIn('Маяк',init['result']['instructions'])
                tools=call('tools/list')['result']['tools'];self.assertEqual(len(tools),6)
                for name,params in [('get_passport',{}),('list_months',{}),
                        ('get_pnl',{'months':['2026-09']}),('get_cashflow',{'months':['2026-09']}),
                        ('get_balances',{'months':['2026-09']}),('get_month_notes',{'month':'2026-09'})]:
                    result=call('tools/call',{'name':name,'arguments':params})['result']
                    self.assertFalse(result.get('isError',False),result)
                    if name=='get_pnl':self.assertIn('75000.00',result['content'][0]['text'])
                result=call('tools/call',{'name':'get_pnl','arguments':{'months':['2026-10']}})['result']
                self.assertTrue(result['isError'])
                invalid=client.post('/mcp',headers={**headers,'Authorization':'Bearer '+self.token(sub='other-client')},json={})
                self.assertEqual(invalid.status_code,401)
                wrong_host=client.post('/mcp',headers={**headers,'host':'evil.example'},json={})
                self.assertEqual(wrong_host.status_code,421)
                wrong_origin=client.post('/mcp',headers={**headers,'origin':'https://evil.example'},json={})
                self.assertEqual(wrong_origin.status_code,403)


class PluginTests(unittest.TestCase):
    def test_local_package_and_actual_launch(self):
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters,stdio_client
        with tempfile.TemporaryDirectory() as d:
            out=build_plugin(Path(d)/'finagent-finance-local',mode='local',python=sys.executable)
            manifest=json.loads((out/'plugin.json').read_text())
            self.assertLessEqual(len(manifest['extensions']['com.openai']['interface']['shortDescription']),30)
            cfg=json.loads((out/'mcp.json').read_text())['mcpServers']['finance']
            self.assertEqual(cfg['type'],'stdio')
            async def scenario():
                args=[a.replace('${PLUGIN_ROOT}',str(out)) for a in cfg['args']]
                async with stdio_client(StdioServerParameters(command=cfg['command'],args=args)) as (rd,wr):
                    async with ClientSession(rd,wr) as c:
                        await c.initialize();self.assertEqual(len((await c.list_tools()).tools),6)
                        ans=await c.call_tool('get_pnl',{'months':['2026-09']})
                        self.assertIn('75000.00',ans.content[0].text)
            asyncio.run(asyncio.wait_for(scenario(),timeout=20))
            self.assertFalse(any(f.suffix in {'.csv','.xlsx'} for f in out.rglob('*')))
            with self.assertRaises(ContractError):build_plugin(out,mode='local',python=sys.executable)

    def test_remote_manifest_has_oauth_and_no_credentials(self):
        with tempfile.TemporaryDirectory() as d:
            out=build_plugin(Path(d)/'finagent-finance-remote',mode='remote',
                             endpoint=ENV['FINANCE_MCP_PUBLIC_URL'],oauth_client_id='registered-test-client')
            s=json.loads((out/'mcp.json').read_text())['mcpServers']['finance']
            self.assertEqual(s['type'],'streamable-http')
            self.assertEqual(s['extensions']['com.openai']['auth']['type'],'oauth')
            self.assertNotIn('secret',(out/'mcp.json').read_text())
            self.assertNotIn('token',(out/'mcp.json').read_text().lower().replace('tokenendpointauthmethod',''))
            with self.assertRaises(ContractError):
                build_plugin(Path(d)/'bad',mode='remote',endpoint='http://host/mcp',oauth_client_id='client')
            self.assertFalse((Path(d)/'bad').exists())

    def test_packaged_guide_matches_source(self):
        from finance_mcp import plugin_package
        packaged=Path(plugin_package.__file__).parent/'resources/guided-setup.md'
        source=Path(__file__).resolve().parents[1]/'docs/guided-setup.md'
        self.assertEqual(packaged.read_bytes(),source.read_bytes())


class ArchiveSafetyTests(unittest.TestCase):
    def test_local_secrets_and_export_are_not_source(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            for name in ['src/finance_mcp/code.py','README.md','deploy/remote.env.example',
                         'deploy/remote.env','src/finance_mcp/.local.env','src/finance_mcp/.env',
                         'exports/client.csv','generated-plugins/secret.json','.venv/private.txt',
                         'credentials.json','src/finance_mcp/token.json',
                         'examples/client_credentials.json','docs/client_secret_local.json',
                         'src/finance_mcp/__pycache__/cached.pyc']:
                f=root/name;f.parent.mkdir(parents=True,exist_ok=True);f.write_text('synthetic sentinel')
            files={str(f.relative_to(root)) for f in public_source_files(root)}
            self.assertEqual(files,{'src/finance_mcp/code.py','README.md','deploy/remote.env.example'})
