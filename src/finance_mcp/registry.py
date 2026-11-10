"""Read-only approved article identities; never create codes from labels or mutate the registry."""
import argparse
import json
from pathlib import Path

from .contract import ContractError, REPORTS


def required(value):
    if not isinstance(value, str) or not value.strip():
        raise ContractError("Реестр: нужно непустое текстовое поле")
    return value.strip()


class ArticleRegistry:
    def __init__(self, document, client_id):
        if not isinstance(document, dict) or type(document.get("version")) is not int or document.get("version") != 1:
            raise ContractError("Реестр: нужна version=1")
        if document.get("client_id") != client_id:
            raise ContractError("Реестр относится к другому клиенту")
        self.client_id = client_id
        entries = document.get("entries")
        if not isinstance(entries, list) or not entries:
            raise ContractError("Реестр не должен быть пустым")
        self.entries, self.ids, self.aliases = {}, {}, {}
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"kind", "code", "source_ids", "labels", "approved_by"}:
                raise ContractError("Некорректные поля записи реестра")
            kind, code = required(entry["kind"]), required(entry["code"])
            required(entry["approved_by"])
            if kind not in REPORTS or (kind, code) in self.entries:
                raise ContractError("Неизвестный отчёт или повторный code реестра")
            labels, ids = entry["labels"], entry["source_ids"]
            if not isinstance(labels, list) or not labels or not isinstance(ids, list):
                raise ContractError("Реестр: нужны labels и список source_ids")
            labels, ids = [required(x) for x in labels], [required(x) for x in ids]
            if len(set(labels)) != len(labels) or len(set(ids)) != len(ids):
                raise ContractError("Повторный алиас или ID в реестре")
            self.entries[kind, code] = (frozenset(labels), frozenset(ids))
            for source_id in ids:
                if (kind, source_id) in self.ids:
                    raise ContractError("Source ID назначен нескольким кодам")
                self.ids[kind, source_id] = code
            for label in labels:
                self.aliases.setdefault((kind, label), set()).add(code)

    @classmethod
    def read(cls, path, client_id):
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ContractError("Не удалось прочитать реестр статей") from None
        return cls(document, client_id)

    def resolve(self, kind, code, label, source_id=None):
        if source_id not in (None, ""):
            result = self.ids.get((kind, required(source_id)))
            if result is None:
                raise ContractError("Неизвестный source_id: согласуйте карту статей")
            if code not in (None, "") and required(code) != result:
                raise ContractError("Конфликт code и source_id")
        elif code not in (None, ""):
            result = required(code)
        else:
            candidates = self.aliases.get((kind, label), set())
            if len(candidates) != 1:
                raise ContractError("Неизвестный или неоднозначный алиас статьи")
            result = next(iter(candidates))
        entry = self.entries.get((kind, result))
        if entry is None or label not in entry[0]:
            raise ContractError("Код/название не согласованы в реестре статей")
        return result

    def assert_compatible(self, previous):
        if self.client_id != previous.client_id:
            raise ContractError("Нельзя сравнивать реестры разных клиентов")
        for key, (labels, ids) in previous.entries.items():
            if key not in self.entries:
                raise ContractError("Из обновления реестра удалён прежний code")
            new_labels, new_ids = self.entries[key]
            if not labels <= new_labels or not ids <= new_ids:
                raise ContractError("Изменены прежние ID/алиасы; коды и историю нельзя переназначать")


def main():
    parser = argparse.ArgumentParser(description="Validate approved article registry without changing it")
    parser.add_argument("registry")
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--previous")
    args = parser.parse_args()
    try:
        current = ArticleRegistry.read(args.registry, args.client_id)
        if args.previous:
            current.assert_compatible(ArticleRegistry.read(args.previous, args.client_id))
        print(json.dumps({"ok": True, "entries": len(current.entries), "previous_checked": bool(args.previous)}))
    except ContractError as exc:
        parser.exit(2, f"Реестр: {exc}\n")


if __name__ == "__main__":
    main()
