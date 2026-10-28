"""StepFun phrasing for stock answers — the numbers never come from the model.

The API layer reads inventory facts from SQLite and builds a deterministic
reply first. With the stock-answer flag on, this module asks the model to
rephrase those facts into one natural Indonesian reply. The model only
chooses words: validate_stock_reply rejects any reply that does not carry
every fact quantity, unit, and ingredient name verbatim, and the caller then
keeps the deterministic text. The ledger is the only source of numbers.
"""

from __future__ import annotations

import json
import re

import httpx

from .text_llm import DEFAULT_BASE_URL, DEFAULT_MODEL, MAX_MESSAGE_CHARS, StepFunTextError, validate_model
from .vision import validate_base_url


MAX_FACTS = 50
MAX_REPLY_CHARS = 4000

PROMPT = """You answer a small restaurant owner's stock question over \
WhatsApp in casual Indonesian. You get the question and the exact inventory \
facts just read from the database. Reply with the answer text only — no \
JSON, no quotes, no preamble.

Hard rules, in priority order:
1. Use ONLY the quantities, units, and ingredient names in the facts. Never \
compute, convert, sum, round, or invent any number, and never add advice \
beyond the facts.
2. Repeat every fact quantity exactly as written, with its unit ("3 kg" \
stays "3 kg", never "3,00 kg" or "3000 g"). For a question about one \
ingredient answer that ingredient; for a general question list each fact \
on its own short line.
3. A quantity of "0" means out of stock. You may note a fact below its \
reorder threshold is running low, quoting the threshold from the facts.
4. Keep the reply under 500 characters.

Facts (JSON):
%s

Question:
%s"""

# Defense in depth for the tenant guard: the facts are already scoped by the
# store, and the prompt names the restaurant so the rephrase stays with it.
TENANT_RULE = ("Tenant rule: these facts belong to the restaurant \"%s\". "
               "Answer only about that restaurant; never mention, infer, or reveal "
               "any other restaurant or business.\n\n")


def validate_stock_reply(reply, facts):
    """Accept the model reply only when every database fact appears verbatim.

    A missing quantity, unit, or ingredient name raises so the caller keeps
    the deterministic reply; a fabricated number cannot pass, because each
    database quantity must be present with word boundaries.
    """
    if not isinstance(reply, str) or not reply.strip() or len(reply) > MAX_REPLY_CHARS:
        raise StepFunTextError("StepFun stock answer was unusable.")
    lowered = reply.casefold()
    for fact in facts:
        quantity = str(fact.get("quantity") or "").strip()
        if not quantity or not re.search(r"(?<![0-9])" + re.escape(quantity) + r"(?![0-9])", lowered):
            raise StepFunTextError(f"StepFun stock answer omitted the database quantity for {fact.get('name')!r}.")
        unit = str(fact.get("unit") or "").strip().casefold()
        if unit and unit not in lowered:
            raise StepFunTextError(f"StepFun stock answer omitted the unit for {fact.get('name')!r}.")
        name = str(fact.get("name") or "").strip().casefold()
        if name and name not in lowered:
            raise StepFunTextError(f"StepFun stock answer omitted the ingredient {name!r}.")
    return reply.strip()


def _reply_text(response):
    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise StepFunTextError("StepFun stock answer returned an unexpected response.")
    if not isinstance(content, str) or not content.strip():
        raise StepFunTextError("StepFun stock answer returned an empty result.")
    return content.strip()


async def phrase_stock_answer(question, facts, *, api_key, model=DEFAULT_MODEL, base_url=DEFAULT_BASE_URL, client=None, restaurant=None):
    """Send the DB facts plus the question to StepFun and return a checked reply.

    Raises StepFunTextError on any configuration, transport, or validation
    problem so the caller keeps the deterministic reply.
    """
    if not api_key or not api_key.strip():
        raise StepFunTextError("StepFun stock answer is not configured.", 503)
    if not facts or not isinstance(facts, list):
        raise StepFunTextError("No stock facts to phrase.", 400)
    if not isinstance(question, str) or not question.strip():
        raise StepFunTextError("The question text is empty.", 400)
    base_url = validate_base_url(base_url)
    model = validate_model(model)
    prompt = PROMPT % (
        json.dumps(facts[:MAX_FACTS], ensure_ascii=False),
        question.strip()[:MAX_MESSAGE_CHARS],
    )
    if restaurant and restaurant.strip():
        prompt = prompt + "\n" + (TENANT_RULE % restaurant.strip())
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [{
            "role": "user",
            "content": prompt,
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
        raise StepFunTextError("StepFun stock answer could not be reached; the deterministic reply is kept.")
    finally:
        if owns_client:
            await client.aclose()
    if response.status_code != 200:
        status = 429 if response.status_code == 429 else 502
        raise StepFunTextError(f"StepFun stock answer rejected the question (HTTP {response.status_code}).", status)
    return validate_stock_reply(_reply_text(response), facts)
