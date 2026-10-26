"""StepFun vision extraction for supplier invoice photos.

The model only proposes line items. Nothing it returns is trusted: quantities,
units, and totals are re-validated by service.extraction.normalize_item and
service.db.Store before a draft is stored. Values the photo does not show stay
None so the owner must type them in; the parser never invents a number.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from urllib.parse import urlsplit

import httpx

from .extraction import canonical_unit_name, match_catalog_name


DEFAULT_BASE_URL = "https://api.stepfun.com/v1"
DEFAULT_MODEL = "step-1o-turbo-vision"
MAX_ITEMS = 20
MAX_IMAGE_BYTES = 5 * 1024 * 1024
SUPPORTED_MEDIA_TYPES = ("image/jpeg", "image/png", "image/webp")

_MAGIC = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/webp": (b"RIFF",),
}
_NUMBER_TEXT = re.compile(r"\d+(?:[.,]\d{1,6})?")

PROMPT = """You read Indonesian supplier invoices (nota pembelian) for a small \
restaurant. Return one JSON object and nothing else, with this exact shape:

{"supplier": string|null,
 "items": [{"raw_name": string,
            "quantity": string|null,
            "unit": string|null,
            "line_total_idr": integer|null,
            "confidence": "high"|"low"}],
 "unreadable": [string]}

Rules, in priority order:
1. Copy numbers exactly as printed on that line. Never compute, convert,
   re-price, round, or infer a missing value. If a line does not print a
   quantity, a unit, or a line total, use null for that field.
2. One item per printed line. At most %d items. Ignore store headers,
   addresses, dates, invoice numbers, subtotals, totals, tax, discounts, and
   payment notes.
3. quantity is a plain decimal string ("2", "0.5", "1.25"). unit is one of
   kg, g, l, ml, ikat, karung, tray, pack, dus, pcs, buah. If the printed unit
   is different or absent, use null.
4. line_total_idr is the printed rupiah amount for that whole line, as a whole
   number without separators ("31000"). Ignore "Rp", dots, and spaces.
5. raw_name is the printed item name, lowercase, without the quantity or price.
6. Use "low" confidence when handwriting, glare, or a fold made the line hard
   to read, and add a short note to "unreadable".
7. The nota may also list goods unrelated to the restaurant. The "items"
   array must contain ONLY items used for restaurant or kitchen operations:
   food ingredients, seasonings, beverages for the menu, and cooking or
   packaging supplies. Never put a skipped item in "items". For every other
   good — snacks, cigarettes, personal care, household goods, medicine —
   add its name to "unreadable" prefixed with "skip:". When unsure whether
   an item is kitchen-related, keep it in "items".
""" % MAX_ITEMS


class StepFunVisionError(Exception):
    """Safe provider error that never includes the API key or response bodies."""

    def __init__(self, message, status=502):
        self.status = status
        super().__init__(message)


def validate_base_url(value):
    value = (value or DEFAULT_BASE_URL).strip().rstrip("/")
    parsed = urlsplit(value)
    parsed.port
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("STEPFUN_BASE_URL must be an HTTPS URL without credentials, query, or fragment.")
    return value


def validate_model(value):
    value = (value or DEFAULT_MODEL).strip()
    if not value or len(value) > 100 or any(char.isspace() for char in value):
        raise ValueError("STEPFUN_VISION_MODEL must be a single model name such as step-1o-turbo-vision.")
    return value


def _check_image(raw, media_type):
    """Check a decoded photo's size and magic bytes against its declared type."""
    if media_type not in SUPPORTED_MEDIA_TYPES:
        raise StepFunVisionError("Use a JPEG, PNG, or WEBP invoice photo.", 415)
    if not raw:
        raise StepFunVisionError("The invoice photo is empty.", 400)
    if len(raw) > MAX_IMAGE_BYTES:
        raise StepFunVisionError("The invoice photo exceeds 5 MiB; send a smaller photo.", 413)
    if not any(raw.startswith(prefix) for prefix in _MAGIC[media_type]):
        raise StepFunVisionError("The photo content does not match its media_type.", 415)
    return raw


def decode_image(data, media_type):
    """Decode a base64 photo and check its declared type against its magic bytes."""
    if media_type not in SUPPORTED_MEDIA_TYPES:
        raise StepFunVisionError("Use a JPEG, PNG, or WEBP invoice photo.", 415)
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise StepFunVisionError("image_base64 is not valid base64.", 400)
    return _check_image(raw, media_type)


def _quantity_text(value):
    """Accept a model number only if it is a plain positive decimal; else None."""
    if value is None or isinstance(value, bool):
        return None
    text = value.strip() if isinstance(value, str) else str(value) if isinstance(value, (int, float)) else None
    if not text or not _NUMBER_TEXT.fullmatch(text.replace(",", ".")):
        return None
    return text.replace(",", ".")


def _idr_amount(value):
    """Accept a printed rupiah amount; never derive one."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        amount = value
    elif isinstance(value, str):
        digits = re.sub(r"[.\s]", "", value.strip())
        if not digits.isdigit():
            return None
        amount = int(digits)
    else:
        return None
    if amount <= 0 or amount > 1_000_000_000_000:
        return None
    return amount


def _content_json(response):
    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise StepFunVisionError("StepFun vision returned an unexpected response.")
    if not isinstance(content, str) or not content.strip():
        raise StepFunVisionError("StepFun vision returned an empty result.")
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except ValueError:
        raise StepFunVisionError("StepFun vision did not return readable JSON; retake the photo.")


def clean_extraction(data):
    """Map the model JSON onto the same item shape the text parser produces."""
    if not isinstance(data, dict):
        raise StepFunVisionError("StepFun vision did not return an invoice object.")
    supplier = data.get("supplier")
    supplier = supplier.strip()[:200] if isinstance(supplier, str) and supplier.strip() else None
    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        raise StepFunVisionError("StepFun vision returned no item list.")
    warnings = []
    if len(raw_items) > MAX_ITEMS:
        return {
            "supplier": supplier,
            "items": [],
            "warnings": [f"The photo shows more than {MAX_ITEMS} lines; split the invoice into separate photos."],
        }

    items = []
    for index, raw in enumerate(raw_items, 1):
        label = f"Line {index}"
        if not isinstance(raw, dict):
            warnings.append(f"{label}: the photo line could not be read; add it manually.")
            continue
        name = raw.get("raw_name")
        name = name.strip()[:200] if isinstance(name, str) else ""
        if not name:
            warnings.append(f"{label}: no item name was read from the photo.")
            continue
        raw_name, ingredient_id = match_catalog_name(name)
        quantity = _quantity_text(raw.get("quantity"))
        unit = canonical_unit_name(raw.get("unit"))
        total = _idr_amount(raw.get("line_total_idr"))
        if quantity is None:
            warnings.append(f"{label}: quantity is missing or unreadable; type it in.")
        if unit is None:
            warnings.append(f"{label}: unit is missing or unsupported.")
        if total is None:
            warnings.append(f"{label}: line total is missing or unreadable; type it in.")
        if ingredient_id is None:
            warnings.append(f"{label}: unknown ingredient '{raw_name}'; select a catalog item manually.")
        if raw.get("confidence") == "low":
            warnings.append(f"{label}: the photo was partly unreadable; verify this line.")
        items.append({
            "ingredient_id": ingredient_id,
            "raw_name": raw_name,
            "quantity": quantity,
            "unit": unit,
            "line_total_idr": total,
        })
    for note in data.get("unreadable") or []:
        if isinstance(note, str) and note.strip():
            warnings.append(f"Photo note: {note.strip()[:200]}")
    return {"supplier": supplier, "items": items, "warnings": warnings}


async def extract_invoice(image, *, media_type="image/jpeg", api_key, model=DEFAULT_MODEL, base_url=DEFAULT_BASE_URL, client=None):
    """Send one invoice photo to StepFun and return proposed draft items."""
    if not api_key or not api_key.strip():
        raise StepFunVisionError("StepFun vision is not configured.", 503)
    base_url = validate_base_url(base_url)
    model = validate_model(model)
    image = _check_image(image, media_type)
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{base64.b64encode(image).decode('ascii')}"}},
            ],
        }],
    }
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=60.0, follow_redirects=False)
    try:
        response = await client.post(
            base_url + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
    except httpx.HTTPError:
        raise StepFunVisionError("StepFun vision could not be reached; try again.")
    finally:
        if owns_client:
            await client.aclose()
    if response.status_code != 200:
        status = 429 if response.status_code == 429 else 502
        raise StepFunVisionError(f"StepFun vision rejected the photo (HTTP {response.status_code}).", status)
    return clean_extraction(_content_json(response))
