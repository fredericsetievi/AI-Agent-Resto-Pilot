"""Live PostgresStore tests for the ported features (opt-in).

Runs against the real Supabase project only when SUPABASE_LIVE_TESTS=1 is
set; otherwise the whole module is skipped. Everything is scoped to a
temporary tenant (slug pg-live-*) that is deleted in teardown, so the demo
restaurant's data is never touched.
"""

import base64
import json
import os
from uuid import uuid4
import uuid
from datetime import datetime, timedelta, timezone

import pymupdf
import pytest
from fastapi.testclient import TestClient

from conftest import login_owner
from service.api import create_app
from service.db import DomainError
from service.pg import pg_connection
from service.pdfpages import render_pdf_pages
from service.pg_store import PostgresStore
from service.telegram import parse_update

pytestmark = pytest.mark.skipif(
    os.environ.get("SUPABASE_LIVE_TESTS", "").strip().lower() not in {"1", "true", "yes"},
    reason="live Supabase tests run only with SUPABASE_LIVE_TESTS=1",
)

NOTA = {"supplier": "Pasar PDF", "items": [{"raw_name": "Cabai Merah", "quantity": "2", "unit": "kg", "line_total_idr": 100000}], "unreadable": []}


@pytest.fixture(scope="module")
def store():
    return PostgresStore(seed=False)


@pytest.fixture
def tenant(store):
    slug = "pg-live-" + uuid.uuid4().hex[:8]
    restaurant_id = str(uuid.uuid4())
    phone = "629" + uuid.uuid4().hex[:9]  # unique digits; the live schema requires phone_number
    with pg_connection() as conn:
        # Purge leftovers from aborted earlier runs (best effort).
        for statement in (
            "DELETE FROM whatsapp_messages WHERE thread_id IN (SELECT id FROM whatsapp_threads WHERE substring(slug from 1 for 8) = 'pg-live-')",
            "DELETE FROM whatsapp_events WHERE thread_id IN (SELECT id FROM whatsapp_threads WHERE substring(slug from 1 for 8) = 'pg-live-')",
            "DELETE FROM whatsapp_threads WHERE substring(slug from 1 for 8) = 'pg-live-'",
            "DELETE FROM whatsapp_links WHERE substring(slug from 1 for 8) = 'pg-live-'",
            "DELETE FROM restaurant_rules WHERE substring(slug from 1 for 8) = 'pg-live-'",
            "DELETE FROM proactive_alerts WHERE substring(slug from 1 for 8) = 'pg-live-'",
            "DELETE FROM stock_counts WHERE substring(slug from 1 for 8) = 'pg-live-'",
            "DELETE FROM movements WHERE substring(slug from 1 for 8) = 'pg-live-'",
            "DELETE FROM purchases WHERE substring(slug from 1 for 8) = 'pg-live-'",
            "DELETE FROM drafts WHERE substring(slug from 1 for 8) = 'pg-live-'",
            "DELETE FROM stock WHERE substring(slug from 1 for 8) = 'pg-live-'",
            "DELETE FROM custom_ingredients WHERE substring(slug from 1 for 8) = 'pg-live-'",
            "DELETE FROM activity WHERE substring(slug from 1 for 8) = 'pg-live-'",
        ):
            store._execute(conn, statement)
        store._execute(conn,
            "INSERT INTO restaurants (id, name, phone_number, created_at) VALUES (%s, 'Live test resto', %s, now())",
            (restaurant_id, phone))
        store._execute(conn,
            "INSERT INTO tenants (slug, restaurant_id) VALUES (%s, %s) ON CONFLICT (slug) DO NOTHING",
            (slug, restaurant_id))
    yield slug
    # Children first (whatsapp_messages/events cascade from threads; rules,
    # alerts, links, stock_counts cascade from tenants at the final step).
    with pg_connection() as conn:
        cleanup = [
            "DELETE FROM whatsapp_messages WHERE thread_id IN (SELECT id FROM whatsapp_threads WHERE slug=%s)",
            "DELETE FROM whatsapp_events WHERE thread_id IN (SELECT id FROM whatsapp_threads WHERE slug=%s)",
            "DELETE FROM whatsapp_threads WHERE slug=%s",
            "DELETE FROM whatsapp_links WHERE slug=%s",
            "DELETE FROM restaurant_rules WHERE slug=%s",
            "DELETE FROM proactive_alerts WHERE slug=%s",
            "DELETE FROM stock_counts WHERE slug=%s",
            "DELETE FROM movements WHERE slug=%s",
            "DELETE FROM purchases WHERE slug=%s",
            "DELETE FROM drafts WHERE slug=%s",
            "DELETE FROM stock WHERE slug=%s",
            "DELETE FROM custom_ingredients WHERE slug=%s",
            "DELETE FROM activity WHERE slug=%s",
        ]
        for statement in cleanup:
            store._execute(conn, statement, (slug,))
        store._execute(conn, "DELETE FROM tenants WHERE slug=%s", (slug,))
        store._execute(conn, "DELETE FROM restaurants WHERE id=%s", (restaurant_id,))


def insert_purchase(store, slug, when_iso, ingredient_id, quantity, line_total):
    with pg_connection() as conn:
        draft_id = uuid.uuid4().hex
        store._execute(conn,
            "INSERT INTO drafts (id, version, status, body, slug) VALUES (%s, 1, 'recorded', '{}', %s)",
            (draft_id, slug))
        unit_price = int(line_total) // int(quantity) if quantity else None
        items = [{"ingredient_id": ingredient_id, "quantity": quantity, "canonical_quantity": quantity,
                  "unit": "kg", "line_total_idr": line_total, "unit_price_idr": unit_price}]
        body = json.dumps({"items": items, "total_idr": line_total, "supplier": "Pasar Uji"})
        store._execute(conn,
            "INSERT INTO purchases (id, draft_id, supplier, created_at, body, slug) VALUES (%s, %s, 'Pasar Uji', %s, %s, %s)",
            (uuid.uuid4().hex, draft_id, when_iso, body, slug))


# --- Multi-platform channel links -------------------------------------------


def test_one_phone_links_to_both_platforms(store, tenant):
    with pg_connection() as conn:
        for platform in ("whatsapp", "telegram"):
            store._execute(conn,
                "INSERT INTO whatsapp_links (id, slug, phone, platform, label) VALUES (%s, %s, %s, %s, %s)",
                (uuid.uuid4().hex, tenant, "628111000001", platform, None))
    links = store.linked_numbers(tenant)
    mine = {(link["phone"], link["platform"]) for link in links if link["phone"] == "628111000001"}
    assert mine == {("628111000001", "whatsapp"), ("628111000001", "telegram")}
    assert store.restaurant_for_sender("628111000001", "whatsapp")["id"] == tenant
    assert store.restaurant_for_sender("628111000001", "telegram")["id"] == tenant
    with pytest.raises(DomainError):
        store.add_whatsapp_number(tenant, uuid4().hex,
                                  {"whatsapp_number": "628111000001", "platform": "whatsapp"})


def test_remove_link_is_per_platform(store, tenant):
    with pg_connection() as conn:
        for platform in ("whatsapp", "telegram"):
            store._execute(conn,
                "INSERT INTO whatsapp_links (id, slug, phone, platform, label) VALUES (%s, %s, %s, %s, %s)",
                (uuid.uuid4().hex, tenant, "628222000002", platform, None))
    store.remove_whatsapp_number(tenant, "628222000002", uuid4().hex, platform="whatsapp")
    mine = {link["platform"] for link in store.linked_numbers(tenant) if link["phone"] == "628222000002"}
    assert mine == {"telegram"}


# --- Rules ---------------------------------------------------------------------


def test_rules_crud_and_enabled_filter(store, tenant):
    created = store.add_rule(tenant, "kabari kalau cabai di bawah 3 kg")
    assert created["enabled"] is True
    store.add_rule(tenant, "jangan ingatkan stok telur")
    listed = store.list_rules(tenant)
    assert len(listed) == 2
    store.set_rule(tenant, listed[1]["id"], enabled=False)
    assert [rule["enabled"] for rule in store.list_rules(tenant)] == [True, False]
    assert store.delete_rule(tenant, listed[0]["id"]) == {"ok": True}
    assert [rule["id"] for rule in store.list_rules(tenant)] == [listed[1]["id"]]


# --- Proactive candidates and cooldown -----------------------------------------


def test_low_stock_candidates_and_cooldown(store, tenant):
    first = store.low_stock_candidates(tenant, cooldown_hours=24)
    assert first, "empty stock must produce candidates"
    store.mark_proactive_alerts(tenant, first)
    assert store.low_stock_candidates(tenant, cooldown_hours=24) == []


# --- Data questions v2 -----------------------------------------------------------


def test_data_facts_windows_and_price(store, tenant):
    now = datetime.now(timezone.utc)
    insert_purchase(store, tenant, now, "cabai", "2", 100000)
    insert_purchase(store, tenant, now - timedelta(days=10), "cabai", "1", 60000)

    spend = store.data_facts("berapa pengeluaran 7 hari terakhir?", tenant)
    fact = spend["facts"][0]
    assert fact["total_idr"] == 100000  # the 10-day-old purchase is outside the window

    price = store.data_facts("harga cabai sekarang?", tenant)["facts"][0]
    assert price["last_price_idr"] == 50000 and price["previous_price_idr"] == 60000


def test_data_facts_empty_window_is_honest(store, tenant):
    result = store.data_facts("pengeluaran hari ini?", "pg-live-tidak-ada")
    assert result is None  # unregistered slug: no tenant, no facts


# --- Store-level fixture tests for the ported methods ----------------------------


def test_dashboard_stock_count_updates_stock(store, tenant):
    record = store.record_stock_count(tenant, "cabai", "2,5", note="pagi")
    assert record["estimated_quantity"] == "0" and record["counted_quantity"] == "2.5"
    history = store.recent_stock_counts(tenant)
    assert history and history[0]["counted_quantity"] == "2.5"


def test_dashboard_stock_count_rejects_bad_values(store, tenant):
    with pytest.raises(DomainError):
        store.record_stock_count(tenant, "cabai", "banyak")
    with pytest.raises(DomainError):
        store.record_stock_count(tenant, "bukan-bahan", "1")


# --- End to end: PDF intake over the live store -----------------------------------


class FakeResponse:
    def __init__(self, body):
        self.status_code = 200
        self._body = body

    def json(self):
        return self._body


class FakeVisionClient:
    def __init__(self, completions):
        self.completions = list(completions)
        self.calls = 0

    async def post(self, url, *, headers=None, json=None):
        import json as jsonlib

        self.calls += 1
        payload = self.completions[min(self.calls - 1, len(self.completions) - 1)]
        return FakeResponse({"choices": [{"message": {"content": jsonlib.dumps(payload)}}]})


def test_pdf_intake_over_the_live_store(tmp_path, store, tenant):
    vision = FakeVisionClient([NOTA])
    app = create_app(tmp_path / "pdf.sqlite3", seed=False, vision_api_key="synthetic-key",
                     vision_http_client=vision)
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    login_owner(client)
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "nota halaman 1")
    pdf_bytes = document.tobytes()
    document.close()
    response = client.post("/v1/intake/pdf", headers={"Idempotency-Key": uuid4().hex},
                           json={"pdf_base64": base64.b64encode(pdf_bytes).decode()})
    assert response.status_code == 201, response.text
    assert [item["ingredient_id"] for item in response.json()["data"]["items"]] == ["cabai"]
    assert vision.calls == 1
