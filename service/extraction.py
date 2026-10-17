"""Small, deterministic parser for the bounded RestoPilot text demo.

This module deliberately handles only explicit, single-item clauses. It is not
a general Indonesian language parser; callers should keep warnings visible and
require correction before recording a purchase.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any


_ALIASES: tuple[tuple[str, str], ...] = (
    ("cabai merah", "cabai"),
    ("cabai", "cabai"),
    ("cabe", "cabai"),
    ("telur ayam", "telur"),
    ("telur", "telur"),
    ("beras", "beras"),
    ("minyak goreng", "minyak"),
    ("minyak", "minyak"),
    ("bawang merah", "bawang"),
    ("bawang", "bawang"),
)

_UNIT_ALIASES = {
    "kg": "kg",
    "kilo": "kg",
    "kiloan": "kg",
    "kilogram": "kg",
    "g": "g",
    "gr": "g",
    "gram": "g",
    "l": "l",
    "lt": "l",
    "liter": "l",
    "litre": "l",
    "ml": "ml",
    "mililiter": "ml",
    "milliliter": "ml",
    "ikat": "ikat",
    "karung": "karung",
    "tray": "tray",
    "pack": "pack",
    "pak": "pack",
    "dus": "dus",
    "pcs": "pcs",
    "buah": "buah",
}

_EXPECTED_UNIT = {"cabai": "kg", "telur": "kg", "beras": "kg", "bawang": "kg", "minyak": "l"}
_MAX_VALUE = Decimal("1000000")
_MAX_TOTAL = 1_000_000_000_000
_NUMBER = r"[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)"
_PRICE_SUFFIX = re.compile(
    rf"(?ix)\s+(?:rp\s*)?(?P<amount>{_NUMBER})\s*(?P<scale>rb|ribu|jt|juta)\s*$"
)
_RP_INTEGER = re.compile(r"(?ix)\s+rp\s*(?P<amount>\d+(?:\.\d{3})*)\s*$")
_UNPREFIXED_INTEGER = re.compile(r"(?i)\s+(?P<amount>\d+(?:\.\d{3})*)\s*$")
_TRAILING_QUANTITY = re.compile(
    rf"(?ix)\s+(?P<quantity>{_NUMBER})(?:\s+(?P<unit>[a-zA-Z]+))?\s*$"
)
_SOURCE_ONLY_MODIFIERS = re.compile(
    r"(?i)\b(?:jangan\s+beli|tidak\s+usah\s+beli|rencana(?:nya)?|nanti\s+beli|per\s+(?:kg|kilo|g|gram|l|liter|ml)|diskon|discount|pajak|ppn|ongkir|potongan|termasuk\s+pajak)\b"
)
_CURRENCY_MARKER = re.compile(r"(?i)\brp\s*[\d.,]+\s*$")
_MULTI_CLAUSE = re.compile(r"(?i)\b(?:dan|lalu|kemudian|serta)\b")


def _decimal_text(value: str) -> Decimal | None:
    """Parse the supported decimal-comma/decimal-point quantity notation."""
    candidate = value.strip()
    if not re.fullmatch(_NUMBER, candidate):
        return None
    if "," in candidate and "." in candidate:
        return None
    candidate = candidate.replace(",", ".")
    try:
        parsed = Decimal(candidate)
    except InvalidOperation:
        return None
    if not parsed.is_finite() or parsed <= 0 or parsed > _MAX_VALUE:
        return None
    if max(0, -parsed.as_tuple().exponent) > 6:
        return None
    return parsed


def _plain_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _parse_price(text: str) -> tuple[str, int | None, str | None]:
    """Return remaining text, integer IDR total, and an optional problem."""
    match = _PRICE_SUFFIX.search(text)
    if match:
        amount = _decimal_text(match.group("amount"))
        if amount is None:
            return text[: match.start()].strip(), None, "Invalid or out-of-range price; enter a positive amount."
        scale = match.group("scale").casefold()
        multiplier = Decimal(1_000 if scale in {"rb", "ribu"} else 1_000_000)
        total = amount * multiplier
        if total != total.to_integral_value() or total > _MAX_TOTAL:
            return text[: match.start()].strip(), None, "Price must resolve to a whole IDR amount no greater than 1000000000000."
        return text[: match.start()].strip(), int(total), None

    match = _RP_INTEGER.search(text)
    if match:
        digits = match.group("amount").replace(".", "")
        if len(digits) > 13:
            return text[: match.start()].strip(), None, "Price is too long; enter a whole IDR amount no greater than 1000000000000."
        total = int(digits)
        if total <= 0 or total > _MAX_TOTAL:
            return text[: match.start()].strip(), None, "Price must be positive and no greater than 1000000000000 IDR."
        return text[: match.start()].strip(), total, None

    # A bare integer after an explicit quantity and unit is likely a total, but
    # the parser requires an explicit Rp or rb/ribu/jt/juta marker to avoid
    # confusing a total with another quantity.
    match = _UNPREFIXED_INTEGER.search(text)
    if match and _TRAILING_QUANTITY.search(text[: match.start()]):
        return text[: match.start()].strip(), None, "Unprefixed whole-number price is unsupported; use Rp or an rb/ribu/jt/juta suffix."

    # A malformed currency token is not silently treated as part of an item name.
    if _CURRENCY_MARKER.search(text):
        return re.sub(r"(?i)\s+rp\s*[\d.,]+\s*$", "", text).strip(), None, "Could not safely read the Rp price. Use Rp followed by an integer amount."
    return text.strip(), None, None


def _parse_quantity(text: str) -> tuple[str, str | None, str | None, str | None]:
    match = _TRAILING_QUANTITY.search(text)
    if not match:
        return text.strip(), None, None, None
    quantity = _decimal_text(match.group("quantity"))
    unit_text = match.group("unit")
    if quantity is None:
        return text[: match.start()].strip(), None, _UNIT_ALIASES.get((unit_text or "").casefold(), unit_text.casefold() if unit_text else None), "Invalid quantity; use a positive number with at most six decimal places."
    unit = None
    if unit_text:
        unit = _UNIT_ALIASES.get(unit_text.casefold(), unit_text.casefold())
    return text[: match.start()].strip(), _plain_decimal(quantity), unit, None


def _match_name(name: str) -> tuple[str, str | None]:
    candidate = re.sub(r"\s+", " ", name.strip(" ,.-")).casefold()
    for alias, canonical_id in _ALIASES:
        if candidate == alias:
            return candidate, canonical_id
    return candidate, None


def _parse_clause(clause: str, line_number: int) -> tuple[dict[str, Any] | None, list[str]]:
    warnings: list[str] = []
    source = clause.strip()
    if not source:
        return None, warnings
    if _SOURCE_ONLY_MODIFIERS.search(source):
        return None, [f"Line {line_number}: instruction, per-unit price, tax, or discount language needs manual correction."]
    if _MULTI_CLAUSE.search(source):
        return None, [f"Line {line_number}: multiple item clauses are ambiguous; put one item on each line."]

    source = re.sub(r"(?i)^\s*(?:tolong\s+)?beli\s+", "", source).strip()
    if not source:
        return None, [f"Line {line_number}: no item name was found."]

    # Price is parsed first so its numeric amount can never become a quantity.
    remainder, line_total, price_problem = _parse_price(source)
    remainder, quantity, unit, quantity_problem = _parse_quantity(remainder)
    raw_name, ingredient_id = _match_name(remainder)
    if not raw_name:
        return None, [f"Line {line_number}: no item name was found."]
    if price_problem:
        warnings.append(f"Line {line_number}: {price_problem}")
    if quantity_problem:
        warnings.append(f"Line {line_number}: {quantity_problem}")
    if quantity is None:
        warnings.append(f"Line {line_number}: quantity is missing or unsupported.")
    if unit is None:
        warnings.append(f"Line {line_number}: unit is missing; choose a supported unit or pack.")
    if line_total is None:
        warnings.append(f"Line {line_number}: line total is missing or unsupported.")
    if ingredient_id is None:
        warnings.append(f"Line {line_number}: unknown ingredient '{raw_name}'; select a catalog item manually.")

    return {
        "ingredient_id": ingredient_id,
        "raw_name": raw_name,
        "quantity": quantity,
        "unit": unit,
        "line_total_idr": line_total,
    }, warnings


def extract_purchase(text: str) -> dict[str, Any]:
    """Extract at most 20 explicit purchase lines without model/provider calls."""
    if not isinstance(text, str) or not text.strip():
        return {"items": [], "warnings": ["No purchase text was provided."]}
    lines = re.split(r"[\r\n;]+", text)
    nonempty = [(index + 1, line.strip()) for index, line in enumerate(lines) if line.strip()]
    warnings: list[str] = []
    if len(nonempty) > 20:
        warnings.append("Input exceeds the 20-item limit; split the invoice into separate inputs before continuing.")
        return {"items": [], "warnings": warnings}
    items = []
    for line_number, line in nonempty:
        item, line_warnings = _parse_clause(line, line_number)
        if item is not None:
            items.append(item)
        warnings.extend(line_warnings)
    return {"items": items, "warnings": warnings}


def normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize one parsed item using exact decimal arithmetic."""
    result = {
        "ingredient_id": item.get("ingredient_id"),
        "raw_name": item.get("raw_name"),
        "quantity": item.get("quantity"),
        "unit": item.get("unit"),
        "line_total_idr": item.get("line_total_idr"),
        "canonical_quantity": None,
        "canonical_unit": None,
        "unit_price_idr": None,
        "warnings": [],
    }
    warnings: list[str] = result["warnings"]
    qty_raw = item.get("quantity")
    qty: Decimal | None = None
    if qty_raw is not None and isinstance(qty_raw, (str, int, Decimal)) and not isinstance(qty_raw, bool):
        qty = _decimal_text(str(qty_raw))
    if qty_raw is None:
        warnings.append("Quantity is missing.")
    elif qty is None:
        warnings.append("Quantity is invalid or outside the supported bounds.")

    unit_raw = item.get("unit")
    unit = _UNIT_ALIASES.get(unit_raw.casefold(), unit_raw.casefold()) if isinstance(unit_raw, str) else None
    if unit_raw is None:
        warnings.append("Unit is missing.")
    elif unit not in {"kg", "g", "l", "ml"}:
        warnings.append(f"Unit '{unit_raw}' is not supported for automatic normalization; choose a catalog pack conversion.")

    ingredient_id = item.get("ingredient_id")
    expected_unit = _EXPECTED_UNIT.get(ingredient_id)
    dimension = "mass" if unit in {"kg", "g"} else "volume" if unit in {"l", "ml"} else None
    expected_dimension = "mass" if expected_unit == "kg" else "volume" if expected_unit == "l" else None
    if expected_unit and dimension and dimension != expected_dimension:
        warnings.append(f"Unit '{unit}' does not match the catalog dimension for '{ingredient_id}'.")
    elif qty is not None and unit in {"kg", "g", "l", "ml"}:
        factor = Decimal(1000) if unit in {"g", "ml"} else Decimal(1)
        canonical_unit = "kg" if unit in {"kg", "g"} else "l"
        canonical_quantity = qty / factor
        result["canonical_quantity"] = _plain_decimal(canonical_quantity)
        result["canonical_unit"] = canonical_unit

    if ingredient_id not in _EXPECTED_UNIT:
        warnings.append("Ingredient is not mapped to the demo catalog; select a catalog item manually.")

    total_raw = item.get("line_total_idr")
    total: int | None = None
    if isinstance(total_raw, int) and not isinstance(total_raw, bool):
        total = total_raw
    elif isinstance(total_raw, str) and len(total_raw) <= 13 and re.fullmatch(r"\d+", total_raw):
        total = int(total_raw)
    if total_raw is not None and (total is None or total <= 0 or total > _MAX_TOTAL):
        warnings.append("Line total must be a positive whole IDR amount no greater than 1000000000000.")
        total = None
    if total_raw is None:
        warnings.append("Line total is missing.")

    canonical_quantity = result["canonical_quantity"]
    if total is not None and canonical_quantity is not None and not warnings:
        normalized_quantity = Decimal(canonical_quantity)
        price = Decimal(total) / normalized_quantity
        result["unit_price_idr"] = _plain_decimal(price)
    return result
