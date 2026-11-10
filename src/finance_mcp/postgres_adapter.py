"""Read an allowlisted PostgreSQL export in one bounded read-only repeatable-read snapshot."""
import os
from pathlib import Path
import re

from .contract import ContractError
from .snapshot import FIELDS, SnapshotAdapter, text
from .sources import column_map, read_passport, read_registry, validate_config


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,62}", value):
        raise ContractError("Недопустимый SQL-идентификатор")
    return value


class PostgresAdapter(SnapshotAdapter):
    def __init__(self, config, base=Path(".")):
        validate_config(config)
        try:
            import psycopg
            from psycopg import sql, IsolationLevel
        except ImportError:
            raise ContractError("Для PostgreSQL установите пакет с extra [postgres]") from None
        env = identifier(config.get("dsn_env", "FINANCE_MCP_DSN"))
        dsn = os.environ.get(env)
        if not dsn:
            raise ContractError("Не задана переменная окружения с DSN PostgreSQL")
        schema = identifier(config.get("schema"))
        client_id = text(config["client_id"], "client_id")
        passport = read_passport(config, Path(base))
        registry = read_registry(config, Path(base))
        tables = {}
        try:
            with psycopg.connect(dsn, connect_timeout=5, options="-c statement_timeout=10000 -c lock_timeout=2000") as conn:
                conn.read_only = True
                conn.isolation_level = IsolationLevel.REPEATABLE_READ
                for name, spec in config["tables"].items():
                    columns = column_map(name, spec)
                    columns = {key: identifier(value) for key, value in columns.items()}
                    relation = identifier(spec.get("relation"))
                    query = sql.SQL("SELECT {} FROM {}.{} WHERE {} = %s LIMIT %s").format(
                        sql.SQL(", ").join(sql.Identifier(column) for column in columns.values()),
                        sql.Identifier(schema), sql.Identifier(relation), sql.Identifier(columns["client_id"]))
                    limit = config.get("max_rows", 100000)
                    data = conn.execute(query, (client_id, limit + 1)).fetchall()
                    if len(data) > limit:
                        raise ContractError("Превышен max_rows")
                    tables[name] = [dict(zip(columns, row)) for row in data]
        except (psycopg.Error, ValueError):
            # Neither DSN nor raw database errors may enter stdout/logs/tool replies.
            raise ContractError("Не удалось прочитать PostgreSQL: проверьте подключение, права и формат экспорта") from None
        super().__init__(tables, passport, client_id, config.get("decimal_separator", "."), registry=registry)
