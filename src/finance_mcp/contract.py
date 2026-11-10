"""SDK-independent v1 adapter contract and validation at the publication boundary."""
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import re
from typing import Literal, Protocol

ReportKind = Literal["pnl", "cashflow", "balances"]
ValueStatus = Literal["value", "missing", "not_applicable"]
REPORTS = frozenset({"pnl", "cashflow", "balances"})
CONTRACT_VERSION = "1.1"


def timestamp(value, field):
    try:
        if not isinstance(value, str):
            raise ValueError
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError
        return stamp
    except ValueError:
        raise ContractError(f"{field}: нужна ISO-дата с часовым поясом") from None


class ContractError(ValueError):
    """Safe, anticipated validation message for the MCP client."""


def validate_month(month: str) -> None:
    if not isinstance(month, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}", month):
        raise ContractError("Период должен иметь формат YYYY-MM")
    try:
        date.fromisoformat(month + "-01")
    except ValueError:
        raise ContractError("Некорректный месяц") from None


@dataclass(frozen=True)
class Period:
    month: str
    closed: bool
    reports: tuple[ReportKind, ...]

    def __post_init__(self):
        validate_month(self.month)
        if type(self.closed) is not bool or not isinstance(self.reports, tuple):
            raise ContractError("Некорректный статус периода или список отчётов")
        if len(set(self.reports)) != len(self.reports) or not set(self.reports) <= REPORTS:
            raise ContractError("Неизвестные или повторные отчёты периода")


@dataclass(frozen=True)
class Row:
    code: str
    label: str
    dimension: str
    amount: Decimal | None
    status: ValueStatus = "value"

    def __post_init__(self):
        if not all(isinstance(x, str) and x.strip() for x in (self.code, self.label, self.dimension)):
            raise ContractError("Строке нужны code, label и dimension")
        if self.status not in {"value", "missing", "not_applicable"}:
            raise ContractError("Неизвестный статус значения")
        if self.status == "value":
            if not isinstance(self.amount, Decimal) or not self.amount.is_finite():
                raise ContractError("Денежное значение должно быть конечным Decimal")
        elif self.amount is not None:
            raise ContractError("Отсутствующее/неприменимое значение должно быть None")


@dataclass(frozen=True)
class Report:
    kind: ReportKind
    month: str
    currency: str
    source: str
    as_of: str | None
    complete: bool
    rows: tuple[Row, ...]
    received_at: str | None = None
    as_of_status: Literal["confirmed", "unknown"] = "confirmed"
    source_time_raw: str | None = None

    def __post_init__(self):
        validate_month(self.month)
        if self.kind not in REPORTS or not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ContractError("Неизвестный отчёт или валюта")
        if not self.source.strip() or type(self.complete) is not bool:
            raise ContractError("Нужны источник и признак полноты")
        received = timestamp(self.received_at, "received_at") if self.received_at is not None else None
        if self.as_of_status == "confirmed":
            state = timestamp(self.as_of, "as_of")
            if received is not None and state > received:
                raise ContractError("Дата состояния позже времени получения")
        elif self.as_of_status == "unknown":
            if self.as_of is not None or received is None:
                raise ContractError("Для unknown нужны as_of=null и aware received_at")
        else:
            raise ContractError("Неизвестный as_of_status")
        if self.source_time_raw is not None and (not isinstance(self.source_time_raw, str) or not self.source_time_raw.strip()):
            raise ContractError("source_time_raw должен содержать исходный текст даты")
        if not isinstance(self.rows, tuple) or not self.rows:
            raise ContractError("Пустой отчёт нельзя публиковать как данные")
        keys = [(r.code, r.dimension) for r in self.rows]
        if len(keys) != len(set(keys)):
            raise ContractError("Повторная строка отчёта")
        if self.complete and any(r.status == "missing" for r in self.rows):
            raise ContractError("Отчёт с пропусками не может быть полным")


class FinanceAdapter(Protocol):
    """One instance exposes exactly one client's approved data; no SQL in the MCP tools."""

    def passport(self) -> str: ...
    def periods(self) -> tuple[Period, ...]: ...
    def report(self, kind: ReportKind, month: str) -> Report: ...
    def notes(self, month: str) -> str: ...


class FinanceService:
    """Reject unavailable/open periods and inconsistent adapter results before publication."""

    def __init__(self, adapter: FinanceAdapter):
        self.adapter = adapter

    def periods(self) -> tuple[Period, ...]:
        periods = self.adapter.periods()
        if not isinstance(periods, tuple) or not all(isinstance(p, Period) for p in periods):
            raise ContractError("Адаптер должен вернуть tuple[Period, ...]")
        if len({p.month for p in periods}) != len(periods):
            raise ContractError("Повторный период в адаптере")
        return periods

    def _period(self, month: str) -> Period:
        validate_month(month)
        period = next((p for p in self.periods() if p.month == month), None)
        if period is None:
            raise ContractError("Нет данных за запрошенный период")
        if not period.closed:
            raise ContractError("Период ещё не закрыт; отчёт недоступен")
        return period

    def reports(self, kind: ReportKind, months: list[str]) -> tuple[Report, ...]:
        if kind not in REPORTS:
            raise ContractError("Неизвестный отчёт")
        if not isinstance(months, list) or not 1 <= len(months) <= 12:
            raise ContractError("Нужно от 1 до 12 периодов")
        for month in months:
            validate_month(month)
        if len(set(months)) != len(months):
            raise ContractError("Периоды не должны повторяться")
        # Validate the entire request before fetching; no silently partial response.
        for month in months:
            if kind not in self._period(month).reports:
                raise ContractError("Этот отчёт недоступен за запрошенный период")
        result = tuple(self.adapter.report(kind, month) for month in months)
        for month, report in zip(months, result):
            if not isinstance(report, Report) or (report.kind, report.month) != (kind, month):
                raise ContractError("Адаптер вернул другой отчёт или период")
        return result

    def notes(self, month: str) -> str:
        self._period(month)
        return self.adapter.notes(month)
