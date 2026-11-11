"""Finish local Google onboarding with a real stdio report and safe Codex registration."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib

from .contract import ContractError
from .diagnostics import probe_stdio
from .google_connect import main as connect, render_analysis, KINDS


def find_codex():
    options = [shutil.which("codex")]
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        folder = Path(os.environ["LOCALAPPDATA"]) / "OpenAI" / "Codex" / "bin"
        options += [str(p) for p in sorted(folder.glob("*/codex.exe"),
                                          key=lambda p: p.stat().st_mtime, reverse=True)]
    for value in options:
        if value and Path(value).is_file() and (os.name != "nt" or Path(value).suffix.lower() == ".exe"):
            return str(Path(value).absolute())
    raise ContractError("Codex CLI не найден. Установите официальный Codex или укажите путь к codex.exe.")


def cli(command, *, runner=None):
    try:
        result = (runner or subprocess.run)(command, capture_output=True, text=True,
                                             encoding="utf-8", timeout=30, check=False)
        if result.returncode:
            raise ContractError("Codex не подтвердил настройку MCP; прежние подключения не удаляйте")
        return result.stdout
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        raise ContractError("Не удалось запустить Codex CLI; проверьте установленное приложение") from None


def config_path():
    # Respect the caller's existing Codex profile; never change HOME/CODEX_HOME.
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"


def register_codex(config, *, name, executable, codex=None, runner=None, settings=None):
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
        raise ContractError("Некорректное имя подключения Codex")
    config = Path(config).absolute()
    executable = Path(executable).absolute()
    if not config.is_file() or not executable.is_file():
        raise ContractError("Файлы установленного MCP не найдены")
    codex = codex or find_codex()
    wanted = {"type": "stdio", "command": str(executable),
              "args": ["-X", "utf8", "-m", "finance_mcp.server", "--config", str(config)]}
    try:
        before = json.loads(cli([codex, "mcp", "list", "--json"], runner=runner))
        if not isinstance(before, list) or any(not isinstance(s, dict) or not isinstance(s.get("name"), str) for s in before):
            raise ValueError
    except (ValueError, TypeError):
        raise ContractError("Не удалось прочитать список MCP из Codex") from None
    existing = next((s for s in before if s["name"] == name), None)
    if existing:
        transport = existing.get("transport", {})
        if (existing.get("enabled") is False or any(transport.get(k) != v for k, v in wanted.items())
                or transport.get("env") or transport.get("env_vars") or transport.get("cwd")):
            raise ContractError("Такое имя MCP уже занято другим подключением; выберите новый профиль")
        # An interrupted previous registration may have saved the command but not its timeout.
        settings = Path(settings or config_path())
        try:
            import tomlkit
            current = settings.read_bytes()
            doc = tomlkit.parse(current.decode("utf-8"))
            own = doc["mcp_servers"][name]
            timeout = own.get("startup_timeout_sec", 10)
            if own["command"] != wanted["command"] or list(own["args"]) != wanted["args"]:
                raise ValueError
            if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
                raise ValueError
            if timeout < 90:
                backup = settings.with_name(settings.name + ".finagent-repair-" +
                    datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".bak")
                with backup.open("xb") as f:
                    f.write(current)
                os.chmod(backup, 0o600)
                own["startup_timeout_sec"] = 90
                with tempfile.NamedTemporaryFile(dir=settings.parent, prefix=".finagent-", delete=False) as f:
                    temporary = Path(f.name)
                    f.write(tomlkit.dumps(doc).encode("utf-8"))
                try:
                    if settings.read_bytes() != current:
                        raise ValueError
                    os.replace(temporary, settings)
                finally:
                    temporary.unlink(missing_ok=True)
            return {"status": "reused", "name": name, "ui_connected": False}
        except (OSError, ValueError, TypeError, KeyError, ImportError):
            raise ContractError("Существующее подключение Codex не полностью проверено; настройки не заменяйте вручную") from None

    settings = Path(settings or config_path())
    original = settings.read_bytes() if settings.exists() else None
    # Preserve an exact local backup; never include this private file in a bundle.
    backup = None
    if original is not None:
        backup = settings.with_name(settings.name + ".finagent-" +
                                    datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".bak")
        with backup.open("xb") as f:
            f.write(original)
        os.chmod(backup, 0o600)
    cli([codex, "mcp", "add", name, "--", wanted["command"], *wanted["args"]], runner=runner)
    # Verify the CLI's saved transport, then set only this new server's startup timeout.
    try:
        after = json.loads(cli([codex, "mcp", "get", name, "--json"], runner=runner))
        if after.get("enabled") is False or any(after.get("transport", {}).get(k) != v for k, v in wanted.items()):
            raise ValueError
        import tomlkit
        current = settings.read_bytes()
        doc = tomlkit.parse(current.decode("utf-8"))
        raw_before = tomllib.loads(original.decode("utf-8")) if original is not None else {}
        raw_after = tomllib.loads(current.decode("utf-8"))
        baseline_servers = raw_before.get("mcp_servers", {})
        saved_servers = raw_after.get("mcp_servers", {})
        if any(saved_servers.get(k) != v for k, v in baseline_servers.items()):
            raise ValueError
        other_before = {k: v for k, v in raw_before.items() if k != "mcp_servers"}
        other_after = {k: v for k, v in raw_after.items() if k != "mcp_servers"}
        if other_before != other_after:
            raise ValueError
        # The Google adapter makes authenticated reads before initialize; the default 10s is too short.
        doc["mcp_servers"][name]["startup_timeout_sec"] = 90
        content = tomlkit.dumps(doc).encode("utf-8")
        with tempfile.NamedTemporaryFile(dir=settings.parent, prefix=".finagent-", delete=False) as f:
            temporary = Path(f.name)
            f.write(content)
        try:
            if settings.read_bytes() != current:
                raise ContractError("Настройки Codex изменились параллельно; повторите проверку подключения")
            os.replace(temporary, settings)
        finally:
            temporary.unlink(missing_ok=True)
        verified = tomllib.loads(settings.read_text(encoding="utf-8"))["mcp_servers"][name]
        if verified["command"] != wanted["command"] or verified["args"] != wanted["args"] or verified["startup_timeout_sec"] != 90:
            raise ValueError
    except ContractError:
        raise
    except (OSError, ValueError, TypeError, KeyError, ImportError):
        raise ContractError("Настройка Codex не полностью проверена; сохранённую резервную копию оставьте для диагностики") from None
    return {"status": "registered", "name": name, "ui_connected": False}


def finish(config, *, name, codex=None, runner=None, settings=None, probe=None):
    result = (probe or probe_stdio)(config=config, timeout=60, financial=True)
    if not result.get("ok") or not result.get("financial"):
        raise ContractError("MCP пока не подтвердил чтение отчёта через stdio; подключение Codex не изменено")
    report = result["financial"]
    registration = register_codex(config, name=name, executable=sys.executable,
                                  codex=codex, runner=runner, settings=settings)
    return {"status": "mcp_verified", "registration": registration,
            "report": report, "ui_connected": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Войти через Google и подключить финансовые отчёты к Codex")
    parser.add_argument("--profile", default="google")
    parser.add_argument("--url")
    parser.add_argument("--app")
    parser.add_argument("--home", type=Path)
    parser.add_argument("--codex")
    args = parser.parse_args(argv)
    try:
        codex = args.codex or find_codex()
        options = ["--profile", args.profile]
        for flag, value in (("--url", args.url), ("--app", args.app), ("--home", args.home)):
            if value is not None:
                options += [flag, str(value)]
        connection = connect(options, emit_result=False)
        # A unique name keeps the earlier 0.3.0/0.6.0 acceptance environment intact.
        name = "finagent-" + args.profile.lower().replace(".", "-")
        result = finish(connection["config"], name=name, codex=codex)
        report = result["report"]
        analysis = {"kind": report["kind"], "month": report["month"], "currency": report["currency"],
                    "rows": report["rows"], "observations": [
                        "Отчёт получен реальным вызовом MCP.",
                        "Полнота и методика не подтверждены; итоги и детали не складываются."]}
        print(render_analysis(analysis))
        print("Подключение сохранено в Codex. Перезапустите Codex и откройте новый чат.")
        print("Напишите: «Прочитай паспорт и доступные месяцы финансового MCP, затем разбери мой отчёт».")
        print("Проверка именно в интерфейсе Codex ещё требуется.")
        return result
    except (ContractError, OSError) as exc:
        message = str(exc) if isinstance(exc, ContractError) else "Не удалось завершить локальную настройку"
        parser.exit(2, message + "\n")


if __name__ == "__main__":
    main()
