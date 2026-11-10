"""Validate one complete client export before exposing an immutable adapter snapshot."""
from datetime import datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
import re

from .contract import ContractError, Period, Report, Row, REPORTS, validate_month

FIELDS = {
    "periods": ("client_id", "month", "closed"),
    "reports": ("client_id", "month", "kind", "currency", "source", "as_of", "complete"),
    "rows": ("client_id", "month", "kind", "code", "label", "dimension", "amount", "status"),
    "notes": ("client_id", "month", "note"),
}
OPTIONAL_FIELDS = {"reports": ("received_at", "as_of_status", "source_time_raw"), "rows": ("source_id",)}


def text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"Нужно непустое текстовое поле {field}")
    return value.strip()


def boolean(value):
    if type(value) is bool:
        return value
    if value in ("true", "false"):
        return value == "true"
    raise ContractError("Логическое значение должно быть true или false")


def money(value, separator):
    if isinstance(value, bool) or value is None:
        raise ContractError("У value должна быть денежная сумма")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, (int, float)):
        # Excel stores numbers, not Python Decimal. Convert each cell before any arithmetic.
        result = Decimal(str(value))
    elif isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]+(?:" + re.escape(separator) + r"[0-9]+)?", value.strip()):
        try:
            result = Decimal(value.strip().replace(separator, "."))
        except InvalidOperation:
            raise ContractError("Некорректная сумма") from None
    else:
        raise ContractError("Некорректная сумма или десятичный разделитель")
    if not result.is_finite():
        raise ContractError("Сумма должна быть конечной")
    return result


class SnapshotAdapter:
    """One read-only, validated snapshot; replace the instance to refresh data."""

    def __init__(self, tables, passport, client_id, decimal_separator=".", registry=None):
        if decimal_separator not in (".", ","):
            raise ContractError("Десятичный разделитель должен быть точкой или запятой")
        self._passport = text(passport, "passport")
        client_id = text(client_id, "client_id")
        if set(tables) != set(FIELDS):
            raise ContractError("Нужны таблицы periods, reports, rows, notes")
        # Check every row, even if it belongs to an unavailable period/report.
        for name, fields in FIELDS.items():
            for record in tables[name]:
                if not set(fields) <= set(record) or set(record) - set(fields) - set(OPTIONAL_FIELDS.get(name, ())):
                    raise ContractError(f"Некорректный состав полей таблицы {name}")
                if text(record["client_id"], "client_id") != client_id:
                    raise ContractError("В снимке есть данные другого клиента")

        periods = {}
        for record in tables["periods"]:
            month = text(record["month"], "month")
            validate_month(month)
            if month in periods:
                raise ContractError("Повторный период")
            periods[month] = boolean(record["closed"])
        if not periods:
            raise ContractError("Пустой список периодов")

        metadata = {}
        for record in tables["reports"]:
            key = self._key(record)
            if key[0] not in periods:
                raise ContractError("Отчёт ссылается на неизвестный период")
            if key in metadata:
                raise ContractError("Повторный отчёт")
            metadata[key] = record

        rows = {key: [] for key in metadata}
        for record in tables["rows"]:
            key = self._key(record)
            if key not in metadata:
                raise ContractError("Строка без записи в реестре отчётов")
            status = text(record["status"], "status")
            raw = record["amount"]
            if status == "value":
                amount = money(raw, decimal_separator)
            else:
                if raw not in (None, ""):
                    raise ContractError("У missing/not_applicable не должно быть суммы")
                amount = None
            label = text(record["label"], "label")
            code = (registry.resolve(key[1], record["code"], label, record.get("source_id")) if registry
                    else text(record["code"], "code"))
            if not registry and record.get("source_id") not in (None, ""):
                raise ContractError("Для source_id требуется согласованный реестр статей")
            rows[key].append(Row(code, label,
                                 text(record["dimension"], "dimension"), amount, status))

        reports = {}
        for (month, kind), record in metadata.items():
            def optional_time(field):
                value = record.get(field)
                if isinstance(value, datetime):
                    return value.isoformat()
                return None if value in (None, "") else text(value, field)
            try:
                reports[month, kind] = Report(kind, month, text(record["currency"], "currency"),
                                              text(record["source"], "source"), optional_time("as_of"),
                                              boolean(record["complete"]), tuple(rows[month, kind]),
                                              received_at=optional_time("received_at"),
                                              as_of_status=record.get("as_of_status", "confirmed"),
                                              source_time_raw=optional_time("source_time_raw"))
            except (ValueError, TypeError):
                raise ContractError("Некорректный отчёт: проверьте строки, валюту, полноту и дату среза") from None
        notes = {}
        for record in tables["notes"]:
            month = text(record["month"], "month")
            if month not in periods or month in notes:
                raise ContractError("Неизвестный или повторный период примечания")
            notes[month] = text(record["note"], "note")

        self._periods = tuple(Period(m, closed, tuple(sorted(k for mm, k in reports if mm == m)))
                              for m, closed in sorted(periods.items()))
        self._reports = MappingProxyType(reports)
        self._notes = MappingProxyType(notes)

    @staticmethod
    def _key(record):
        month = text(record["month"], "month")
        validate_month(month)
        kind = text(record["kind"], "kind")
        if kind not in REPORTS:
            raise ContractError("Неизвестный тип отчёта")
        return month, kind

    def passport(self):
        return self._passport

    def periods(self):
        return self._periods

    def report(self, kind, month):
        if not any(p.month == month and p.closed for p in self._periods):
            raise ContractError("Нет закрытого периода")
        try:
            return self._reports[month, kind]
        except KeyError:
            raise ContractError("Отчёт недоступен") from None

    def notes(self, month):
        if not any(p.month == month and p.closed for p in self._periods):
            raise ContractError("Нет закрытого периода")
        return self._notes.get(month, "Примечаний нет.")
