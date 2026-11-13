"""Chat corrections of recorded purchases: ubah/batal pembelian.

The proposal flow mirrors stock opname: the command stores a pending
proposal on the thread, the reply shows the exact before/after, and only
YA applies it (compensating stock movements + audit). BATAL discards.
An 8-digit purchase id targets precisely; without one, plain words
(ingredient, quantity, kemarin) resolve against the restaurant's recent
purchases — tests use fresh restaurants so seeded demo history cannot
make the resolution ambiguous.
"""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from service.db import Store
from service.purchase_edits import parse_purchase_edit_command

SENDER = "628888000111"  # not the demo-linked number, so fresh stores can claim it


def ingest(store, text, *, sender=SENDER, restaurant=None):
    response = store.whatsapp_ingest(uuid4().hex, {"sender": sender, "text": text})
    if restaurant:
        response["_restaurant_id"] = restaurant
    return response


def reply(response):
    return response["messages"][-1]["body"]


def action(response):
    return response["result"]["action"]


def stock_of(store, restaurant, ingredient_id="cabai"):
    with store.transaction() as conn:
        row = conn.execute("SELECT quantity FROM inventory WHERE restaurant_id=? AND ingredient_id=?", (restaurant, ingredient_id)).fetchone()
    return Decimal(row[0])


def fresh_store(tmp_path, name="Warung Uji", dbname="fresh.sqlite3"):
    """A restaurant with no purchase history and SENDER linked to it."""
    store = Store(tmp_path / dbname)
    created = store.create_restaurant(uuid4().hex, {
        "name": name, "owner_username": "uji" + uuid4().hex[:6],
        "owner_password": "rahasia-123", "whatsapp_number": SENDER,
    })
    return store, created["id"]


def record_purchase(store, restaurant, text="beli cabai 2 kg 100rb"):
    first = ingest(store, text, restaurant=restaurant)
    assert first["result"]["action"] == "draft_created", first
    approved = ingest(store, "ya", restaurant=restaurant)
    assert approved["result"]["action"] == "approved", approved
    return approved["result"]["purchase_id"]


def age_purchases(store, hours=26.0):
    """Move recorded purchases into the past so 'kemarin' points at them.

    Anchored to yesterday's noon rather than now-minus-N-hours: a 26h shift
    only lands on yesterday when the current clock is past 02:00, so the
    suite went red when run after midnight.
    """
    yesterday_noon = (datetime.now(timezone.utc) - timedelta(days=1)).replace(
        hour=12, minute=0, second=0, microsecond=0)
    with store.transaction() as conn:
        conn.execute("UPDATE purchases SET created_at=?", (yesterday_noon.isoformat(),))


def test_parse_commands():
    parsed = parse_purchase_edit_command("ubah pembelian a1b2c3d4 jadi cabai 3 kg 20rb")
    assert parsed == {"kind": "edit", "id8": "a1b2c3d4", "remainder": "jadi cabai 3 kg 20rb"}
    assert parse_purchase_edit_command("batal pembelian beras kemarin") == {"kind": "void", "id8": None, "remainder": "beras kemarin"}
    assert parse_purchase_edit_command("halo") is None
    assert parse_purchase_edit_command("beli cabai 2 kg 100rb") is None


def test_edit_by_id_proposes_then_applies_stock_and_body(tmp_path):
    store, rid = fresh_store(tmp_path, dbname="edit.sqlite3")
    purchase_id = record_purchase(store, rid)
    assert stock_of(store, rid) == Decimal(2)

    proposed = ingest(store, f"ubah pembelian {purchase_id[:8]} jadi cabai 3 kg 150rb")
    assert action(proposed) == "purchase_edit_proposed"
    assert "2 kg" in reply(proposed) and "3 kg" in reply(proposed) and "YA" in reply(proposed)
    assert stock_of(store, rid) == Decimal(2)  # nothing applied before the owner's YA
    applied = ingest(store, "ya")
    assert action(applied) == "purchase_edited"
    assert stock_of(store, rid) == Decimal(3)  # +1 kg compensation

    with store.transaction() as conn:
        row = conn.execute("SELECT body, voided_at FROM purchases WHERE id=?", (purchase_id,)).fetchone()
    body = json.loads(row[0])
    assert row[1] is None and not body.get("voided")
    assert body["items"][0]["canonical_quantity"] == "3"
    assert Decimal(body["total_idr"]) == Decimal(150000)


def test_edit_without_price_inherits_the_old_unit_price(tmp_path):
    store, rid = fresh_store(tmp_path, dbname="inherit.sqlite3")
    purchase_id = record_purchase(store, rid, "beli cabai 2 kg 100rb")
    proposed = ingest(store, f"ubah pembelian {purchase_id[:8]} jadi cabai 3 kg")
    assert action(proposed) == "purchase_edit_proposed"
    applied = ingest(store, "ya")
    assert action(applied) == "purchase_edited"
    with store.transaction() as conn:
        body = json.loads(conn.execute("SELECT body FROM purchases WHERE id=?", (purchase_id,)).fetchone()[0])
    assert Decimal(body["total_idr"]) == Decimal(150000)  # 3 kg at the recorded 50.000/kg


def test_void_by_id_reverses_stock_and_keeps_history(tmp_path):
    store, rid = fresh_store(tmp_path, dbname="void.sqlite3")
    purchase_id = record_purchase(store, rid)
    before = stock_of(store, rid)

    proposed = ingest(store, f"batal pembelian {purchase_id[:8]}")
    assert action(proposed) == "purchase_edit_proposed"
    assert "Stok akan dikurangi" in reply(proposed)
    applied = ingest(store, "ya")
    assert action(applied) == "purchase_voided"
    assert stock_of(store, rid) == before - Decimal(2)

    with store.transaction() as conn:
        row = conn.execute("SELECT body, voided_at FROM purchases WHERE id=?", (purchase_id,)).fetchone()
    assert json.loads(row[0]).get("voided") is True and row[1] is not None
    detail = store.restaurant_detail(rid)
    assert detail["recorded_purchases"] == 0  # voided purchases leave the counts

    again = ingest(store, f"batal pembelian {purchase_id[:8]}")
    assert action(again) == "edit_target_missing"


def test_plain_words_resolve_the_target_when_no_id_is_given(tmp_path):
    store, rid = fresh_store(tmp_path, dbname="plain.sqlite3")
    telur_id = record_purchase(store, rid, "beli telur 3 kg 45rb")
    cabai_id = record_purchase(store, rid, "beli cabai 2 kg 100rb")
    age_purchases(store)

    proposed = ingest(store, "ubah pembelian telur kemarin menjadi 2 kg")
    assert action(proposed) == "purchase_edit_proposed"
    assert telur_id[:8] in reply(proposed) and cabai_id[:8] not in reply(proposed)
    ingest(store, "batal")

    beras = record_purchase(store, rid, "beli beras 5 kg 60rb")
    age_purchases(store)
    proposed2 = ingest(store, "ubah pembelian beras 5 kg kemarin menjadi 4 kg")
    assert action(proposed2) == "purchase_edit_proposed"
    assert beras[:8] in reply(proposed2)
    ingest(store, "batal")


def test_ambiguous_plain_words_answer_with_the_candidates(tmp_path):
    store, rid = fresh_store(tmp_path, dbname="ambiguous.sqlite3")
    first = record_purchase(store, rid, "beli cabai 2 kg 100rb")
    second = record_purchase(store, rid, "beli cabai 2 kg 90rb")
    clash = ingest(store, "ubah pembelian cabai 2 kg jadi 3 kg")
    assert action(clash) == "edit_target_ambiguous"
    assert first[:8] in reply(clash) and second[:8] in reply(clash)


def test_pending_edit_owns_ya_and_batal_until_resolved(tmp_path):
    store, rid = fresh_store(tmp_path, dbname="pending.sqlite3")
    record_purchase(store, rid)
    proposed = ingest(store, "ubah pembelian cabai 2 kg jadi 3 kg")
    assert action(proposed) == "purchase_edit_proposed"
    # Any other text is answered with a reminder, not swallowed.
    reminder = ingest(store, "tolong dong")
    assert "YA" in reply(reminder) and action(reminder) == "edit_pending_reminder"
    # BATAL discards the proposal without touching the purchase.
    cancelled = ingest(store, "batal")
    assert action(cancelled) == "purchase_edit_cancelled"
    assert stock_of(store, rid) == Decimal(2)
    with store.transaction() as conn:
        pending = conn.execute("SELECT pending_edit FROM whatsapp_threads WHERE sender=?", (SENDER,)).fetchone()[0]
    assert pending is None


def test_edit_target_missing_and_active_draft_guard(tmp_path):
    store, rid = fresh_store(tmp_path, dbname="guard.sqlite3")
    purchase_id = record_purchase(store, rid)
    missing = ingest(store, "ubah pembelian 12345678 jadi cabai 3 kg")
    assert action(missing) == "edit_target_missing"

    draft = ingest(store, "beli beras 5 kg 60rb")
    assert action(draft) == "draft_created"
    blocked = ingest(store, "ubah pembelian cabai 2 kg jadi 3 kg")
    # The active draft owns the conversation: finish it before any correction.
    assert action(blocked) == "active_draft_exists"
    ingest(store, "batal")  # discard the unrelated draft
    after = ingest(store, f"ubah pembelian {purchase_id[:8]} jadi cabai 3 kg")
    assert action(after) == "purchase_edit_proposed"
    assert purchase_id[:8] in reply(after)
