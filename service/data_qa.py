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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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


# --- Time windows (v2): bounded Indonesian phrases, day boundaries in WIB ------

_TZ = ZoneInfo("Asia/Jakarta")
_N_DAYS = re.compile(r"(?i)(\d{1,4})\s*hari(?:\s*(?:terakhir|ke\s*belakangeng)? )?")


def parse_window(text, *, default_days=7):
    """Resolve one time window from bounded Indonesian phrases.

    Returns {"start", "end", "label", "days"} where start/end are ISO strings
    in the restaurant timezone (None = unbounded). Day boundaries follow
    Asia/Jakarta like every other timestamp the app prints. Unrecognised
    text falls back to the classic trailing window.
    """
    t = str(text or "").casefold()
    now = datetime.now(_TZ)

    def day_start(offset_days=0):
        date = (now + timedelta(days=offset_days)).date()
        return datetime(date.year, date.month, date.day, tzinfo=_TZ)

    def iso(dt):
        return dt.isoformat() if dt else None

    days = None
    if "bulan lalu" in t:
        first_this = day_start(0).replace(day=1)
        start = (first_this - timedelta(days=1)).replace(day=1)
        return {"start": iso(start), "end": iso(first_this), "label": "bulan lalu", "days": None}
    if "bulan ini" in t:
        return {"start": iso(day_start(0).replace(day=1)), "end": None, "label": "bulan ini", "days": None}
    if "kemarin" in t:
        return {"start": iso(day_start(-1)), "end": iso(day_start(0)), "label": "kemarin", "days": 1}
    if "hari ini" in t:
        return {"start": iso(day_start(0)), "end": None, "label": "hari ini", "days": 1}
    if "minggu lalu" in t:
        return {"start": iso(day_start(-14)), "end": iso(day_start(-7)), "label": "minggu lalu", "days": 7}
    days_match = _N_DAYS.search(t)
    if days_match:
        days = int(days_match.group(1))
        if 1 <= days <= 3650:
            return {"start": iso(now - timedelta(days=days)), "end": None, "label": f"{days} hari terakhir", "days": days}
    if "minggu ini" in t:
        return {"start": iso(now - timedelta(days=default_days)), "end": None, "label": f"{default_days} hari terakhir", "days": default_days}
    if "semua" in t or "seluruh" in t or "sejak awal" in t:
        return {"start": None, "end": None, "label": "semua waktu", "days": None}
    return {"start": iso(now - timedelta(days=default_days)), "end": None, "label": f"{default_days} hari terakhir", "days": default_days}


def find_windows(text, limit=2):
    """Every distinct window phrase in the text, in the order it appears —
    used to pair two periods for a comparison ("minggu ini vs minggu lalu")."""
    t = str(text or "").casefold()
    found = []
    for phrase in ("bulan lalu", "bulan ini", "kemarin", "hari ini", "minggu lalu", "minggu ini"):
        if phrase in t and (not found or phrase != found[-1]["_phrase"]):
            window = parse_window(phrase)
            window["_phrase"] = phrase
            window["_position"] = t.index(phrase)
            found.append(window)
    match = _N_DAYS.search(t)
    if match and not any(w["label"] == f"{int(match.group(1))} hari terakhir" for w in found):
        window = parse_window(match.group(0))
        window["_phrase"] = match.group(0)
        window["_position"] = match.start()
        found.append(window)
    found.sort(key=lambda window: window["_position"])
    for window in found:
        window.pop("_phrase", None)
        window.pop("_position", None)
    return found[:limit]


def is_compare_question(text):
    return bool(re.search(r"(?i)\bvs\b|dibanding|banding", str(text or ""))) and bool(SPEND_MARKER.search(str(text or "")))


def is_rank_question(text):
    return bool(re.search(r"(?i)paling\s*(?:banyak|besar)|terbesar|paling\s*mahal\s*(?:pengeluaran)?", str(text or ""))) and bool(SPEND_MARKER.search(str(text or "")))


def is_average_question(text):
    return bool(re.search(r"(?i)rata[-\s]?rata", str(text or ""))) and bool(SPEND_MARKER.search(str(text or "")))


def spend_fact(total_idr, purchase_count, label, top=None):
    """One spend total for a labelled time window; empty when nothing recorded."""
    if not purchase_count:
        return None
    fact = {"kind": "spend", "label": label, "total_idr": total_idr,
            "purchase_count": purchase_count, "_require": [money_pattern(total_idr)]}
    if top:
        fact["top_item"] = top
        fact["_require"].append(re.escape(str(top["name"])))
    return fact


def ingredient_spend_fact(name, total_idr, count, label):
    """Spend for one named ingredient inside a time window."""
    return {"kind": "spend_ingredient", "name": name, "label": label, "total_idr": total_idr,
            "purchase_count": count, "_require": [re.escape(name), money_pattern(total_idr)]}


def compare_fact(a_total, a_label, a_count, b_total, b_label, b_count):
    """Two windows side by side, with the percentage move of the first."""
    fact = {"kind": "spend_compare", "label_a": a_label, "label_b": b_label,
            "total_a": a_total, "total_b": b_total, "count_a": a_count, "count_b": b_count,
            "_require": [money_pattern(a_total), money_pattern(b_total)]}
    if b_total > 0 and a_total != b_total:
        change = round((a_total - b_total) / b_total * 100, 1)
        fact["change_percent"] = f"{change:.1f}"
        fact["_require"].append(percent_pattern(fact["change_percent"]))
    return fact


def rank_fact(label, ranked):
    """Top ingredients by recorded spend; ranked is [(name, total)] descending."""
    fact = {"kind": "rank", "label": label,
            "items": [{"name": name, "total_idr": total} for name, total in ranked],
            "_require": []}
    for name, total in ranked:
        fact["_require"].append(re.escape(name))
        fact["_require"].append(money_pattern(total))
    return fact


def average_fact(total_idr, weeks, label):
    """Total divided by whole weeks inside the window (minimum one).

    The locked figure is the average itself — the reply must present the
    average, never the raw total.
    """
    average = int(total_idr / weeks + 0.5)
    return {"kind": "average", "label": label, "total_idr": total_idr, "weeks": weeks, "average_idr": average,
            "_require": [money_pattern(average), re.escape(f"{weeks} minggu")]}


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
        parts = [f"Pengeluaran {fact['label']}: Rp{format_idr(fact['total_idr'])} dari {fact['purchase_count']} pembelian."]
        top = fact.get("top_item")
        if top:
            parts.append(f"Terbanyak: {top['name']} {top['quantity']} {top['unit']}.")
        return " ".join(parts)
    if facts[0]["kind"] == "spend_ingredient":
        fact = facts[0]
        return f"Pengeluaran {fact['label']} untuk {fact['name']}: Rp{format_idr(fact['total_idr'])} (dari {fact['purchase_count']} catatan)."
    if facts[0]["kind"] == "spend_compare":
        fact = facts[0]
        head = f"Pengeluaran {fact['label_a']}: Rp{format_idr(fact['total_a'])} ({fact['count_a']} pembelian) vs {fact['label_b']}: Rp{format_idr(fact['total_b'])} ({fact['count_b']})."
        change = fact.get("change_percent")
        if change:
            direction = "naik" if float(change) > 0 else "turun"
            magnitude = f"{abs(float(change)):.1f}".replace(".", ",")
            head += f" Selisih {direction} {magnitude}%."
        return head
    if facts[0]["kind"] == "rank":
        fact = facts[0]
        listing = "; ".join(f"{position}. {item['name']} Rp{format_idr(item['total_idr'])}" for position, item in enumerate(fact["items"], 1))
        return f"Pengeluaran terbesar {fact['label']}: {listing}."
    if facts[0]["kind"] == "average":
        fact = facts[0]
        return f"Rata-rata per minggu ({fact['label']}): Rp{format_idr(fact['average_idr'])} (dari {fact['weeks']} minggu)."
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


async def phrase_opname_answer(items, *, api_key, model=DEFAULT_MODEL, base_url=DEFAULT_BASE_URL, client=None):
    """Reword a physical stock count confirmation with the figures locked.

    Each item requires the ingredient name and both quantities (system
    record and physical count) to appear with their unit; validate_data_reply
    also rejects any other quantity-like figure the reply invents. Callers
    keep the deterministic opname_reply on StepFunTextError.
    """
    if not api_key or not api_key.strip():
        raise StepFunTextError("The opname phrasing model is not configured.", 503)
    if not items:
        raise StepFunTextError("No stock count items to phrase.", 400)
    facts = []
    for item in items:
        unit = re.escape(str(item.get("unit") or ""))
        facts.append({
            "name": item["name"], "estimated": item["estimated_quantity"],
            "counted": item["counted_quantity"], "delta": item["delta"], "unit": item["unit"],
            "_require": [
                re.escape(item["name"]),
                rf"(?<![0-9]){re.escape(str(item['estimated_quantity']))}\s?{unit}",
                rf"(?<![0-9]){re.escape(str(item['counted_quantity']))}\s?{unit}",
            ],
        })
    base_url = validate_base_url(base_url)
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [{
            "role": "user",
            "content": (
                "You confirm a physical stock count (stok opname) to a small restaurant owner "
                "over WhatsApp in casual Indonesian. You get the exact comparison just read "
                "from the database: the recorded quantity versus the owner's counted quantity. "
                "Reply with the confirmation text only.\n\n"
                "Hard rules, in priority order:\n"
                "1. Use ONLY the quantities, units, and ingredient names in the facts. Never "
                "compute, convert, round, or invent any number.\n"
                "2. Every estimated and counted quantity must appear exactly as written with "
                "its unit (\"3 kg\" stays \"3 kg\").\n"
                "3. Ask the owner to confirm applying the correction (balas YA), or to review "
                "it in the dashboard. Keep the reply under 400 characters.\n\n"
                f"Count comparison (JSON):\n{json.dumps(facts, ensure_ascii=False)}"
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
        raise StepFunTextError("The opname phrasing model could not be reached; the deterministic text is kept.")
    finally:
        if owns_client:
            await client.aclose()
    if response.status_code != 200:
        status = 429 if response.status_code == 429 else 502
        raise StepFunTextError(f"The opname phrasing model rejected the request (HTTP {response.status_code}).", status)
    return validate_data_reply(_answer_text(response), facts)
