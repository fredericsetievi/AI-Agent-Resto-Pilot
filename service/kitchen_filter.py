"""Kitchen-item filter agent — decide which purchased goods are cooking items.

Mixed monthly invoices often bundle household goods (sikat gigi, odol, sabun,
snacks, cigarettes) with restaurant supplies. The rules parser and the text
rescue LLM happily extract every line, and approving such a draft would
auto-create "Sikat gigi" as a kitchen ingredient. This agent classifies the
extracted items with one bounded LLM call and marks the non-kitchen ones.

Design rules:
* The LLM never decides alone: every flagged item is marked, never silently
  deleted. The draft reply shows the flags and asks the owner to confirm —
  the owner removes the item (or keeps it) before approval.
* Deterministic keyword pre-screen skips the model entirely when every item
  name is a known catalog ingredient, so normal purchase messages cost zero
  model calls.
* Any provider or validation problem keeps the items untouched — a broken
  filter must never lose a real purchase line.
"""

from __future__ import annotations

import json
import re

from .text_llm import DEFAULT_BASE_URL, DEFAULT_MODEL, MAX_MESSAGE_CHARS, StepFunTextError, validate_model
from .vision import validate_base_url


# Items whose names are already in a restaurant's catalog are cooking items
# by definition; the model is only asked about unmapped raw names.
PROMPT = """You classify purchased goods for a small restaurant's kitchen. You get \
a JSON array of purchased item names (Indonesian or English, informal \
spellings included). Reply with one JSON object and nothing else:

{"kitchen": [indices of items that ARE food ingredients, seasonings, \
beverages for the menu, or cooking/packaging supplies],
 "non_kitchen": [indices of items that are NOT for restaurant kitchen use]}

Rules, in priority order:
1. Kitchen items: fresh or dry food ingredients, spices, cooking oil, rice, \
eggs, drinks served on the menu, take-away packaging, charcoal, napkins and \
other kitchen-consumable supplies.
2. Non-kitchen items: personal care (toothpaste/odol, soap, shampoo), \
household goods (sikat gigi, deterjen, tissue rumah), medicine, cigarettes, \
phone credit, and any goods a household buys for itself.
3. Every index must appear exactly once, in "kitchen" or "non_kitchen". \
Never invent indices.
4. When genuinely unsure, put the item in "kitchen" — better to keep a line \
and let the owner delete it than to lose a real purchase.
"""

NON_KITCHEN_WARNING = ("Item {index}: '{name}' bukan bahan masak — hapus baris ini "
                       "sebelum menyetujui (atau biarkan jika memang untuk resto).")
FLAGGED_REVIEW_NOTE = ("Catatan: baris bertanda bukan bahan masak dihapus otomatis "
                       "saat kamu balas 'YA'? Tidak — buang dulu lewat tombol hapus "
                       "di dashboard atau ketik ulang pesannya tanpa barang itu.")


def validate_classification(data, count):
    """Accept only a complete, in-range partition of the item indices."""
    if not isinstance(data, dict):
        raise StepFunTextError("Kitchen filter returned no classification object.")
    kitchen = data.get("kitchen")
    non_kitchen = data.get("non_kitchen")
    for label, value in (("kitchen", kitchen), ("non_kitchen", non_kitchen)):
        if not isinstance(value, list) or any(isinstance(index, bool) or not isinstance(index, int) for index in value):
            raise StepFunTextError(f"Kitchen filter '{label}' is not an index list.")
        if any(index < 0 or index >= count for index in value):
            raise StepFunTextError(f"Kitchen filter '{label}' has an out-of-range index.")
    combined = sorted(kitchen + non_kitchen)
    if combined != list(range(count)):
        raise StepFunTextError("Kitchen filter must cover every item exactly once.")
    if len(set(kitchen) & set(non_kitchen)):
        raise StepFunTextError("Kitchen filter listed an index in both groups.")
    return {"kitchen": kitchen, "non_kitchen": non_kitchen}


def _content_json(response):
    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise StepFunTextError("Kitchen filter returned an unexpected response.")
    if not isinstance(content, str) or not content.strip():
        raise StepFunTextError("Kitchen filter returned an empty result.")
    candidate = content.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z]*\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate)
    try:
        return json.loads(candidate)
    except ValueError:
        raise StepFunTextError("Kitchen filter did not return readable JSON.")


async def classify_items(names, *, api_key, model=DEFAULT_MODEL, base_url=DEFAULT_BASE_URL, client=None):
    """Ask the model which purchased items are kitchen supplies.

    Returns {"kitchen": [index], "non_kitchen": [index]}; indices refer to the
    caller's original positions (the global item indices when `(index, name)`
    tuples are passed, so upstream catalog-mapped gaps are preserved). Raises
    StepFunTextError on any configuration, transport, or validation problem
    so the caller keeps the items untouched.
    """
    if not api_key or not api_key.strip():
        raise StepFunTextError("Kitchen filter is not configured.", 503)
    if not isinstance(names, list) or not names:
        raise StepFunTextError("No item names to classify.", 400)
    # `names` entries are (global_index, name) pairs or plain strings; the
    # model always answers in the index space of the payload it receives.
    if names and isinstance(names[0], (tuple, list)):
        global_indices = [int(index) for index, _name in names]
        entry_names = [str(name) for _index, name in names]
    else:
        global_indices = list(range(len(names)))
        entry_names = [str(name) for name in names]
    base_url = validate_base_url(base_url)
    model = validate_model(model)
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [{
            "role": "user",
            "content": PROMPT + "\nItems (JSON):\n" + json.dumps(
                        [{"index": position, "name": name[:100]} for position, name in enumerate(entry_names)],
                        ensure_ascii=False)[:MAX_MESSAGE_CHARS],
        }],
    }
    import httpx

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=60.0, follow_redirects=False)
    try:
        response = await client.post(
            base_url + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
    except Exception:
        raise StepFunTextError("Kitchen filter could not be reached; the items stay untouched.")
    finally:
        if owns_client:
            await client.aclose()
    if response.status_code != 200:
        status = 429 if response.status_code == 429 else 502
        raise StepFunTextError(f"Kitchen filter rejected the request (HTTP {response.status_code}).", status)
    verdict = validate_classification(_content_json(response), len(entry_names))
    # The model answers in the payload space it sees (0..n-1); map back to the
    # caller's global item indices, which can be non-contiguous when upstream
    # catalog matching already resolved some names (api.py passes only the
    # unknown ones, keyed by their position in the full draft).
    return {
        "kitchen": [global_indices[position] for position in verdict["kitchen"]],
        "non_kitchen": [global_indices[position] for position in verdict["non_kitchen"]],
    }
