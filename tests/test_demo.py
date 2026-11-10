"""Independent row-level oracle, alternate adapter and real stdio protocol controls."""
import asyncio
import inspect
from dataclasses import replace
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from finance_mcp.contract import FinanceService, Period, Report, Row
from finance_mcp.demo import DemoAdapter

# Independent literal oracle: all published codes and values, not sums from the generator.
EXPECTED = {
    ("2026-08", "pnl"): {"revenue": "120000.00", "cost": "-45000.00", "rent": "-15000.00",
                           "tax": "0.00", "net_profit": "60000.00"},
    ("2026-09", "pnl"): {"revenue": "150000.00", "cost": "-60000.00", "rent": "-15000.00",
                           "tax": "0.00", "net_profit": "75000.00"},
    ("2026-08", "cashflow"): {"receipts": "100000.00", "payments": "-55000.00",
                                "loan": "10000.00", "net_flow": "55000.00"},
    ("2026-09", "cashflow"): {"receipts": "160000.00", "payments": "-80000.00",
                                "loan": "0.00", "net_flow": "80000.00"},
    ("2026-08", "balances"): {"opening": "20000.00", "movement": "55000.00", "closing": "75000.00"},
    ("2026-09", "balances"): {"opening": "75000.00", "movement": "80000.00", "closing": "155000.00"},
}


class OtherAdapter:
    """Fresh independent source; no DemoAdapter inheritance or private SDK mutation."""

    def passport(self):
        return "Synthetic client B; only cashflow, EUR, incomplete."

    def periods(self):
        return (Period("2025-01", True, ("cashflow",)),)

    def report(self, kind, month):
        return Report(kind, month, "EUR", "Generated client B", "2025-02-01T00:00:00+00:00", False,
                      (Row("zero", "Ноль", "Магазин", Decimal("0.00")),
                       Row("unknown", "Нет данных", "Магазин", None, "missing"),
                       Row("na", "Не применимо", "Магазин", None, "not_applicable")))

    def notes(self, month):
        return "Missing source data."


class ContractTests(unittest.TestCase):
    def test_all_demo_rows_and_identities(self):
        service = FinanceService(DemoAdapter())
        count = 0
        for (month, kind), want in EXPECTED.items():
            report, = service.reports(kind, [month])
            got = {row.code: str(row.amount) for row in report.rows}
            self.assertEqual(got, want)
            self.assertEqual(len(report.rows), len(want))
            self.assertEqual(report.currency, "RUB")
            self.assertTrue(report.complete)
            count += len(got)
        self.assertEqual(count, 24)
        previous = None
        for month in ("2026-08", "2026-09"):
            amounts = lambda kind: {r.code: r.amount for r in service.reports(kind, [month])[0].rows}
            pnl, cash, bal = amounts("pnl"), amounts("cashflow"), amounts("balances")
            self.assertEqual(sum(v for k, v in pnl.items() if k != "net_profit"), pnl["net_profit"])
            self.assertEqual(cash["receipts"] + cash["payments"] + cash["loan"], cash["net_flow"])
            self.assertEqual(bal["opening"] + bal["movement"], bal["closing"])
            self.assertEqual(bal["movement"], cash["net_flow"])
            if previous is not None:
                self.assertEqual(previous, bal["opening"])
            previous = bal["closing"]

    def test_other_adapter_missing_zero_not_applicable(self):
        service = FinanceService(OtherAdapter())
        report, = service.reports("cashflow", ["2025-01"])
        self.assertEqual([r.status for r in report.rows], ["value", "missing", "not_applicable"])
        self.assertEqual([r.amount for r in report.rows], [Decimal("0.00"), None, None])
        with self.assertRaises(ValueError):
            service.reports("pnl", ["2025-01"])
        with self.assertRaises(ValueError):
            replace(report, complete=True)

    def test_invalid_data_is_rejected(self):
        for amount in (0.1, Decimal("NaN"), Decimal("Infinity"), None):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                Row("a", "A", "Всего", amount)
        row = Row("a", "A", "Всего", Decimal("1.00"))
        for rows in ((), (row, row)):
            with self.assertRaises(ValueError):
                Report("pnl", "2025-01", "RUB", "source", "2025-02-01T00:00:00+00:00", True, rows)

    def test_invalid_requests_and_wrong_source_response(self):
        service = FinanceService(DemoAdapter())
        for months in ([], ["2026-10"], ["2024-01"], ["2026-13"], ["2026-08", "2026-08"],
                       ["2026-08", "2026-10"], ["2026-8"], [None], ["2026-08"] * 13):
            with self.subTest(months=months), self.assertRaises(ValueError):
                service.reports("pnl", months)
        with self.assertRaises(ValueError):
            service.reports("stock", ["2026-08"])

        class WrongAdapter(DemoAdapter):
            def report(self, kind, month):
                return super().report(kind, "2026-09")
        with self.assertRaises(ValueError):
            FinanceService(WrongAdapter()).reports("pnl", ["2026-08"])

    def test_contract_import_has_no_sdk_dependency(self):
        code = "import sys; import finance_mcp.contract; assert 'mcp' not in sys.modules"
        subprocess.run([sys.executable, "-c", code], check=True, timeout=10)


class ProtocolTests(unittest.TestCase):
    def test_installed_stdio_server_twice_from_unrelated_directory(self):
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        async def scenario(cwd):
            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            params = StdioServerParameters(command=sys.executable,
                                           args=["-m", "finance_mcp.server"], cwd=cwd, env=env)
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as client:
                    init = await client.initialize()
                    self.assertIn("Маяк", init.instructions)
                    tools = (await client.list_tools()).tools
                    self.assertEqual({t.name for t in tools}, {"get_passport", "list_months", "get_pnl",
                                     "get_cashflow", "get_balances", "get_month_notes"})
                    self.assertTrue(all(t.annotations.read_only_hint for t in tools))
                    result = await client.call_tool("get_passport", {})
                    self.assertFalse(result.is_error)
                    self.assertIn("ДЕМО", result.content[0].text)
                    months = await client.call_tool("list_months", {})
                    periods = json.loads(months.content[0].text)["periods"]
                    self.assertEqual([p["closed"] for p in periods], [True, True, False])
                    count = 0
                    for kind, tool in (("pnl", "get_pnl"), ("cashflow", "get_cashflow"),
                                       ("balances", "get_balances")):
                        result = await client.call_tool(tool, {"months": ["2026-08", "2026-09"]})
                        self.assertFalse(result.is_error)
                        self.assertIsNone(result.structured_content)
                        reports = json.loads(result.content[0].text)["reports"]
                        self.assertEqual(len(reports), 2)
                        for report in reports:
                            got = {r["code"]: r["amount"] for r in report["rows"]}
                            self.assertEqual(got, EXPECTED[(report["month"], kind)])
                            count += len(got)
                    self.assertEqual(count, 24)
                    for month in ("2026-08", "2026-09"):
                        result = await client.call_tool("get_month_notes", {"month": month})
                        self.assertFalse(result.is_error)
                        self.assertIn("ДЕМО", result.content[0].text)
                    for args in ({"months": ["2026-10"]}, {"months": ["2024-01"]},
                                 {"months": []}, {"months": ["2026-08", "2026-10"]}):
                        result = await client.call_tool("get_pnl", args)
                        self.assertTrue(result.is_error)
                        self.assertNotEqual(result.content[0].text, "Error executing tool get_pnl")
                        if "2026-10" in args["months"]:
                            self.assertIn("не закрыт", result.content[0].text)

        with tempfile.TemporaryDirectory() as cwd:
            for _ in range(2):
                asyncio.run(asyncio.wait_for(scenario(cwd), timeout=45))

    def test_server_accepts_other_adapter(self):
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
        code = ("from decimal import Decimal\n"
                "from finance_mcp.contract import Period, Report, Row\n"
                "from finance_mcp.server import create_server\n"
                + inspect.getsource(OtherAdapter)
                + "\ncreate_server(OtherAdapter()).run(transport='stdio')\n")

        async def scenario(cwd):
            params = StdioServerParameters(command=sys.executable, args=["-c", code], cwd=cwd)
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as client:
                    init = await client.initialize()
                    self.assertIn("client B", init.instructions)
                    result = await client.call_tool("get_cashflow", {"months": ["2025-01"]})
                    self.assertFalse(result.is_error)
                    report, = json.loads(result.content[0].text)["reports"]
                    self.assertEqual(report["currency"], "EUR")
                    self.assertFalse(report["complete"])
                    self.assertEqual([r["amount"] for r in report["rows"]], ["0.00", None, None])
                    self.assertEqual([r["status"] for r in report["rows"]],
                                     ["value", "missing", "not_applicable"])
                    bad = await client.call_tool("get_pnl", {"months": ["2025-01"]})
                    self.assertTrue(bad.is_error)
                    self.assertIn("недоступен", bad.content[0].text)
        with tempfile.TemporaryDirectory() as cwd:
            asyncio.run(asyncio.wait_for(scenario(cwd), timeout=45))


if __name__ == "__main__":
    unittest.main()
