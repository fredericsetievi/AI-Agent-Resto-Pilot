"""StepFun text rescue for free-form purchase messages.

The rules parser in service.extraction only reads explicit, single-item
clauses in a fixed word order. This module lets the model normalize messy
messages ("tolong catat beli 150 ribu cabai 3kg nih") into the same item
shape. The model only proposes line items. Nothing it returns is trusted:
quantities, units, and totals are re-validated here with the same checks the
vision path uses, and values the sender did not clearly state stay None so
the owner must type them in; the parser never invents a number.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

import httpx

from .extraction import canonical_unit_name, match_catalog_name
from .vision import validate_base_url


DEFAULT_BASE_URL = "https://api.stepfun.com/v1"
DEFAULT_MODEL = "step-3.7-flash"
MAX_ITEMS = 20
MAX_MESSAGE_CHARS = 4000

_NUMBER_TEXT = re.compile(r"\d+(?:[.,]\d{1,6})?")

PROMPT = """You read informal Indonesian purchase messages for a small \
restaurant. The sender may use greetings, filler words, and any word order. \
Return one JSON object and nothing else, with this exact shape:

{"items": [{"raw_name": string,
            "quantity": string|null,
            "unit": string|null,
            "line_total_idr": integer|null}]}

Rules, in priority order:
1. One item per purchased good, in the order mentioned. At most %d items. \
Ignore greetings, thanks, chat noise, and anything that is not a purchase.
2. raw_name is the ingredient as the sender wrote it, lowercase, without \
quantities, prices, or filler words. Keep unusual market spellings; the app \
matches catalog names itself.
3. quantity is a plain decimal string ("3", "0.5"). Convert number words to \
digits ("tiga" -> "3", "setengah" -> "0.5"). If no amount is stated, use null.
4. unit is one of kg, g, l, ml, ikat, karung, tray, pack, dus, pcs, buah. \
Glued forms count ("3kg" -> quantity "3", unit "kg"). A package outside the \
list stays null; never guess a conversion.
5. line_total_idr is the total price the sender states for that item, as a \
whole number without separators: "150rb" -> 150000, "150 ribu" -> 150000, \
"1,5 jt" -> 1500000; ignore "Rp" and dots. If the sender states no price, \
use null. Never compute a price from a per-unit price, and never split one \
stated total across items: when one total covers several items, use null.
""" % MAX_ITEMS


class StepFunTextError(Exception):
    """Safe provider error that never includes the API key or response bodies."""

    def __init__(self, message, status=502):
        self.status = status
        super().__init__(message)


def validate_model(value):
    value = (value or DEFAULT_MODEL).strip()
    if not value or len(value) > 100 or any(char.isspace() for char in value):
        raise ValueError("STEPFUN_TEXT_MODEL must be a single model name such as step-3.7-flash.")
    return value


def _quantity_text(value):
    """Accept a model number only if it is a plain positive decimal; else None."""
    if value is None or isinstance(value, bool):
        return None
    text = value.strip() if isinstance(value, str) else str(value) if isinstance(value, (int, float)) else None
    if not text or not _NUMBER_TEXT.fullmatch(text.replace(",", ".")):
        return None
    return text.replace(",", ".")


def _idr_amount(value):
    """Accept a stated rupiah amount; never derive one."""
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
        raise StepFunTextError("StepFun text returned an unexpected response.")
    if not isinstance(content, str) or not content.strip():
        raise StepFunTextError("StepFun text returned an empty result.")
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except ValueError:
        raise StepFunTextError("StepFun text did not return readable JSON.")


def clean_extraction(data):
    """Map the model JSON onto the same item shape the text parser produces."""
    if not isinstance(data, dict):
        raise StepFunTextError("StepFun text did not return a purchase object.")
    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        raise StepFunTextError("StepFun text returned no item list.")
    warnings = []
    if len(raw_items) > MAX_ITEMS:
        return {
            "items": [],
            "warnings": [f"The message mentions more than {MAX_ITEMS} items; split it into separate messages."],
        }

    items = []
    for index, raw in enumerate(raw_items, 1):
        label = f"Line {index}"
        if not isinstance(raw, dict):
            warnings.append(f"{label}: this item could not be read; add it manually.")
            continue
        name = raw.get("raw_name")
        name = name.strip()[:200] if isinstance(name, str) else ""
        if not name:
            warnings.append(f"{label}: no item name was understood.")
            continue
        raw_name, ingredient_id = match_catalog_name(name)
        quantity = _quantity_text(raw.get("quantity"))
        unit = canonical_unit_name(raw.get("unit"))
        total = _idr_amount(raw.get("line_total_idr"))
        if quantity is None:
            warnings.append(f"{label}: quantity is missing or unclear; type it in.")
        if unit is None:
            warnings.append(f"{label}: unit is missing or unsupported.")
        if total is None:
            warnings.append(f"{label}: line total is missing or unclear; type it in.")
        if ingredient_id is None:
            warnings.append(f"{label}: unknown ingredient '{raw_name}'; select a catalog item manually.")
        items.append({
            "ingredient_id": ingredient_id,
            "raw_name": raw_name,
            "quantity": quantity,
            "unit": unit,
            "line_total_idr": total,
        })
    return {"items": items, "warnings": warnings}


async def extract_message(text, *, api_key, model=DEFAULT_MODEL, base_url=DEFAULT_BASE_URL, client=None):
    """Send one free-form message to StepFun and return proposed draft items."""
    if not api_key or not api_key.strip():
        raise StepFunTextError("StepFun text rescue is not configured.", 503)
    if not isinstance(text, str) or not text.strip():
        raise StepFunTextError("The message text is empty.", 400)
    base_url = validate_base_url(base_url)
    model = validate_model(model)
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [{
            "role": "user",
            "content": PROMPT + "\nMessage to read:\n" + text.strip()[:MAX_MESSAGE_CHARS],
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
        raise StepFunTextError("StepFun text could not be reached; the rules result is kept.")
    finally:
        if owns_client:
            await client.aclose()
    if response.status_code != 200:
        status = 429 if response.status_code == 429 else 502
        raise StepFunTextError(f"StepFun text rejected the message (HTTP {response.status_code}).", status)
    return clean_extraction(_content_json(response))
