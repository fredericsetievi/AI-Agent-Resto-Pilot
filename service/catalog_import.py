"""Bulk catalog import from spreadsheet files uploaded by owners.

Trust model: the file content is untrusted owner input, same as chat text.
Header detection is heuristic because real warung sheets carry junk title
rows and Indonesian column labels; anything that does not clearly map to a
known column is ignored, and every parsed value is re-validated here before
it can reach the store. The importer never contacts a network; callers pass
the already-read bytes.
"""

from __future__ import annotations

import csv
import io
import re
from decimal import Decimal, InvalidOperation

MAX_ITEMS = 200
MAX_NAME_CHARS = 60
HEADER_SCAN_ROWS = 10

# Indonesian warung sheets label the product column in several ways
# ('nama brg' is the common abbreviation of 'nama barang').
NAME_HEADERS = {"nama", "nama brg", "nama bahan", "name", "item", "bahan", "ingredient"}
UNIT_HEADERS = {"satuan", "unit"}
QUANTITY_HEADERS = {"qty", "quantity", "stock", "stok", "sisa", "jumlah"}
CODE_HEADERS = {"kode", "code", "barcode"}
THRESHOLD_HEADERS = {"min", "minimal", "reorder"}


class CatalogImportError(ValueError):
    """Raised for files the importer cannot accept; the message is user-facing."""


def _header_key(value):
    return re.sub(r"\s+", " ", str(value if value is not None else "").strip().casefold())


def _decimal_text(value):
    """Parse one spreadsheet cell into a canonical decimal string, else None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        number = value
    else:
        text = str(value).strip()
        try:
            number = Decimal(text)
        except (InvalidOperation, ValueError):
            # Indonesian sheets print decimals with a comma ('2,5'), the same
            # leniency the caption numbers in service.vision get.
            if re.fullmatch(r"\d+,\d+", text):
                number = Decimal(text.replace(",", "."))
            else:
                return None
    if not number.is_finite():
        return None
    return format(number.normalize(), "f") if number else "0"


def _clean_name(value):
    name = re.sub(r"\s+", " ", str(value if value is not None else "")).strip()
    if not 1 <= len(name) <= MAX_NAME_CHARS:
        return None
    return name


def _column_map(row):
    """Map one header row to catalog columns; unmapped columns are ignored."""
    columns = {}
    for index, cell in enumerate(row):
        key = _header_key(cell)
        if not key:
            continue
        if "name" not in columns and key in NAME_HEADERS:
            columns["name"] = index
        elif "unit" not in columns and key in UNIT_HEADERS:
            columns["unit"] = index
        elif "quantity" not in columns and key in QUANTITY_HEADERS:
            columns["quantity"] = index
        elif "code" not in columns and key in CODE_HEADERS:
            columns["code"] = index
        elif "threshold" not in columns and key in THRESHOLD_HEADERS:
            columns["threshold"] = index
    return columns if "name" in columns else None


def _xlsx_rows(content):
    # Imported lazily: openpyxl is an optional extra on some deployment hosts.
    # A module-level import here would fail the whole service.api import chain
    # and silently remove every RestoPilot route (the Nov 2026 Atoms outage).
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - deployment packaging guard
        raise CatalogImportError(
            "Fitur impor Excel belum tersedia di server ini (paket openpyxl hilang). "
            "Gunakan file CSV sebagai gantinya."
        ) from exc
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        return [list(row) for row in next(iter(workbook.worksheets)).iter_rows(values_only=True)]
    except CatalogImportError:
        raise
    except Exception as exc:  # openpyxl raises a zoo of formats for junk files
        raise CatalogImportError("File Excel tidak dapat dibaca. Simpan ulang sebagai .xlsx lalu coba lagi.") from exc


def _csv_rows(content):
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CatalogImportError("File CSV tidak dapat dibaca sebagai teks UTF-8.") from exc
    return [list(row) for row in csv.reader(io.StringIO(text))]


def parse_catalog_file(filename, content):
    """Parse one uploaded spreadsheet into catalog items.

    Returns {"items": [{name, unit, quantity, code, threshold}], "skipped": n}
    where items carry None for every absent optional value. Rows without a
    usable name, duplicate names (case-insensitive, first wins), and invalid
    cells are skipped and counted; more than MAX_ITEMS valid items is a
    hard error so owners split large files instead of silently losing rows.
    """
    suffix = str(filename or "").rsplit(".", 1)[-1].lower()
    if suffix == "xlsx":
        rows = _xlsx_rows(content)
    elif suffix == "csv":
        rows = _csv_rows(content)
    else:
        raise CatalogImportError("Format file tidak didukung. Gunakan file .xlsx atau .csv.")

    columns = None
    header_index = 0
    for index, row in enumerate(rows[:HEADER_SCAN_ROWS]):
        columns = _column_map(row)
        if columns:
            header_index = index
            break
    if not columns:
        raise CatalogImportError(f"Kolom nama bahan tidak ditemukan pada {HEADER_SCAN_ROWS} baris pertama.")

    items, skipped = [], 0
    seen = set()
    for row in rows[header_index + 1:]:
        if not any(cell is not None and str(cell).strip() for cell in row):
            continue  # blank padding row, not a skipped item
        name = _clean_name(row[columns["name"]] if columns["name"] < len(row) else None)
        if name is None:
            skipped += 1
            continue
        key = name.casefold()
        if key in seen:
            skipped += 1
            continue

        def cell(role):
            index = columns.get(role)
            return row[index] if index is not None and index < len(row) else None

        unit = str(cell("unit") or "").strip() or None
        code = str(cell("code") or "").strip() or None
        items.append({
            "name": name,
            "unit": unit,
            "quantity": _decimal_text(cell("quantity")),
            "code": code,
            "threshold": _decimal_text(cell("threshold")),
        })
        seen.add(key)
        if len(items) > MAX_ITEMS:
            raise CatalogImportError(f"File berisi lebih dari {MAX_ITEMS} bahan. Pecah menjadi beberapa file lalu impor bertahap.")
    return {"items": items, "skipped": skipped}
