"""Trusted installer configuration: source paths/column maps, never arbitrary SQL."""
import json
from pathlib import Path

from .contract import ContractError
from .snapshot import FIELDS, OPTIONAL_FIELDS, text


def column_map(table, spec):
    fields = FIELDS[table]
    columns = spec.get("columns", {f: f for f in fields})
    if not isinstance(columns, dict) or not set(fields) <= set(columns) or set(columns) - set(fields) - set(OPTIONAL_FIELDS.get(table, ())):
        raise ContractError(f"Карта колонок {table} должна содержать все поля контракта")
    if any(not isinstance(v, str) or not v.strip() for v in columns.values()):
        raise ContractError("Некорректное имя колонки")
    if len(set(columns.values())) != len(columns):
        raise ContractError("Повторная колонка в карте")
    return columns


def read_config(path):
    path = Path(path).resolve()
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ContractError("Не удалось прочитать JSON-конфигурацию") from None
    if not isinstance(config, dict) or type(config.get("version")) is not int or config.get("version") != 1:
        raise ContractError("Нужна конфигурация version=1")
    return config, path.parent


def validate_config(config):
    allowed = {"version", "adapter", "client_id", "passport", "decimal_separator", "max_rows", "tables", "schema", "dsn_env", "article_registry"}
    if set(config) - allowed:
        raise ContractError("Неизвестное поле конфигурации; SQL и DSN задавать в JSON нельзя")
    if type(config.get("max_rows", 100000)) is not int or not 1 <= config.get("max_rows", 100000) <= 1000000:
        raise ContractError("max_rows должен быть от 1 до 1000000")
    text(config.get("client_id"), "client_id")
    if not isinstance(config.get("tables"), dict) or set(config["tables"]) != set(FIELDS):
        raise ContractError("Нужно описать все четыре таблицы экспорта")
    for name, spec in config["tables"].items():
        if not isinstance(spec, dict):
            raise ContractError("Описание таблицы должно быть объектом")
        if set(spec) - {"file", "sheet", "delimiter", "columns", "relation"}:
            raise ContractError("Неизвестное поле таблицы; разрешены только пути, имена и карта колонок")
        column_map(name, spec)


def read_passport(config, base):
    try:
        return text((base / text(config.get("passport"), "passport")).read_text(encoding="utf-8"), "passport")
    except OSError:
        raise ContractError("Не удалось прочитать паспорт") from None


def read_registry(config, base):
    if "article_registry" not in config:
        return None
    from .registry import ArticleRegistry
    return ArticleRegistry.read(base / text(config["article_registry"], "article_registry"),
                                text(config["client_id"], "client_id"))


def load_adapter(path):
    config, base = read_config(path)
    validate_config(config)
    if config.get("adapter") in ("csv", "xlsx"):
        from .file_adapter import FileAdapter
        return FileAdapter(config, base)
    if config.get("adapter") == "postgres":
        from .postgres_adapter import PostgresAdapter
        return PostgresAdapter(config, base)
    raise ContractError("Неизвестный адаптер: нужны csv, xlsx или postgres")
