"""Proactive low-stock alerts: the ledger decides, the model only phrases.

One check walks a restaurant's inventory with its rule-adjusted thresholds,
picks candidates that passed the cooldown, and turns them into one WhatsApp/
Telegram message. The deterministic text is built first; with the stock
phrasing flag on, the model may rephrase the same facts (plus the owner's
free-text rules as context) and validate_stock_reply discards any reply that
does not repeat every quantity, unit, and ingredient name verbatim.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation, ROUND_CEILING

import httpx

from .stock_llm import DEFAULT_BASE_URL, DEFAULT_MODEL, StepFunTextError, validate_stock_reply
from .vision import validate_base_url


MAX_PROMPT_RULES_CHARS = 2000


def refill_suggestion(quantity, threshold):
    """Quantity to buy so the item is back at its threshold: ceil(threshold - stock).

    Returns a plain string in the ingredient's unit, or None when either
    figure is unreadable or the stock already meets the threshold. The
    ledger stays the only source of numbers — this is deterministic math.
    """
    try:
        gap = Decimal(str(threshold)) - Decimal(str(quantity))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if gap <= 0:
        return None
    return format(gap.to_integral_value(rounding=ROUND_CEILING), "f")

PROMPT = """You send a restaurant owner one proactive low-stock alert over \
WhatsApp in casual Indonesian. You get the low-stock facts just read from \
the database and the owner's own standing rules for this restaurant. Reply \
with the alert text only — no JSON, no quotes, no preamble.

Hard rules, in priority order:
1. Use ONLY the quantities, units, ingredient names, and thresholds in the \
facts. Never compute, convert, sum, round, or invent any number. Never add \
advice beyond the facts.
2. Repeat every fact quantity exactly as written, with its unit ("3 kg" \
stays "3 kg"). List each fact on its own short line.
3. You may end with one short nudge to review and approve a purchase draft, \
without inventing quantities or prices for it.
4. Respect the owner's rules below for tone and emphasis only; they never \
override the facts. Keep the reply under 500 characters.

Low-stock facts (JSON):
%s

Owner's rules for this restaurant:
%s"""


def _rp(value):
    return f"Rp{int(value):,}".replace(",", ".")


def build_refill_plan_text(facts):
    """Deterministic nightly shopping plan with estimated cash (ledger-only figures).

    Appended after the (possibly LLM-phrased) low-stock alert, so the plan
    never passes through the language model — the suggested quantities come
    from ceil(threshold - stock) and the estimates from the last recorded
    purchase price per ingredient.
    """
    planned = []
    total = 0
    for fact in facts:
        suggest = fact.get("suggest_qty")
        if not suggest:
            continue
        est = fact.get("suggest_est_idr")
        if isinstance(est, int) and not isinstance(est, bool) and est > 0:
            planned.append(f"• {fact['name']} — beli ±{suggest} {fact['unit']} (~{_rp(est)})")
            total += est
        else:
            planned.append(f"• {fact['name']} — beli ±{suggest} {fact['unit']} (harga belum tercatat)")
    if not planned:
        return None
    lines = ["🌙 Rencana belanja besok:"] + planned
    if total:
        lines.append(f"Estimasi modal: {_rp(total)}")
    lines.append("Catat pembeliannya di chat besok pagi; estimasi dihitung dari harga beli terakhir.")
    return "\n".join(lines)


def build_alert_text(facts):
    """Deterministic alert body; only prints what the database returned."""
    lines = ["⚠️ Notifikasi stok rendah:"]
    for fact in facts:
        lines.append(f"• {fact['name']} — {fact['quantity']} {fact['unit']} (ambang {fact['threshold']} {fact['unit']})")
    lines.append("Cek dashboard dan balas YA untuk mencatat pembelian.")
    return "\n".join(lines)


def _alert_text(response):
    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise StepFunTextError("StepFun stock alert returned an unexpected response.")
    if not isinstance(content, str) or not content.strip():
        raise StepFunTextError("StepFun stock alert returned an empty result.")
    return content.strip()


async def phrase_alert(facts, rules_text, *, api_key, model=DEFAULT_MODEL, base_url=DEFAULT_BASE_URL, client=None):
    """Send the DB facts plus the owner's rules to StepFun; return a checked reply.

    validate_stock_reply enforces every fact quantity, unit, and name
    verbatim, so a hallucinated number can never survive; callers keep the
    deterministic alert on StepFunTextError.
    """
    import json

    if not api_key or not api_key.strip():
        raise StepFunTextError("StepFun stock alert is not configured.", 503)
    if not facts or not isinstance(facts, list):
        raise StepFunTextError("No low-stock facts to phrase.", 400)
    base_url = validate_base_url(base_url)
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [{
            "role": "user",
            "content": PROMPT % (
                json.dumps(facts, ensure_ascii=False),
                (rules_text or "(none)")[:MAX_PROMPT_RULES_CHARS],
            ),
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
        raise StepFunTextError("StepFun stock alert could not be reached; the deterministic alert is kept.")
    finally:
        if owns_client:
            await client.aclose()
    if response.status_code != 200:
        status = 429 if response.status_code == 429 else 502
        raise StepFunTextError(f"StepFun stock alert rejected the request (HTTP {response.status_code}).", status)
    return validate_stock_reply(_alert_text(response), facts)
