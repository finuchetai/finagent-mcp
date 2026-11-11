"""Dependency-free preflight and a bounded MCP initialize probe with process-group cleanup."""
import argparse
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path
import signal
import queue
import threading
import time
import subprocess
import sys
import tempfile
import urllib.request


def preflight(*, postgres_bin=None, postgres_share=None, packages=False, network=False):
    checks = []
    def add(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
    add("python", sys.version_info >= (3, 11), ".".join(map(str, sys.version_info[:3])))
    for name in ("venv", "ensurepip"):
        add(name, importlib.util.find_spec(name) is not None, "available" if importlib.util.find_spec(name) else "missing")
    try:
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "write-test").write_text("ok")
        add("temporary_directory", True, "writable")
    except OSError:
        add("temporary_directory", False, "not_writable")
    if packages:
        try:
            result = subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True, timeout=5)
            add("pip", result.returncode == 0, "runnable" if result.returncode == 0 else "missing")
        except (OSError, subprocess.TimeoutExpired):
            add("pip", False, "unusable")
        for distribution, module, expected in (("mcp", "mcp", "2.2.0"), ("openpyxl", "openpyxl", "3.1.5"), ("psycopg", "psycopg", "3.3.4")):
            try:
                version = importlib.metadata.version(distribution)
                result = subprocess.run([sys.executable, "-c", "import " + module], capture_output=True, timeout=8)
                add(distribution, version == expected and result.returncode == 0, version if result.returncode == 0 else "import_failed")
            except (importlib.metadata.PackageNotFoundError, OSError, subprocess.TimeoutExpired):
                add(distribution, False, "missing_or_unusable")
    if postgres_bin is not None:
        add("postgres_test_platform", os.name == "posix", "requires_Linux_or_WSL_Unix_socket_tests")
        add("postgres_test_user", not hasattr(os, "geteuid") or os.geteuid() != 0, "initdb_requires_non_root")
        for name in ("initdb", "pg_ctl", "postgres"):
            path = Path(postgres_bin) / name
            try:
                result = subprocess.run([str(path), "--version"], capture_output=True, timeout=5)
                add(name, result.returncode == 0, "runnable" if result.returncode == 0 else "not_runnable_check_runtime_libraries")
            except (OSError, subprocess.TimeoutExpired):
                add(name, False, "missing_not_executable_or_timeout")
        if postgres_share is not None:
            add("postgres_share", (Path(postgres_share) / "postgres.bki").is_file(), "postgres.bki_required")
    if network:
        try:
            with urllib.request.urlopen("https://pypi.org/simple/mcp/", timeout=5) as response:
                add("package_index", response.status == 200, "reachable")
        except (OSError, ValueError):
            add("package_index", False, "unreachable_check_network_permission_DNS_proxy")
    return {"ok": all(c["ok"] for c in checks), "checks": checks,
            "not_checked": [name for name, requested in (("installed_packages", packages), ("postgres_server", postgres_bin is not None), ("package_index", network)) if not requested]}


def _kill_tree(process):
    # Only the process group/session created by this probe may be terminated.
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=3)
        finally:
            if process.poll() is None:
                process.kill()


def probe_stdio(*, config=None, timeout=15, server_command=None, financial=False):
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 1 <= timeout <= 60:
        raise ValueError("timeout must be between 1 and 60 seconds")
    if server_command is None:
        server_command = [sys.executable, "-m", "finance_mcp.server"]
        if config:
            server_command += ["--config", str(Path(config).resolve())]
    # Own the server process group directly. The SDK's stdio transport creates a
    # separate kill scope, which would escape a watchdog around an SDK worker.
    process = subprocess.Popen(server_command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                               start_new_session=os.name == "posix")
    inbox = queue.Queue(maxsize=16)
    stop = threading.Event()
    def read():
        try:
            while not stop.is_set():
                line = process.stdout.readline(1048576)
                while not stop.is_set():
                    try:
                        inbox.put(line or None, timeout=.1)
                        break
                    except queue.Full:
                        continue
                if not line:
                    return
        except (OSError, ValueError):
            return
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    phase = "initialize"
    def send(message):
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
    def response(request_id):
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            try:
                line = inbox.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError from None
            if line is None:
                raise ValueError("server exited")
            message = json.loads(line)
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise ValueError("not JSON-RPC")
            if message.get("id") == request_id:
                if "error" in message or not isinstance(message.get("result"), dict):
                    raise ValueError("protocol error")
                return message["result"]
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "finance-mcp-doctor", "version": "0.3.0"}}})
        initialized = response(1)
        if not isinstance(initialized.get("protocolVersion"), str) or not isinstance(initialized.get("serverInfo"), dict):
            raise ValueError("invalid initialize response")
        phase = "list_tools"
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = response(2).get("tools")
        required = {"get_passport", "list_months", "get_pnl", "get_cashflow", "get_balances", "get_month_notes"}
        names = {t.get("name") for t in tools if isinstance(t, dict)} if isinstance(tools, list) else set()
        if names not in (required, required | {"refresh_source"}) or len(tools) != len(names):
            raise ValueError("unexpected tool set")
        result = {"ok": True, "phase": "complete", "tools": len(tools)}
        if financial:
            def call(request_id, name, arguments):
                send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                      "params": {"name": name, "arguments": arguments}})
                value = response(request_id)
                if value.get("isError") or not value.get("content"):
                    raise ValueError("financial tool failed")
                return value["content"][0]["text"]
            phase = "get_passport"
            if not call(3, "get_passport", {}).strip():
                raise ValueError("empty passport")
            phase = "list_months"
            periods = json.loads(call(4, "list_months", {}))["periods"]
            period = next((p for p in periods if p["closed"] and p["reports"]), None)
            if period is None:
                raise ValueError("no approved period")
            kind = period["reports"][0]
            if kind not in {"pnl", "cashflow", "balances"}:
                raise ValueError("invalid report")
            phase = "get_" + kind
            reports = json.loads(call(5, phase, {"months": [period["month"]]}))["reports"]
            if len(reports) != 1 or not reports[0]["rows"] or reports[0]["month"] != period["month"] or reports[0]["kind"] != kind:
                raise ValueError("invalid financial answer")
            result["financial"] = reports[0]
        return result
    except (TimeoutError, OSError, ValueError, KeyError, TypeError):
        timed_out = time.monotonic() >= deadline
        return {"ok": False, "phase": phase, "reason": "timeout" if timed_out else "process_error",
                "cause": "undetermined", "raw_stderr_included": False,
                "next_step": "Run --check separately; compare the unchanged package in an environment with permitted stdio/process execution. A timeout alone proves neither an adapter bug nor a sandbox cause."}
    finally:
        stop.set()
        _kill_tree(process)
        process.wait(timeout=2)
        reader.join(timeout=1)
        try:
            process.stdin.close()
        except OSError:
            pass
        if not reader.is_alive():
            process.stdout.close()


def main():
    parser = argparse.ArgumentParser(description="Read-only finance MCP environment and transport diagnostics")
    sub = parser.add_subparsers(dest="mode", required=True)
    env = sub.add_parser("environment")
    env.add_argument("--packages", action="store_true", help="Check all pinned optional packages after installation")
    env.add_argument("--network", action="store_true", help="Explicitly test access to PyPI")
    env.add_argument("--postgres-bin")
    env.add_argument("--postgres-share")
    stdio = sub.add_parser("stdio")
    stdio.add_argument("--config")
    stdio.add_argument("--financial", action="store_true")
    stdio.add_argument("--timeout", type=float, default=15)
    args = parser.parse_args()
    try:
        result = (preflight(postgres_bin=args.postgres_bin, postgres_share=args.postgres_share,
                            packages=args.packages, network=args.network) if args.mode == "environment"
                  else probe_stdio(config=args.config, timeout=args.timeout, financial=args.financial))
    except (ValueError, OSError):
        parser.exit(2, "Invalid diagnostic arguments or unavailable process execution\n")
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
