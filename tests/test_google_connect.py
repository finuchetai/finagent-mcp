"""Onboarding through real discovery, saved files, Google adapter and MCP tools.

Only HTTP responses / OAuth OS boundary are substituted. No customer data or grants.
"""
import asyncio
from copy import deepcopy
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from finance_mcp.contract import ContractError
from finance_mcp.google_connect import (
    discover, save_profile, spreadsheet_id, month_value, first_analysis, main)
from finance_mcp.google_sheets_adapter import GoogleSheetsAdapter
from finance_mcp.sources import load_adapter
from finance_mcp.server import create_server

LINK = "https://docs.google.com/spreadsheets/d/synthetic-book/edit#gid=0"


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_content(self, size):
        yield json.dumps(self.payload).encode()


class Transport:
    def __init__(self):
        self.metadata = {
            "spreadsheetId": "synthetic-book", "properties": {"title": "Test company", "locale": "ru_RU"},
            "sheets": [{"properties": {"sheetId": 0, "title": "ДДС", "sheetType": "GRID",
                                      "gridProperties": {"rowCount": 100, "columnCount": 3}}}]}
        self.rows = [["Статья", "2026-09", "2026-10"],
                     ["Поступления", 45000, 200.25], ["Расходы", -20, -10.10],
                     ["Денежный поток", 44980, 190.15], ["Пропуск", "", ""], ["Ноль", 0, 0]]
        self.calls = []
        self.status = 200
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.status, Exception):
            raise self.status
        if url.endswith("/values:batchGet"):
            return Response({"valueRanges": [{"values": deepcopy(self.rows)}]}, self.status)
        return Response(deepcopy(self.metadata), self.status)


class ConnectTests(unittest.TestCase):
    def setUp(self):
        self.http = Transport()
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.destination = Path(self.temp.name) / "owner"
    def connect(self, **changes):
        result = discover(LINK, self.http)
        options = dict(month="2026-10", kind="cashflow", currency="RUB", ready=True,
                       destination=self.destination, oauth_profile="owner", session=self.http, units_confirmed=True)
        options.update(changes)
        return save_profile(result, result["candidates"][0], **options)
    def test_full_chain_saved_profile_reloaded_and_real_mcp(self):
        result = self.connect()
        self.assertFalse(result["ui_connected"])
        with patch("finance_mcp.google_oauth.authorized_oauth_session", return_value=self.http):
            adapter = load_adapter(result["config"])
        rows = adapter.report("cashflow", "2026-10").rows
        self.assertEqual(len(rows), 5)
        # Every source row, including total, detail, missing and actual zero.
        self.assertEqual([(r.label, r.amount, r.status) for r in rows], [
            ("Поступления", Decimal("200.25"), "value"), ("Расходы", Decimal("-10.1"), "value"),
            ("Денежный поток", Decimal("190.15"), "value"), ("Пропуск", None, "missing"),
            ("Ноль", Decimal("0"), "value")])
        analysis = result["first_analysis"]
        self.assertFalse(analysis["complete"])
        self.assertIsNone(analysis["as_of"])
        self.assertEqual(len(analysis["rows"]), 5)
        self.assertEqual(analysis["rows"], first_analysis(adapter, "2026-10", "cashflow")["rows"])
        config = json.loads(Path(result["config"]).read_text())
        self.assertEqual(set(config["tables"]["rows"]["wide"]["months"]), {"2026-10"})
        manifest = json.loads((self.destination / "mcp.json").read_text())
        self.assertEqual(manifest["mcpServers"]["finance-local"]["args"][-1], result["config"])
        async def calls():
            server = create_server(adapter)
            reply = await server.call_tool("get_cashflow", {"months": ["2026-10"]})
            self.assertIn("190.15", str(reply))
            self.http.rows[1][2] = 900.25
            reply = await server.call_tool("get_cashflow", {"months": ["2026-10"]})
            self.assertIn("900.25", str(reply))
            self.http.status = RuntimeError("private transport credentials")
            with self.assertRaises(Exception) as caught:
                await server.call_tool("get_cashflow", {"months": ["2026-10"]})
            self.assertNotIn("credentials", str(caught.exception))
        asyncio.run(calls())
        self.assertTrue(all(url.startswith("https://sheets.googleapis.com/") for url, _ in self.http.calls))
        self.assertTrue(all(not kwargs["allow_redirects"] for _, kwargs in self.http.calls))
    def test_link_validation_before_network(self):
        self.assertEqual(spreadsheet_id(LINK), "synthetic-book")
        for link in ["http://docs.google.com/spreadsheets/d/x/edit", "https://docs.google.com.evil/spreadsheets/d/x/edit",
                     "https://docs.google.com@evil/spreadsheets/d/x/edit",
                     "https://docs.google.com:443/spreadsheets/d/x/edit", "file:///tmp/private",
                     "https://docs.google.com/spreadsheets/d/x/export", None]:
            with self.subTest(link=link), self.assertRaises(ContractError):
                discover(link, self.http)
        self.assertEqual(self.http.calls, [])
    def test_months_and_money_not_confused(self):
        for raw, expected in [("октябрь 2026", "2026-10"), ("Oct 2026", "2026-10"),
                              ("01.09.2026", "2026-09"), ("2026-10", "2026-10")]:
            self.assertEqual(month_value(raw), expected)
        self.assertIsNone(month_value(45000))
        self.assertIsNone(month_value(True))
        self.assertIsNone(month_value("2026-13"))
        serial = (date(2026, 10, 1) - date(1899, 12, 30)).days
        self.assertEqual(month_value(serial, allow_serial=True), "2026-10")
        self.http.rows[0][1:] = [serial, "2026-11"]
        self.assertEqual(set(discover(LINK, self.http)["candidates"][0]["months"]), {"2026-10", "2026-11"})
    def test_unsupported_structure_never_silently_mapped(self):
        cases = [
            [["Статья", "2026-10", "2026-10"], ["Доход", 10, 20]],
            [["Статья", "2026-10", "План"], ["Доход", 10, 20]],
            [["Статья", "2026-10"], ["Доход", "#REF!"]],
            [["Статья", "2026-10"], ["Доход", True]],
            [["Статья", "2026-10"], ["Доход", "10,25"]],
            [["Статья", "2026-10"], ["", 10]],
            [["Статья", "2026-10"], ["Доход", 10], ["Доход", 20]],
            [["Статья", "2026-10"], [" Доход", 10]],
            [["Статья", "2026-10"], ["Доход", 10, "другое направление"]],
            [["Статья", "2026-10"], ["Доход", ""]],
            [["Дата", "Сумма"], ["2026-10-01", 10]],
        ]
        for rows in cases:
            with self.subTest(rows=rows):
                self.http.rows = rows
                result = discover(LINK, self.http)
                self.assertEqual(result["candidates"], [])
                self.assertTrue(result["unsupported"])
    def test_plan_sheet_is_not_offered_as_fact(self):
        self.http.metadata["sheets"][0]["properties"]["title"] = "План ДДС"
        result = discover(LINK, self.http)
        self.assertEqual(result["candidates"], [])
        self.assertIn("плана", result["unsupported"][0]["reason"])
    def test_numeric_date_headers_are_saved_as_json_numbers(self):
        serial = (date(2026, 10, 1) - date(1899, 12, 30)).days
        self.http.rows[0][2] = float(serial)
        result = self.connect()
        config = json.loads(Path(result["config"]).read_text())
        self.assertEqual(config["tables"]["rows"]["wide"]["months"]["2026-10"]["header"], serial)
        self.assertEqual(result["first_analysis"]["rows"][0]["amount"], "200.25")
    def test_single_column_notes_do_not_block_report(self):
        self.http.metadata["sheets"].append({"properties": {
            "sheetId": 1, "title": "Примечания", "sheetType": "GRID",
            "gridProperties": {"rowCount": 100, "columnCount": 1}}})
        result = discover(LINK, self.http)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["unsupported"][0]["title"], "Примечания")
    def test_local_directory_profiles_cannot_traverse(self):
        for profile in [".", "..", "CON", "owner."]:
            with self.subTest(profile=profile), self.assertRaises(SystemExit):
                main(["--home", self.temp.name, "--profile", profile, "--url", LINK])
    def test_scaled_and_nonmonetary_reports_are_not_exposed_as_currency(self):
        baseline = deepcopy(self.http.rows)
        for label in ["Рентабельность,%", "Количество, шт.", "Маржинальность", "Численность, чел."]:
            self.http.rows = deepcopy(baseline)
            self.http.rows.append([label, 0.15, 0.2])
            self.assertFalse(discover(LINK, self.http)["candidates"], label)
        self.http.rows = baseline
        self.http.metadata["sheets"][0]["properties"]["title"] = "ДДС, тыс. руб."
        self.assertFalse(discover(LINK, self.http)["candidates"])
    def test_large_grid_is_refused_even_if_returned_prefix_has_empty_tail(self):
        self.http.metadata["sheets"][0]["properties"]["gridProperties"]["rowCount"] = 5001
        result = discover(LINK, self.http)
        self.assertFalse(result["candidates"])
        self.assertIn("5000", result["unsupported"][0]["reason"])
    def test_refresh_rejects_grid_growth_and_sheet_replacement(self):
        result = self.connect()
        config = json.loads(Path(result["config"]).read_text())
        adapter = GoogleSheetsAdapter(config, self.destination, session=self.http)
        original = adapter.report("cashflow", "2026-10")
        props = self.http.metadata["sheets"][0]["properties"]
        for change in ["rows", "columns", "identity"]:
            baseline = deepcopy(props)
            if change == "rows": props["gridProperties"]["rowCount"] += 10
            if change == "columns": props["gridProperties"]["columnCount"] += 1
            if change == "identity": props["sheetId"] += 1
            with self.subTest(change=change), self.assertRaises(ContractError):
                adapter.refresh()
            self.assertEqual(adapter.report("cashflow", "2026-10"), original)
            props.clear()
            props.update(baseline)
    def test_changed_units_in_preamble_invalidate_saved_map(self):
        self.http.rows.insert(0, ["Суммы в единицах валюты"])
        result = self.connect()
        config = json.loads(Path(result["config"]).read_text())
        adapter = GoogleSheetsAdapter(config, self.destination, session=self.http)
        original = adapter.report("cashflow", "2026-10")
        self.http.rows[0][0] = "Суммы в тыс. руб."
        with self.assertRaisesRegex(ContractError, "пояснения"):
            adapter.refresh()
        self.assertEqual(adapter.report("cashflow", "2026-10"), original)
    def test_selection_and_owner_confirmation_required(self):
        for changes in [dict(ready=False), dict(kind="netprofit"), dict(currency="руб"),
                        dict(month="2026-12"), dict(units_confirmed=False)]:
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                self.connect(**changes)
            self.assertFalse(self.destination.exists())
    def test_refresh_checks_new_renamed_and_moved_rows(self):
        result = self.connect()
        config = json.loads(Path(result["config"]).read_text())
        adapter = GoogleSheetsAdapter(config, self.destination, session=self.http)
        old = adapter.report("cashflow", "2026-10")
        baseline = deepcopy(self.http.rows)
        for change in ["append", "rename", "move", "delete"]:
            self.http.rows = deepcopy(baseline)
            if change == "append": self.http.rows.append(["Новая статья", 10, 10])
            if change == "rename": self.http.rows[1][0] = "Новое имя"
            if change == "move": self.http.rows[1], self.http.rows[2] = self.http.rows[2], self.http.rows[1]
            if change == "delete": self.http.rows.pop()
            with self.subTest(change=change), self.assertRaises(ContractError):
                adapter.refresh()
            self.assertEqual(adapter.report("cashflow", "2026-10"), old)
    def test_failed_adapter_validation_leaves_no_profile(self):
        found = discover(LINK, self.http)
        self.http.rows[1][2] = "#REF!"
        with self.assertRaises(ContractError):
            save_profile(found, found["candidates"][0], month="2026-10", kind="cashflow", currency="RUB",
                         ready=True, destination=self.destination, oauth_profile="owner", session=self.http, units_confirmed=True)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])
    def test_repeat_reuses_profile_and_refuses_different_book(self):
        self.connect()
        saved = (self.destination / "config.json").read_bytes()
        with self.assertRaisesRegex(ContractError, "существует"):
            self.connect()
        self.assertEqual(saved, (self.destination / "config.json").read_bytes())
        with patch("finance_mcp.google_oauth.authorized_oauth_session", return_value=self.http),              patch("builtins.input", side_effect=AssertionError("must not ask again")), patch("builtins.print") as out:
            main(["--home", self.temp.name, "--url", LINK, "--json"])
            self.assertIn("reused", out.call_args[0][0])
        with self.assertRaises(SystemExit):
            main(["--home", self.temp.name, "--url", LINK.replace("synthetic-book", "other")])
    def test_google_errors_are_safe(self):
        for status in [401, 403, 404, 429, RuntimeError("SECRET")]:
            self.http.status = status
            with self.assertRaises(ContractError) as caught:
                discover(LINK, self.http)
            self.assertNotIn("SECRET", str(caught.exception))
    def test_filled_boundary_and_oversized_width_refused(self):
        self.http.metadata["sheets"][0]["properties"]["gridProperties"]["rowCount"] = len(self.http.rows)
        result = discover(LINK, self.http)
        self.assertFalse(result["candidates"])
        self.http.metadata["sheets"][0]["properties"]["gridProperties"].update(rowCount=100, columnCount=101)
        self.assertFalse(discover(LINK, self.http)["candidates"])
    def test_changing_discovery_never_saves(self):
        original = self.http.get
        n = 0
        def changing(url, **kwargs):
            nonlocal n
            result = original(url, **kwargs)
            if url.endswith("batchGet"):
                n += 1
                if n == 2:
                    result.payload["valueRanges"][0]["values"][1][1] = 42
            return result
        self.http.get = changing
        with self.assertRaisesRegex(ContractError, "меняется"):
            discover(LINK, self.http)


if __name__ == "__main__":
    unittest.main()
