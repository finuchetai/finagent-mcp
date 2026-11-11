"""Local Google onboarding: a link, a discovered wide report and an approved profile.

No Google writes, arbitrary endpoints, inferred accounting or silent profile replacement.
The CLI is an installer step outside MCP stdio. Source text is untrusted data.
"""
import argparse
import csv
from datetime import date, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from urllib.parse import urlsplit

from .contract import ContractError, FinanceService, REPORTS, validate_month
from .google_oauth import authorized_oauth_session, login, profile_name, TokenStore
from .google_sheets_adapter import GoogleSheetsAdapter, MAX_BYTES
from .snapshot import FIELDS, money

API = "https://sheets.googleapis.com/v4/spreadsheets/"
MAX_ROWS = 5000
MAX_COLS = 100
KINDS = {"pnl": "ОПиУ", "cashflow": "ДДС", "balances": "Остатки"}
MONTH_NAMES = [
    ("январь", "января", "january", "jan"), ("февраль", "февраля", "february", "feb"),
    ("март", "марта", "march", "mar"), ("апрель", "апреля", "april", "apr"),
    ("май", "мая", "may"), ("июнь", "июня", "june", "jun"),
    ("июль", "июля", "july", "jul"), ("август", "августа", "august", "aug"),
    ("сентябрь", "сентября", "september", "sep"), ("октябрь", "октября", "october", "oct"),
    ("ноябрь", "ноября", "november", "nov"), ("декабрь", "декабря", "december", "dec"),
]


def spreadsheet_id(link):
    """Accept only a normal private Sheets URL; never request the supplied host."""
    if not isinstance(link, str):
        raise ContractError("Нужна ссылка Google Sheets")
    try:
        u = urlsplit(link.strip())
        match = re.fullmatch(r"/spreadsheets/d/([A-Za-z0-9_-]+)(?:/(?:edit|view|preview|copy))?/?", u.path)
        if (u.scheme != "https" or u.netloc != "docs.google.com" or not match):
            raise ValueError
        return match[1]
    except ValueError:
        raise ContractError("Нужна ссылка https://docs.google.com/spreadsheets/d/…/edit") from None


def col_name(number):
    result = ""
    while number:
        number, rem = divmod(number - 1, 26)
        result = chr(65 + rem) + result
    return result


def json_get(session, url, params):
    try:
        with session.get(url, params=params, timeout=30, stream=True, allow_redirects=False) as r:
            if r.status_code in (401, 403):
                raise ContractError("Google: проверьте аккаунт, разрешение приложения и доступ к книге")
            if r.status_code == 404:
                raise ContractError("Книга не найдена или недоступна выбранному аккаунту")
            if r.status_code != 200:
                raise ContractError("Google временно недоступен; повторите подключение")
            chunks, size = [], 0
            for chunk in r.iter_content(65536):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ContractError("Книга превышает лимит чтения 16 MiB")
                chunks.append(chunk)
            value = json.loads(b"".join(chunks), parse_float=Decimal)
            if not isinstance(value, dict):
                raise ContractError("Некорректный ответ Google Sheets")
            return value
    except ContractError:
        raise
    except Exception:
        raise ContractError("Не удалось прочитать Google Sheets; подключение не сохранено") from None


def month_value(value, *, allow_serial=False):
    if isinstance(value, bool):
        return None
    if allow_serial and isinstance(value, (int, Decimal)) and value == int(value) and 36526 <= value <= 73415:
        d = date(1899, 12, 30) + timedelta(days=int(value))
        return d.strftime("%Y-%m") if d.day == 1 else None
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    match = re.fullmatch(r"([0-9]{4})-([0-9]{2})(?:-01)?", v)
    if match:
        month = "-".join(match.groups())
    else:
        match = re.fullmatch(r"(?:01[./])?([0-9]{2})[./]([0-9]{4})", v)
        if match:
            month = match[2] + "-" + match[1]
        else:
            match = re.fullmatch(r"([a-zа-яё]+)\s+([0-9]{4})", v)
            if not match:
                return None
            number = next((i for i, names in enumerate(MONTH_NAMES, 1) if match[1] in names), None)
            if not number:
                return None
            month = f"{match[2]}-{number:02d}"
    try:
        validate_month(month)
        return month
    except ContractError:
        return None


def detect_kind(title):
    title = title.strip().lower()
    if re.search(r"план|бюджет|plan|budget|forecast|прогноз|отклонен|variance", title):
        return None
    if re.search(r"(^|\W)(ддс|cashflow|cash flow)(\W|$)|движени.*денеж", title):
        return "cashflow"
    if re.search(r"(^|\W)(опиу|p&l|pnl)(\W|$)|прибыл.*убыт|profit.*loss", title):
        return "pnl"
    if re.search(r"остат|balances", title):
        return "balances"
    return None


def candidate(props, rows, height, width):
    """Conservative supported format: column A labels, unique dated month columns."""
    if re.search(r"план|бюджет|plan|budget|forecast|прогноз|отклонен|variance", props["title"], re.I):
        raise ContractError("Лист плана/прогноза не подключается как фактический отчёт")
    if not isinstance(rows, list) or any(not isinstance(row, list) or len(row) > width for row in rows):
        raise ContractError("Некорректные строки книги")
    if len(rows) >= height:
        raise ContractError("Лист заполнен до границы чтения; автоматическое подключение не поддерживается")
    headers = []
    for index, row in enumerate(rows[:30]):
        allow_serial = bool(row) and str(row[0]).strip().lower() in {"статья", "показатель", "article", "metric"}
        months = [(i, month_value(v, allow_serial=allow_serial), v) for i, v in enumerate(row)
                  if i > 0 and month_value(v, allow_serial=allow_serial)]
        if months:
            headers.append((index, months))
    if not headers:
        return None
    if len(headers) != 1:
        raise ContractError("Несколько таблиц или строк месяцев на листе; нужна однозначная структура")
    header, months = headers[0]
    if len({m for _, m, _ in months}) != len(months):
        raise ContractError("Месяц повторяется: возможно, рядом план и факт; автоматическая карта небезопасна")
    if any(re.search(r"план|plan|budget|прогноз|forecast|variance|отклонен", str(v), re.I)
           for row in rows[:header + 1] for v in row):
        raise ContractError("Обнаружен план/прогноз: этот формат автоматически не подключается")
    unit_context = " ".join([props["title"]] + [str(v) for row in rows[:header+1] for v in row])
    if re.search(r"(?i)(тыс[.]?|тысяч|млн|миллион|thousand|million)", unit_context):
        raise ContractError("Обнаружен масштаб в тысячах/миллионах; автоматически как единицы валюты не подключаем")
    columns = {c for c, _, _ in months}
    entries, labels = [], set()
    for index in range(header + 1, len(rows)):
        row = rows[index]
        if not any(v not in ("", None) for v in row):
            continue
        label = row[0] if row else ""
        values = [row[c] if c < len(row) else "" for c in columns]
        if not isinstance(label, str) or not label.strip():
            raise ContractError("Найдена строка без названия; нельзя молча исключить её из отчёта")
        if re.search(r"(?i)(%|процент|рентабельност|маржинальност|количеств|кол-во|(?:^|[^а-яa-z])(?:шт[.]?|чел[.]?|units|headcount)(?:$|[^а-яa-z]))", label):
            raise ContractError("В отчёте есть проценты или количества; нельзя выдавать их как денежные суммы")
        if label != label.strip() or label in labels:
            raise ContractError("Названия строк должны быть уникальны и без крайних пробелов")
        for i, value in enumerate(row):
            if i and i not in columns and value not in ("", None):
                raise ContractError("Кроме месяцев есть дополнительные колонки; формат требует уточнения")
        for value in values:
            if value not in ("", None):
                money(value, ".")  # Reject formula errors, text amounts and booleans.
        labels.add(label)
        entries.append({"row": index, "label": label, "code": f"article_{len(entries)+1:05d}",
                        "dimension": "source_row"})
    if not entries or not any(row[c] not in ("", None)
                              for row in rows[header+1:] for c in columns if c < len(row)):
        raise ContractError("Нет непустых денежных значений для первого анализа")
    return {"sheet_id": props["sheetId"], "title": props["title"],
            "range": "'" + props["title"].replace("'", "''") + f"'!A1:{col_name(width)}{height}",
            "kind": detect_kind(props["title"]), "header_row": header,
            "grid": {"sheet_id": props["sheetId"], "title": props["title"], "rows": height, "columns": width},
            "layout_signature": json.dumps(rows[:header+1], ensure_ascii=False, default=str),
            "months": {m: {"column": c, "header": int(v) if isinstance(v, Decimal) else v} for c, m, v in months},
            "entries": entries}


def discover(link, session):
    sid = spreadsheet_id(link)
    meta = json_get(session, API + sid, {
        "fields": "spreadsheetId,properties(title,locale),sheets(properties(sheetId,title,sheetType,gridProperties))"})
    try:
        sheets = [s["properties"] for s in meta["sheets"]
                  if s["properties"].get("sheetType", "GRID") == "GRID"]
        if not 1 <= len(sheets) <= 30:
            raise ContractError("Автоматическое подключение поддерживает от 1 до 30 листов")
        ranges, limits, readable, unsupported = [], [], [], []
        for sheet in sheets:
            grid = sheet["gridProperties"]
            height = min(grid["rowCount"], MAX_ROWS)
            width = min(grid["columnCount"], MAX_COLS)
            if grid["rowCount"] > MAX_ROWS or grid["columnCount"] > MAX_COLS:
                unsupported.append({"title": sheet["title"], "reason": "Лист превышает лимит 5000 строк или 100 колонок"})
                continue
            if height < 2 or width < 2:
                unsupported.append({"title": sheet["title"], "reason": "Нет таблицы с названиями и месяцами"})
                continue
            readable.append(sheet)
            ranges.append("'" + sheet["title"].replace("'", "''") + f"'!A1:{col_name(width)}{height}")
            limits.append((height, width))
        sheets = readable
        if not sheets:
            return {"spreadsheet_id": sid, "title": meta["properties"]["title"],
                    "candidates": [], "unsupported": unsupported}
        params = [("ranges", r) for r in ranges] + [
            ("valueRenderOption", "UNFORMATTED_VALUE"), ("dateTimeRenderOption", "SERIAL_NUMBER")]
        first = json_get(session, API + sid + "/values:batchGet", params)
        second = json_get(session, API + sid + "/values:batchGet", params)
        if first != second:
            raise ContractError("Книга меняется во время чтения; повторите после окончания правок")
        blocks = first["valueRanges"]
        if len(blocks) != len(sheets):
            raise ContractError("Google вернул не все запрошенные листы")
        found = []
        for props, block, (height, width) in zip(sheets, blocks, limits):
            # Do not silently truncate a wide sheet.
            if props["gridProperties"]["columnCount"] > MAX_COLS:
                unsupported.append({"title": props["title"], "reason": "Больше 100 колонок"})
                continue
            try:
                item = candidate(props, block.get("values", []), height, width)
                if item:
                    found.append(item)
                else:
                    unsupported.append({"title": props["title"], "reason": "Нет строки с месяцами и годом"})
            except ContractError as exc:
                unsupported.append({"title": props["title"], "reason": str(exc)})
        return {"spreadsheet_id": sid, "title": meta["properties"]["title"],
                "candidates": found, "unsupported": unsupported}
    except ContractError:
        raise
    except (KeyError, TypeError, ValueError, IndexError):
        raise ContractError("Некорректная структура ответа Google Sheets") from None


def first_analysis(adapter, month, kind):
    """Show source measures separately; never add published totals and their details."""
    report = FinanceService(adapter).reports(kind, [month])[0]
    items = [{"code": r.code, "label": r.label, "amount": str(r.amount) if r.amount is not None else None,
              "status": r.status} for r in report.rows]
    return {"month": month, "kind": kind, "currency": report.currency, "source": report.source,
            "complete": report.complete, "as_of": report.as_of, "as_of_status": report.as_of_status,
            "received_at": report.received_at, "rows": items,
            "observations": [f"Доступно строк: {len(items)}; пропусков: {sum(r['status']=='missing' for r in items)}.",
                "Строки показаны отдельно; итоги и детализация не складываются.",
                "Методика и полнота не подтверждены. По ДДС нельзя определить прибыль."]}


def save_profile(discovery, selected, *, month, kind, currency, ready, destination, oauth_profile,
                 session, python=None, units_confirmed=False):
    validate_month(month)
    profile_name(oauth_profile)
    if units_confirmed is not True:
        raise ContractError("Подтвердите: все строки — денежные суммы в единицах выбранной валюты, без процентов и количеств")
    if ready is not True:
        raise ContractError("Нужно подтверждение владельца: выбранный месяц готов для анализа")
    if kind not in REPORTS or not re.fullmatch(r"[A-Z]{3}", currency or ""):
        raise ContractError("Подтвердите вид отчёта и валюту из трёх букв")
    if selected not in discovery["candidates"] or month not in selected["months"]:
        raise ContractError("Выберите обнаруженный отчёт и доступный месяц")
    destination = Path(destination).absolute()
    if destination.exists():
        raise ContractError("Профиль уже существует; повторно используйте его или выберите другое имя")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".connecting-", dir=destination.parent))
    try:
        os.chmod(staging, 0o700)
        client = "local-owner"
        source = f"Google Sheets / {selected['title']}"
        config = {"version": 1, "adapter": "google_sheets", "client_id": client,
                  "spreadsheet_id": discovery["spreadsheet_id"], "oauth_profile": oauth_profile,
                  "passport": "passport.md", "max_rows": MAX_ROWS, "tables": {}}
        tables = {
            "periods": [{"client_id": client, "month": month, "closed": "true"}],
            "reports": [{"client_id": client, "month": month, "kind": kind, "currency": currency,
                         "source": source, "as_of": "", "as_of_status": "unknown", "complete": "false"}],
            "notes": [{"client_id": client, "month": month,
                       "note": "Готовность месяца подтверждена владельцем. Полнота и методика неизвестны. "
                               "Строки могут содержать итоги и детали; не суммировать их вместе."}]}
        for name, values in tables.items():
            fields = list(FIELDS[name]) + (["as_of_status"] if name == "reports" else [])
            with (staging / f"{name}.csv").open("w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
                writer.writerows(values)
            config["tables"][name] = {"file": f"{name}.csv", "columns": {f: f for f in fields}}
        config["tables"]["rows"] = {"range": selected["range"], "wide": {
            "kind": kind, "header_row": selected["header_row"], "label_column": 0, "strict_rows": True,
            "grid": selected["grid"], "layout_signature": selected["layout_signature"],
            "months": {month: selected["months"][month]}, "entries": selected["entries"]}}
        (staging / "passport.md").write_text(
            "# Финансовые данные владельца\nОдна согласованная Google-книга; только чтение.\n"
            f"Отчёт: {KINDS[kind]}; период: {month}; валюта: {currency}.\n"
            "Все распознанные строки выбранного листа, без изменения знаков и сумм.\n"
            "Полнота/методика неизвестны; дата состояния учёта неизвестна.\n"
            "source_row — исходная строка, не признак слагаемого; итоги и детали не суммировать.\n"
            "Коды сохранены в карте; изменение названий/позиций требует новой проверки.\n",
            encoding="utf-8")
        with (staging / "passport.md").open("a", encoding="utf-8") as f:
            f.write(f"\nВладелец подтвердил: все строки — денежные суммы в единицах {currency}, без процентов, количеств и масштаба.\n")
        (staging / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        # Real adapter performs a fresh read and validates saved metadata; compare ALL source rows.
        adapter = GoogleSheetsAdapter(config, staging, session=session)
        analysis = first_analysis(adapter, month, kind)
        manifest = {"mcpServers": {"finance-local": {"command": str(Path(python or sys.executable).absolute()),
                    "args": ["-m", "finance_mcp.server", "--config", str(destination / "config.json")]}}}
        (staging / "mcp.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        # mkdir is an exclusive reservation: another connect cannot overwrite a profile.
        destination.mkdir(mode=0o700)
        try:
            for p in staging.iterdir():
                shutil.move(str(p), destination / p.name)
        except Exception:
            shutil.rmtree(destination)
            raise
        return {"status": "source_connected", "config": str(destination / "config.json"),
                "client_config": str(destination / "mcp.json"), "first_analysis": analysis,
                "ui_connected": False}
    finally:
        shutil.rmtree(staging)



def render_analysis(analysis):
    lines = [f"{KINDS[analysis['kind']]} за {analysis['month']} · {analysis['currency']}"]
    for row in analysis["rows"][:8]:
        value = row["amount"] if row["status"] == "value" else "нет значения"
        lines.append(f"{row['label']}: {value}")
    if len(analysis["rows"]) > 8:
        lines.append(f"Показаны первые 8 из {len(analysis['rows'])} строк; остальные доступны в отчёте.")
    lines += analysis["observations"]
    return "\n".join(lines)

def main(argv=None, *, emit_result=True):
    parser = argparse.ArgumentParser(description="Подключить Google-таблицу без ручной карты JSON")
    parser.add_argument("--profile", default="owner")
    parser.add_argument("--url")
    parser.add_argument("--app")
    parser.add_argument("--json", action="store_true", help="Machine-readable result")
    parser.add_argument("--home", type=Path, default=Path.home() / ".finagent" / "profiles")
    args = parser.parse_args(argv)
    try:
        profile_name(args.profile)
        if args.profile in {".", ".."} or args.profile.endswith(".") or args.profile.split(".")[0].upper() in {
                "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
            raise ContractError("Выберите другое имя локального профиля")
        destination = args.home.absolute() / args.profile
        if destination.exists():
            from .sources import load_adapter, read_config
            path = destination / "config.json"
            config, _ = read_config(path)
            if args.url and spreadsheet_id(args.url) != config.get("spreadsheet_id"):
                raise ContractError("В профиле другая книга; выберите новый --profile")
            adapter = load_adapter(path)
            period = next(p for p in adapter.periods() if p.closed)
            result = {"status": "reused", "config": str(path), "ui_connected": False,
                      "first_analysis": first_analysis(adapter, period.month, period.reports[0])}
            if emit_result:
                print(json.dumps(result, ensure_ascii=False) if args.json else render_analysis(result["first_analysis"]))
            return result
        link = args.url or input("Дайте ссылку на Google-таблицу: ").strip()
        spreadsheet_id(link)  # Validate before opening browser.
        store = TokenStore(args.profile)
        if store.read() is None:
            login(args.profile, app_path=args.app, store=store)
        with authorized_oauth_session(args.profile) as session:
            discovery = discover(link, session)
            candidates = discovery["candidates"]
            if not candidates:
                raise ContractError("Не найден поддержанный отчёт: нужны статьи в первой колонке и месяцы с годом в заголовках")
            for i, c in enumerate(candidates, 1):
                print(f"{i}. {c['title']}: {len(c['entries'])} строк; месяцы {', '.join(c['months'])}")
            if discovery["unsupported"]:
                print("Другие листы не подключаются: " + "; ".join(x["title"] for x in discovery["unsupported"]))
            selection = 1 if len(candidates) == 1 else int(input("Номер отчёта: "))
            if not 1 <= selection <= len(candidates):
                raise ContractError("Выберите номер из списка отчётов")
            selected = candidates[selection - 1]
            choices = ["cashflow", "pnl", "balances"]
            print("Вид отчёта: 1 — движение денег, 2 — прибыль и убытки, 3 — остатки.")
            suggested = str(choices.index(selected["kind"]) + 1) if selected["kind"] else None
            answer = input("Номер вида отчёта" + (f" (Enter — {suggested})" if suggested else "") + ": ").strip() or suggested
            if answer not in {"1", "2", "3"}:
                raise ContractError("Выберите 1, 2 или 3")
            kind = choices[int(answer) - 1]
            months = sorted(selected["months"])
            month = months[0] if len(months) == 1 else input("Какой месяц анализируем (YYYY-MM)? ").strip()
            currency = input("В какой валюте суммы (RUB / USD / …)? ").strip().upper()
            units = input(f"Все строки — денежные суммы в единицах {currency}, без процентов, количеств и тысяч/миллионов? Напишите «да»: ").strip().lower() == "да"
            ready = input(f"Месяц {month} готов для анализа? Напишите «да»: ").strip().lower() == "да"
            result = save_profile(discovery, selected, month=month, kind=kind, currency=currency, ready=ready,
                                  destination=destination, oauth_profile=args.profile, session=session, units_confirmed=units)
            if emit_result:
                print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render_analysis(result["first_analysis"]))
            return result
    except (ContractError, OSError, ValueError, IndexError, EOFError, StopIteration) as exc:
        message = str(exc) if isinstance(exc, ContractError) else "Подключение не завершено; проверьте выбор и доступность локальной папки"
        parser.exit(2, message + "\n")


if __name__ == "__main__":
    main()
