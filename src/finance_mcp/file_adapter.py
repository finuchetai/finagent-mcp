"""Read mapped CSV/XLSX exports without changing sources or executing formulas."""
import csv
from pathlib import Path
from zipfile import BadZipFile

from .contract import ContractError
from .snapshot import SnapshotAdapter, text
from .sources import column_map, read_passport, read_registry, validate_config


def records(rows, columns, limit):
    iterator = iter(rows)
    header = next(iterator, None)
    if not header or len(set(header)) != len(header):
        raise ContractError("Пустой или повторный заголовок")
    if not set(columns.values()) <= set(header):
        raise ContractError("В источнике нет обязательных колонок")
    positions = {field: header.index(name) for field, name in columns.items()}
    result = []
    for number, row in enumerate(iterator, start=2):
        if not any(v not in (None, "") for v in row):
            continue
        if len(row) != len(header):
            raise ContractError(f"Неверное число ячеек в строке {number}")
        result.append({field: row[pos] for field, pos in positions.items()})
        if len(result) > limit:
            raise ContractError("Превышен max_rows")
    return result


class FileAdapter(SnapshotAdapter):
    def __init__(self, config, base=Path(".")):
        validate_config(config)
        base = Path(base)
        tables = {}
        paths = {base / text(spec.get("file"), "file") for spec in config["tables"].values()}
        paths.add(base / text(config.get("passport"), "passport"))
        if "article_registry" in config:
            paths.add(base / text(config["article_registry"], "article_registry"))
        def stamps():
            try:
                return {p: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
                        for p in paths for s in (p.stat(),)}
            except OSError:
                raise ContractError("Не удалось прочитать файлы экспорта") from None
        before = stamps()
        for name, spec in config["tables"].items():
            path = base / text(spec.get("file"), "file")
            columns = column_map(name, spec)
            limit = config.get("max_rows", 100000)
            try:
                if config["adapter"] == "csv":
                    separator = spec.get("delimiter", ",")
                    if not isinstance(separator, str) or len(separator) != 1:
                        raise ContractError("CSV delimiter должен быть одним символом")
                    with path.open(encoding="utf-8-sig", newline="") as f:
                        tables[name] = records(csv.reader(f, delimiter=separator, strict=True), columns, limit)
                elif config["adapter"] == "xlsx":
                    tables[name] = self._excel(path, text(spec.get("sheet"), "sheet"), columns, limit)
                else:
                    raise ContractError("FileAdapter поддерживает только csv/xlsx")
            except (OSError, csv.Error, BadZipFile):
                raise ContractError(f"Не удалось прочитать источник таблицы {name}") from None
        passport = read_passport(config, base)
        registry = read_registry(config, base)
        if stamps() != before:
            raise ContractError("Экспорт изменился во время чтения; повторите на согласованной копии")
        super().__init__(tables, passport, config["client_id"],
                         config.get("decimal_separator", "."), registry=registry)

    @staticmethod
    def _excel(path, sheet, columns, limit):
        try:
            from openpyxl import load_workbook
        except ImportError:
            raise ContractError("Для XLSX установите пакет с extra [files]") from None
        formula_book = load_workbook(path, read_only=True, data_only=False, keep_links=False)
        try:
            value_book = load_workbook(path, read_only=True, data_only=True, keep_links=False)
            try:
                if sheet not in formula_book.sheetnames:
                    raise ContractError("Нет заданного листа XLSX")
                def values():
                    for raw_row, cached_row in zip(formula_book[sheet].iter_rows(), value_book[sheet].iter_rows()):
                        row = []
                        for raw, cached in zip(raw_row, cached_row):
                            if raw.data_type == "e" or cached.data_type == "e":
                                raise ContractError("Ошибка ячейки XLSX")
                            if raw.data_type == "f" and cached.value is None:
                                raise ContractError("Формула XLSX без сохранённого результата")
                            row.append(cached.value)
                        yield row
                return records(values(), columns, limit)
            finally:
                value_book.close()
        finally:
            formula_book.close()
