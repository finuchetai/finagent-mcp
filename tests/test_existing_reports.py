"""Existing-book layouts through discovery, persistence, refresh and real MCP."""
import asyncio
from copy import deepcopy
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from test_google_connect import Transport, LINK
from finance_mcp.contract import ContractError
from finance_mcp.google_connect import discover, save_profile
from finance_mcp.google_sheets_adapter import GoogleSheetsAdapter
from finance_mcp.server import create_server

class ExistingReportTests(unittest.TestCase):
    def setUp(self):
        self.http = Transport()
        self.props = self.http.metadata["sheets"][0]["properties"]
        self.props["gridProperties"].update(rowCount=100, columnCount=6)
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "profile"
    def cashflow(self):
        self.props["title"] = "ДДС по выпискам"
        self.http.rows = [
            ["ДДС, USD"], ["Статья", None, "2026-09", "2026-10", "Итого", "Примечание"],
            ["Операционная деятельность"], ["Выручка", None, 90, 110, 200, "банк"],
            ["Расходы", None, -5, -6, -11], ["Кредиты и займы (%)", None, -1, -2, -3],
            ["Чистый денежный поток", None, 84, 102, 186],
            ["Пропуск", None, None, None], ["Ноль", None, 0, 0, 0]]
        return discover(LINK, self.http)
    def balances(self):
        self.props["title"] = "Остатки"
        serial = (date(2026, 10, 1) - date(1899, 12, 30)).days
        self.http.rows = [
            ["Счёт", "Показатель", serial, "2026-11"],
            ["Bank A", "На начало", 50, 65], ["Bank A", "На конец", 65, 0],
            ["Bank B", "На начало", None, None], ["Bank B", "На конец", -5, -1],
            [], ["Примечания"], ["Минус на карте — задолженность; данные не изменять."]]
        return discover(LINK, self.http)
    def save(self, found, kind):
        result = save_profile(found, found["candidates"][0], month="2026-10", kind=kind,
                              currency="USD", ready=True, destination=self.folder,
                              oauth_profile="owner", session=self.http, units_confirmed=True)
        config = json.loads(Path(result["config"]).read_text())
        return result, GoogleSheetsAdapter(config, self.folder, session=self.http)
    def test_cashflow_all_money_rows_without_annual_total(self):
        found = self.cashflow()
        source = deepcopy(self.http.rows)
        result, adapter = self.save(found, "cashflow")
        rows = adapter.report("cashflow", "2026-10").rows
        self.assertEqual([(r.label, r.amount, r.status) for r in rows], [
            ("Выручка", Decimal("110"), "value"), ("Расходы", Decimal("-6"), "value"),
            ("Кредиты и займы (%)", Decimal("-2"), "value"),
            ("Чистый денежный поток", Decimal("102"), "value"),
            ("Пропуск", None, "missing"), ("Ноль", Decimal("0"), "value")])
        self.assertEqual(self.http.rows, source)
        self.assertIn("Итого", adapter.passport())
        self.assertIn("только выбранный месяц", adapter.passport())
        self.assertFalse(result["first_analysis"]["complete"])
        async def call():
            self.http.rows[3][3] = 120
            self.http.rows[3][4] = 210
            reply = await create_server(adapter).call_tool("get_cashflow", {"months": ["2026-10"]})
            self.assertIn("120", str(reply))
        asyncio.run(call())
    def test_balances_composite_labels_dates_notes_and_mcp(self):
        result, adapter = self.save(self.balances(), "balances")
        rows = adapter.report("balances", "2026-10").rows
        self.assertEqual([(r.label, r.amount) for r in rows], [
            ("Bank A / На начало", Decimal("50")), ("Bank A / На конец", Decimal("65")),
            ("Bank B / На начало", None), ("Bank B / На конец", Decimal("-5"))])
        self.assertEqual(len(result["first_analysis"]["rows"]), 4)
        self.assertFalse(result["ui_connected"])
        async def call():
            reply = await create_server(adapter).call_tool("get_balances", {"months": ["2026-10"]})
            self.assertIn("Bank A / На конец", str(reply))
        asyncio.run(call())
        self.http.rows[1][1] = "Другой показатель"
        with self.assertRaisesRegex(ContractError, "строка"):
            adapter.refresh()
    def test_notes_drift_rejected_and_old_snapshot_kept(self):
        _, adapter = self.save(self.balances(), "balances")
        old = adapter.report("balances", "2026-10")
        self.http.rows[-1][0] = "Учёт теперь в тысячах"
        with self.assertRaisesRegex(ContractError, "примечания"):
            adapter.refresh()
        self.assertEqual(old, adapter.report("balances", "2026-10"))
    def test_unknown_column_and_plan_not_silently_dropped(self):
        self.cashflow()
        self.http.rows[1][4] = "Неизвестное направление"
        self.assertFalse(discover(LINK, self.http)["candidates"])
        self.http.rows[1][4] = "План"
        self.assertFalse(discover(LINK, self.http)["candidates"])
    def test_empty_auxiliary_column_cannot_gain_data(self):
        _, adapter = self.save(self.cashflow(), "cashflow")
        self.http.rows[3][1] = 999
        with self.assertRaisesRegex(ContractError, "пустой колонке"):
            adapter.refresh()
    def test_journal_month_values_are_not_headers(self):
        self.props["title"] = "Журнал"
        self.http.rows = [["Дата", "Дата начисления", "Месяц", "Сумма"],
                          [46146, None, "2026-05", 10], [46147, None, "2026-05", -1]]
        found = discover(LINK, self.http)
        self.assertEqual(found["candidates"], [])
    def test_legacy_string_header_and_empty_heading_still_supported(self):
        for heading in ["Category", "", None]:
            self.http.rows = [[heading, "2026-10"], ["Revenue", 10]]
            with self.subTest(heading=heading):
                self.assertEqual(len(discover(LINK, self.http)["candidates"]), 1)
    def test_units_in_notes_or_metadata_are_not_ignored(self):
        self.cashflow()
        self.http.rows[3][5] = "Сумма в тыс. USD"
        self.assertFalse(discover(LINK, self.http)["candidates"])
        self.balances()
        self.http.rows[-1][0] = "Суммы в тысячах"
        self.assertFalse(discover(LINK, self.http)["candidates"])
    def test_metadata_unit_drift_blocks_refresh(self):
        _, adapter = self.save(self.cashflow(), "cashflow")
        self.http.rows[3][5] = "Суммы в тысячах"
        with self.assertRaisesRegex(ContractError, "сведения"):
            adapter.refresh()
    def test_percent_metric_rejected_despite_money_interest_line(self):
        self.cashflow()
        self.http.rows.append(["Маржа, %", None, 0.1, 0.2])
        self.assertFalse(discover(LINK, self.http)["candidates"])
if __name__ == "__main__":
    unittest.main()
