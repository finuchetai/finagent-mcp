"""Generate a synthetic Lipa workbook from the published CSV example without financial calculations."""
import csv
import json
from pathlib import Path

from openpyxl import Workbook


def main():
    base = Path(__file__).resolve().parent / "lipa"
    path = base / "lipa.xlsx"
    if path.exists():
        raise SystemExit("lipa.xlsx уже существует; используйте новую копию примера")
    config = json.loads((base / "csv.json").read_text())
    book = Workbook()
    book.remove(book.active)
    try:
        for name, spec in config["tables"].items():
            sheet = book.create_sheet(name)
            with (base / spec["file"]).open(encoding="utf-8-sig", newline="") as f:
                for row in csv.reader(f, delimiter=spec.get("delimiter", ",")):
                    sheet.append(row)
        book.save(path)
    finally:
        book.close()
    print(path)


if __name__ == "__main__":
    main()
