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
