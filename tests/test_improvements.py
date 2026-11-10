"""Preflight failures, bounded real stdio, honest dates and approved stable article identities."""
import asyncio
from copy import deepcopy
import csv
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from finance_mcp.contract import ContractError
from finance_mcp.demo import DemoAdapter
from finance_mcp.diagnostics import preflight, probe_stdio
from finance_mcp.registry import ArticleRegistry
from finance_mcp.sources import load_adapter
from test_sources import EXAMPLE, ORACLE, assert_adapter, make_xlsx


class EnvironmentTests(unittest.TestCase):
    def test_preflight_before_install_without_site_packages(self):
        script = Path(__file__).resolve().parents[1] / "preflight.py"
        result = subprocess.run([sys.executable, "-S", str(script), "environment"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("package_index", json.loads(result.stdout)["not_checked"])

    @unittest.skipUnless(os.name == "posix", "executable fixture for Linux/WSL")
    def test_missing_binary_and_missing_runtime_are_distinct_from_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            for name in ("initdb", "pg_ctl", "postgres"):
                p = base / name
                p.write_text("#!" + sys.executable + "\nprint('PostgreSQL test')\n")
                p.chmod(0o700)
            self.assertTrue(preflight(postgres_bin=base)["ok"])
            (base / "pg_ctl").unlink()
            check = preflight(postgres_bin=base)
            self.assertFalse(check["ok"])
            self.assertFalse(next(x for x in check["checks"] if x["check"] == "pg_ctl")["ok"])
            p = base / "postgres"
            p.write_text("#!" + sys.executable + "\nimport sys\nprint('PRIVATE_MARKER',file=sys.stderr)\nsys.exit(127)\n")
            check = preflight(postgres_bin=base)
            self.assertIn("not_runnable", next(x for x in check["checks"] if x["check"] == "postgres")["detail"])
            self.assertNotIn("PRIVATE_MARKER", json.dumps(check))

    def test_actual_stdio_initialize_and_tools(self):
        self.assertEqual(probe_stdio(timeout=15), {"ok": True, "phase": "complete", "tools": 6})

    @unittest.skipUnless(os.name == "posix", "process-group cleanup control")
    def test_hanging_stdio_is_bounded_and_descendants_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            heartbeat = Path(folder) / "heartbeat"
            child = f"import time; from pathlib import Path\np=Path({str(heartbeat)!r})\nwhile True:\n p.write_text(str(time.time_ns())); time.sleep(.05)"
            server = f"import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c',{child!r}])\nprint('SECRET_SENTINEL',file=sys.stderr,flush=True)\ntime.sleep(60)"
            start = time.monotonic()
            result = probe_stdio(timeout=4, server_command=[sys.executable, "-c", server])
            self.assertLess(time.monotonic() - start, 8)
            self.assertTrue(heartbeat.exists(), "negative control must really start")
            self.assertEqual(result["reason"], "timeout")
            self.assertEqual(result["cause"], "undetermined")
            self.assertEqual(result["phase"], "initialize")
            self.assertNotIn("SECRET_SENTINEL", json.dumps(result))
            stamp = heartbeat.read_text()
            time.sleep(.3)
            self.assertEqual(heartbeat.read_text(), stamp)
        with self.assertRaises(ValueError):
            probe_stdio(timeout=float("nan"))


class RegistryTests(unittest.TestCase):
    def document(self):
        return {"version": 1, "client_id": "client_b", "entries": [
            {"kind": "pnl", "code": "revenue", "source_ids": ["erp:17"], "labels": ["Работы", "Ремонт"], "approved_by": "fixture"}]}

    def test_rename_and_id_keep_code_without_mutating_map(self):
        document = self.document()
        before = deepcopy(document)
        registry = ArticleRegistry(document, "client_b")
        for label in ("Работы", "Ремонт"):
            self.assertEqual(registry.resolve("pnl", "", label, "erp:17"), "revenue")
            self.assertEqual(registry.resolve("pnl", "", label), "revenue")
        self.assertEqual(document, before)
        for code, label, source in (("cost", "Работы", "erp:17"), ("", "Работы", "unknown"), ("revenue", "Не согласовано", None)):
            with self.assertRaises(ContractError):
                registry.resolve("pnl", code, label, source)

    def test_ambiguity_and_registry_update_guard(self):
        old = self.document()
        new = deepcopy(old)
        new["entries"][0]["labels"].append("Новое имя")
        ArticleRegistry(new, "client_b").assert_compatible(ArticleRegistry(old, "client_b"))
        for change in (lambda d: d["entries"][0].update(code="new_code"),
                       lambda d: d["entries"][0].update(source_ids=[]),
                       lambda d: d["entries"][0].update(labels=["Работы"])):
            bad = deepcopy(old)
            change(bad)
            with self.assertRaises(ContractError):
                ArticleRegistry(bad, "client_b").assert_compatible(ArticleRegistry(old, "client_b"))
        other = deepcopy(old["entries"][0]); other.update(code="other", source_ids=["erp:18"])
        new["entries"].append(other)
        registry = ArticleRegistry(new, "client_b")
        with self.assertRaises(ContractError):
            registry.resolve("pnl", "", "Работы")
        self.assertEqual(registry.resolve("pnl", "", "Работы", "erp:17"), "revenue")
        new["entries"][1]["source_ids"] = ["erp:17"]
        with self.assertRaises(ContractError):
            ArticleRegistry(new, "client_b")


class SourceDateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        shutil.copytree(EXAMPLE, self.base, dirs_exist_ok=True)

    def unknown_dates(self):
        path = self.base / "reports.csv"
        with path.open(newline="") as f:
            reader = csv.DictReader(f); rows = list(reader); fields = reader.fieldnames
        for row in rows:
            row.update(received_at="2025-05-08T12:00:00+03:00", as_of_status="confirmed", source_time_raw="")
            if (row["month"], row["kind"]) == ("2025-04", "pnl"):
                row.update(as_of="", as_of_status="unknown", source_time_raw="07.05.2025 10:00, TZ не указана")
        optional = ["received_at", "as_of_status", "source_time_raw"]
        with path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields + optional); writer.writeheader(); writer.writerows(rows)
        for name in ("csv.json", "xlsx.json"):
            p = self.base / name; config = json.loads(p.read_text())
            config["tables"]["reports"]["columns"] = {k: k for k in fields + optional}
            p.write_text(json.dumps(config))

    def test_legacy_constructor_and_honest_unknown_dates(self):
        report = DemoAdapter().report("pnl", "2026-08")
        self.assertEqual(report.as_of_status, "confirmed")
        with self.assertRaises(ContractError):
            replace(report, as_of="2026-10-05T12:00:00")
        with self.assertRaises(ContractError):
            replace(report, received_at="2026-10-04T12:00:00+00:00")
        unknown = replace(report, as_of=None, as_of_status="unknown", received_at="2026-10-05T12:00:00+00:00")
        self.assertIsNone(unknown.as_of)
        for changes in ({"as_of": unknown.received_at}, {"received_at": None}, {"received_at": "2026-10-05T12:00:00"}):
            with self.assertRaises(ContractError):
                replace(unknown, **changes)

    def test_csv_xlsx_new_dates_and_check_warning(self):
        self.unknown_dates(); make_xlsx(self.base)
        for name in ("csv.json", "xlsx.json"):
            report = load_adapter(self.base / name).report("pnl", "2025-04")
            self.assertIsNone(report.as_of)
            self.assertEqual(report.as_of_status, "unknown")
            self.assertIn("TZ не указана", report.source_time_raw)
            self.assertEqual(report.received_at, "2025-05-08T12:00:00+03:00")
            result = subprocess.run([sys.executable, "-m", "finance_mcp.server", "--config", str(self.base / name), "--check"], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("2025-04/pnl", json.loads(result.stdout)["warnings"][0])

    def test_file_registry_preserves_money_and_resolves_renamed_label(self):
        assert_adapter(self, load_adapter(self.base / "csv-registry.json"))
        path = self.base / "articles.json"
        registry = json.loads(path.read_text())
        entry = next(e for e in registry["entries"] if (e["kind"], e["code"]) == ("pnl", "revenue"))
        entry["labels"].append("Ремонт"); entry["source_ids"] = ["erp:17"]
        path.write_text(json.dumps(registry))
        config = json.loads((self.base / "csv-registry.json").read_text())
        columns = config["tables"]["rows"]["columns"]; columns["source_id"] = "ID"
        rows_path = self.base / "rows.csv"
        with rows_path.open(newline="") as f:
            reader = csv.DictReader(f, delimiter=";"); rows = list(reader); fields = reader.fieldnames
        for row in rows:
            row["ID"] = ""
        selected = next(r for r in rows if r["Отчёт"] == "pnl" and r["Код"] == "revenue")
        selected.update({"Код": "", "Статья": "Ремонт", "ID": "erp:17"})
        with rows_path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields + ["ID"], delimiter=";"); w.writeheader(); w.writerows(rows)
        (self.base / "csv-registry.json").write_text(json.dumps(config))
        adapter = load_adapter(self.base / "csv-registry.json")
        renamed = next(r for r in adapter.report("pnl", "2025-03").rows if r.label == "Ремонт")
        self.assertEqual(renamed.code, "revenue")
        self.assertEqual(str(renamed.amount), "8743.21")
        self.assertEqual(sum(len(adapter.report(kind, month).rows) for month, kind in ORACLE), 40)

    def test_unknown_dates_reach_real_mcp(self):
        self.unknown_dates()
        async def run():
            from mcp import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client
            args = ["-m", "finance_mcp.server", "--config", str(self.base / "csv.json")]
            async with stdio_client(StdioServerParameters(command=sys.executable, args=args)) as (reader, writer):
                async with ClientSession(reader, writer) as client:
                    await client.initialize()
                    result = await client.call_tool("get_pnl", {"months": ["2025-04"]})
                    self.assertFalse(result.is_error)
                    report, = json.loads(result.content[0].text)["reports"]
                    self.assertIsNone(report["as_of"])
                    self.assertEqual(report["as_of_status"], "unknown")
                    self.assertIsNotNone(report["received_at"])
        asyncio.run(asyncio.wait_for(run(), timeout=20))
