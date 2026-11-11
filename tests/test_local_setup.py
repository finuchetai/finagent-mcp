"""Local setup contracts: real TOML state, real stdio, synthetic Codex/Google boundaries."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import tomllib
import unittest
from unittest.mock import patch

from finance_mcp.contract import ContractError
from finance_mcp.diagnostics import probe_stdio
from finance_mcp.local_bundle import build
from finance_mcp.local_setup import register_codex, finish

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bootstrap", ROOT / "scripts/install_local.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class CodexCLI:
    def __init__(self, settings):
        self.settings, self.calls = settings, []
    def __call__(self, command, **kwargs):
        self.calls.append(command)
        raw = tomllib.loads(self.settings.read_text()) if self.settings.exists() else {}
        servers = raw.get("mcp_servers", {})
        def response(name, value):
            return {"name": name, "enabled": value.get("enabled", True),
                    "transport": {"type": "stdio", "command": value["command"],
                                  "args": value.get("args", [])}}
        if command[2] == "list":
            value = [response(k, v) for k, v in servers.items()]
        elif command[2] == "get":
            value = response(command[3], servers[command[3]])
        elif command[2] == "add":
            import tomlkit
            doc = tomlkit.parse(self.settings.read_text()) if self.settings.exists() else tomlkit.document()
            doc.setdefault("mcp_servers", {})[command[3]] = {"command": command[5], "args": command[6:]}
            self.settings.parent.mkdir(parents=True, exist_ok=True)
            self.settings.write_text(tomlkit.dumps(doc))
            value = {}
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(value), "")


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.config = self.base / "client" / "config.json"
        self.config.parent.mkdir()
        self.config.write_text("{}")
        self.settings = self.base / "codex" / "config.toml"
        self.settings.parent.mkdir()
        self.original = '# user comment\nmodel = "chosen"\n[mcp_servers.old-test]\ncommand = "old-python"\nargs = ["keep"]\n'
        self.settings.write_text(self.original)
        self.cli = CodexCLI(self.settings)
    def register(self, **kwargs):
        return register_codex(self.config, name="finagent-test", executable=sys.executable,
                              codex="synthetic-codex", runner=self.cli, settings=self.settings, **kwargs)
    def test_registration_preserves_prior_config_backup_and_repeats(self):
        result = self.register()
        self.assertEqual(result["status"], "registered")
        self.assertFalse(result["ui_connected"])
        after = tomllib.loads(self.settings.read_text())
        self.assertEqual(after["model"], "chosen")
        self.assertEqual(after["mcp_servers"]["old-test"], {"command": "old-python", "args": ["keep"]})
        self.assertIn("# user comment", self.settings.read_text())
        self.assertEqual(after["mcp_servers"]["finagent-test"]["startup_timeout_sec"], 90)
        self.assertEqual(after["mcp_servers"]["finagent-test"]["args"][-1], str(self.config))
        backups = list(self.settings.parent.glob("*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), self.original)
        added = sum(c[2] == "add" for c in self.cli.calls)
        self.assertEqual(self.register()["status"], "reused")
        self.assertEqual(sum(c[2] == "add" for c in self.cli.calls), added)
    def test_repeat_repairs_interrupted_timeout_without_replacing_server(self):
        self.register()
        self.settings.write_text(self.settings.read_text().replace("startup_timeout_sec = 90", "startup_timeout_sec = 10"))
        before_adds = sum(c[2] == "add" for c in self.cli.calls)
        self.assertEqual(self.register()["status"], "reused")
        self.assertEqual(tomllib.loads(self.settings.read_text())["mcp_servers"]["finagent-test"]["startup_timeout_sec"], 90)
        self.assertEqual(sum(c[2] == "add" for c in self.cli.calls), before_adds)
    def test_collision_and_disabled_server_are_not_overwritten(self):
        self.register()
        def changed_command(source):
            import tomlkit
            doc = tomlkit.parse(source)
            doc["mcp_servers"]["finagent-test"]["command"] = "other-python"
            return tomlkit.dumps(doc)
        def disabled(source):
            import tomlkit
            doc = tomlkit.parse(source)
            doc["mcp_servers"]["finagent-test"]["enabled"] = False
            return tomlkit.dumps(doc)
        for mutation in [changed_command, disabled]:
            baseline = self.settings.read_text()
            self.settings.write_text(mutation(baseline))
            before = self.settings.read_bytes()
            with self.assertRaises(ContractError):
                self.register()
            self.assertEqual(before, self.settings.read_bytes())
            self.settings.write_text(baseline)
    def test_no_registration_when_stdio_fails(self):
        with self.assertRaisesRegex(ContractError, "stdio"):
            finish(self.config, name="finagent-test", codex="synthetic-codex", runner=self.cli,
                   settings=self.settings, probe=lambda **kwargs: {"ok": False})
        self.assertEqual(self.cli.calls, [])
        self.assertEqual(self.settings.read_text(), self.original)
    def test_finish_consumes_actual_financial_probe_and_reports_ui_pending(self):
        report = {"kind": "cashflow", "month": "2026-10", "rows": [{"amount": "123.45"}]}
        def probe(**kwargs):
            self.assertTrue(kwargs["financial"])
            self.assertEqual(kwargs["config"], self.config)
            return {"ok": True, "financial": report}
        result = finish(self.config, name="finagent-test", codex="synthetic-codex", runner=self.cli,
                        settings=self.settings, probe=probe)
        self.assertEqual(result["report"], report)
        self.assertFalse(result["ui_connected"])
    def test_real_stdio_returns_a_nonempty_financial_report(self):
        result = probe_stdio(config=ROOT / "examples/lipa/csv.json", timeout=30, financial=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["tools"], 6)
        self.assertEqual(len(result["financial"]["rows"]), 1)
        self.assertEqual(result["financial"]["rows"][0]["code"], "other")
        self.assertEqual(result["financial"]["rows"][0]["amount"], "0.00")
        self.assertEqual(result["financial"]["kind"], "cashflow")


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.app = self.base / "app.json"
        self.app.write_text(json.dumps({"installed": {
            "client_id": "synthetic-test.apps.googleusercontent.com",
            "client_secret": "public-desktop-placeholder",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token"},
            "refresh_token": "DO_NOT_PACKAGE"}))
    def bundle(self):
        destination = self.base / "kit"
        archive = build(destination, self.app, source=ROOT)
        return destination, archive
    def test_bundle_contains_only_app_config_and_verified_source(self):
        destination, archive = self.bundle()
        self.assertTrue(archive.is_file())
        manifest = json.loads((destination / "bundle.json").read_text())
        bootstrap.verify_source(destination / "source", manifest)
        app = json.loads((destination / "source/src/finance_mcp/resources/google-oauth-client.json").read_text())
        self.assertEqual(set(app), {"installed"})
        self.assertNotIn("DO_NOT_PACKAGE", json.dumps(app))
        self.assertFalse(manifest["user_grants_included"])
        self.assertTrue((destination / "Start.cmd").is_file())
        self.assertIn("finance_mcp/local_setup.py", " ".join(manifest["files"]))
        self.assertFalse(any(".venv" in p or ".git/" in p or "exports/" in p for p in manifest["files"]))
        with self.assertRaises(ContractError):
            build(destination, self.app, source=ROOT)
    def test_modified_source_is_rejected_before_installation(self):
        destination, _ = self.bundle()
        (destination / "source/README.md").write_text("tampered")
        with self.assertRaises(ValueError):
            bootstrap.install(destination, runtime_root=self.base / "runtime")
        self.assertFalse((self.base / "runtime").exists())
    def test_bootstrap_builds_isolated_copy_and_reuses_runtime(self):
        destination, _ = self.bundle()
        calls = []
        class Creator:
            def create(self, path):
                python = path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                python.parent.mkdir(parents=True)
                python.write_text("synthetic")
        def install(command, **kwargs):
            calls.append(command)
            # pip can mutate its source; the original verified kit must stay intact.
            path = Path(command[-1].removesuffix("[sheets,local]"))
            (path / "build").mkdir()
            (path / "build/generated").write_text("generated")
            self.assertNotEqual(path, destination / "source")
        python = bootstrap.install(destination, runtime_root=self.base / "runtime", creator=Creator(), runner=install)
        self.assertTrue(python.is_file())
        self.assertEqual(len(calls), 1)
        repeated = bootstrap.install(destination, runtime_root=self.base / "runtime", creator=Creator(), runner=install)
        self.assertEqual(repeated, python)
        self.assertEqual(len(calls), 1)
        bootstrap.verify_source(destination / "source", json.loads((destination / "bundle.json").read_text()))


if __name__ == "__main__":
    unittest.main()
