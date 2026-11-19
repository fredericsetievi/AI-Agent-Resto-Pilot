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
    "ltr": "l",
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
    rf"(?ix)\s+(?:rp\s*)?(?P<amount>{_NUMBER})\s*(?P<scale>rb|ribu|jt|juta|k)\s*$"
)
# An "x" multiplier quotes a per-unit price ("minyak 2L x 38rb" = two liters
# at Rp38.000 each). The x must be a standalone token after whitespace or a
# digit, so "box 3000" keeps its name.
_X_PRICE = re.compile(
    r"(?ix)(?<=[\s\d])(?:x|×)\s*(?:rp\s*)?(?P<amount>\d+(?:\.\d{3})*)\s*(?P<scale>rb|ribu|jt|juta|k)?\s*$"
)
# A trailing "bayar/total <amount>" is a payment statement for the clause; on
# a multi-item line the store treats it as the order total, not the last item's
# price. Handled before the generic price suffix so "bayar" never joins a name.
_PAYMENT_TOTAL = re.compile(
    r"(?ix)\s+(?P<word>total|bayar)\s+(?:rp\s*)?(?P<amount>\d+(?:\.\d{3})*)\s*(?P<scale>rb|ribu|jt|juta|k)?\s*$"
)
# A clause that is only a price annotation ("total 66rb", "bayar 250rb",
# "harga 150000") carries no item; the amount belongs to the other items.
_KEYWORD_AMOUNT = re.compile(
    r"(?ix)^(?P<word>total|bayar|harga)\s+(?:rp\s*)?(?P<amount>\d+(?:\.\d{3})*)\s*(?P<scale>rb|ribu|jt|juta|k)?\s*$"
)
# A trailing "per kg"/"/kg" restates that the price is per unit ("telur 1 kg
# 30000 per kg"); it is an annotation, not instruction language, so strip it
# before the source-only modifier check instead of rejecting the line.
_PER_UNIT_TAIL = re.compile(
    r"(?ix)\s*(?:/|\bper\b)\s*(?:kg|kilo|g|gr|gram|l|ltr|liter|ml|pcs|buah|ekor|ikat|pack|pak|dus)\s*$"
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
# A trailing named-place clause without 'dari' ("beli cabai 5 kg 200rb toko
# abadi") also names the supplier. Strict on purpose: a supplier noun must
# open the clause, only letters may follow, and the clause must close the
# line — quantities, prices, and item names are never eaten.
_SUPPLIER_PLACE_SUFFIX = re.compile(
    r"(?i)\s+(?P<supplier>(?:toko|warung|pasar|supplier|pemasok|bu|pak|mbak|mas)\s+"
    r"[a-z][a-z.\-']*(?:\s+[a-z][a-z.\-']*){0,3})[.!?,]*\s*$")
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
# Time openers before the order verb ("tadi pagi beli telur 2kg", "kemarin
# beli gula 1 kg") are chat, not the item name; strip them so the name
# matches and the owner does not get a phantom "tadi pagi beli telur" row.
_TIME_PREFIX = re.compile(r"(?i)^(?:tadi\s+(?:pagi|siang|sore|malam)|kemarin|nanti(?:\s+(?:pagi|siang|sore|malam))?|barusan)\s+")
# Payment statements lead follow-ups ("sudah bayar telur 30000"); the "bayar"
# word is about money already spent, not the item name. Strip the prefix so
# "telur 30000" parses as a flagged item row instead of a pseudo-item called
# "sudah bayar telur" with quantity 30000. Requires a letter after it so a
# payment-only clause ("bayar 30000") keeps its payment handling.
_PAYMENT_STATEMENT_PREFIX = re.compile(r"(?i)^(?:sudah|udah|udh)?\s*bayar(?:an)?\s+(?=[a-z])")
# Messages close with courtesy ("... 10 dus ya 🙏"); trailing emoji and
# politeness words are chatter, not part of the item name or quantity.
_SYMBOL_TAIL = re.compile(r"[^\w\s]+\s*$")
_POLITENESS_TAIL = re.compile(r"(?i)\s+(?:ya|y|bisa|oke|ok|sip|gan|min|makasih|terima\s+kasih|mantap|siap)$")
# Owners quote a per-unit price with "@" ("Sunco 10 dus @15rb" = ten crates
# at Rp15.000 each); it multiplies by the quantity like a bare per-unit
# price, while "@2ltr" size markers never match (no rb/jt scale and no
# thousand-sized bare amount).
_AT_PRICE = re.compile(
    r"(?ix)@\s*(?:rp\s*)?(?P<amount>\d+(?:\.\d{3})*)\s*(?P<scale>rb|ribu|jt|juta|k)?\s*$"
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
# Owners glue the count onto the name in one word ("telur2kg", "beras10kg");
# the trailing digits+unit tail is a quantity, not part of the ingredient
# name. The unit must be a known unit word so "kopi25rb" (a price scale, not
# a quantity) and "b12vitamin" are never split.
_GLUED_QUANTITY = re.compile(
    rf"(?i)(?P<name>[a-z]{{2,}})(?P<quantity>{_NUMBER})(?P<unit>[a-zA-Z]+)\s*$"
)
# An "@<size><unit>" marker ("Sunco @2ltr 10 dus") names the package contents;
# it is not a per-unit price (that needs an rb/rp-style scale) and not part
# of the ingredient name. Volume/mass units only.
_PACK_SIZE_MARKER = re.compile(
    r"(?i)@\s*(?P<size>" + _NUMBER + r")\s*(?P<unit>ltr|liter|litre|lt|kg|gr|ml|g|l)\b"
)
# A quantity range ("cabai 3-4 kg", "3 s/d 4 kg", "2 sampai 3 dus") is an
# estimate; the lower bound is used and the owner is asked to confirm.
_RANGE_SEP = r"(?:-|–|—|s/?d|sampai)"
_RANGE_QUANTITY_END = re.compile(
    rf"(?i)(?P<lo>{_NUMBER})\s*{_RANGE_SEP}\s*(?P<hi>{_NUMBER})\s*(?P<unit>[a-zA-Z]+)?\s*$"
)
_RANGE_QUANTITY_START = re.compile(
    rf"(?i)^(?P<lo>{_NUMBER})\s*{_RANGE_SEP}\s*(?P<hi>{_NUMBER})\s*(?P<unit>[a-zA-Z]+)\s+(?P<rest>.+)$"
)
# Junk lines in forwarded messages: banners, contact lines, bon headers, and
# "= gloss" definitions carry no item and must never draft a phantom product.
# A gloss with digits ("telur 2 kg = 30rb") stays: the digits may be the
# quantity and price of a real line.
_JUNK_LINE = re.compile(
    r"(?ix)"
    r"^\s*fwd?\s*[:：]"                                  # "Fwd: pesanan"
    r"|(?:^|\s)(?:hubungi|kontak|whatsapp|wa\s*[:：]).*\d"  # contact line with a number
    r"|^\s*bon\b[^\d]*$"                                 # "Bon jika barang tersedia"
    r"|^[^\d]*=[^\d]*$"                                  # "Isul = air isi ulang ..." (no digits)
    r"|\b\d{9,}\b"                                       # bare phone number
)
# Conversational phrasing for is_chatter_item: greetings and questions that
# owners mix into purchase messages ("Papa pagi ini beli", "bagaimana
# isinya, ci?"). A parsed pseudo-item matching this is chat context, never
# stock — it stays visible as chatter but never reaches the catalog.
_CHAT_LINE = re.compile(
    r"(?ix)"
    r"^\s*(?:papa|pa|mama|bu|pak|om|gan|ci|cici?|halo|hallo|hai|hello|hei|hay"
    r"|ok(?:e|ay)?|siap|sip|siipp?|mantap|siipp|makasih|makasi|terima\s*kasih"
    r"|thanks?|thank\s*you|selamat|pagi|siang|sore|malam|assalamualaikum)\b"
    r"|^\s*(?:bagaimana|gimana|kenapa|mengapa|kok|apakah|siapa|kapan|mana)\b"
    r"|\b(?:berapa|yang\s+mana)\b"
    r"|\?\s*$"
)
# A purchase sentence with a tacked-on question ("... 25rb. btw stok beras
# masih berapa?") loses the price when the question tail rides along; the
# tail is chat, not part of the order line.
_QUESTION_TAIL = re.compile(r"(?i)[,.;:]?\s+(?:btw|oh\s+ya|oia)\b.*$")
# Owners announcing a new catalog item lead with the intent ("pengen nambah
# bahan baru: kecap manis 500ml"); the prefix is intent, not the item name.
_NEW_INGREDIENT_PREFIX = re.compile(
    r"(?i)^(?:nambahin|nambah|masukin|masukkan|tambahin|tambah(?:kan)?)?\s*bahan\s+baru\s*[:：]?\s*"
)


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


def _scaled_amount(amount: str, scale: str | None) -> int | None:
    """Resolve "38", "38rb", "38.000" style amounts to a whole IDR value."""
    digits = amount.replace(".", "")
    if len(digits) > 13:
        return None
    value = int(digits)
    scale_fold = (scale or "").casefold()
    multiplier = 1_000 if scale_fold in {"rb", "ribu", "k"} else 1_000_000 if scale_fold in {"jt", "juta"} else 1
    total = value * multiplier
    if total <= 0 or total > _MAX_TOTAL:
        return None
    return total


def _parse_price(text: str) -> tuple[str, int | None, str | None, bool, str | None]:
    """Return remaining text, integer IDR amount, an optional problem, a flag
    telling whether the amount is a per-unit price, and the payment-marker word
    ("total"/"bayar") when the amount was announced as a payment statement."""
    # "minyak 2L x 38rb" quotes a per-unit price through an x multiplier; the
    # amount multiplies by the quantity like an "@" price would. Without an
    # rb/rp-style scale the amount must still read as a price (>= 1000), so
    # the "x5" count notation from order forms is never mistaken for one.
    match = _X_PRICE.search(text)
    if match:
        digits = match.group("amount").replace(".", "")
        scale = (match.group("scale") or "").casefold()
        if len(digits) <= 13 and (scale or int(digits) >= 1000):
            total = _scaled_amount(match.group("amount"), match.group("scale"))
            if total is None:
                return text[: match.start()].strip(), None, "Price must be positive and no greater than 1000000000000 IDR.", True, None
            return text[: match.start()].strip(), total, None, True, None

    # "gula merah 3kg bayar 250rb" announces a payment for the clause; the
    # marker word lets multi-item lines treat it as the order total.
    match = _PAYMENT_TOTAL.search(text)
    if match:
        total = _scaled_amount(match.group("amount"), match.group("scale"))
        if total is None:
            return text[: match.start()].strip(), None, "Price must be positive and no greater than 1000000000000 IDR.", False, None
        return text[: match.start()].strip(), total, None, False, match.group("word").casefold()

    match = _PRICE_SUFFIX.search(text)
    if match:
        amount = _decimal_text(match.group("amount"))
        if amount is None:
            return text[: match.start()].strip(), None, "Invalid or out-of-range price; enter a positive amount.", False, None
        scale = match.group("scale").casefold()
        multiplier = Decimal(1_000 if scale in {"rb", "ribu", "k"} else 1_000_000)
        total = amount * multiplier
        if total != total.to_integral_value() or total > _MAX_TOTAL:
            return text[: match.start()].strip(), None, "Price must resolve to a whole IDR amount no greater than 1000000000000.", False, None
        return text[: match.start()].strip(), int(total), None, False, None

    # "@15rb" / "@Rp15.000" quotes the per-unit price ("Sunco 10 dus @15rb");
    # bare_unit multiplication by the quantity happens in _parse_clause.
    match = _AT_PRICE.search(text)
    if match:
        digits = match.group("amount").replace(".", "")
        if len(digits) > 13:
            return text[: match.start()].strip(), None, "Price is too long; enter a whole IDR amount no greater than 1000000000000.", False, None
        amount = int(digits)
        scale = (match.group("scale") or "").casefold()
        multiplier = 1_000 if scale in {"rb", "ribu", "k"} else 1_000_000 if scale in {"jt", "juta"} else 1
        total = amount * multiplier
        if not scale and amount < 1000:
            pass  # "@2"-style size marker, not a price: leave it in the name
        elif total <= 0 or total > _MAX_TOTAL:
            return text[: match.start()].strip(), None, "Price must be positive and no greater than 1000000000000 IDR.", False, None
        else:
            return text[: match.start()].strip(), total, None, True, None

    match = _RP_INTEGER.search(text)
    if match:
        digits = match.group("amount").replace(".", "")
        if len(digits) > 13:
            return text[: match.start()].strip(), None, "Price is too long; enter a whole IDR amount no greater than 1000000000000.", False, None
        total = int(digits)
        if total <= 0 or total > _MAX_TOTAL:
            return text[: match.start()].strip(), None, "Price must be positive and no greater than 1000000000000 IDR.", False, None
        # An Rp-prefixed amount is always a lot total (per-piece prices with
        # the Rp marker would be written "Rp2.500/pcs" — unsupported nuance).
        return text[: match.start()].strip(), total, None, False, None

    # A bare integer after an explicit quantity reads as a per-unit price —
    # owners quote prices per piece ("banana cake 10 2500" means Rp2.500
    # each, not a Rp250 piece; Nov 2026 pilot finding). Lot totals go through
    # the "total"/"bayar"/Rp/rb markers handled above.
    match = _UNPREFIXED_INTEGER.search(text)
    if match:
        prefix = text[: match.start()]
        if _TRAILING_QUANTITY.search(prefix):
            digits = match.group("amount").replace(".", "")
            if len(digits) > 13:
                return text[: match.start()].strip(), None, "Price is too long; enter a whole IDR amount no greater than 1000000000000.", False, None
            bare = int(digits)
            if bare >= 1000:
                return prefix.strip(), bare, None, True, None
            return text[: match.start()].strip(), None, "Unprefixed whole-number price is unsupported; use Rp or an rb/ribu/jt/juta suffix.", False, None

    # A malformed currency token is not silently treated as part of an item name.
    if _CURRENCY_MARKER.search(text):
        return re.sub(r"(?i)\s+rp\s*[\d.,]+\s*$", "", text).strip(), None, "Could not safely read the Rp price. Use Rp followed by an integer amount.", False, None
    return text.strip(), None, None, False, None


def _collapse_quantity_range(text: str) -> tuple[str, str | None]:
    """Replace an estimated quantity range with its lower bound, plus a note.

    "cabai 3-4 kg" and "3 s/d 4 kg cabai" are market estimates: the owner
    writes the span before buying. The lower bound is a safe draft amount and
    the returned problem text asks the owner to confirm the real figure.
    """
    match = _RANGE_QUANTITY_END.search(text)
    if match:
        lo = _decimal_text(match.group("lo"))
        unit = _UNIT_ALIASES.get((match.group("unit") or "").casefold())
        if lo is None or (match.group("unit") and unit is None):
            return text, None
        hi = _plain_decimal(_decimal_text(match.group("hi")) or Decimal(0))
        replacement = f"{_plain_decimal(lo)} {unit or ''}".strip()
        return (
            text[: match.start()].rstrip() + " " + replacement,
            f"quantity range {_plain_decimal(lo)}-{hi}{(' ' + unit) if unit else ''} was estimated; the lower bound is used — confirm the real amount.",
        )
    match = _RANGE_QUANTITY_START.match(text)
    if match:
        lo = _decimal_text(match.group("lo"))
        unit = _UNIT_ALIASES.get(match.group("unit").casefold())
        if lo is None or unit is None:
            return text, None
        rest = match.group("rest").strip()
        replacement = f"{_plain_decimal(lo)} {unit}".strip()
        return (
            f"{replacement} {rest}".strip(),
            f"quantity range {_plain_decimal(lo)}-{_plain_decimal(_decimal_text(match.group('hi')) or Decimal(0))} {unit} was estimated; the lower bound is used — confirm the real amount.",
        )
    return text, None


def _parse_quantity(text: str) -> tuple[str, str | None, str | None, str | None]:
    # An estimated range ("3-4 kg") collapses to its lower bound with a note.
    text, range_problem = _collapse_quantity_range(text)
    # Delivery notes lead with the count ("kirim 2 gas, 3 melon"); a leading
    # number followed by a known unit word is the quantity, otherwise the
    # word opens the name ("2 gas" = gas, quantity 2).
    leading = _LEADING_QUANTITY.match(text)
    if leading:
        quantity = _decimal_text(leading.group("quantity"))
        unit_text = leading.group("unit")
        rest = (leading.group("rest") or "").strip()
        unit_fold = (unit_text or "").casefold()
        if quantity is not None and unit_fold not in {"rb", "ribu", "jt", "juta", "k"}:
            canonical = _LEADING_UNIT_ALIASES.get(unit_fold) if unit_text else None
            if canonical:
                if rest:
                    return rest, _plain_decimal(quantity), canonical, range_problem
                # A bare "1 ikat" carries a quantity but no item name; the
                # empty remainder fails name matching and the clause is
                # rejected instead of drafting a product called "ikat".
                return "", _plain_decimal(quantity), canonical, range_problem
            # Not a unit word: it belongs to the item name ("2 gas", "3 telur").
            name = (f"{unit_text} {rest}" if unit_text else rest).strip()
            if name:
                return name, _plain_decimal(quantity), None, range_problem
    match = _TRAILING_QUANTITY.search(text)
    if not match:
        # No whitespace-separated count either: owners glue the count onto
        # the name ("telur2kg", "beras10kg"); the digit+unit tail is a
        # quantity, not part of the ingredient name.
        glued = _GLUED_QUANTITY.search(text)
        if glued:
            glued_quantity = _decimal_text(glued.group("quantity"))
            glued_unit = _UNIT_ALIASES.get(glued.group("unit").casefold())
            if glued_quantity is not None and glued_unit and not _SIZE_QUALIFIER_TAIL.search(text[: glued.start()]):
                name = (text[: glued.start()] + glued.group("name")).strip()
                return name, _plain_decimal(glued_quantity), glued_unit, range_problem
        return text.strip(), None, None, range_problem
    if _SIZE_QUALIFIER_TAIL.search(text[: match.start()]):
        # "kemasan 500gr", "isi 250ml", and "@2ltr" describe the package
        # size, not how many packages the owner wants.
        return text.strip(), None, None, range_problem
    quantity = _decimal_text(match.group("quantity"))
    unit_text = match.group("unit")
    if quantity is None:
        return text[: match.start()].strip(), None, _UNIT_ALIASES.get((unit_text or "").casefold(), unit_text.casefold() if unit_text else None), "Invalid quantity; use a positive number with at most six decimal places."
    unit = None
    if unit_text:
        unit = _UNIT_ALIASES.get(unit_text.casefold(), unit_text.casefold())
    return text[: match.start()].strip(), _plain_decimal(quantity), unit, range_problem


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
    # but only when the same word opens the name and the token counts match:
    # a slip recovers the whole name, not one word of it. "bacang ayam beras"
    # merely looks like "bawang" on its first token — the rest names a
    # different product, and guessing "bawang" from that overlap files wrong
    # stock. Such names stay unmapped with the raw name intact.
    scored: list[tuple[float, str]] = []
    head = tokens[0]
    for alias, canonical_id in _ALIASES:
        alias_tokens = [token for token in _NAME_TOKEN.findall(alias) if token not in _NAME_NOISE]
        if not alias_tokens or alias_tokens[0][0] != head[0]:
            continue
        if len(alias_tokens) != len(tokens):
            continue
        scored.append((SequenceMatcher(a=" ".join(tokens), b=" ".join(alias_tokens)).ratio(), canonical_id))
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
    # A trailing "per kg"/"/kg" restates the per-unit price basis; strip it so
    # it is not mistaken for instruction language and the line still parses.
    for _ in range(2):
        stripped = _PER_UNIT_TAIL.sub("", source).rstrip()
        if stripped == source:
            break
        source = stripped
    if not source:
        return None, [f"Line {line_number}: no item name was found."]
    # A clause that is only a price annotation ("total 66rb", "bayar 250rb",
    # "harga 150000") carries no item: return the amount so the line parser
    # can attach it to the other items or flag a shared total.
    keyword = _KEYWORD_AMOUNT.match(source)
    if keyword:
        amount = _scaled_amount(keyword.group("amount"), keyword.group("scale"))
        if amount is None:
            return None, [f"Line {line_number}: {keyword.group('word')} amount must be a positive whole IDR amount."]
        return {"price_marker": keyword.group("word").casefold(), "raw_name": keyword.group("word").casefold(), "line_total_idr": amount}, warnings
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
        stripped = _TIME_PREFIX.sub("", source).strip()
        if stripped == source:
            break
        source = stripped
    for _ in range(4):
        stripped = _PAYMENT_STATEMENT_PREFIX.sub("", source).strip()
        if stripped == source:
            break
        source = stripped
    for _ in range(4):
        stripped = _ORDER_PREFIX.sub("", source).strip()
        if stripped == source:
            break
        source = stripped
    if not source:
        return None, [f"Line {line_number}: no item name was found."]
    for _ in range(2):
        stripped = _NEW_INGREDIENT_PREFIX.sub("", source).strip()
        if stripped == source:
            break
        source = stripped
    if not source:
        return None, [f"Line {line_number}: no item name was found."]
    # An "@<size><unit>" marker ("Sunco @2ltr 10 dus") quotes the package
    # contents; it must be lifted out before price parsing so it never lands
    # in the item name, and the store multiplies it into the stock movement.
    pack_size = pack_unit = None
    pack_match = _PACK_SIZE_MARKER.search(source)
    if pack_match:
        pack_size = _decimal_text(pack_match.group("size"))
        pack_unit = _UNIT_ALIASES.get(pack_match.group("unit").casefold())
        source = re.sub(r"\s{2,}", " ", (source[: pack_match.start()] + " " + source[pack_match.end():])).strip()
        if pack_size is None or pack_unit is None:
            pack_size = pack_unit = None
    if not source:
        return None, [f"Line {line_number}: no item name was found."]

    # Price is parsed first so its numeric amount can never become a quantity.
    remainder, price_value, price_problem, bare_unit, price_marker = _parse_price(source)
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
        **({"price_marker": price_marker} if price_marker else {}),
        **({"matched_from": raw_name} if fuzzy and ingredient_id else {}),
        **({"pack_size": _plain_decimal(pack_size), "pack_unit": pack_unit} if pack_size is not None and pack_unit else {}),
    }, warnings


_PRICE_MARKER_WORDS = {"total", "bayar", "harga"}


def _is_price_marker_item(item: dict[str, Any]) -> bool:
    """A keyword-only clause ("total 66rb") is an annotation, not an item."""
    return (
        item.get("price_marker") is not None
        and item.get("quantity") is None
        and (item.get("raw_name") or "").casefold() in _PRICE_MARKER_WORDS
    )


def _resolve_price_markers(items: list[dict[str, Any]], warnings: list[str]) -> list[dict[str, Any]]:
    """Resolve price-annotation clauses on a multi-clause line.

    A keyword-only clause ("... kecombrang, total 66rb") or a trailing
    "bayar" amount belongs to the order, not to the item it happens to
    follow. With exactly one real item the amount becomes that item's lot
    total; with several items the amount is dropped and flagged instead of
    being misattributed to the last line (Nov 2026 pilot finding).
    """
    has_markers = any(_is_price_marker_item(item) for item in items) or any(
        item.get("price_marker") == "bayar" and item.get("line_total_idr") is not None for item in items
    )
    if not has_markers:
        return items
    kept: list[dict[str, Any]] = []
    amount: int | None = None
    for item in items:
        if _is_price_marker_item(item):
            amount = item.get("line_total_idr") or amount
            continue
        if item.get("price_marker") == "bayar" and item.get("line_total_idr") is not None:
            amount = item.get("line_total_idr")
            item = {**item, "line_total_idr": None, "price_marker": None}
        kept.append(item)
    if amount is None or not kept:
        if amount is not None:
            warnings.append(f"Rp{amount} has no item line to attach to; write the item line first.")
        return kept
    if len(kept) == 1 and kept[0].get("line_total_idr") is None:
        # The lone item's "line total is missing" warning is now moot: the
        # keyword clause supplied the amount.
        warnings[:] = [w for w in warnings if "line total is missing" not in w.casefold()]
        return [{**kept[0], "line_total_idr": amount}]
    warnings.append(
        f"Rp{amount} is the total for several items; enter per-item prices on the dashboard before approving."
    )
    return kept


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
                or item.get("price_marker") is not None
                or (item.get("ingredient_id") is not None and "matched_from" not in item)
            )
            for item, _ in digit_parsed
        ):
            kept: list[dict[str, Any]] = []
            combined_warnings: list[str] = []
            for (item, clause_warnings), has_digit in parsed:
                if item is None:
                    continue
                # Keep quantified clauses, real catalog items, and price
                # annotations; a digit-less clause with neither ("Pagi mas.
                # Selasa ini") is a greeting prefix, not an item.
                if has_digit or item.get("quantity") is not None or item.get("ingredient_id") is not None or item.get("price_marker") is not None:
                    kept.append(item)
                    combined_warnings.extend(clause_warnings)
            if kept:
                return _resolve_price_markers(kept, combined_warnings), combined_warnings
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
            return _resolve_price_markers([entry[0] for entry in parsed], combined_warnings), combined_warnings
    item, line_warnings = _parse_clause(line, line_number)
    if item is None:
        return [], line_warnings
    resolved = _resolve_price_markers([item], line_warnings)
    return resolved, line_warnings


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
        # A purchase sentence with a tacked-on question ("... 25rb. btw stok
        # beras masih berapa?") loses its price to the question tail; cut the
        # chat tail before parsing. A message that is only the question (no
        # digits before it) is left alone for the stock-question matcher.
        tail_match = _QUESTION_TAIL.search(line)
        if tail_match and re.search(r"\d", line[: tail_match.start()]):
            line = line[: tail_match.start()].rstrip(" ,.;:")
        # Junk lines — forwarded banners, contact lines, bon headers, "= "
        # glosses — are chat formatting, not items; drop them instead of
        # drafting phantom products.
        if _JUNK_LINE.search(line):
            continue
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
        # Same position, without 'dari': a strict trailing named-place clause
        # ("... 200rb toko abadi", "... 50rb bu siti"). Checked after 'dari'
        # and only when the clause closes the line, so item text is untouched.
        place_match = _SUPPLIER_PLACE_SUFFIX.search(line)
        if place_match:
            candidate = place_match.group("supplier").strip().rstrip(".,!:")
            line = line[: place_match.start()].rstrip()
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
    # Short conversational sentences ("bagaimana isinya, ci?", "pagi ci",
    # "stok beras masih berapa?") are chat even below the four-word bar:
    # greeting/question phrasing on a line with no quantity, unit, or price
    # never names stock. Priceless-but-real lines ("kecap manis") match no
    # pattern and still draft to ask for prices.
    if _CHAT_LINE.search(name):
        return True
    words = [word for word in re.split(r"[\s,]+", name) if word]
    return len(words) >= 4


def is_chat_line_item(item: dict[str, Any]) -> bool:
    """Stricter chatter test for draft creation.

    Only greeting/question phrasing drops a line here ("Papa pagi ini beli",
    "bagaimana isinya, ci?"): approving such a draft would file the sentence
    as a catalog ingredient. The four-word sentence rule is deliberately NOT
    applied — the LLM-rescue fallback keeps flexible sentences as unmodelled
    draft rows for owner review, and those must survive.
    """
    if item.get("ingredient_id") is not None or item.get("quantity") is not None:
        return False
    name = (item.get("raw_name") or "").strip()
    return bool(name) and bool(_CHAT_LINE.search(name))


# Forwarded supplier messages (PO lists, pasted notas) regularly exceed the
# 20-line input cap. The flag-gated LLM router reads them chunk by chunk; the
# bound below limits one merged draft so a runaway forward cannot produce an
# unusable wall of rows.
FORWARDED_MAX_ITEMS = 50


def split_message_chunks(text: str, max_lines: int = 20) -> list[str]:
    """Split a long multi-line message into cap-sized chunks of non-empty lines.

    Uses the same line separators as extract_purchase, so a chunk that fits
    here also fits the parsers' 20-line cap.
    """
    if not isinstance(text, str):
        return []
    lines = [line.strip() for line in re.split(r"[\r\n;]+", text) if line.strip()]
    return ["\n".join(lines[index:index + max_lines]) for index in range(0, len(lines), max_lines)]


def has_itemized_purchase_signal(text: str, min_lines: int = 3) -> bool:
    """True when enough lines carry digits to look like an itemized list.

    A long forward without numbers (chat log, article) never triggers a model
    trip; an itemized supplier list always quotes quantities or prices.
    """
    if not isinstance(text, str):
        return False
    lines = [line for line in re.split(r"[\r\n;]+", text) if line.strip()]
    return sum(1 for line in lines if re.search(r"\d", line)) >= min_lines


def has_uncontextual_quantity_items(extracted: dict[str, Any]) -> bool:
    """True when the parser turned chat into pseudo-purchase rows.

    The escape hatch past is_chatter_item: a sentence ("reminder ke saya
    setiap malam jam") plus a number that is really a time or label ("jam 8")
    parses as one row with a quantity but no price. Every chatter guard
    requires quantity to be None, so the row passes and a garbage draft is
    filed. Real purchase lines are short names or catalog matches; only when
    EVERY row is a sentence-length name without any price in the whole draft
    does the message deserve a context-gate re-read.
    """
    items = (extracted or {}).get("items") or []
    if not items or any(item.get("line_total_idr") is not None for item in items):
        return False
    if not any(item.get("quantity") is not None or item.get("unit") is not None for item in items):
        return False
    for item in items:
        if item.get("ingredient_id") is not None:
            return False
        name = str(item.get("raw_name") or "").strip()
        # A short name plus a count reads as a real line, even a brand-new
        # one ("beli kecap manis 2 botol"); only sentence-length rows gate.
        if len(name.split()) <= 3:
            return False
    return True


def _pack_conversion(item: dict[str, Any]) -> tuple[Decimal | None, str | None]:
    """Validate an owner-quoted pack size ("pack_size": "2", "pack_unit": "l")."""
    size = item.get("pack_size")
    pack_unit = item.get("pack_unit")
    if isinstance(size, bool) or not isinstance(size, (str, int, Decimal)) or not isinstance(pack_unit, str):
        return None, None
    return _decimal_text(str(size)), canonical_unit_name(pack_unit)


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
    elif qty >= 1000:
        # A real warung purchase is never 1,000+ of a unit; a value that big
        # is a misread count ("jam 8", a price read as quantity) or a typo.
        # Flag it so it needs clarification instead of looking legitimate.
        warnings.append("Quantity is unusually large (1,000 or more); double-check it.")

    raw_name = item.get("raw_name")
    if isinstance(raw_name, str) and len(raw_name.strip()) > 60:
        # A sentence pasted as an item name ("sudah bayar telur", a copy of a
        # message) would create a garbage catalog ingredient on approval.
        warnings.append("Item name is unusually long; verify this row.")

    unit_raw = item.get("unit")
    unit = _UNIT_ALIASES.get(unit_raw.casefold(), unit_raw.casefold()) if isinstance(unit_raw, str) else None
    if unit_raw is None:
        warnings.append("Unit is missing.")

    # An owner-quoted package contents ("Sunco @2ltr 10 dus") rides through
    # normalization so the draft shows what the pack conversion means.
    pack_size, pack_unit = _pack_conversion(item)
    if pack_size is not None and pack_unit:
        result["pack_size"] = _plain_decimal(pack_size)
        result["pack_unit"] = pack_unit

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
        # Pack units (dus, box, karung...) need a conversion. An owner-quoted
        # pack size ("@2ltr") carries it: multiply the package count by the
        # pack contents when the pack unit is mass/volume and matches the
        # catalog dimension; otherwise the conversion warning stands.
        base = pack_unit if pack_unit in {"kg", "g", "l", "ml"} else None
        expected = _EXPECTED_UNIT.get(ingredient_id)
        dimension = "mass" if base in {"kg", "g"} else "volume" if base in {"l", "ml"} else None
        expected_dimension = "mass" if expected == "kg" else "volume" if expected == "l" else None
        if base and qty is not None and pack_size is not None and (expected is None or expected_dimension == dimension):
            factor = Decimal(1000) if base in {"g", "ml"} else Decimal(1)
            total = Decimal(qty) * pack_size / factor
            result["canonical_quantity"] = _plain_decimal(total)
            result["canonical_unit"] = "kg" if base in {"kg", "g"} else "l"
        else:
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
    if total is not None and canonical_quantity is not None:
        # Computed whenever both sides are valid, even with other warnings —
        # an absurd-quantity row still shows its implied unit price. Quantized
        # to cents: "beras 999999 kg 1000rb" must not render as
        # "Rp1,000001000001..." (division precision garbage).
        normalized_quantity = Decimal(canonical_quantity)
        price = (Decimal(total) / normalized_quantity).quantize(Decimal("0.01"))
        result["unit_price_idr"] = _plain_decimal(price)
    return result
