"""Build a private local Codex test kit with a registered Desktop public client, never a user grant."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

from . import __version__
from .contract import ContractError
from .google_oauth import app_config


def build(destination, app, *, source=None):
    from .plugin_package import public_source_files
    source = Path(source or Path(__file__).resolve().parents[2])
    destination = Path(destination).absolute()
    if destination.exists() or Path(str(destination) + ".zip").exists():
        raise ContractError("Комплект уже существует; выберите новую папку")
    config = app_config(app)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir()
    try:
        target = destination / "source"
        for path in public_source_files(source):
            # The registered client is injected below; never copy an ignored app file implicitly.
            if path.name == "google-oauth-client.json":
                continue
            rel = path.relative_to(source)
            out = target / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, out)
        oauth = target / "src/finance_mcp/resources/google-oauth-client.json"
        oauth.write_text(json.dumps(config, indent=2), encoding="utf-8")
        (destination / "install.py").write_text(
            'from pathlib import Path\nimport runpy\n'
            'runpy.run_path(str(Path(__file__).resolve().parent / "source/scripts/install_local.py"), run_name="__main__")\n',
            encoding="utf-8")
        (destination / "Start.cmd").write_text(
            '@echo off\r\nsetlocal\r\nchcp 65001 >nul\r\n'
            'where py >nul 2>nul\r\nif errorlevel 1 (\r\n'
            '  python "%~dp0install.py" %*\r\n) else (\r\n'
            '  py -3 "%~dp0install.py" %*\r\n)\r\n'
            'if errorlevel 1 echo Настройка не завершена. Сохраните текст ошибки для агента.\r\npause\r\n',
            encoding="utf-8")
        (destination / "Начать.txt").write_text(
            "Финагент — локальный тест Google Sheets в Codex\n\n"
            "Нужны установленный Codex и Python 3.11+. Распакуйте ZIP целиком.\n"
            "Откройте Start.cmd. Комплект создаст отдельное окружение, предложит вход Google,\n"
            "попросит ссылку на вашу таблицу с финансовыми данными и сохранит подключение Codex.\n"
            "Поддержаны месячные отчёты ДДС/ОПиУ и остатки со счётом и показателем.\n"
            "Итоговые колонки не входят в выбранный месяц; исходная структура сохраняется.\n"
            "Копия, отдельная тестовая книга и изменение ячеек не нужны.\n"
            "После настройки перезапустите Codex и попросите разобрать финансовый отчёт.\n"
            "Используйте тот Google-аккаунт, который добавлен в закрытый тест приложения.\n"
            "Если Google отказывает или показывает непроверенное приложение — остановитесь\n"
            "и передайте сообщение автору; обходить предупреждения комплект не просит.\n"
            "Пароли, коды и токены никому не отправляйте. Таблица только читается.\n"
            "Этот комплект не содержит разрешение автора или его финансовую книгу.\n"
            "Старую тестовую установку сохраняйте отдельно. Публичная Google-проверка впереди.\n",
            encoding="utf-8")
        files = {p.relative_to(target).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(target.rglob("*")) if p.is_file()}
        digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        manifest = {"version": __version__, "source_sha256": digest, "files": files,
                    "google_app": "Desktop public client", "user_grants_included": False}
        (destination / "bundle.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        archive = Path(str(destination) + ".zip")
        with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED) as z:
            for path in sorted(destination.rglob("*")):
                if path.is_file():
                    z.write(path, str(Path(destination.name) / path.relative_to(destination)))
        return archive
    except Exception:
        # Remove only this newly created unpublished build tree.
        shutil.rmtree(destination)
        raise


def main():
    p = argparse.ArgumentParser(description="Собрать закрытый локальный комплект Google/Codex")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--app", type=Path, required=True)
    args = p.parse_args()
    try:
        print(build(args.output, args.app))
    except (ContractError, OSError):
        p.exit(2, "Не удалось собрать комплект: проверьте Desktop app и новую папку вывода\n")


if __name__ == "__main__":
    main()
