"""Tanya-data v2: flexible windows, per-ingredient spend, compare, rank, average.

Every figure is read from the purchase ledger; the window parser only knows a
bounded list of Indonesian phrases, and the validators lock each figure into
any model rephrase.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from conftest import login_owner
from service.data_qa import build_answer, parse_window
from service.db import RESTAURANT, Store


TENANT = RESTAURANT["id"]
_TZ = ZoneInfo("Asia/Jakarta")


def wib(year, month, day, hour=12):
    """Noon WIB on a date, converted to UTC — safely inside the day."""
    local = datetime(year, month, day, hour, tzinfo=_TZ)
    return local.astimezone(timezone.utc)


def insert_purchase(store, when_utc, ingredient_id, quantity, line_total):
    """A recorded purchase at an arbitrary time, straight into the ledger."""
    with store.transaction() as conn:
        draft_id = uuid4().hex
        conn.execute("INSERT INTO drafts (id, version, status, body, restaurant_id) VALUES (?, 1, 'recorded', '{}', ?)",
                     (draft_id, TENANT))
        items = [{"ingredient_id": ingredient_id, "quantity": quantity, "canonical_quantity": quantity,
                  "unit": "kg", "line_total_idr": line_total}]
        body = json.dumps({"items": items, "total_idr": line_total, "supplier": "Pasar Uji"})
        conn.execute("INSERT INTO purchases (id, draft_id, supplier, created_at, body, restaurant_id) VALUES (?, ?, 'Pasar Uji', ?, ?, ?)",
                     (uuid4().hex, draft_id, when_utc.isoformat(), body, TENANT))


# --- Window parsing -------------------------------------------------------------


def test_parse_window_labels_and_bounds():
    today = datetime.now(_TZ).date()
    kemarin = parse_window("pengeluaran kemarin?")
    assert kemarin["label"] == "kemarin"
    assert kemarin["start"] == (datetime(today.year, today.month, today.day, tzinfo=_TZ) - timedelta(days=1)).isoformat()
    assert parse_window("pengeluaran bulan ini?")["start"].startswith(f"{today.year}-{today.month:02d}-01")
    assert parse_window("pengeluaran bulan lalu?")["label"] == "bulan lalu"
    assert parse_window("pengeluaran 30 hari terakhir?")["label"] == "30 hari terakhir"
    assert parse_window("total belanja semua waktu?")["start"] is None
    assert parse_window("berapa pengeluaran?")["label"] == "7 hari terakhir"  # classic default


# --- Ledger facts per window ------------------------------------------------------


def v2_store(tmp_path):
    store = Store(tmp_path / "v2.sqlite3", seed=False)
    now = datetime.now(timezone.utc)
    insert_purchase(store, now, "cabai", "2", 100000)                 # today
    insert_purchase(store, wib(*today_minus(1).timetuple()[:3]), "bawang", "1", 50000)   # yesterday
    insert_purchase(store, now - timedelta(days=10), "cabai", "1", 60000)   # inside "minggu lalu"
    return store


def today_minus(days):
    return (datetime.now(_TZ) - timedelta(days=days)).date()


def test_spend_yesterday_excludes_other_days(tmp_path):
    store = v2_store(tmp_path)
    result = store.data_facts("berapa pengeluaran kemarin?", TENANT)
    fact = result["facts"][0]
    assert fact["label"] == "kemarin" and fact["total_idr"] == 50000 and fact["purchase_count"] == 1


def test_spend_this_month_and_named_ingredient(tmp_path):
    store = v2_store(tmp_path)
    # bulan ini: today + yesterday + the 10-day-old purchase only if that date
    # still falls in the running month, so assert via a consistent subset.
    result = store.data_facts("pengeluaran bulan ini?", TENANT)
    fact = result["facts"][0]
    assert fact["total_idr"] >= 150000  # cabai hari ini + bawang kemarin at minimum
    named = store.data_facts("pengeluaran cabai bulan ini?", TENANT)["facts"][0]
    assert named["name"] == "Cabai merah"
    assert named["total_idr"] in (100000, 160000)  # depends on whether the 10-day-old is inside


def test_compare_two_windows(tmp_path):
    store = v2_store(tmp_path)
    result = store.data_facts("pengeluaran minggu ini vs minggu lalu?", TENANT)
    fact = result["facts"][0]
    assert fact["total_a"] == 150000  # today cabai + yesterday bawang
    assert fact["total_b"] == 60000   # the 10-day-old cabai
    assert any("150" in pattern for pattern in fact["_require"])


def test_rank_orders_by_recorded_spend(tmp_path):
    store = v2_store(tmp_path)
    result = store.data_facts("pengeluaran terbesar minggu ini?", TENANT)
    fact = result["facts"][0]
    assert fact["items"][0]["name"] == "Cabai merah" and fact["items"][0]["total_idr"] == 100000


def test_average_divides_by_weeks(tmp_path):
    store = v2_store(tmp_path)
    result = store.data_facts("berapa rata-rata belanja per minggu?", TENANT)
    fact = result["facts"][0]
    assert fact["total_idr"] == 210000 and fact["weeks"] >= 1


def test_empty_window_gets_an_honest_empty_text(tmp_path):
    store = Store(tmp_path / "empty.sqlite3", seed=False)  # no purchases at all
    result = store.data_facts("pengeluaran 7 hari terakhir?", TENANT)
    assert result["facts"] == []
    assert "7 hari terakhir" in result["empty_text"]


# --- Deterministic answer text -----------------------------------------------------


def test_build_answer_v2_formats():
    assert build_answer([{"kind": "spend_ingredient", "name": "Cabai merah", "label": "bulan ini",
                          "total_idr": 160000, "purchase_count": 2}]
                        ) == "Pengeluaran bulan ini untuk Cabai merah: Rp160.000 (dari 2 catatan)."
    compare = build_answer([{"kind": "spend_compare", "label_a": "7 hari terakhir", "total_a": 150000, "count_a": 2,
                             "label_b": "minggu lalu", "total_b": 60000, "count_b": 1, "change_percent": "150.0"}])
    assert compare == ("Pengeluaran 7 hari terakhir: Rp150.000 (2 pembelian) vs minggu lalu: Rp60.000 (1). "
                       "Selisih naik 150,0%.")
    rank = build_answer([{"kind": "rank", "label": "bulan ini", "items": [
        {"name": "Cabai merah", "total_idr": 160000}, {"name": "Bawang merah", "total_idr": 50000}]}])
    assert rank == "Pengeluaran terbesar bulan ini: 1. Cabai merah Rp160.000; 2. Bawang merah Rp50.000."
    assert build_answer([{"kind": "average", "label": "semua waktu", "total_idr": 210000, "weeks": 3, "average_idr": 70000}]
                        ) == "Rata-rata per minggu (semua waktu): Rp70.000 (dari 3 minggu)."


# --- End to end through the simulator -----------------------------------------------


def qa_client(tmp_path):
    from fastapi.testclient import TestClient
    from service.api import create_app

    app = create_app(tmp_path / "v2.sqlite3", seed=False)  # the test writes its own ledger
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    login_owner(client)
    return client


def simulate(client, text):
    return client.post("/v1/whatsapp/simulate", headers={"Idempotency-Key": uuid4().hex}, json={"sender": "628123456789", "text": text})


def test_simulator_answers_window_and_compare_questions(tmp_path):
    client = qa_client(tmp_path)
    store = client.app.state.store
    insert_purchase(store, wib(*today_minus(1).timetuple()[:3]), "bawang", "1", 50000)
    insert_purchase(store, datetime.now(timezone.utc) - timedelta(days=10), "cabai", "1", 60000)

    answer = data_of(simulate(client, "berapa pengeluaran kemarin?"))
    assert answer["result"]["action"] == "stock_answer"
    assert "kemarin" in answer["messages"][-1]["body"] and "Rp50.000" in answer["messages"][-1]["body"]

    compare = data_of(simulate(client, "pengeluaran minggu ini vs minggu lalu?"))
    body = compare["messages"][-1]["body"]
    assert "Rp50.000" in body and "Rp60.000" in body and "turun 16,7%" in body


def data_of(response):
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_purchase_comes_first_then_its_spend_is_visible(tmp_path):
    client = qa_client(tmp_path)
    result = data_of(simulate(client, "beli cabai 2 kg 100rb"))
    assert result["result"]["action"] == "draft_created"
    data_of(simulate(client, "ya"))
    answer = data_of(simulate(client, "pengeluaran hari ini?"))
    assert "Rp100.000" in answer["messages"][-1]["body"]
