"""Supplier agent fase 2a: contacts, backfill, quotes, comparison, guards.

Tests use fresh restaurants (no seeded purchase history) so counts and
resolutions are exact; the demo tenant carries synthetic history.
"""
import contextlib
import json
from decimal import Decimal
from uuid import uuid4

from service.db import Store
from service.suppliers import SupplierMixin, parse_supplier_command, supplier_alert_hint

SENDER = "628888000111"  # linked to the fresh restaurant, not the demo tenant


def ingest(store, text, *, sender=SENDER, restaurant=None):
    response = store.whatsapp_ingest(uuid4().hex, {"sender": sender, "text": text})
    if restaurant:
        response["_restaurant_id"] = restaurant
    return response


def reply(response):
    return response["messages"][-1]["body"]


def action(response):
    return response["result"]["action"]


def fresh_store(tmp_path, dbname="suppliers.sqlite3"):
    store = Store(tmp_path / dbname)
    created = store.create_restaurant(uuid4().hex, {
        "name": "Warung Supplier", "owner_username": "sup" + uuid4().hex[:6],
        "owner_password": "rahasia-123", "whatsapp_number": SENDER,
    })
    return store, created["id"]


def record_purchase(store, restaurant, text="beli cabai 2 kg 100rb dari pasar pagi"):
    first = ingest(store, text, restaurant=restaurant)
    assert first["result"]["action"] == "draft_created", first
    approved = ingest(store, "ya")
    assert approved["result"]["action"] == "approved", approved
    ingest(store, "lewati")  # clear the one-shot payment/supplier follow-up offer
    return approved["result"]["purchase_id"]


def test_backfill_creates_supplier_entities_and_links_purchases(tmp_path):
    store, rid = fresh_store(tmp_path, "backfill.sqlite3")
    purchase_id = record_purchase(store, "beli cabai 2 kg 100rb dari pasar pagi")
    suppliers = store.suppliers_list(rid)
    assert len(suppliers) == 1 and suppliers[0]["name"] == "pasar pagi"
    assert suppliers[0]["purchase_count"] == 1
    with store.transaction() as conn:
        supplier_id = conn.execute("SELECT supplier_id FROM purchases WHERE id=?", (purchase_id,)).fetchone()[0]
    assert supplier_id == suppliers[0]["id"]


def test_add_list_update_remove_supplier(tmp_path):
    store, rid = fresh_store(tmp_path, "crud.sqlite3")
    added = store.supplier_add(uuid4().hex, {"name": "Pak Budi", "contact_phone": "628888000777",
                                             "contact_platform": "whatsapp", "notes": "ayam"}, rid)
    detail = added
    assert detail["name"] == "Pak Budi" and detail["contact_phone"] == "628888000777"
    assert [s["name"] for s in store.suppliers_list(rid)] == ["Pak Budi"]
    updated = store.supplier_update(rid, detail["id"], uuid4().hex, {"contact_phone": "628999000111"})
    assert updated["contact_phone"] == "628999000111"
    assert store.supplier_remove(rid, detail["id"], uuid4().hex)["ok"] is True
    assert store.suppliers_list(rid) == []


def test_owner_linked_number_cannot_become_supplier_contact(tmp_path):
    store, rid = fresh_store(tmp_path, "disjoint.sqlite3")
    # SENDER is linked to this restaurant as its owner number.
    try:
        store.supplier_add(uuid4().hex, {"name": "Pemilik Sendiri", "contact_phone": SENDER}, rid)
        raise AssertionError("owner-linked phone must not become a supplier contact")
    except Exception as exc:
        assert getattr(exc, "status", None) == 409 and "pemilik" in str(exc)


def test_supplier_contact_cannot_be_linked_as_owner_number(tmp_path):
    store, rid = fresh_store(tmp_path, "disjoint2.sqlite3")
    store.supplier_add(uuid4().hex, {"name": "Pak Budi", "contact_phone": "628888000777"}, rid)
    try:
        store.add_whatsapp_number(rid, uuid4().hex, {"whatsapp_number": "628888000777", "platform": "whatsapp"})
        raise AssertionError("supplier contact must not be linkable as an owner number")
    except Exception as exc:
        assert getattr(exc, "status", None) == 409 and "supplier" in str(exc)


def test_comparison_uses_quotes_and_purchase_baseline(tmp_path):
    store, rid = fresh_store(tmp_path, "compare.sqlite3")
    store.supplier_add(uuid4().hex, {"name": "Pasar Baru"}, rid)
    record_purchase(store, rid, "beli cabai 2 kg 100rb dari pasar pagi")  # 50rb/kg
    record_purchase(store, rid, "beli cabai 1 kg 45rb dari pasar baru")  # 45rb/kg
    quoted = ingest(store, "catat harga cabai supplier pasar baru 44rb")
    assert action(quoted) == "supplier_quote_recorded", reply(quoted)
    comparison = store.supplier_price_comparison(rid, "cabai")
    with store.transaction() as conn:
        dbg = conn.execute("SELECT supplier, created_at, supplier_id, body FROM purchases WHERE restaurant_id=?", (rid,)).fetchall()
        for d in dbg:
            body = json.loads(d[3])
            print("DBG-PURCHASE:", d[0], "|", d[1][:26], "| items:", [(i.get("raw_name"), i.get("canonical_quantity"), i.get("unit_price_idr")) for i in body.get("items", [])])
    print("DBG-QUOTES:", comparison["quotes"])
    print("DBG-BASELINE:", comparison["purchase_baseline"])
    assert comparison["quote_count"] == 1
    assert comparison["quotes"][0]["supplier_name"].casefold() == "pasar baru"
    assert comparison["quotes"][0]["unit_price_idr"] == 44000  # "44rb" = 44.000
    baseline = comparison["purchase_baseline"]
    assert baseline["supplier"] == "pasar baru" and baseline["unit_price_idr"] == 45000


def test_comparison_without_data_answers_honestly(tmp_path):
    store, rid = fresh_store(tmp_path, "empty.sqlite3")
    result = ingest(store, "supplier cabai termurah?")
    assert "Sebutkan bahan" in reply(result) or "belum" in reply(result).casefold()
    comparison = store.supplier_price_comparison(rid, "cabai")
    assert comparison is None or comparison["quotes"] == []


def test_chat_commands_list_add_compare_quote(tmp_path):
    store, rid = fresh_store(tmp_path, "chat.sqlite3")
    added = ingest(store, "tambah supplier pak budi wa 628555000999")
    assert action(added) == "supplier_added", reply(added)
    assert "pak budi" in reply(added).casefold()
    listed = ingest(store, "daftar supplier")
    assert action(listed) == "supplier_list" and "pak budi" in reply(listed).casefold()
    recorded = ingest(store, "catat harga cabai pak budi 48rb")
    assert action(recorded) == "supplier_quote_recorded", reply(recorded)
    compared = ingest(store, "supplier cabai termurah?")
    assert action(compared) == "supplier_compare"
    assert "48.000" in reply(compared) and "Termurah" in reply(compared)


def test_purchase_verbs_are_not_supplier_commands(tmp_path):
    store, rid = fresh_store(tmp_path, "guard.sqlite3")
    response = ingest(store, "beli cabai 2 kg 100rb")
    assert action(response) == "draft_created"


def test_parse_supplier_command_matrix():
    assert parse_supplier_command("daftar supplier")["kind"] == "list"
    assert parse_supplier_command("tambah supplier pak budi wa 628555")["kind"] == "add"
    assert parse_supplier_command("tanya harga ayam ke supplier")["kind"] == "inquiry"
    assert parse_supplier_command("catat harga cabai supplier pasar baru 44rb")["kind"] == "quote"
    assert parse_supplier_command("supplier cabai termurah?")["kind"] == "compare"
    assert parse_supplier_command("beli cabai 2 kg 100rb") is None
    assert parse_supplier_command("halo") is None


def test_proactive_alert_hint_names_the_cheapest_known_source(tmp_path):
    store, rid = fresh_store(tmp_path, "hint.sqlite3")
    record_purchase(store, rid, "beli cabai 2 kg 100rb dari pasar pagi")
    store.supplier_add(uuid4().hex, {"name": "Pasar Baru", "contact_phone": "628777000666",
                                     "contact_platform": "telegram", "notes": "cabai"}, rid)
    supplier_id = store.suppliers_list(rid)[0]["id"]
    store.supplier_quote_record(uuid4().hex, {"supplier_id": supplier_id, "ingredient_id": "cabai",
                                              "ingredient_name": "Cabai merah", "unit_price_idr": 44000}, rid)
    candidates = [{"ingredient_id": "cabai", "name": "Cabai merah", "quantity": "1", "unit": "kg"}]
    hint = supplier_alert_hint(store, rid, candidates)
    assert hint and "44.000" in hint and "Pasar Baru" in hint
    # No quotes or purchases for the ingredient → honest empty hint.
    empty = supplier_alert_hint(store, rid, [{"ingredient_id": "telur", "name": "Telur"}])
    assert empty is None


class _TenantColumnStub(SupplierMixin):
    """Captures supplier SQL so tests can pin the dialect-aware tenant column.

    Regression: supplier_for_phone hardcoded ``restaurant_id`` in its SELECT,
    which 500s every hosted Telegram message (Postgres uses ``slug``).
    """

    def __init__(self, postgres):
        self.inventory_postgres = postgres
        self.captured_sql = None

    def transaction(self):
        return contextlib.nullcontext(None)

    def _inventory_query(self, conn, sql, params):
        self.captured_sql = sql

        class _Cursor:
            def fetchone(self):
                return None

        return _Cursor()


def test_supplier_for_phone_selects_dialect_tenant_column():
    stub = _TenantColumnStub(postgres=True)
    assert stub.supplier_for_phone("628111222333") is None
    assert "slug" in stub.captured_sql
    assert "restaurant_id" not in stub.captured_sql

    stub = _TenantColumnStub(postgres=False)
    assert stub.supplier_for_phone("628111222333") is None
    assert "restaurant_id" in stub.captured_sql


def test_quote_without_keyword_stays_out_of_purchase_draft(tmp_path):
    store, rid = fresh_store(tmp_path, "quote-gate.sqlite3")
    # 'harga telur 45rb dari pak budi' is a price quote; it must never reach
    # the purchase draft parser, while 'beli telur 45rb' stays a purchase.
    assert parse_supplier_command("harga telur 45rb dari pak budi")["kind"] == "quote"
    assert parse_supplier_command("beli telur 45rb") is None
    store.supplier_add(uuid4().hex, {"name": "Pak Budi"}, rid)
    response = ingest(store, "harga telur 45rb dari pak budi")
    assert action(response) == "supplier_quote_recorded", reply(response)
    quotes = store.supplier_quotes_recent(rid)
    assert len(quotes) == 1 and quotes[0]["unit_price_idr"] == 45000


def test_supplier_reply_line_with_two_items_stores_both(tmp_path):
    store, rid = fresh_store(tmp_path, "multi-reply.sqlite3")
    supplier = store.supplier_add(uuid4().hex, {"name": "Pak Budi"}, rid)
    result = store.supplier_reply_intake(rid, supplier["id"], "telur 45rb\ncabai 32rb")
    names = [(q["ingredient_name"], q["price"]) for q in result["stored"]]
    assert ("Telur ayam", 45000) in names and ("Cabai merah", 32000) in names
    assert result["unparsed"] == []


def test_quote_unit_phrase_normalizes_per_kg(tmp_path):
    store, rid = fresh_store(tmp_path, "unit-norm.sqlite3")
    supplier = store.supplier_add(uuid4().hex, {"name": "Pak Budi"}, rid)
    result = store.supplier_reply_intake(rid, supplier["id"], "telur 45rb per setengah kg")
    stored = result["stored"]
    assert stored[0]["price"] == 90000  # 45rb per half kg = 90rb per kg
    assert "setengah kg" in (stored[0].get("note") or "")


def test_quote_unknown_unit_keeps_raw_price_and_note(tmp_path):
    store, rid = fresh_store(tmp_path, "unit-note.sqlite3")
    supplier = store.supplier_add(uuid4().hex, {"name": "Pak Budi"}, rid)
    result = store.supplier_reply_intake(rid, supplier["id"], "cabai 30rb per ikat")
    stored = result["stored"]
    assert stored[0]["price"] == 30000  # unknown multiplier: never invented
    assert "per ikat" in (stored[0].get("note") or "")


def test_hapus_supplier_requires_konfirmasi_then_removes(tmp_path):
    store, rid = fresh_store(tmp_path, "hapus-chat.sqlite3")
    store.supplier_add(uuid4().hex, {"name": "Pak Budi"}, rid)
    confirm_needed = ingest(store, "hapus supplier pak budi")
    assert action(confirm_needed) == "supplier_remove_confirm"
    assert "KONFIRMASI" in reply(confirm_needed)
    assert store.suppliers_list(rid), "nothing removed without confirmation"
    removed = ingest(store, "hapus supplier pak budi konfirmasi")
    assert action(removed) == "supplier_removed"
    assert store.suppliers_list(rid) == []


def test_update_supplier_contact_via_chat(tmp_path):
    store, rid = fresh_store(tmp_path, "update-chat.sqlite3")
    store.supplier_add(uuid4().hex, {"name": "Pak Budi"}, rid)
    response = ingest(store, "ubah supplier budi wa 628123456999")
    assert action(response) == "supplier_updated", reply(response)
    assert store.suppliers_list(rid)[0]["contact_phone"] == "628123456999"


def test_minta_harga_is_an_inquiry(tmp_path):
    store, rid = fresh_store(tmp_path, "minta.sqlite3")
    store.supplier_add(uuid4().hex, {"name": "Pak Budi", "contact_phone": "628888000777"}, rid)
    response = ingest(store, "minta harga cabai ke supplier")
    assert action(response) == "supplier_inquiry_created"


def test_supplier_history_via_chat(tmp_path):
    store, rid = fresh_store(tmp_path, "history-chat.sqlite3")
    store.supplier_add(uuid4().hex, {"name": "Pak Budi"}, rid)
    ingest(store, "catat harga telur pak budi 48rb")
    response = ingest(store, "riwayat harga supplier pak budi")
    assert action(response) == "supplier_history", reply(response)
    assert "48.000" in reply(response)


def test_unknown_question_is_logged_for_tuning(tmp_path):
    store, rid = fresh_store(tmp_path, "unknown.sqlite3")
    ingest(store, "berapa modal kulit lumpia bulan depan?")
    with store.transaction() as conn:
        row = conn.execute(
            "SELECT message FROM activity WHERE event='intent_unknown' ORDER BY created_at DESC LIMIT 1").fetchone()
    assert row and "kulit lumpia" in row[0]


def seed_market_prices(store, rid):
    """Pak Budi cheaper for cabai; pasar pagi cheaper for telur."""
    store.supplier_add(uuid4().hex, {"name": "Pak Budi"}, rid)
    store.supplier_add(uuid4().hex, {"name": "pasar pagi"}, rid)
    ingest(store, "catat harga cabai pak budi 30rb")
    ingest(store, "catat harga telur pak budi 50rb")
    ingest(store, "catat harga cabai pasar pagi 33rb")
    ingest(store, "catat harga telur pasar pagi 45rb")


def test_market_list_answers_best_price_per_ingredient(tmp_path):
    store, rid = fresh_store(tmp_path, "market.sqlite3")
    seed_market_prices(store, rid)
    response = ingest(store, "supplier cabai telur termurah?")
    body = reply(response)
    assert action(response) == "supplier_shopping_list"
    cabai_line = next(line for line in body.splitlines() if "Cabai merah" in line)
    telur_line = next(line for line in body.splitlines() if "Telur ayam" in line)
    assert "Pak Budi" in cabai_line and "Rp30.000" in cabai_line
    assert "pasar pagi" in telur_line and "Rp45.000" in telur_line


def test_market_list_with_quantities_estimates_cash(tmp_path):
    store, rid = fresh_store(tmp_path, "market-qty.sqlite3")
    seed_market_prices(store, rid)
    response = ingest(store, "supplier cabai 5kg telur 3kg termurah")
    body = reply(response)
    assert "Estimasi modal" in body
    assert "Rp285.000" in body  # 5kg cabai x 30rb + 3kg telur x 45rb
    assert "150.000" in body and "135.000" in body  # per-item line totals


def test_market_list_gram_quantity_converts_to_kg(tmp_path):
    store, rid = fresh_store(tmp_path, "market-gram.sqlite3")
    seed_market_prices(store, rid)
    response = ingest(store, "supplier cabai 500 gram telur 3kg termurah")
    assert "Rp15.000" in reply(response)  # 0.5kg cabai x 30rb
    assert "Rp150.000" in reply(response)  # total 15rb + 135rb


def test_market_list_names_missing_quotes_and_unknowns(tmp_path):
    store, rid = fresh_store(tmp_path, "market-mixed.sqlite3")
    seed_market_prices(store, rid)
    response = ingest(store, "supplier cabai beras kulit lumpia termurah")
    body = reply(response)
    assert "Beras" in body and "belum ada kutipan" in body
    assert "Tidak dikenali: kulit lumpia" in body


def test_single_compare_regression_stays_single(tmp_path):
    store, rid = fresh_store(tmp_path, "single.sqlite3")
    seed_market_prices(store, rid)
    response = ingest(store, "supplier cabai termurah?")
    body = reply(response)
    assert action(response) == "supplier_compare"
    assert "Termurah saat ini: Pak Budi" in body


def test_parse_shopping_gate(tmp_path):
    assert parse_supplier_command("daftar belanja cabai telur")["kind"] == "shopping"
    assert parse_supplier_command("belanja pasar hari ini")["kind"] == "shopping"
    assert parse_supplier_command("daftar supplier")["kind"] == "list"


def test_daftar_belanja_without_names_uses_below_threshold_stock(tmp_path):
    store, rid = fresh_store(tmp_path, "refill.sqlite3")
    seed_market_prices(store, rid)
    with store.transaction() as conn:
        conn.execute("UPDATE inventory SET quantity='2' WHERE restaurant_id=? AND ingredient_id='cabai'", (rid,))
    response = ingest(store, "daftar belanja pasar")
    body = reply(response)
    assert action(response) == "shopping_plan"
    assert "stok di bawah ambang" in body
    cabai_line = next(line for line in body.splitlines() if "Cabai merah" in line)
    assert "Pak Budi" in cabai_line and "Rp30.000" in cabai_line
    assert "Estimasi modal" in body


def test_daftar_belanja_when_stock_is_healthy_answers_honestly(tmp_path):
    store, rid = fresh_store(tmp_path, "refill-none.sqlite3")
    with store.transaction() as conn:
        for ingredient in store._catalog_for(rid)["list"]:
            store._inventory_query(conn, "INSERT OR REPLACE INTO inventory (restaurant_id, ingredient_id, quantity) VALUES (?,?,?)",
                                   (rid, ingredient["id"], "99"))
    response = ingest(store, "daftar belanja pasar")
    assert action(response) == "shopping_empty"
    assert "di atas ambang" in reply(response)
