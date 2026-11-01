"""Data questions beyond stock: spending and last-price lookups.

Same contract as the stock answers: detection is deterministic keywords, the
facts are read from the purchase ledger, and the model — when the phrasing
flag is on — may only reword them. Every fact carries `_require` patterns for
its exact figures ("Rp1.234.000", "16,3%"), and validate_data_reply rejects
any reply missing one, so a hallucinated number can never reach the owner.
"""

from __future__ import annotations

import json
import re

import httpx

from .stock_llm import DEFAULT_BASE_URL, DEFAULT_MODEL, StepFunTextError, MAX_REPLY_CHARS
from .vision import validate_base_url
from .whatsapp import PURCHASE_INTENT


SPEND_MARKER = re.compile(r"(?i)\b(?:pengeluaran|belanja|belanjakan|spend|spending|rekap)\b")
PRICE_MARKER = re.compile(r"(?i)\bharga\b|\b(?:naik|turun|naiknya)\b[^.?]{0,40}?\b(?:persen|%)")
_MAX_QUESTION_CHARS = 500


def format_idr(value):
    """Integer rupiah with Indonesian thousands separators: 1230000 -> 1.234.000."""
    return f"{int(value):,}".replace(",", ".")


def money_pattern(value):
    """Regex accepting 'Rp1.234.000' or 'Rp 1.234.000' and nothing looser."""
    return r"Rp\s?" + re.escape(format_idr(value))


def percent_pattern(change_percent):
    """The deterministic formatter writes a comma decimal: 16.3 -> '16,3%'."""
    text = str(change_percent).replace(".", ",")
    return re.escape(text) + r"\s?%"


def is_spend_question(text):
    if not isinstance(text, str) or not text.strip() or PURCHASE_INTENT.search(text):
        return False
    return bool(SPEND_MARKER.search(text))


def is_price_question(text):
    if not isinstance(text, str) or not text.strip() or PURCHASE_INTENT.search(text):
        return False
    return bool(PRICE_MARKER.search(text))


def spend_fact(summary, days):
    """One spend fact from the weekly summary; empty when nothing was recorded."""
    if not summary.get("purchase_count"):
        return None
    fact = {"kind": "spend", "days": days, "total_idr": summary["total_spend_idr"],
            "purchase_count": summary["purchase_count"], "_require": [money_pattern(summary["total_spend_idr"])]}
    top = summary.get("top_item")
    if top:
        fact["top_item"] = top
        fact["_require"].append(re.escape(str(top["name"])))
    return fact


def price_fact(name, unit, history):
    """One price fact: last recorded unit price, the one before it, and the move."""
    if not history:
        return None
    last = history[-1]
    fact = {"kind": "price", "name": name, "unit": unit, "last_price_idr": last["unit_price_idr"],
            "last_created_at": last["created_at"], "last_supplier": last.get("supplier"),
            "_require": [re.escape(name), money_pattern(last["unit_price_idr"])]}
    if len(history) >= 2:
        previous = history[-2]
        fact["previous_price_idr"] = previous["unit_price_idr"]
        change = (last["unit_price_idr"] - previous["unit_price_idr"]) / previous["unit_price_idr"] * 100
        rounded = round(change, 1)
        fact["change_percent"] = f"{rounded:.1f}"
        if rounded != 0:
            fact["_require"].append(percent_pattern(f"{rounded:.1f}"))
    return fact


def build_answer(facts, days=None):
    """Deterministic answer text; only prints what the ledger returned."""
    if not facts:
        return "Belum ada data untuk dijawab."
    if facts[0]["kind"] == "spend":
        fact = facts[0]
        parts = [f"Pengeluaran {fact['days']} hari terakhir: Rp{format_idr(fact['total_idr'])} dari {fact['purchase_count']} pembelian."]
        top = fact.get("top_item")
        if top:
            parts.append(f"Terbanyak: {top['name']} {top['quantity']} {top['unit']}.")
        return " ".join(parts)
    lines = []
    for fact in facts:
        head = f"Harga terakhir {fact['name']}: Rp{format_idr(fact['last_price_idr'])}/{fact['unit']}"
        if fact.get("previous_price_idr"):
            change = float(fact["change_percent"])
            direction = "naik" if change > 0 else "turun"
            head += f" ({direction} {fact['change_percent'].replace('.', ',')}% dari Rp{format_idr(fact['previous_price_idr'])})"
        lines.append(head)
    return "\n".join(lines)


_MONEY_IN_TEXT = re.compile(r"Rp\s?[0-9][0-9.]*")
_PERCENT_IN_TEXT = re.compile(r"[0-9]+(?:,[0-9]+)?\s?%")


def validate_data_reply(reply, facts):
    """Accept the model reply only when its figures are exactly the ledger's.

    Two-way lock: every required figure must appear (with 'Rp', thousands
    dots, and comma decimals as written), and every Rp/percent figure the
    reply mentions must match one of the required ones — a fabricated number
    cannot slip through alongside the real ones.
    """
    if not isinstance(reply, str) or not reply.strip() or len(reply) > MAX_REPLY_CHARS:
        raise StepFunTextError("The data answer was unusable.")
    required = [pattern for fact in facts for pattern in fact.get("_require", [])]
    for pattern in required:
        if not re.search(pattern, reply):
            raise StepFunTextError(f"The data answer missed a required figure ({pattern}).")
    for figure in _MONEY_IN_TEXT.findall(reply) + _PERCENT_IN_TEXT.findall(reply):
        if not any(re.fullmatch(pattern, figure) for pattern in required):
            raise StepFunTextError(f"The data answer invented a figure ({figure}).")
    return reply.strip()


def _answer_text(response):
    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise StepFunTextError("The data answer returned an unexpected response.")
    if not isinstance(content, str) or not content.strip():
        raise StepFunTextError("The data answer returned an empty result.")
    return content.strip()


async def phrase_data_answer(question, facts, rules_text, *, api_key, model=DEFAULT_MODEL, base_url=DEFAULT_BASE_URL, client=None):
    """Reword the ledger facts into one natural Indonesian reply — numbers locked."""
    if not api_key or not api_key.strip():
        raise StepFunTextError("The data answer model is not configured.", 503)
    if not facts:
        raise StepFunTextError("No data facts to phrase.", 400)
    base_url = validate_base_url(base_url)
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [{
            "role": "user",
            "content": (
                "You answer a small restaurant owner's question about recorded spending and "
                "purchase prices over WhatsApp in casual Indonesian. You get the question and "
                "the exact facts read from the purchase ledger. Reply with the answer text only.\n\n"
                "Hard rules, in priority order:\n"
                "1. Use ONLY the figures in the facts. Never compute, convert, round, or invent "
                "any number or percentage.\n"
                "2. Every listed figure must appear in your reply exactly as written, including "
                "the 'Rp' and thousands dots (\"Rp1.234.000\") and comma decimals (\"16,3%\").\n"
                "3. Respect the owner's rules below for tone only; they never change the figures. "
                "Keep the reply under 400 characters.\n\n"
                f"Facts (JSON):\n{json.dumps(facts, ensure_ascii=False)}\n\n"
                f"Owner's rules for this restaurant:\n{(rules_text or '(none)')[:2000]}\n\n"
                f"Question:\n{question.strip()[:_MAX_QUESTION_CHARS]}"
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
        raise StepFunTextError("The data answer model could not be reached; the deterministic answer is kept.")
    finally:
        if owns_client:
            await client.aclose()
    if response.status_code != 200:
        status = 429 if response.status_code == 429 else 502
        raise StepFunTextError(f"The data answer model rejected the request (HTTP {response.status_code}).", status)
    return validate_data_reply(_answer_text(response), facts)
