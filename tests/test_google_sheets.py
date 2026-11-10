import copy
import asyncio
import os
from unittest.mock import patch
import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from finance_mcp.contract import ContractError, FinanceService
from finance_mcp.google_sheets_adapter import GoogleSheetsAdapter

ROOT = Path(__file__).resolve().parents[1] / 'examples/lipa'

class Response:
    status_code = 200
    def __init__(self, payload): self.payload = payload
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_content(self, size): yield json.dumps(self.payload).encode()

class Session:
    def __init__(self, payload): self.payload, self.calls = payload, []
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.payload, Exception): raise self.payload
        return Response(self.payload)

class GoogleSheetsTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT/'csv.json').read_text())
        self.config.update(adapter='google_sheets', spreadsheet_id='approved-test-id', credentials_env='SHEETS_KEY')
        blocks = []
        for name, spec in self.config['tables'].items():
            with (ROOT/spec.pop('file')).open() as f: rows = list(csv.reader(f, delimiter=spec.get("delimiter", ",")))
            spec.pop('delimiter', None)
            spec['range'] = f"'{name}'!A1:P1000"
            blocks.append({'values': rows})
        self.payload = {'valueRanges': blocks}
        self.session = Session(self.payload)
    def adapter(self): return GoogleSheetsAdapter(self.config, ROOT, session=self.session)
    def test_read_only_and_exact_decimal(self):
        adapter = self.adapter()
        self.assertEqual(adapter.report('pnl','2025-03').currency, 'RUB')
        self.assertEqual(len(self.session.calls), 2)
        url, kw = self.session.calls[0]
        self.assertIn('/approved-test-id/values:batchGet',url)
        self.assertEqual(kw['timeout'],30)
        self.assertFalse(kw['allow_redirects'])
        self.assertIn(('valueRenderOption','UNFORMATTED_VALUE'),kw['params'])
    def test_invalid_refresh_preserves_previous_snapshot_and_hides_transport_error(self):
        a=self.adapter(); old=a.report('pnl','2025-03'); passport=a.passport()
        self.session.payload=RuntimeError('secret token')
        with self.assertRaises(ContractError) as caught: a.refresh()
        self.assertNotIn('secret', str(caught.exception))
        self.assertEqual(a.report('pnl','2025-03'),old)
        self.assertEqual(a.passport(),passport)
    def test_inconsistent_double_read_rejected(self):
        calls=0
        original=self.session.get
        def changing(*args,**kw):
            nonlocal calls
            result=original(*args,**kw); calls+=1
            if calls==2:
                result.payload=copy.deepcopy(self.payload)
                result.payload['valueRanges'][0]['values'][1][0]='changed'
            return result
        self.session.get=changing
        with self.assertRaisesRegex(ContractError,'меняется'): self.adapter()
    def test_boundary_and_formula_errors_rejected(self):
        self.config['tables']['periods']['range']="'periods'!A1:P2"
        with self.assertRaisesRegex(ContractError,'границы'): self.adapter()
        self.setUp();self.payload['valueRanges'][0]['values'][1][0]='#REF!'
        with self.assertRaisesRegex(ContractError,'ошибка'): self.adapter()
    def test_refresh_revalidates_all_client_rows(self):
        a=self.adapter()
        self.payload['valueRanges'][2]['values'][1][0]='other-client'
        with self.assertRaises(ContractError): a.refresh()
        self.assertEqual(a.report('pnl','2025-03').currency,'RUB')
    def test_wide_read_and_layout_drift(self):
        with TemporaryDirectory() as temp:
            base=Path(temp)
            for name in ('periods','reports','notes'):
                (base/f'{name}.csv').write_bytes((ROOT/f'{name}.csv').read_bytes())
                self.config['tables'][name]=dict(self.config['tables'][name],file=f'{name}.csv'); self.config['tables'][name].pop('range')
            (base/'passport.md').write_bytes((ROOT/'passport.md').read_bytes())
            self.config['tables']['rows']={'range':"'Report'!A1:C100",'wide':{
                'kind':'pnl','header_row':0,'label_column':0,
                'months':{'2025-03':{'column':1,'header':'2025-03'}},
                'entries':[{'row':1,'label':'Revenue','code':'revenue','dimension':'all'},
                           {'row':2,'label':'Missing','code':'missing','dimension':'all'}]}}
            # Reports with no rows are accepted by snapshot validation only if contract permits;
            # keep one partial report for this fixture.
            with (base/'reports.csv').open() as f: reports=list(csv.reader(f))
            reports=[reports[0]]+[r for r in reports[1:] if r[1]=='2025-03' and r[2]=='pnl']
            reports[1][6]='false'
            with (base/'reports.csv').open('w') as f: csv.writer(f).writerows(reports)
            self.session.payload={'valueRanges':[{'values':[['Article','2025-03'],['Revenue',123.45],['Missing']]}]}
            a=GoogleSheetsAdapter(self.config,base,session=self.session)
            report=a.report('pnl','2025-03')
            self.assertEqual(str(report.rows[0].amount),'123.45')
            self.assertIsNone(report.rows[1].amount)
            self.session.payload['valueRanges'][0]['values'][1][0]='Renamed'
            with self.assertRaisesRegex(ContractError,'строка'): a.refresh()
            self.assertEqual(str(a.report('pnl','2025-03').rows[0].amount),'123.45')
    def test_server_exposes_refresh_and_calls_it(self):
        from finance_mcp.server import create_server
        server = create_server(self.adapter())
        async def check():
            tools = await server.list_tools()
            self.assertEqual(len(tools), 7)
            self.assertIn('refresh_source', [t.name for t in tools])
            result = await server.call_tool('refresh_source', {})
            self.assertIn('refreshed', str(result))
            result = await server.call_tool('get_pnl', {'months':['2025-03']})
            self.assertIn('RUB', str(result))
        asyncio.run(check())
    def test_report_tool_automatically_reads_updated_values(self):
        from finance_mcp.server import create_server
        server=create_server(self.adapter())
        # Find the numeric amount column using the source's own header.
        block=self.payload['valueRanges'][2]['values']
        headers=block[0]
        amount_index=headers.index(self.config['tables']['rows']['columns']['amount'])
        month_index=headers.index(self.config['tables']['rows']['columns']['month'])
        kind_index=headers.index(self.config['tables']['rows']['columns']['kind'])
        target=next(r for r in block[1:] if r[month_index]=='2025-03' and r[kind_index]=='pnl')
        target[amount_index]='9876543,21'
        async def check():
            result=await server.call_tool('get_pnl', {'months':['2025-03']})
            self.assertIn('9876543.21',str(result))
        asyncio.run(check())
    def test_failed_automatic_refresh_never_returns_stale_financial_answer(self):
        from finance_mcp.server import create_server
        server=create_server(self.adapter())
        self.session.payload=RuntimeError('offline')
        async def check():
            with self.assertRaises(Exception) as error:
                await server.call_tool('get_pnl', {'months':['2025-03']})
            self.assertIn('снимок не обновлён',str(error.exception))
        asyncio.run(check())
    def test_credentials_restricted_scope_and_safe_errors(self):
        from finance_mcp.google_sheets_adapter import authorized_session, READ_SCOPE
        try:
            import google.auth
        except ImportError:
            self.skipTest('sheets extra not installed')
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        key = rsa.generate_private_key(public_exponent=65537,key_size=2048)
        info = dict(type='service_account',token_uri='https://oauth2.googleapis.com/token',
                    client_email='synthetic@example.test',
                    private_key=key.private_bytes(serialization.Encoding.PEM,
                      serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode())
        with TemporaryDirectory() as temp:
            path=Path(temp)/'key.json';path.write_text(json.dumps(info))
            with patch.dict(os.environ,{'SHEETS_KEY':str(path)}):
                session=authorized_session('SHEETS_KEY')
                self.assertEqual(session.credentials.scopes,[READ_SCOPE]);session.close()
                info['token_uri']='https://evil.test/token';path.write_text(json.dumps(info))
                with self.assertRaises(ContractError) as caught: authorized_session('SHEETS_KEY')
                self.assertNotIn('PRIVATE KEY',str(caught.exception))
                self.assertNotIn(str(path),str(caught.exception))
    def test_invalid_url_and_unbounded_range_rejected_before_network(self):
        self.config['spreadsheet_id']='https://evil.test'
        with self.assertRaises(ContractError): self.adapter()
        self.assertEqual(self.session.calls,[])
        self.config['spreadsheet_id']='approved'
        self.config['tables']['rows']['range']='Report!A:H'
        with self.assertRaises(ContractError): self.adapter()
        self.assertEqual(self.session.calls,[])

if __name__=='__main__': unittest.main()
