"""Independent client Lipa: row-level oracle and real CSV/XLSX/MCP acceptance checks."""
import asyncio
from copy import deepcopy
import csv
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import xml.etree.ElementTree as ET

from finance_mcp.contract import CONTRACT_VERSION, ContractError, FinanceService
from finance_mcp.sources import load_adapter

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "lipa"
# Independent literal oracle: no adapter or source loader is used to derive expectations.
ORACLE = {
    ("2025-02", "cashflow"): {("other", "Север"): ("Прочее", "0.00", "value")},
    ("2025-03", "pnl"): {
        ("revenue", "Север"): ("Работы", "8743.21", "value"), ("revenue", "Юг"): ("Работы", "6020.34", "value"),
        ("cost", "Север"): ("Материалы и аренда", "-4020.11", "value"), ("cost", "Юг"): ("Материалы и аренда", "-3500.24", "value"),
        ("profit", "Север"): ("Прибыль", "4723.10", "value"), ("profit", "Юг"): ("Прибыль", "2520.10", "value"),
    },
    ("2025-04", "pnl"): {
        ("revenue", "Север"): ("Работы", "9010.20", "value"), ("revenue", "Юг"): ("Работы", "7010.50", "value"),
        ("cost", "Север"): ("Материалы и аренда", "-4000.10", "value"), ("cost", "Юг"): ("Материалы и аренда", "-3500.40", "value"),
        ("profit", "Север"): ("Прибыль", "5010.10", "value"), ("profit", "Юг"): ("Прибыль", "3510.10", "value"),
        ("depreciation", "Север"): ("Амортизация", None, "missing"), ("vat", "Юг"): ("НДС", None, "not_applicable"),
    },
    ("2025-03", "cashflow"): {
        ("receipts", "Север"): ("Оплаты клиентов", "8000.10", "value"), ("receipts", "Юг"): ("Оплаты клиентов", "6500.20", "value"),
        ("payments", "Север"): ("Выплаты", "-3900.05", "value"), ("payments", "Юг"): ("Выплаты", "-3400.15", "value"),
        ("net_flow", "Север"): ("Чистый поток", "4100.05", "value"), ("net_flow", "Юг"): ("Чистый поток", "3100.05", "value"),
    },
    ("2025-04", "cashflow"): {
        ("receipts", "Север"): ("Оплаты клиентов", "9200.25", "value"), ("receipts", "Юг"): ("Оплаты клиентов", "6900.10", "value"),
        ("payments", "Север"): ("Выплаты", "-4200.10", "value"), ("payments", "Юг"): ("Выплаты", "-3300.05", "value"),
        ("loan", "Север"): ("Заём", "1000.00", "value"),
        ("net_flow", "Север"): ("Чистый поток", "6000.15", "value"), ("net_flow", "Юг"): ("Чистый поток", "3600.05", "value"),
    },
    ("2025-03", "balances"): {
        ("opening", "Север"): ("Начало", "1200.10", "value"), ("opening", "Юг"): ("Начало", "800.20", "value"),
        ("movement", "Север"): ("Движение", "4100.05", "value"), ("movement", "Юг"): ("Движение", "3100.05", "value"),
        ("closing", "Север"): ("Конец", "5300.15", "value"), ("closing", "Юг"): ("Конец", "3900.25", "value"),
    },
    ("2025-04", "balances"): {
        ("opening", "Север"): ("Начало", "5300.15", "value"), ("opening", "Юг"): ("Начало", "3900.25", "value"),
        ("movement", "Север"): ("Движение", "6000.15", "value"), ("movement", "Юг"): ("Движение", "3600.05", "value"),
        ("closing", "Север"): ("Конец", "11300.30", "value"), ("closing", "Юг"): ("Конец", "7500.30", "value"),
    },
}


def assert_report(case, report):
    """Compare every exported column, metadata and row key with a separately written oracle."""
    key = (report["month"], report["kind"])
    expected = {k: (label, Decimal(value) if value is not None else None, status)
                for k, (label, value, status) in ORACLE[key].items()}
    actual = {(r["code"], r["dimension"]): (r["label"], Decimal(r["amount"]) if r["amount"] is not None else None, r["status"])
              for r in report["rows"]}
    case.assertEqual(len(report["rows"]), len(expected))
    case.assertEqual(actual, expected)
    case.assertEqual(report["currency"], "RUB")
    case.assertEqual(report["source"], "Учебный экспорт Липа v1")
    case.assertEqual(datetime.fromisoformat(report["as_of"]), datetime.fromisoformat("2025-05-07T10:00:00+03:00"))
    case.assertEqual(report["complete"], key != ("2025-04", "pnl"))


def assert_adapter(case, adapter):
    service = FinanceService(adapter)
    case.assertIn("Липа", adapter.passport())
    periods = service.periods()
    case.assertEqual([(p.month, p.closed, set(p.reports)) for p in periods], [
        ("2025-02", True, {"cashflow"}), ("2025-03", True, {"pnl", "cashflow", "balances"}),
        ("2025-04", True, {"pnl", "cashflow", "balances"}), ("2025-05", False, set())])
    count = 0
    for month, kind in ORACLE:
        report, = service.reports(kind, [month])
        assert_report(case, asdict(report))
        count += len(report.rows)
    case.assertEqual(count, 40)
    for month in ("2025-03", "2025-04"):
        balances = {(r.code, r.dimension): r.amount for r in adapter.report("balances", month).rows}
        flows = {(r.code, r.dimension): r.amount for r in adapter.report("cashflow", month).rows}
        for dimension in ("Север", "Юг"):
            case.assertEqual(balances["opening", dimension] + balances["movement", dimension], balances["closing", dimension])
            case.assertEqual(balances["movement", dimension], flows["net_flow", dimension])
            case.assertEqual(sum(v for (code, dim), v in flows.items() if dim == dimension and code != "net_flow"), flows["net_flow", dimension])
    for dimension in ("Север", "Юг"):
        b1 = {(r.code, r.dimension): r.amount for r in adapter.report("balances", "2025-03").rows}
        b2 = {(r.code, r.dimension): r.amount for r in adapter.report("balances", "2025-04").rows}
        case.assertEqual(b1["closing", dimension], b2["opening", dimension])
    case.assertEqual(service.notes("2025-03"), "Учебные данные. Без заёмных поступлений.")
    case.assertIn("амортизации", service.notes("2025-04"))
    for month, kind in (("2025-05", "pnl"), ("2025-02", "pnl"), ("2024-01", "cashflow")):
        with case.assertRaises(ContractError):
            service.reports(kind, [month])


def make_xlsx(base):
    from openpyxl import Workbook
    config = json.loads((base / "csv.json").read_text())
    book = Workbook()
    book.remove(book.active)
    for name, spec in config["tables"].items():
        sheet = book.create_sheet(name)
        with (base / spec["file"]).open(newline="", encoding="utf-8-sig") as f:
            for row in csv.reader(f, delimiter=spec.get("delimiter", ",")):
                sheet.append(row)
    book.save(base / "lipa.xlsx")
    book.close()


async def protocol_check(case, config_path, env=None):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    with tempfile.TemporaryDirectory() as cwd:
        params = StdioServerParameters(command=sys.executable,
            args=["-m", "finance_mcp.server", "--config", str(config_path.resolve())], cwd=cwd, env=env)
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                init = await client.initialize()
                case.assertIn("Липа", init.instructions)
                case.assertEqual(len((await client.list_tools()).tools), 6)
                passport = await client.call_tool("get_passport", {})
                case.assertIn("Липа", passport.content[0].text)
                months = await client.call_tool("list_months", {})
                case.assertEqual(len(json.loads(months.content[0].text)["periods"]), 4)
                count = 0
                for month, kind in ORACLE:
                    tool = {"pnl": "get_pnl", "cashflow": "get_cashflow", "balances": "get_balances"}[kind]
                    result = await client.call_tool(tool, {"months": [month]})
                    case.assertFalse(result.is_error)
                    report, = json.loads(result.content[0].text)["reports"]
                    assert_report(case, report)
                    count += len(report["rows"])
                case.assertEqual(count, 40)
                notes = await client.call_tool("get_month_notes", {"month": "2025-04"})
                case.assertIn("амортизации", notes.content[0].text)
                for month in ("2025-05", "2025-02", "2024-01"):
                    result = await client.call_tool("get_pnl", {"months": [month]})
                    case.assertTrue(result.is_error)


class FileSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        shutil.copytree(EXAMPLE, self.base, dirs_exist_ok=True)

    def edit_csv(self, name, mutate):
        path = self.base / name
        delimiter = ";" if name == "rows.csv" else ","
        with path.open(newline="") as f:
            rows = list(csv.reader(f, delimiter=delimiter))
        mutate(rows)
        with path.open("w", newline="") as f:
            csv.writer(f, delimiter=delimiter).writerows(rows)

    def test_csv_and_xlsx_all_rows_repeat_without_source_changes(self):
        make_xlsx(self.base)
        hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.base.iterdir()}
        for config in ("csv.json", "xlsx.json"):
            for _ in range(2):
                assert_adapter(self, load_adapter(self.base / config))
        self.assertEqual(hashes, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.base.iterdir()})

    def test_csv_bad_rows_rejected(self):
        original = (self.base / "rows.csv").read_bytes()
        mutations = [lambda r: r.append(r[1]), lambda r: r[1].__setitem__(0, "other"),
                     lambda r: r[1].__setitem__(6, "1.23"), lambda r: r[1].__setitem__(6, "NaN"),
                     lambda r: r[1].__setitem__(7, "missing"), lambda r: r[1].__setitem__(2, "unknown"),
                     lambda r: r[1].__setitem__(1, "2024-01"), lambda r: r[1].pop(),
                     lambda r: r[0].__setitem__(7, r[0][6])]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                (self.base / "rows.csv").write_bytes(original)
                self.edit_csv("rows.csv", mutate)
                with self.assertRaises(ContractError):
                    load_adapter(self.base / "csv.json")

    def test_report_metadata_and_oracle_detect_corruption(self):
        original = (self.base / "reports.csv").read_bytes()
        for mutate in (lambda r: r.append(r[1]), lambda r: r[1].__setitem__(5, "2025-01-01"),
                       lambda r: r[1].__setitem__(6, "1"), lambda r: r.__delitem__(1)):
            (self.base / "reports.csv").write_bytes(original)
            self.edit_csv("reports.csv", mutate)
            with self.assertRaises(ContractError):
                load_adapter(self.base / "csv.json")
        (self.base / "reports.csv").write_bytes(original)
        self.edit_csv("rows.csv", lambda r: r[1].__setitem__(6, "0,01"))
        # A valid but incorrect amount must fail reconciliation, not silently pass validation.
        with self.assertRaises(AssertionError):
            assert_adapter(self, load_adapter(self.base / "csv.json"))

    def test_xlsx_errors_formulas_and_numeric_cells(self):
        from openpyxl import load_workbook
        for value in ("=1+2", "#DIV/0!"):
            make_xlsx(self.base)
            book = load_workbook(self.base / "lipa.xlsx")
            book["rows"]["G2"] = value
            book.save(self.base / "lipa.xlsx")
            book.close()
            with self.assertRaises(ContractError):
                load_adapter(self.base / "xlsx.json")
        make_xlsx(self.base)
        book = load_workbook(self.base / "lipa.xlsx")
        book["rows"]["G2"] = 0.0
        book.save(self.base / "lipa.xlsx")
        book.close()
        assert_adapter(self, load_adapter(self.base / "xlsx.json"))

    def test_cached_formula_read_without_evaluation(self):
        from openpyxl import load_workbook
        make_xlsx(self.base)
        book = load_workbook(self.base / "lipa.xlsx")
        book["rows"]["G2"] = "=0+0"
        book.save(self.base / "lipa.xlsx")
        book.close()
        path = self.base / "lipa.xlsx"
        with zipfile.ZipFile(path) as z:
            members = {n: z.read(n) for n in z.namelist()}
        xml = ET.fromstring(members["xl/worksheets/sheet3.xml"])
        ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
        cell = xml.find(f".//{ns}c[@r='G2']")
        self.assertEqual(cell.find(ns + "f").text, "0+0")
        cell.find(ns + "v").text = "0"
        members["xl/worksheets/sheet3.xml"] = ET.tostring(xml, encoding="utf-8")
        with zipfile.ZipFile(path, "w") as z:
            for name, data in members.items():
                z.writestr(name, data)
        assert_adapter(self, load_adapter(self.base / "xlsx.json"))

    def test_config_limits_and_complete_with_missing(self):
        original = json.loads((self.base / "csv.json").read_text())
        for mutate in (lambda c: c.update(max_rows=1), lambda c: c.update(sql="SELECT 1"),
                       lambda c: c.update(version=True),
                       lambda c: c["tables"]["rows"]["columns"].update(amount="Нет такой колонки")):
            config = deepcopy(original)
            mutate(config)
            (self.base / "bad.json").write_text(json.dumps(config))
            with self.assertRaises(ContractError):
                load_adapter(self.base / "bad.json")
        self.edit_csv("reports.csv", lambda r: r[5].__setitem__(6, "true"))
        with self.assertRaises(ContractError):
            load_adapter(self.base / "csv.json")

    def test_cli_check_and_snapshot_refresh(self):
        adapter = load_adapter(self.base / "csv.json")
        self.edit_csv("rows.csv", lambda r: r.append(r[1]))
        assert_adapter(self, adapter)  # Existing snapshot is unchanged.
        with self.assertRaises(ContractError):
            load_adapter(self.base / "csv.json")
        result = subprocess.run([sys.executable, "-m", "finance_mcp.server", "--config",
                                 str(self.base / "csv.json"), "--check"], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")

    def test_export_changed_during_read_is_rejected(self):
        from finance_mcp.sources import read_passport
        def changed(config, base):
            value = read_passport(config, base)
            with (base / "rows.csv").open("a") as f:
                f.write("\n")
            return value
        with patch("finance_mcp.file_adapter.read_passport", changed):
            with self.assertRaisesRegex(ContractError, "изменился"):
                load_adapter(self.base / "csv.json")

    def test_csv_xlsx_via_real_mcp_and_cli(self):
        make_xlsx(self.base)
        for config in ("csv.json", "xlsx.json"):
            result = subprocess.run([sys.executable, "-m", "finance_mcp.server", "--config",
                                     str(self.base / config), "--check"], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"contract_version": CONTRACT_VERSION, "periods": 4, "reports": 7, "rows": 40})
            asyncio.run(asyncio.wait_for(protocol_check(self, self.base / config), timeout=45))


if __name__ == "__main__":
    unittest.main()
