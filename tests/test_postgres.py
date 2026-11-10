"""Real isolated PostgreSQL rehearsal; opt in with FINANCE_MCP_TEST_PG_BINDIR (no existing DSN)."""
import asyncio
from copy import deepcopy
import csv
from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from finance_mcp.contract import ContractError
from finance_mcp.sources import load_adapter
from test_sources import EXAMPLE, assert_adapter, protocol_check


@unittest.skipUnless(os.environ.get("FINANCE_MCP_TEST_PG_BINDIR"),
                     "Set FINANCE_MCP_TEST_PG_BINDIR to run the real temporary PostgreSQL rehearsal")
class PostgresSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg import sql
        cls.temp = tempfile.TemporaryDirectory(prefix="t559-pg-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.base = Path(cls.temp.name)
        cls.bin = Path(os.environ["FINANCE_MCP_TEST_PG_BINDIR"])
        from finance_mcp.diagnostics import preflight
        check = preflight(postgres_bin=cls.bin, postgres_share=os.environ.get("FINANCE_MCP_TEST_PG_SHAREDIR"))
        if not check["ok"]:
            raise RuntimeError("PostgreSQL prerequisite check failed: " + json.dumps(check))
        cls.data = cls.base / "db"
        cmd = [str(cls.bin / "initdb"), "-D", str(cls.data), "--auth=trust", "--no-locale", "--encoding=UTF8", "--username=kit_admin"]
        share = os.environ.get("FINANCE_MCP_TEST_PG_SHAREDIR")
        if share:
            cmd += ["-L", share]
        subprocess.run(cmd, check=True, capture_output=True, timeout=30)
        def stop():
            subprocess.run([str(cls.bin / "pg_ctl"), "-D", str(cls.data), "-m", "immediate", "-w", "stop"],
                           capture_output=True, timeout=20)
        cls.addClassCleanup(stop)
        subprocess.run([str(cls.bin / "pg_ctl"), "-D", str(cls.data), "-l", str(cls.base / "postgres.log"),
                        "-o", f"-k {cls.base} -h '' -p 55459", "-w", "start"],
                       check=True, capture_output=True, timeout=30)
        cls.admin = f"host={cls.base} port=55459 user=kit_admin dbname=postgres"
        cls.reader = f"host={cls.base} port=55459 user=kit_reader dbname=postgres"
        cls.fixture = cls.base / "fixture"
        shutil.copytree(EXAMPLE, cls.fixture)
        config = json.loads((EXAMPLE / "csv.json").read_text())
        with psycopg.connect(cls.admin, autocommit=True) as conn:
            conn.execute("CREATE ROLE kit_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT")
            conn.execute("CREATE SCHEMA raw_data")
            conn.execute("CREATE SCHEMA finance_export")
            for name, spec in config["tables"].items():
                with (EXAMPLE / spec["file"]).open(newline="") as f:
                    source = list(csv.DictReader(f, delimiter=spec.get("delimiter", ",")))
                columns = spec.get("columns", {field: field for field in source[0]})
                fields = list(columns)
                types = {"closed": "boolean", "complete": "boolean", "amount": "numeric(16,2)", "as_of": "timestamptz"}
                definitions = sql.SQL(", ").join(sql.SQL("{} {}").format(sql.Identifier(field), sql.SQL(types.get(field, "text"))) for field in fields)
                conn.execute(sql.SQL("CREATE TABLE raw_data.{} ({})").format(sql.Identifier(name), definitions))
                for record in source:
                    values = []
                    for field in fields:
                        value = record[columns[field]]
                        if field in ("closed", "complete"):
                            value = value == "true"
                        elif field == "amount":
                            value = Decimal(value.replace(",", ".")) if value else None
                        elif field == "as_of":
                            value = datetime.fromisoformat(value)
                        values.append(value)
                    query = sql.SQL("INSERT INTO raw_data.{} ({}) VALUES ({})").format(
                        sql.Identifier(name), sql.SQL(", ").join(map(sql.Identifier, fields)),
                        sql.SQL(", ").join(sql.Placeholder() for _ in fields))
                    conn.execute(query, values)
                    other = list(values)
                    other[fields.index("client_id")] = "other"
                    conn.execute(query, other)
                conn.execute(sql.SQL("CREATE VIEW finance_export.{} WITH (security_barrier=true) AS SELECT * FROM raw_data.{} WHERE client_id = 'lipa'").format(sql.Identifier(name), sql.Identifier(name)))
            conn.execute("GRANT USAGE ON SCHEMA finance_export TO kit_reader")
            conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA finance_export TO kit_reader")
            conn.execute("CREATE VIEW finance_export.reports_probe AS SELECT client_id,month,kind,currency,"
                         "'ro=' || current_setting('transaction_read_only') || ';iso=' || current_setting('transaction_isolation') || ';timeout=' || current_setting('statement_timeout') AS source,"
                         "as_of,complete FROM finance_export.reports")
            conn.execute("GRANT SELECT ON finance_export.reports_probe TO kit_reader")

    def load(self, changes=None):
        config = json.loads((self.fixture / "postgres.json").read_text())
        if changes:
            changes(config)
        path = self.base / "config.json"
        config["passport"] = str(self.fixture / "passport.md")
        path.write_text(json.dumps(config))
        with patch.dict(os.environ, {"FINANCE_MCP_DSN": self.reader}):
            return load_adapter(path)

    def test_postgres_all_40_rows_repeat_and_read_only_settings(self):
        for _ in range(2):
            assert_adapter(self, self.load())
        probe = self.load(lambda c: c["tables"]["reports"].update(relation="reports_probe"))
        report = probe.report("pnl", "2025-03")
        self.assertEqual(report.source, "ro=on;iso=repeatable read;timeout=10s")

    def test_real_role_permissions_other_client_and_injection(self):
        import psycopg
        for statement in ("SELECT * FROM raw_data.rows", "DELETE FROM finance_export.rows", "CREATE TABLE finance_export.nope(x int)"):
            with self.subTest(statement=statement), psycopg.connect(self.reader) as conn:
                with self.assertRaises(psycopg.Error):
                    conn.execute(statement)
                conn.rollback()
        with psycopg.connect(self.reader) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM finance_export.rows WHERE client_id='other'").fetchone()[0], 0)
        with self.assertRaises(ContractError):
            self.load(lambda c: c.update(client_id="other"))
        with self.assertRaises(ContractError):
            self.load(lambda c: c.update(client_id="lipa' OR true --"))
        with self.assertRaises(ContractError):
            self.load(lambda c: c["tables"]["rows"].update(relation="rows; DROP SCHEMA raw_data CASCADE"))
        assert_adapter(self, self.load())

    def test_postgres_mapped_columns(self):
        import psycopg
        with psycopg.connect(self.admin, autocommit=True) as conn:
            conn.execute("CREATE OR REPLACE VIEW finance_export.periods_mapped AS SELECT client_id AS customer, month AS period, closed AS is_closed FROM finance_export.periods")
            conn.execute("GRANT SELECT ON finance_export.periods_mapped TO kit_reader")
        assert_adapter(self, self.load(lambda c: c["tables"]["periods"].update(relation="periods_mapped", columns={"client_id": "customer", "month": "period", "closed": "is_closed"})))

    def test_postgres_unknown_dates_and_registry(self):
        import psycopg
        from finance_mcp.snapshot import FIELDS, OPTIONAL_FIELDS
        with psycopg.connect(self.admin, autocommit=True) as conn:
            conn.execute("CREATE VIEW finance_export.reports_dates AS SELECT client_id,month,kind,currency,source,"
                         "CASE WHEN month='2025-04' AND kind='pnl' THEN NULL ELSE as_of END AS as_of,complete,"
                         "'2025-05-08T12:00:00+03:00'::timestamptz AS received_at,"
                         "CASE WHEN month='2025-04' AND kind='pnl' THEN 'unknown' ELSE 'confirmed' END AS as_of_status,"
                         "'07.05.2025 10:00, TZ unknown'::text AS source_time_raw FROM finance_export.reports")
            conn.execute("GRANT SELECT ON finance_export.reports_dates TO kit_reader")
        def configure(config):
            config["tables"]["reports"] = {"relation": "reports_dates", "columns":
                {name: name for name in FIELDS["reports"] + OPTIONAL_FIELDS["reports"]}}
            config["article_registry"] = str(self.fixture / "articles.json")
        adapter = self.load(configure)
        report = adapter.report("pnl", "2025-04")
        self.assertIsNone(report.as_of)
        self.assertEqual(report.as_of_status, "unknown")
        self.assertIsNotNone(report.received_at)
        self.assertIn("TZ unknown", report.source_time_raw)
        self.assertEqual(len(report.rows), 8)
        self.assertIsNotNone(adapter.report("pnl", "2025-03").as_of)

    def test_db_failure_is_sanitized(self):
        config = json.loads((self.fixture / "postgres.json").read_text())
        path = self.fixture / "unavailable.json"
        path.write_text(json.dumps(config))
        secret = "TASK559_SECRET_MARKER"
        with patch.dict(os.environ, {"FINANCE_MCP_DSN": f"host=/no/such/task559/socket password={secret} user=nobody"}):
            with self.assertRaises(ContractError) as caught:
                load_adapter(path)
            result = subprocess.run([sys.executable, "-m", "finance_mcp.server", "--config", str(path), "--check"],
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertNotIn(secret, result.stderr)
            self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn(secret, str(caught.exception))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ContractError):
                load_adapter(path)
        with self.assertRaises(ContractError):
            self.load(lambda c: c.update(max_rows=1))

    def test_postgres_real_mcp_and_source_unchanged(self):
        import psycopg
        def fingerprint():
            with psycopg.connect(self.admin) as conn:
                return conn.execute("SELECT md5(string_agg(row_to_json(r)::text, '' ORDER BY row_to_json(r)::text)) FROM raw_data.rows r").fetchone()[0]
        before = fingerprint()
        env = dict(os.environ, FINANCE_MCP_DSN=self.reader)
        asyncio.run(asyncio.wait_for(protocol_check(self, self.fixture / "postgres.json", env), timeout=45))
        self.assertEqual(before, fingerprint())

    def test_new_snapshot_detects_db_duplicates(self):
        import psycopg
        adapter = self.load()
        with psycopg.connect(self.admin, autocommit=True) as conn:
            conn.execute("INSERT INTO raw_data.notes VALUES ('lipa','2025-03','duplicate')")
        try:
            with self.assertRaises(ContractError):
                self.load()
            assert_adapter(self, adapter)
        finally:
            with psycopg.connect(self.admin, autocommit=True) as conn:
                conn.execute("DELETE FROM raw_data.notes WHERE note='duplicate'")


if __name__ == "__main__":
    unittest.main()
