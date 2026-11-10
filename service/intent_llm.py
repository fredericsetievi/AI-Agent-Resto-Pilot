"""Bounded intent routing for chatty commands the exact words missed.

The classifier may only answer with one of four intents and never acts on
its own: the caller turns a clear approve/reject/summary into the same
command the owner could have typed, everything else — especially anything
ambiguous — falls through to the existing behavior unchanged. Purchases
never reach this module: the caller skips it whenever extraction found a
purchase signal, so a purchase note cannot be reinterpreted.
"""

from __future__ import annotations

import json
import re

import httpx

from .stock_llm import DEFAULT_BASE_URL, DEFAULT_MODEL, StepFunTextError
from .vision import validate_base_url


VALID_INTENTS = ("approve", "reject", "summary", "purchase_edit", "none")
_INTENT_JSON = re.compile(r"\{[^{}]*\}")


def _intent_from(response):
    """Extract the intent from the model reply; anything else is a failure."""
    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise StepFunTextError("The intent classifier returned an unexpected response.")
    if not isinstance(content, str) or not content.strip():
        raise StepFunTextError("The intent classifier returned an empty result.")
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    match = _INTENT_JSON.search(text)
    if not match:
        raise StepFunTextError("The intent classifier returned no JSON object.")
    try:
        parsed = json.loads(match.group(0))
    except ValueError:
        raise StepFunTextError("The intent classifier returned unreadable JSON.")
    intent = parsed.get("intent") if isinstance(parsed, dict) else None
    if intent not in VALID_INTENTS:
        raise StepFunTextError("The intent classifier answered with an unknown intent.")
    return intent


async def classify_intent(text, *, api_key, model=DEFAULT_MODEL, base_url=DEFAULT_BASE_URL, client=None):
    """Classify one message into approve/reject/summary/none; raises on any problem."""
    if not api_key or not api_key.strip():
        raise StepFunTextError("The intent classifier is not configured.", 503)
    if not isinstance(text, str) or not text.strip():
        raise StepFunTextError("The message text is empty.", 400)
    base_url = validate_base_url(base_url)
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [{
            "role": "user",
            "content": (
                "You route one WhatsApp message from a restaurant owner to the purchase-assistant "
                "command it expresses. The assistant understands these commands:\n"
                "- approve: an unambiguous confirmation to proceed with the draft it is waiting on "
                "(\"ya\", \"setuju\", \"gas\", \"setuju banget deh\", \"acc all\").\n"
                "- reject: an unambiguous cancellation of that draft (\"batal\", \"gajadi\", \"batalin deh\").\n"
                "- summary: a request for a recap or report (\"rekap dong\", \"ringkasan minggu ini\").\n"
                "- purchase_edit: the sender says a RECORDED purchase is wrong and must be changed or voided (\"pembelian cabai kemarin salah, harusnya 3 kg\", \"batalin pembelian beras kemarin\").\n"
                "- none: everything else — purchase notes, questions, greetings, thanks, and anything "
                "ambiguous or half-said. When unsure, answer none; a wrong approve is costly, a none "
                "is safe.\n\n"
                f"Message:\n{text.strip()[:500]}\n\n"
                'Return one JSON object and nothing else: {"intent": "approve" | "reject" | "summary" | "purchase_edit" | "none"}'
            ),
        }],
    }
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=30.0, follow_redirects=False)
    try:
        response = await client.post(
            base_url + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
    except httpx.HTTPError:
        raise StepFunTextError("The intent classifier could not be reached; the default flow is kept.")
    finally:
        if owns_client:
            await client.aclose()
    if response.status_code != 200:
        status = 429 if response.status_code == 429 else 502
        raise StepFunTextError(f"The intent classifier rejected the request (HTTP {response.status_code}).", status)
    return _intent_from(response)
