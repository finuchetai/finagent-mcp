"""Read one explicitly approved spreadsheet; refresh replaces only validated snapshots."""
from datetime import datetime, timezone
from decimal import Decimal
import json
import csv
import os
from pathlib import Path
import re
from threading import RLock

from .contract import ContractError
from .file_adapter import records
from .snapshot import SnapshotAdapter, text
from .sources import column_map, read_passport, read_registry, validate_config

READ_SCOPE = 'https://www.googleapis.com/auth/spreadsheets.readonly'
MAX_BYTES = 16 * 1024 * 1024
ERRORS = {'#N/A', '#REF!', '#VALUE!', '#DIV/0!', '#NAME?', '#NUM!', '#ERROR!', '#NULL!'}


def authorized_session(env_name):
    """Use only an explicit service-account file; never discover a user's Drive credentials."""
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', env_name):
        raise ContractError('Некорректное имя переменной credentials_env')
    path = os.environ.get(env_name)
    if not path:
        raise ContractError('Укажите локальный файл сервисного аккаунта через credentials_env')
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import AuthorizedSession
        # Do not accept arbitrary external-account/token endpoints from a credential file.
        info = json.loads(Path(path).read_text(encoding='utf-8'))
        if info.get('type') != 'service_account' or info.get('token_uri') != 'https://oauth2.googleapis.com/token':
            raise ValueError('Unsupported credentials')
        credentials = service_account.Credentials.from_service_account_info(info, scopes=[READ_SCOPE])
        return AuthorizedSession(credentials, refresh_timeout=30)
    except ImportError:
        raise ContractError('Для Google Sheets установите extra [sheets]') from None
    except Exception:
        raise ContractError('Не удалось открыть локальные учётные данные Google') from None


class GoogleSheetsAdapter:
    refresh_before_report = True

    """No background polling: explicit refresh, with acquisition time distinct from as_of."""
    def __init__(self, config, base=Path('.'), *, session=None):
        validate_config(config)
        self._config, self._base = config, Path(base)
        sid = text(config.get('spreadsheet_id'), 'spreadsheet_id')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', sid):
            raise ContractError('Нужен ID одной Google-таблицы, не URL')
        self._url = f'https://sheets.googleapis.com/v4/spreadsheets/{sid}/values:batchGet'
        self._ranges, self._capacities = [], []
        for spec in config['tables'].values():
            if 'file' in spec:
                if 'range' in spec or 'wide' in spec:
                    raise ContractError('Локальная таблица не должна содержать range/wide')
                continue
            value = text(spec.get('range'), 'range')
            # Explicit bounded A1 ranges, including a header and spare trailing rows.
            match = re.fullmatch(r"(?:'([^']|'')+'|[^!']+)!([A-Z]+)([1-9][0-9]*):([A-Z]+)([1-9][0-9]*)", value)
            if not match:
                raise ContractError('Нужен ограниченный A1-диапазон: Лист!A1:H1000')
            def col(s):
                result = 0
                for c in s: result = result * 26 + ord(c) - 64
                return result
            left, top, right, bottom = match.group(2, 3, 4, 5)
            height, width = int(bottom)-int(top)+1, col(right)-col(left)+1
            if not 2 <= height <= config.get('max_rows', 100000)+2 or not 1 <= width <= 100:
                raise ContractError('Диапазон превышает лимиты строк/колонок')
            self._ranges.append(value); self._capacities.append((height, width))
        if not self._ranges:
            raise ContractError('Нужен хотя бы один диапазон Google Sheets')
        if 'credentials_env' in config and 'oauth_profile' in config:
            raise ContractError('Выберите один способ Google: OAuth или сервисный аккаунт')
        if session is not None:
            self._session = session
        elif 'credentials_env' in config:
            self._session = authorized_session(text(config['credentials_env'], 'credentials_env'))
        else:
            from .google_oauth import authorized_oauth_session
            self._session = authorized_oauth_session(config.get('oauth_profile', 'default'))
        self._grids = []
        for spec in config["tables"].values():
            grid = spec.get("wide", {}).get("grid")
            if grid is not None:
                if (not isinstance(grid, dict) or set(grid) != {"sheet_id", "title", "rows", "columns"}
                        or type(grid["sheet_id"]) is not int or grid["sheet_id"] < 0
                        or not isinstance(grid["title"], str) or not grid["title"]
                        or type(grid["rows"]) is not int or not 2 <= grid["rows"] <= 5000
                        or type(grid["columns"]) is not int or not 1 <= grid["columns"] <= 100):
                    raise ContractError("Некорректная граница Google-листа")
                self._grids.append(grid)
        self._lock = RLock()
        self._snapshot = None
        self.refresh()

    def _read(self):
        params = [('ranges', value) for value in self._ranges]
        params += [('valueRenderOption', 'UNFORMATTED_VALUE'), ('dateTimeRenderOption', 'SERIAL_NUMBER')]
        try:
            with self._session.get(self._url, params=params, timeout=30, stream=True,
                                   allow_redirects=False) as response:
                if response.status_code != 200:
                    raise ContractError('Google Sheets: проверьте доступ Читатель, API и соединение')
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_BYTES: raise ContractError('Google Sheets: превышен лимит ответа 16 MiB')
                    chunks.append(chunk)
                payload = json.loads(b''.join(chunks), parse_float=Decimal)
        except ContractError:
            raise
        except Exception:
            # Do not leak response bodies, tokens, credentials paths or private source values.
            raise ContractError('Не удалось прочитать Google Sheets; снимок не обновлён') from None
        if not isinstance(payload, dict) or not isinstance(payload.get('valueRanges'), list) or len(payload['valueRanges']) != len(self._ranges):
            raise ContractError('Некорректный ответ Google Sheets')
        return payload['valueRanges']

    def _check_grid(self):
        if not self._grids:
            return
        try:
            url = self._url.removesuffix("/values:batchGet")
            with self._session.get(url, params={"fields": "sheets(properties(sheetId,title,gridProperties))"},
                                   timeout=30, stream=True, allow_redirects=False) as response:
                if response.status_code != 200:
                    raise ContractError("Не удалось проверить границы Google-листа; снимок не обновлён")
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ContractError("Превышен лимит метаданных Google")
                    chunks.append(chunk)
                payload = json.loads(b"".join(chunks))
            sheets = {s["properties"]["sheetId"]: s["properties"] for s in payload["sheets"]}
            for expected in self._grids:
                actual = sheets.get(expected["sheet_id"])
                if (actual is None or actual["title"] != expected["title"]
                        or actual["gridProperties"]["rowCount"] != expected["rows"]
                        or actual["gridProperties"]["columnCount"] != expected["columns"]):
                    raise ContractError("Изменилась граница или идентичность листа; нужна повторная проверка карты")
        except ContractError:
            raise
        except Exception:
            raise ContractError("Не удалось проверить границы Google-листа; снимок не обновлён") from None

    def refresh(self):
        with self._lock:
            self._check_grid()
            first, second = self._read(), self._read()
            self._check_grid()
            if first != second:
                raise ContractError('Таблица меняется во время чтения; повторите обновление после правок')
            tables = {}
            try:
                remote = iter(zip(first, self._capacities))
                for name, spec in self._config['tables'].items():
                    if 'file' in spec:
                        with (self._base / text(spec['file'], 'file')).open(encoding='utf-8-sig', newline='') as f:
                            tables[name] = records(csv.reader(f), column_map(name, spec), self._config.get('max_rows', 100000))
                        continue
                    block, (height, width) = next(remote)
                    rows = block.get('values', [])
                    if not isinstance(rows, list) or len(rows) >= height:
                        raise ContractError('Диапазон заполнен до границы; расширьте его, чтобы избежать обрезки')
                    if 'wide' in spec:
                        if name != 'rows': raise ContractError('wide допустим только для rows')
                        tables[name] = self._wide(rows, spec['wide'], width)
                        continue
                    padded = []
                    header_width = len(rows[0]) if rows else 0
                    for row in rows:
                        if not isinstance(row, list) or len(row) > header_width or len(row) > width:
                            raise ContractError('Некорректная ширина строки Google Sheets')
                        if any(isinstance(v, str) and v in ERRORS for v in row):
                            raise ContractError('В Google Sheets есть ошибка ячейки')
                        padded.append(row + [''] * (header_width-len(row)))
                    tables[name] = records(padded, column_map(name, spec), self._config.get('max_rows', 100000))
                loaded_at = datetime.now(timezone.utc).isoformat()
                for record in tables['reports']:
                    record['received_at'] = loaded_at
                candidate = SnapshotAdapter(tables, read_passport(self._config, self._base), self._config['client_id'],
                                            self._config.get('decimal_separator', '.'), registry=read_registry(self._config, self._base))
            except ContractError:
                raise
            except (OSError, csv.Error, TypeError, ValueError, KeyError, AttributeError, IndexError, StopIteration):
                raise ContractError('Некорректные данные Google Sheets; снимок не обновлён') from None
            self._snapshot = candidate
            self._loaded_at = loaded_at
            return {'status': 'refreshed', 'loaded_at': self._loaded_at,
                    'note': 'Время чтения, не дата состояния учёта; сверка методики отдельно.'}

    def _wide(self, rows, spec, width):
        """Explicit reviewed map of a wide report, no inferred accounting or totals."""
        if set(spec) - {'kind', 'header_row', 'label_column', 'months', 'entries', 'strict_rows', 'grid', 'layout_signature', 'layout'} or not {'kind', 'header_row', 'label_column', 'months', 'entries'} <= set(spec):
            raise ContractError('Нужна полная карта wide')
        def cell(row, column):
            if type(row) is not int or type(column) is not int or row < 0 or not 0 <= column < width:
                raise ContractError('Некорректный индекс wide')
            value = rows[row][column] if row < len(rows) and column < len(rows[row]) else ''
            if isinstance(value, str) and value in ERRORS:
                raise ContractError('В выбранной ячейке Google Sheets ошибка')
            return value
        if "layout_signature" in spec:
            actual_layout = json.dumps(rows[:spec["header_row"]+1], ensure_ascii=False, default=str)
            if actual_layout != spec["layout_signature"]:
                raise ContractError("Изменились заголовки или пояснения единиц; нужна повторная проверка карты")
        layout = spec.get("layout")
        excluded = []
        if layout is not None:
            if (set(layout) - {"label_columns", "ignored_columns", "excluded_rows", "metadata_signature"}
                    or not {"label_columns", "ignored_columns", "excluded_rows"} <= set(layout)):
                raise ContractError("Некорректная карта структуры отчёта")
            label_columns = layout["label_columns"]
            if not isinstance(label_columns, list) or not label_columns or len(set(label_columns)) != len(label_columns):
                raise ContractError("Некорректные колонки названий")
            for column in label_columns:
                cell(spec["header_row"], column)
            excluded = layout["excluded_rows"]
            for item in excluded:
                if set(item) != {"row", "values"} or type(item["row"]) is not int or item["row"] <= spec["header_row"]:
                    raise ContractError("Некорректная строка примечания")
                actual = rows[item["row"]] if item["row"] < len(rows) else []
                if actual != item["values"]:
                    raise ContractError("Изменились разделы или примечания; нужна повторная проверка карты")
            for item in layout["ignored_columns"]:
                if set(item) != {"column", "header", "role"} or item["role"] not in {"empty", "total", "metadata"}:
                    raise ContractError("Некорректная дополнительная колонка")
                if cell(spec["header_row"], item["column"]) != item["header"]:
                    raise ContractError("Изменились дополнительные колонки; нужна повторная проверка карты")
                if item["role"] == "empty" and any(cell(i, item["column"]) not in ("", None)
                                                     for i in range(spec["header_row"] + 1, len(rows))):
                    raise ContractError("В пустой колонке появились данные; нужна повторная проверка карты")
            if "metadata_signature" in layout:
                columns = [c["column"] for c in layout["ignored_columns"] if c["role"] == "metadata"]
                values = [[i, [cell(i, c) for c in columns]]
                          for i in range(spec["header_row"] + 1, len(rows))] if columns else []
                if json.dumps(values, ensure_ascii=False, default=str) != layout["metadata_signature"]:
                    raise ContractError("Изменились дополнительные сведения; нужна повторная проверка карты")
        if spec.get('strict_rows', False):
            if spec['strict_rows'] is not True:
                raise ContractError('strict_rows должен быть true')
            mapped = {entry['row'] for entry in spec['entries']} | {entry['row'] for entry in excluded}
            actual = {i for i, row in enumerate(rows) if i > spec['header_row']
                      and any(value not in ('', None) for value in row)}
            if mapped != actual:
                raise ContractError('Изменился состав строк отчёта; нужна повторная проверка карты')
        result, used = [], set()
        for month, mapping in spec['months'].items():
            if set(mapping) != {'column', 'header'} or cell(spec['header_row'], mapping['column']) != mapping['header']:
                raise ContractError('Изменился заголовок месяца; нужна повторная проверка карты')
            for entry in spec['entries']:
                if set(entry) - {'row', 'label', 'code', 'dimension', 'label_values'} or not {'row', 'label', 'code', 'dimension'} <= set(entry):
                    raise ContractError('Некорректная строка wide')
                if 'label_values' in entry:
                    if layout is None or len(entry['label_values']) != len(layout['label_columns']):
                        raise ContractError('Нужна карта составного названия')
                    actual = [cell(entry['row'], c) for c in layout['label_columns']]
                    if actual != entry['label_values'] or ' / '.join(actual) != entry['label']:
                        raise ContractError('Изменилась строка отчёта; нужна повторная проверка карты')
                elif cell(entry['row'], spec['label_column']) != entry['label']:
                    raise ContractError('Изменилась строка отчёта; нужна повторная проверка карты')
                key = (month, entry['row'])
                if key in used: raise ContractError('Повторная строка в карте wide')
                used.add(key)
                amount = cell(entry['row'], mapping['column'])
                result.append(dict(client_id=self._config['client_id'], month=month, kind=spec['kind'],
                                   code=entry['code'], label=entry['label'], dimension=entry['dimension'],
                                   amount=amount, status='missing' if amount in ('', None) else 'value'))
        if len(result) > self._config.get('max_rows', 100000): raise ContractError('Превышен max_rows')
        return result

    def passport(self):
        with self._lock:
            return self._snapshot.passport() + '\nGoogle Sheets: снимок прочитан ' + self._loaded_at + '; это не as_of. Обновление: refresh_source.'

    def periods(self):
        with self._lock: return self._snapshot.periods()

    def report(self, kind, month):
        with self._lock: return self._snapshot.report(kind, month)

    def notes(self, month):
        with self._lock: return self._snapshot.notes(month)
