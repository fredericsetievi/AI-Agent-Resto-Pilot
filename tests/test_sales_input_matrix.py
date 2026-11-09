"""Sales-input stability matrix: how real warung phrasing behaves.

Every realistic owner input must resolve to exactly one of:
- parsed: a correct draft (document_type + lines as asserted), or
- unresolved: a durable draft with clarification notes (never silently wrong), or
- dropped: no candidate at all.

``dropped`` is only acceptable for questions/legacy commands. A real report
that is dropped without any feedback is a stability finding, recorded here so
the behaviour is visible and deliberate.
"""

from datetime import date
from decimal import Decimal

import pytest

from service.operation_extraction import parse_operations_text

TODAY = date(2026, 11, 9)


def parse(text):
    return parse_operations_text(text, today=TODAY)


def names(result):
    return [(line["kind"], line["name"], line["quantity"], line["unit"]) for line in result["lines"]]


# --- Parsed: colloquial phrasings that must produce correct drafts -----------

@pytest.mark.parametrize("text,expected", [
    # Spoken/written variants of the same sale report.
    ("hari ini laku 30 nasi goreng", [("sale", "nasi goreng", "30", "porsi")]),
    ("nasi goreng laku keras 30", [("sale", "nasi goreng", "30", "porsi")]),
    ("jualan mie ayam 15", [("sale", "mie ayam", "15", "porsi")]),
    ("tadi siang jual es teh 40", [("sale", "es teh", "40", "porsi")]),
    ("Terjual: nasi goreng 30 porsi", [("sale", "nasi goreng", "30", "porsi")]),
    # Two dishes joined the way people actually type/say it.
    ("terjual ayam geprek 12, es teh 20", [("sale", "ayam geprek", "12", "porsi"), ("sale", "es teh", "20", "porsi")]),
    ("terjual 30 nasi goreng dan 15 es teh", [("sale", "nasi goreng", "30", "porsi"), ("sale", "es teh", "15", "porsi")]),
    ("terjual 30 nasi goreng sama 15 es teh", [("sale", "nasi goreng", "30", "porsi"), ("sale", "es teh", "15", "porsi")]),
    # Drinks measured in gelas/cup stay drafts with the unit visible.
    ("terjual es teh 40 gelas", [("sale", "es teh", "40", "gelas")]),
    ("terjual kopi 12 cup", [("sale", "kopi", "12", "cup")]),
    # Revenue-only reports stay revenue-only (no portions invented from money).
    ("omzet hari ini 2,5jt", [("sale", "penjualan", None, None)]),
])
def test_realistic_sale_phrasings_parse_correctly(text, expected):
    result = parse(text)
    assert result is not None, f"real report silently dropped: {text!r}"
    assert result["document_type"] == "sales_report"
    assert names(result) == expected, f"wrong draft for {text!r}"
    assert result["unresolved"] == [], f"unexpected clarifications for {text!r}: {result['unresolved']}"


# --- Unresolved: messy input must hold a draft, never post a wrong one -------

@pytest.mark.parametrize("text", [
    "penjualan goceng tadi malam",             # slang money, no digits
    "terjual ayam geprek level 3 sebanyak 5",  # menu name contains a number
    "terjual 1,5 kg sambal",                   # kg sales need owner confirmation, by design
])
def test_messy_but_real_reports_hold_for_clarification(text):
    result = parse(text)
    assert result is not None, f"real report silently dropped: {text!r}"
    assert result["unresolved"], f"ambiguous input produced a clean draft: {text!r}"


# --- Dropped: silent no-ops. Questions are fine; real reports are findings ---

@pytest.mark.parametrize("text", [
    "Berapa sisa beras?",
    "stok cukup berapa hari?",
    "YA", "BATAL", "halo",
])
def test_questions_and_legacy_commands_stay_dropped(text):
    assert parse(text) is None


@pytest.mark.parametrize("text,why", [
    ("nasi goreng 30", "bare list without an operation word"),
    ("terjul 30 nasi goreng", "ASR-garbled signal word"),
    ("trjual nasi goreng 30 porsi", "ASR-garbled signal word"),
    ("es teh 40 gelas tadi", "no signal word"),
])
def test_dangerous_silent_drops_documented(text, why):
    """These are real reports a warung owner would send, yet parse to None.

    The owner gets no draft, no clarification, nothing - the worst failure
    mode because they believe the sale was recorded. Recorded here so the
    behaviour is a deliberate decision, not an accident.
    """
    assert parse(text) is None, f"now parses - update this documentation test: {text!r} ({why})"


# --- Bridge: colloquial parser output flows through approval to the ledger ---

def test_colloquial_sale_reaches_review_and_reduces_stock_on_approval(tmp_path):
    """'hari ini laku 30 nasi goreng' must behave exactly like the canonical
    phrasing: review -> YA approval -> recipe-weighted stock decrement."""
    from uuid import uuid4

    from conftest import SENDER_NUMBER
    from service.db import Store

    store = Store(tmp_path / "matrix-approval.sqlite3", seed=False)
    with store.transaction() as conn:
        conn.execute(
            "UPDATE inventory SET quantity=? WHERE restaurant_id=? AND ingredient_id=?",
            ("30", "demo-resto", "beras"),
        )
    store.save_inventory_recipe(
        uuid4().hex,
        {"name": "Nasi goreng", "items": [{"ingredient_id": "beras", "quantity": "0.1"}]},
        "demo-resto",
    )

    review = store.whatsapp_ingest(uuid4().hex, {"sender": SENDER_NUMBER, "text": "hari ini laku 30 nasi goreng"})
    operation = review["result"]["operation"]
    assert operation["status"] == "review"
    [line] = operation["candidate"]["lines"]
    assert (line["kind"], line["name"], line["quantity"], line["unit"]) == ("sale", "nasi goreng", "30", "porsi")

    posted = store.whatsapp_ingest(
        uuid4().hex,
        {"sender": SENDER_NUMBER, "text": f"YA {operation['id'][:8]} V{operation['version']}"},
    )
    assert posted["result"]["action"] == "operation_posted"
    with store.transaction() as conn:
        balance = conn.execute(
            "SELECT quantity FROM inventory WHERE restaurant_id=? AND ingredient_id=?",
            ("demo-resto", "beras"),
        ).fetchone()[0]
    assert Decimal(balance) == Decimal("27")  # 30 portions x 0.1 kg
