"""Sales & Menu insights: ranking, trends, and margin from recorded sales.

Answers are computed deterministically from the sale events owners
record (recipe-based sales with portions and revenue) and the recipes —
the same ledger the inventory operations write. Purchases in the last
30 days price the ingredient cost, mirroring the price-evidence rule.
"""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient

from conftest import login_owner
from service.api import create_app
from service.db import RESTAURANT

TENANT = RESTAURANT["id"]
TODAY = datetime.now(timezone.utc).date().isoformat()
YESTERDAY = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()


def sale(event_id, name, *, portions=None, revenue=None, day=TODAY, confirmed=True):
    body = {"id": event_id, "kind": "sale", "name": name, "confirmed": confirmed,
            "created_at": datetime.now(timezone.utc).isoformat(), "items": []}
    if portions is not None:
        body["portions"] = portions
    if revenue is not None:
        body["revenue_idr"] = revenue
    body["business_date"] = day
    return body


def seed_events(store, events):
    with store.transaction() as conn:
        for event in events:
            store._inventory_query(conn, "INSERT INTO inventory_events(id,restaurant_id,body,created_at) VALUES (?,?,?,?)",
                                   (event["id"], TENANT, json.dumps(event), datetime.now(timezone.utc).isoformat()))


def seed_recipe(store, name, items):
    recipe = {"id": "recipe-" + name.casefold().replace(" ", "-"), "name": name, "items": items}
    with store.transaction() as conn:
        store._inventory_query(conn, "INSERT INTO inventory_recipes(id,restaurant_id,body) VALUES (?,?,?)",
                               (recipe["id"], TENANT, json.dumps(recipe)))
    return recipe


def record_price_purchase(store, text):
    """Record a purchase through the chat flow so the unit price enters the ledger."""
    first = store.whatsapp_ingest(uuid4().hex, {"sender": "628123456789", "text": text})
    assert first["result"]["action"] == "draft_created", first
    approved = store.whatsapp_ingest(uuid4().hex, {"sender": "628123456789", "text": "ya"})
    assert approved["result"]["action"] == "approved", approved


def test_menu_facts_ignores_non_menu_messages(tmp_path):
    app = create_app(tmp_path / "menu.sqlite3")
    store = app.state.store
    assert store.menu_facts("beli cabai 2 kg 100rb", TENANT) is None
    assert store.menu_facts("berapa sisa cabai?", TENANT) is None
    assert store.menu_facts("halo", TENANT) is None
    assert store.menu_facts("pengeluaran cabai minggu ini?", TENANT) is None


def test_ranking_answers_with_portions_and_revenue(tmp_path):
    app = create_app(tmp_path / "rank.sqlite3")
    store = app.state.store
    seed_events(store, [
        sale("s1", "Nasi goreng", portions="10", revenue=150000, day=TODAY),
        sale("s2", "Nasi goreng", portions="8", revenue=120000, day=YESTERDAY),
        sale("s3", "Mie goreng", portions="4", revenue=60000, day=TODAY),
        sale("s4", "Mie goreng", portions="6", revenue=90000, day=TODAY, confirmed=False),  # unconfirmed: excluded
    ])
    body = store.menu_facts("menu terlaris minggu ini?", TENANT)["body"]
    nasi = body.index("Nasi goreng")
    mie = body.index("Mie goreng")
    assert nasi < mie  # ranked by portions
    assert "10 porsi" in body and "Omzet" in body or "omzet" in body
    assert "belum" not in body.splitlines()[0].casefold()


def test_trend_compares_the_previous_window(tmp_path):
    app = create_app(tmp_path / "trend.sqlite3")
    store = app.state.store
    seed_events(store, [
        sale("s1", "Nasi goreng", portions="10", revenue=150000, day=TODAY),
        sale("s2", "Nasi goreng", portions="8", revenue=120000, day=(datetime.now(timezone.utc) - timedelta(days=8)).date().isoformat()),
    ])
    body = store.menu_facts("penjualan nasi goreng minggu ini?", TENANT)["body"]
    assert "10" in body and "8" in body and "+25" in body


def test_margin_uses_recipe_and_recorded_purchase_prices(tmp_path):
    app = create_app(tmp_path / "margin.sqlite3")
    store = app.state.store
    seed_recipe(store, "Nasi goreng", [{"ingredient_id": "cabai", "quantity": "0.1"},
                                       {"ingredient_id": "beras", "quantity": "0.2"}])
    seed_events(store, [sale("s1", "Nasi goreng", portions="10", revenue=150000, day=TODAY)])
    record_price_purchase(store, "beli cabai 2 kg 100rb")   # 50.000/kg → 5.000 per portion
    record_price_purchase(store, "beli beras 5 kg 60rb")    # 12.000/kg → 2.400 per portion
    body = store.menu_facts("margin nasi goreng?", TENANT)["body"]
    # omzet 15.000/porsi − bahan 7.400/porsi = 7.600/porsi
    assert "7.400" in body and "15.000" in body and "7.600" in body
    assert "estimasi" in body.casefold()


def test_margin_names_missing_prices_honestly(tmp_path):
    app = create_app(tmp_path / "missing.sqlite3")
    store = app.state.store
    seed_recipe(store, "Nasi goreng", [{"ingredient_id": "cabai", "quantity": "0.1"},
                                       {"ingredient_id": "beras", "quantity": "0.2"}])
    seed_events(store, [sale("s1", "Nasi goreng", portions="10", revenue=150000, day=TODAY)])
    record_price_purchase(store, "beli cabai 2 kg 100rb")  # beras has no recorded price yet
    body = store.menu_facts("margin nasi goreng?", TENANT)["body"]
    assert "belum lengkap" in body and "beras" in body.casefold()


def test_no_sales_answers_honestly(tmp_path):
    app = create_app(tmp_path / "empty.sqlite3")
    store = app.state.store
    body = store.menu_facts("menu terlaris minggu ini?", TENANT)["body"]
    assert "Belum ada penjualan" in body


def test_simulator_answers_menu_questions(tmp_path):
    app = create_app(tmp_path / "chat.sqlite3")
    store = app.state.store
    seed_recipe(store, "Nasi goreng", [{"ingredient_id": "cabai", "quantity": "0.1"}])
    seed_events(store, [
        sale("s1", "Nasi goreng", portions="10", revenue=150000, day=TODAY),
        sale("s2", "Mie goreng", portions="4", revenue=60000, day=TODAY),
    ])
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as client:
        from conftest import login_owner
        login_owner(client)
        headers = {"Idempotency-Key": uuid4().hex}
        response = client.post("/v1/whatsapp/simulate", headers=headers,
                               json={"sender": "628123456789", "text": "menu apa yang paling laku minggu ini?"})
        assert response.status_code == 200, response.text
        messages = response.json()["data"]["messages"]
        answer = messages[-1]["body"]
        assert "Nasi goreng" in answer and "Mie goreng" in answer
        assert "10 porsi" in answer


def test_flexible_phrasings_all_reach_the_engine(tmp_path):
    """The owner phrasings from the flexibility matrix must all answer."""
    app = create_app(tmp_path / "flex.sqlite3")
    store = app.state.store
    seed_events(store, [sale("s1", "Nasi goreng", portions="10", revenue=150000, day=TODAY)])
    good = [
        "menu terlaris minggu ini?",
        "menu apa yang paling laku minggu ini?",
        "omzet minggu ini berapa?",
        "penjualan minggu ini?",
        "rekap penjualan 30 hari?",
        "menu best seller minggu ini?",
        "yang paling dicari minggu ini?",
        "jualannya gimana minggu ini?",
        "margin nasi goreng berapa?",
        "untung nasi goreng?",
    ]
    for question in good:
        result = store.menu_facts(question, TENANT)
        assert result is not None, question
        assert "Nasi goreng" in result["body"] or "Penjualan" in result["body"] or "Margin" in result["body"], question


def test_vs_phrasing_compares_two_windows(tmp_path):
    app = create_app(tmp_path / "vs.sqlite3")
    store = app.state.store
    seed_events(store, [
        sale("s1", "Nasi goreng", portions="10", revenue=150000, day=TODAY),
        sale("s2", "Nasi goreng", portions="8", revenue=120000,
             day=(datetime.now(timezone.utc) - timedelta(days=8)).date().isoformat()),
    ])
    body = store.menu_facts("nasi goreng minggu ini vs minggu lalu?", TENANT)["body"]
    assert "10" in body and "8" in body and "+25" in body


def test_numbered_window_widens_the_answer(tmp_path):
    app = create_app(tmp_path / "days.sqlite3")
    store = app.state.store
    seed_events(store, [
        sale("s1", "Nasi goreng", portions="10", revenue=150000,
             day=(datetime.now(timezone.utc) - timedelta(days=20)).date().isoformat()),
    ])
    body = store.menu_facts("rekap penjualan 30 hari?", TENANT)["body"]
    assert "Nasi goreng" in body  # a 20-day-old sale only shows in a 30-day window
    assert "30 hari" in body


def test_trend_question_survives_the_operations_classifier(tmp_path):
    """'penjualan nasi goreng minggu ini?' must be answered, not claimed as an operations report."""
    app = create_app(tmp_path / "chat.sqlite3")
    store = app.state.store
    seed_events(store, [sale("s1", "Nasi goreng", portions="10", revenue=150000, day=TODAY)])
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as client:
        from conftest import login_owner
        login_owner(client)
        response = client.post("/v1/whatsapp/simulate", headers={"Idempotency-Key": uuid4().hex},
                               json={"sender": "628123456789", "text": "penjualan nasi goreng minggu ini?"})
        assert response.status_code == 200, response.text
        answer = response.json()["data"]["messages"][-1]["body"]
        assert "Penjualan" in answer and "Operasi" not in answer


def test_worst_seller_question_ranks_ascending(tmp_path):
    app = create_app(tmp_path / "worst.sqlite3")
    store = app.state.store
    seed_events(store, [
        sale("s1", "Nasi goreng", portions="10", revenue=150000, day=TODAY),
        sale("s2", "Mie ayam", portions="2", revenue=30000, day=TODAY),
    ])
    body = store.menu_facts("menu paling sepi minggu ini?", TENANT)["body"]
    assert "paling sepi" in body
    assert body.index("Mie ayam") < body.index("Nasi goreng")  # worst first


def test_revenue_wordings_reach_the_engine(tmp_path):
    app = create_app(tmp_path / "revenue.sqlite3")
    store = app.state.store
    # "hari ini" windows use the restaurant-local date; seed with the local
    # today so the sale lands inside the window even right after midnight WIB
    # (when the UTC date is still yesterday).
    today_local = datetime.now().date().isoformat()
    seed_events(store, [sale("s1", "Nasi goreng", portions="10", revenue=150000, day=today_local)])
    for question in ("berapa pendapatan hari ini?", "berapa pemasukan minggu ini?"):
        body = store.menu_facts(question, TENANT)["body"]
        assert body and ("150.000" in body or "porsi" in body), question


def test_two_menu_comparison(tmp_path):
    app = create_app(tmp_path / "pair.sqlite3")
    store = app.state.store
    seed_events(store, [
        sale("s1", "Nasi goreng", portions="10", revenue=150000, day=TODAY),
        sale("s2", "Mie ayam", portions="4", revenue=60000, day=TODAY),
    ])
    body = store.menu_facts("bandingkan nasi goreng dan mie ayam bulan ini", TENANT)["body"]
    assert "Perbandingan" in body
    assert "Mie ayam" in body and "Nasi goreng" in body
    assert "Lebih laku: Nasi goreng" in body


def test_question_with_numbers_is_still_a_question(tmp_path):
    app = create_app(tmp_path / "numq.sqlite3")
    store = app.state.store
    seed_events(store, [sale("s1", "Nasi goreng", portions="10", revenue=150000, day=TODAY)])
    body = store.menu_facts("berapa terjual nasi goreng 4 hari terakhir?", TENANT)["body"]
    assert body and "4 hari terakhir" in body
