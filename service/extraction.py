"""Small, deterministic parser for the bounded RestoPilot text demo.

This module deliberately handles only explicit, single-item clauses. It is not
a general Indonesian language parser; callers should keep warnings visible and
require correction before recording a purchase.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Any


_ALIASES: tuple[tuple[str, str], ...] = (
    ("cabai merah", "cabai"),
    ("cabai", "cabai"),
    ("cabe merah", "cabai"),
    ("cabe", "cabai"),
    ("telur ayam", "telur"),
    ("telur", "telur"),
    ("beras", "beras"),
    ("minyak goreng", "minyak"),
    ("minyak", "minyak"),
    ("bawang merah", "bawang"),
    ("b merah", "bawang"),
    ("bawang", "bawang"),
    ("bawang putih", "bawang_putih"),
    ("bawang putih kupas", "bawang_putih"),
    ("bp kapos", "bawang_putih"),
    ("kecombrang", "kecombrang"),
    ("combrang", "kecombrang"),
    ("gula merah", "gula_merah"),
    ("gula jawa", "gula_merah"),
    ("daun pisang", "daun_pisang"),
    ("daun pising", "daun_pisang"),
    ("daun pusang", "daun_pisang"),
)

# Market names carry preparation words that do not change the ingredient.
_NAME_NOISE = {"kupas", "iris", "rajang", "halus", "segar", "mentah", "buah", "dll", "dan", "per"}
# A prepared product is not the raw ingredient; leaving it unmatched asks the owner.
_NAME_BLOCKERS = {"kering", "bubuk", "instan", "kaleng", "botol", "sachet", "saset"}
_NAME_TOKEN = re.compile(r"[a-z]+")
# Measured on real notas: "kombrang"->kecombrang is 0.89 and "cabay"->cabai is
# 0.80, while unrelated items score below 0.60. 0.78 takes the safe band.
_FUZZY_CUTOFF = 0.78

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

_EXPECTED_UNIT = {"cabai": "kg", "telur": "kg", "beras": "kg", "bawang": "kg", "minyak": "l", "bawang_putih": "kg", "kecombrang": "kg", "gula_merah": "kg", "daun_pisang": "kg"}
_MAX_VALUE = Decimal("1000000")
_MAX_TOTAL = 1_000_000_000_000
_NUMBER = r"[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)"
_PRICE_SUFFIX = re.compile(
    rf"(?ix)\s+(?:rp\s*)?(?P<amount>{_NUMBER})\s*(?P<scale>rb|ribu|jt|juta)\s*$"
)
_RP_INTEGER = re.compile(r"(?ix)\s+rp\s*(?P<amount>\d+(?:\.\d{3})*)\s*$")
_UNPREFIXED_INTEGER = re.compile(r"(?i)\s+(?P<amount>\d+(?:\.\d{3})*)\s*$")
_TRAILING_QUANTITY = re.compile(
    # The unit may be glued to the count ("500pcs", "2kg") — currency scales
    # never reach this pattern because price parsing strips "150rb" first.
    rf"(?ix)\s+(?P<quantity>{_NUMBER})\s*(?P<unit>[a-zA-Z]+)?\s*$"
)
_SOURCE_ONLY_MODIFIERS = re.compile(
    r"(?i)\b(?:jangan\s+beli|tidak\s+usah\s+beli|rencana(?:nya)?|nanti\s+beli|per\s+(?:kg|kilo|g|gram|l|liter|ml)|diskon|discount|pajak|ppn|ongkir|potongan|termasuk\s+pajak)\b"
)
_CURRENCY_MARKER = re.compile(r"(?i)\brp\s*[\d.,]+\s*$")
_MULTI_CLAUSE = re.compile(r"(?i)\b(?:dan|lalu|kemudian|serta)\b")
# Trailing "dari <name>" names the supplier ("beli garam 1 kg 10rb dari Toko
# Sebelah"). The name must start with a letter so trailing prices never match.
_SUPPLIER_SUFFIX = re.compile(r"(?i)\s+dari\s+(?P<supplier>[a-z][^,;\r\n]{0,59})\s*$")
# A supplier header line ("dari Dika:", "beli dari Pasar Pagi") declares the
# supplier for the items that follow and carries no item of its own.
_SUPPLIER_HEADER = re.compile(
    r"^\s*(?:(?:tolong\s+)?(?:beli|pembelian|buy)\s+)?dari\s+"
    r"(?P<supplier>[a-z][^,;\r\n]{0,59})\s*$",
    re.IGNORECASE,
)
# Owners list several items in one breath ("kembung 25 ekor, mas 6 ekor").
# Split points are commas that are not inside a number (decimal commas such as
# "Rp7,5" have no following space) and the shared coordinating conjunctions.
_CLAUSE_SPLIT = re.compile(r"(?i),(?=\s)|\s+(?:dan|lalu|kemudian|serta)\s+")
# Forwarded supplier POs number their lines ("1. Sedotan plastik 1 pak");
# like bullet markers the leading index is formatting, not the item name.
_LIST_MARKER = re.compile(r"^\s*(?:[-*•·](?!\d)\s*|\d{1,3}[.)]\s+)+")
# Owners open with order verbs and pronouns ("sy PO Sunco 10 dus",
# "kirim 2 gas, 3 melon"); the verb is intent, not part of the item name.
_ORDER_PREFIX = re.compile(r"(?i)^(?:tolong|sy|saya|mau|mo|pengen|po|pesan|order|buy|kirim|beli|pembelian)\s+")
# Messages close with courtesy ("... 10 dus ya 🙏"); trailing emoji and
# politeness words are chatter, not part of the item name or quantity.
_SYMBOL_TAIL = re.compile(r"[^\w\s]+\s*$")
_POLITENESS_TAIL = re.compile(r"(?i)\s+(?:ya|y|bisa|oke|ok|sip|gan|min|makasih|terima\s+kasih|mantap|siap)$")
# Owners quote a per-unit price with "@" ("Sunco 10 dus @15rb" = ten crates
# at Rp15.000 each); it multiplies by the quantity like a bare per-unit
# price, while "@2ltr" size markers never match (no rb/jt scale and no
# thousand-sized bare amount).
_AT_PRICE = re.compile(
    r"(?ix)@\s*(?:rp\s*)?(?P<amount>\d+(?:\.\d{3})*)\s*(?P<scale>rb|ribu|jt|juta)?\s*$"
)
# Counts lead the line in delivery notes ("kirim 2 gas"); a leading number
# followed by a known unit word is a quantity, otherwise the word opens the
# name ("2 gas" = gas, quantity 2).
_LEADING_UNIT_ALIASES = {
    **_UNIT_ALIASES,
    "slop": "slop",
    "kodi": "kodi",
    "bungkus": "pack",
    "ons": "ons",
    "ltr": "l",
    "lt": "l",
}
_LEADING_QUANTITY = re.compile(
    rf"(?i)^\s*(?P<quantity>{_NUMBER})\s*(?P<unit>[a-zA-Z]+)?\s*(?P<rest>.*)$"
)
# Package-size qualifiers in front of a trailing number ("teh racik merah
# kemasan 500gr") mean the number names the packaging, not the count.
_SIZE_QUALIFIER_TAIL = re.compile(r"(?i)(?:kemasan|isi|@)\s*$")


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


def _parse_price(text: str) -> tuple[str, int | None, str | None, bool]:
    """Return remaining text, integer IDR amount, an optional problem, and a
    flag telling whether the amount is a per-unit price from a bare number."""
    match = _PRICE_SUFFIX.search(text)
    if match:
        amount = _decimal_text(match.group("amount"))
        if amount is None:
            return text[: match.start()].strip(), None, "Invalid or out-of-range price; enter a positive amount.", False
        scale = match.group("scale").casefold()
        multiplier = Decimal(1_000 if scale in {"rb", "ribu"} else 1_000_000)
        total = amount * multiplier
        if total != total.to_integral_value() or total > _MAX_TOTAL:
            return text[: match.start()].strip(), None, "Price must resolve to a whole IDR amount no greater than 1000000000000.", False
        return text[: match.start()].strip(), int(total), None, False

    # "@15rb" / "@Rp15.000" quotes the per-unit price ("Sunco 10 dus @15rb");
    # bare_unit multiplication by the quantity happens in _parse_clause.
    match = _AT_PRICE.search(text)
    if match:
        digits = match.group("amount").replace(".", "")
        if len(digits) > 13:
            return text[: match.start()].strip(), None, "Price is too long; enter a whole IDR amount no greater than 1000000000000.", False
        amount = int(digits)
        scale = (match.group("scale") or "").casefold()
        multiplier = 1_000 if scale in {"rb", "ribu"} else 1_000_000 if scale in {"jt", "juta"} else 1
        total = amount * multiplier
        if not scale and amount < 1000:
            pass  # "@2"-style size marker, not a price: leave it in the name
        elif total <= 0 or total > _MAX_TOTAL:
            return text[: match.start()].strip(), None, "Price must be positive and no greater than 1000000000000 IDR.", False
        else:
            return text[: match.start()].strip(), total, None, True

    match = _RP_INTEGER.search(text)
    if match:
        digits = match.group("amount").replace(".", "")
        if len(digits) > 13:
            return text[: match.start()].strip(), None, "Price is too long; enter a whole IDR amount no greater than 1000000000000.", False
        total = int(digits)
        if total <= 0 or total > _MAX_TOTAL:
            return text[: match.start()].strip(), None, "Price must be positive and no greater than 1000000000000 IDR.", False
        # An Rp-prefixed amount is always a lot total (per-piece prices with
        # the Rp marker would be written "Rp2.500/pcs" — unsupported nuance).
        return text[: match.start()].strip(), total, None, False

    # A bare integer after an explicit quantity reads as a per-unit price —
    # owners quote prices per piece ("banana cake 10 2500" means Rp2.500
    # each, not a Rp250 piece; Nov 2026 pilot finding). The marker-free
    # form avoids confusing a total with another quantity: real quantities
    # are small, so a bare trailing whole number of at least 1000 is the
    # price. Write "total" before the number for a lot price instead.
    match = _UNPREFIXED_INTEGER.search(text)
    if match:
        prefix = text[: match.start()]
        # "total" before the amount flips the reading: "cabai 2 kg total
        # 30000" is Rp30.000 for the lot even without Rp/rb markers. The
        # marker word must not break the quantity anchor, so quantity
        # detection runs on the prefix with "total" removed.
        lot_total = bool(re.search(r"(?i)\btotal\b", prefix))
        if lot_total:
            prefix = re.sub(r"(?i)\s*\btotal\b\s*", " ", prefix)
        if _TRAILING_QUANTITY.search(prefix):
            digits = match.group("amount").replace(".", "")
            if len(digits) > 13:
                return text[: match.start()].strip(), None, "Price is too long; enter a whole IDR amount no greater than 1000000000000.", False
            bare = int(digits)
            if bare >= 1000:
                remainder = re.sub(r"(?i)\s*\btotal\b\s*$", "", text[: match.start()]).strip()
                return remainder, bare, None, not lot_total
            return text[: match.start()].strip(), None, "Unprefixed whole-number price is unsupported; use Rp or an rb/ribu/jt/juta suffix.", False

    # A malformed currency token is not silently treated as part of an item name.
    if _CURRENCY_MARKER.search(text):
        return re.sub(r"(?i)\s+rp\s*[\d.,]+\s*$", "", text).strip(), None, "Could not safely read the Rp price. Use Rp followed by an integer amount.", False
    return text.strip(), None, None, False


def _parse_quantity(text: str) -> tuple[str, str | None, str | None, str | None]:
    # Delivery notes lead with the count ("kirim 2 gas, 3 melon"); a leading
    # number followed by a known unit word is the quantity, otherwise the
    # word opens the name ("2 gas" = gas, quantity 2).
    leading = _LEADING_QUANTITY.match(text)
    if leading:
        quantity = _decimal_text(leading.group("quantity"))
        unit_text = leading.group("unit")
        rest = (leading.group("rest") or "").strip()
        unit_fold = (unit_text or "").casefold()
        if quantity is not None and unit_fold not in {"rb", "ribu", "jt", "juta"}:
            canonical = _LEADING_UNIT_ALIASES.get(unit_fold) if unit_text else None
            if canonical:
                if rest:
                    return rest, _plain_decimal(quantity), canonical, None
                # A bare "1 ikat" carries a quantity but no item name; the
                # empty remainder fails name matching and the clause is
                # rejected instead of drafting a product called "ikat".
                return "", _plain_decimal(quantity), canonical, None
            # Not a unit word: it belongs to the item name ("2 gas", "3 telur").
            name = f"{unit_text} {rest}".strip()
            if name:
                return name, _plain_decimal(quantity), None, None
    match = _TRAILING_QUANTITY.search(text)
    if not match:
        return text.strip(), None, None, None
    if _SIZE_QUALIFIER_TAIL.search(text[: match.start()]):
        # "kemasan 500gr", "isi 250ml", and "@2ltr" describe the package
        # size, not how many packages the owner wants.
        return text.strip(), None, None, None
    quantity = _decimal_text(match.group("quantity"))
    unit_text = match.group("unit")
    if quantity is None:
        return text[: match.start()].strip(), None, _UNIT_ALIASES.get((unit_text or "").casefold(), unit_text.casefold() if unit_text else None), "Invalid quantity; use a positive number with at most six decimal places."
    unit = None
    if unit_text:
        unit = _UNIT_ALIASES.get(unit_text.casefold(), unit_text.casefold())
    return text[: match.start()].strip(), _plain_decimal(quantity), unit, None


def _match_name(name: str) -> tuple[str, str | None, bool]:
    """Return (candidate, ingredient_id, fuzzy): fuzzy marks a spelling recovery
    that the owner should confirm, while exact alias/prefix matches are trusted."""
    candidate = re.sub(r"\s+", " ", name.strip(" ,.-")).casefold()
    for alias, canonical_id in _ALIASES:
        if candidate == alias:
            return candidate, canonical_id, False
    tokens = [token for token in _NAME_TOKEN.findall(candidate) if token not in _NAME_NOISE]
    if not tokens or _NAME_BLOCKERS.intersection(tokens):
        return candidate, None, False
    # Longest alias that opens the printed name wins, so "bawang putih kupas"
    # beats the shorter "bawang" alias. Prefix-only matching keeps "daun bawang"
    # from collapsing into "bawang".
    best: tuple[int, str] | None = None
    for alias, canonical_id in _ALIASES:
        alias_tokens = [token for token in _NAME_TOKEN.findall(alias) if token not in _NAME_NOISE]
        if alias_tokens and tokens[: len(alias_tokens)] == alias_tokens and (best is None or len(alias_tokens) > best[0]):
            best = (len(alias_tokens), canonical_id)
    if best:
        return candidate, best[1], False

    # Spelling slips ("kombrang", "bwang merah") are close enough to recover,
    # but only when the same word opens the name; that keeps a different item
    # from being pulled in on a coincidental letter overlap.
    scored: list[tuple[float, str]] = []
    head = tokens[0]
    for alias, canonical_id in _ALIASES:
        alias_tokens = [token for token in _NAME_TOKEN.findall(alias) if token not in _NAME_NOISE]
        if not alias_tokens or alias_tokens[0][0] != head[0]:
            continue
        scored.append((SequenceMatcher(a=" ".join(tokens[: len(alias_tokens)]), b=" ".join(alias_tokens)).ratio(), canonical_id))
    scored.sort(reverse=True)
    if scored and scored[0][0] >= _FUZZY_CUTOFF:
        return candidate, scored[0][1], True
    return candidate, None, False


def match_catalog_name(name: str) -> tuple[str, str | None]:
    """Public alias lookup so non-text extractors reuse the same catalog mapping."""
    candidate, ingredient_id, _fuzzy = match_catalog_detail(name)
    return candidate, ingredient_id


def match_catalog_detail(name: str) -> tuple[str, str | None, bool]:
    """Public alias lookup that also reports spelling-recovery matches."""
    return _match_name(name)


def catalog_aliases() -> tuple[tuple[str, str], ...]:
    """Public alias list so the stock-question matcher reuses the same catalog mapping."""
    return _ALIASES


def canonical_unit_name(unit: str | None) -> str | None:
    """Public unit alias lookup so non-text extractors reuse the same unit vocabulary."""
    if not isinstance(unit, str) or not unit.strip():
        return None
    return _UNIT_ALIASES.get(unit.strip().casefold(), unit.strip().casefold())


def _strip_politeness_tail(source: str) -> str:
    """Drop trailing emoji and courtesy words ("... 10 dus ya 🙏")."""
    for _ in range(4):
        updated = _SYMBOL_TAIL.sub("", source).rstrip()
        updated = _POLITENESS_TAIL.sub("", updated).rstrip()
        if updated == source:
            break
        source = updated
    return source


def _parse_clause(clause: str, line_number: int) -> tuple[dict[str, Any] | None, list[str]]:
    warnings: list[str] = []
    source = clause.strip()
    if not source:
        return None, warnings
    if _SOURCE_ONLY_MODIFIERS.search(source):
        return None, [f"Line {line_number}: instruction, per-unit price, tax, or discount language needs manual correction."]
    if _MULTI_CLAUSE.search(source):
        return None, [f"Line {line_number}: multiple item clauses are ambiguous; put one item on each line."]

    # Forwarded preorder lists use bullet markers ("- bolu pandan 2 pcs …")
    # and numbered PO lines ("1. Sedotan plastik 1 pak"); a marker is
    # formatting, not part of the item name. A dash marker directly
    # followed by a digit stays untouched (negative amounts are not bullets).
    source = _LIST_MARKER.sub("", source)
    source = _strip_politeness_tail(source)
    if not source:
        return None, [f"Line {line_number}: no item name was found."]
    for _ in range(4):
        stripped = _ORDER_PREFIX.sub("", source).strip()
        if stripped == source:
            break
        source = stripped
    if not source:
        return None, [f"Line {line_number}: no item name was found."]

    # Price is parsed first so its numeric amount can never become a quantity.
    remainder, price_value, price_problem, bare_unit = _parse_price(source)
    remainder, quantity, unit, quantity_problem = _parse_quantity(remainder)
    line_total = price_value
    if bare_unit and price_value is not None:
        # The bare number is a per-unit price ("banana cake 10 2500" = ten
        # pieces at Rp2.500 each). Without a quantity there is no line total.
        if quantity is None:
            warnings.append(f"Line {line_number}: the price reads as per-piece; add a quantity so the total can be computed.")
            line_total = None
        else:
            total_dec = Decimal(price_value) * Decimal(quantity)
            if total_dec == total_dec.to_integral_value():
                line_total = int(total_dec)
            else:
                warnings.append(f"Line {line_number}: per-piece price times quantity is not a whole rupiah amount.")
                line_total = None
    raw_name, ingredient_id, fuzzy = match_catalog_detail(remainder)
    if not raw_name:
        return None, [f"Line {line_number}: no item name was found."]
    if price_problem:
        warnings.append(f"Line {line_number}: {price_problem}")
    if quantity_problem:
        warnings.append(f"Line {line_number}: {quantity_problem}")
    if quantity is None:
        warnings.append(f"Line {line_number}: quantity is missing or unsupported.")
    if unit is None and _EXPECTED_UNIT.get(ingredient_id) in {"kg", "g", "l", "ml"}:
        # Only known mass/volume ingredients must name their unit; a bare
        # count ("bolkus jumbo 15 4000") is pieces, and the store layer
        # defaults it (Nov 2026 pilot finding).
        warnings.append(f"Line {line_number}: unit is missing; choose a supported unit or pack.")
    if line_total is None:
        warnings.append(f"Line {line_number}: line total is missing or unsupported.")
    # An unknown ingredient is not a parse failure: the item stays intact and
    # the store decides between dashboard correction and auto-create on approval.
    # A fuzzy recovery is recorded so the confirmation reply can show the guess.

    return {
        "ingredient_id": ingredient_id,
        "raw_name": raw_name,
        "quantity": quantity,
        "unit": unit,
        "line_total_idr": line_total,
        **({"matched_from": raw_name} if fuzzy and ingredient_id else {}),
    }, warnings


def _parse_line_clauses(line: str, line_number: int) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse one input line, splitting comma/conjunction lists into items.

    Owners write "kembung 25 ekor, mas 6 ekor" in one breath. The line is
    split on commas followed by whitespace and on "dan/lalu/kemudian/serta".
    For comma lists (real forwarded POs open with a greeting clause, "Nanti
    siang, kirim 2 gas, …"), quantified clauses that all parse produce the
    items and digit-less chatter prefixes are dropped. Conjunction lists
    keep the strict all-digits rule so dish-style names ("bumbu dan rempah
    3 kg") stay one item; anything else falls back to the single-clause
    parse, including its multi-clause ambiguity warning.
    """
    clauses = [clause.strip() for clause in _CLAUSE_SPLIT.split(line) if clause.strip()]
    has_comma_split = re.search(r",(?=\s)", line) is not None
    if has_comma_split and len(clauses) > 1:
        parsed = [(_parse_clause(clause, line_number), bool(re.search(r"\d", clause))) for clause in clauses]
        digit_parsed = [(item, warnings) for ((item, warnings), has_digit) in parsed if has_digit]
        # Every quantified clause must parse into a real item — a quantity or
        # a confident catalog match. A money-bearing clause with neither ("bawa
        # cash 2 jt" inside a greeting, fuzzy-matched or not) is chat context,
        # not an order, so the line falls back to the single-clause parse and
        # its chatter filter.
        if digit_parsed and all(
            item is not None
            and (
                item.get("quantity") is not None
                or (item.get("ingredient_id") is not None and "matched_from" not in item)
            )
            for item, _ in digit_parsed
        ):
            kept: list[dict[str, Any]] = []
            combined_warnings: list[str] = []
            for (item, clause_warnings), has_digit in parsed:
                if item is None:
                    continue
                # Keep quantified clauses and real catalog items; a digit-less
                # clause with neither ("Pagi mas. Selasa ini") is a greeting
                # prefix, not an item the owner meant to order.
                if has_digit or item.get("quantity") is not None or item.get("ingredient_id") is not None:
                    kept.append(item)
                    combined_warnings.extend(clause_warnings)
            if kept:
                return kept, combined_warnings
    if len(clauses) > 1 and all(re.search(r"\d", clause) for clause in clauses):
        parsed: list[tuple[dict[str, Any], list[str]]] = []
        for clause in clauses:
            item, clause_warnings = _parse_clause(clause, line_number)
            if item is None:
                parsed = []
                break
            parsed.append((item, clause_warnings))
        if parsed:
            combined_warnings = []
            for _, clause_warnings in parsed:
                combined_warnings.extend(clause_warnings)
            return [entry[0] for entry in parsed], combined_warnings
    item, line_warnings = _parse_clause(line, line_number)
    return ([] if item is None else [item]), line_warnings


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
    supplier = None
    for line_number, line in nonempty:
        # A supplier header line ("dari Dika:", "beli dari Pasar Pagi") names
        # the supplier for the following items and parses no item itself.
        header_match = _SUPPLIER_HEADER.match(line)
        if header_match:
            candidate = (header_match.group("supplier") or "").strip()
            candidate = candidate.rstrip(".,!:")
            if supplier is None and candidate:
                supplier = candidate
            continue
        # A trailing "dari <name>" clause names the supplier; strip it before
        # line parsing so the name never corrupts the item match.
        supplier_match = _SUPPLIER_SUFFIX.search(line)
        if supplier_match:
            candidate = supplier_match.group("supplier").strip().rstrip(".,!:")
            line = line[: supplier_match.start()].rstrip()
            if supplier is None and candidate:
                supplier = candidate
        if not line:
            # A supplier-only line ("dari Toko Sebelah") records the name and
            # carries no item to parse.
            continue
        line_items, line_warnings = _parse_line_clauses(line, line_number)
        items.extend(line_items)
        warnings.extend(line_warnings)
    result: dict[str, Any] = {"items": items, "warnings": warnings}
    if supplier:
        result["supplier"] = supplier
    return result


def is_chatter_item(item: dict[str, Any]) -> bool:
    """Detect a chat-context pseudo-item: a sentence parsed as one product.

    Greetings and market chatter that mention money ("halo, lagi ke pasar,
    bawa cash 2 jt") parse as a single item with a price but no catalog
    match, quantity, or unit, and a sentence-length name. Real product
    lines carry a quantity or unit, or name a short ingredient — including
    brand-new items ("beli kecap manis 30000") which must still draft.
    """
    if item.get("ingredient_id") is not None or item.get("quantity") is not None:
        return False
    name = (item.get("raw_name") or "").strip()
    if not name:
        return False
    words = [word for word in re.split(r"[\s,]+", name) if word]
    return len(words) >= 4


def normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize one parsed item using exact decimal arithmetic."""
    matched_from = item.get("matched_from")
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
    if isinstance(matched_from, str) and matched_from.strip():
        result["matched_from"] = matched_from.strip()[:200]
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
    elif qty is not None and unit in {"pcs", "buah"}:
        # Plain counts record as-is: a cake warung buys in pieces, and
        # inventory for countable items tracks counts, not mass. Pack units
        # (tray, ikat, dus...) still require a catalog pack conversion.
        result["canonical_quantity"] = _plain_decimal(qty)
        result["canonical_unit"] = unit
    elif unit is not None and unit not in {"kg", "g", "l", "ml", "pcs", "buah"}:
        warnings.append(f"Unit '{unit_raw}' is not supported for automatic normalization; choose a catalog pack conversion.")

    if ingredient_id not in _EXPECTED_UNIT and not str(item.get("raw_name") or "").strip():
        # With a raw name the store can auto-create the ingredient on approval;
        # without one, only a manual catalog selection can resolve the item.
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
