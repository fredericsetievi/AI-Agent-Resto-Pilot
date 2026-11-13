"""Daily cash envelope: budget in, change back, unrecorded spend reported.

The Meta test account cannot reach Indonesian numbers, so the pilot runs on
Telegram; the send path is shared and covered in test_proactive_templates /
test_reminders. Here: the deterministic command parse, the per-day envelope
roundtrip, the ingest branch (never a stock change, survives a waiting draft),
the unrecorded-gap math, and the envelope-aware reminder bodies.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from conftest import login_owner
from service.api import create_app
from service.cash_envelope import cash_envelope_status_text, parse_envelope_command
from service.db import RESTAURANT_TIMEZONE
from test_tenant_isolation import META, FakeGraphClient

SENDER = "628123456789"  # the demo restaurant's linked number


def owner_client(tmp_path, **kwargs):
    app = create_app(tmp_path / "envelope.sqlite3", reminder_check_interval_seconds=0, **kwargs)
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    login_owner(client)
    return client


def hour_in_band(hour):
    """The reminder engine refuses hours outside 7–21; clamp the test clock."""
    return hour if 7 <= hour <= 21 else 9


def inbound_hours_ago(store, hours=8.0):
    """An inbound message 8h back: inside WhatsApp's 24-hour window (free-form
    send works) yet outside the 6-hour silence window (the nudge may fire)."""
    store.whatsapp_ingest(uuid4().hex, {"sender": SENDER, "text": "berapa sisa cabai?"})
    with store.transaction() as conn:
        conn.execute(
            "UPDATE whatsapp_messages SET created_at=? WHERE direction='inbound'",
            ((datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(),))


def set_slot_to_now(client, slot):
    store = client.app.state.store
    resto = store.list_restaurants()[0]["id"]
    hour = hour_in_band(datetime.now(RESTAURANT_TIMEZONE).hour)
    store.set_reminder_settings(resto, **{f"{slot}_hour": hour})
    return store, resto


def record_purchase(store):
    """One approved purchase through the chat: 10 bolkus at 25.000 = 250.000."""
    store.whatsapp_ingest(uuid4().hex, {"sender": SENDER, "text": "bolkus jumbo 10 25000"})
    store.whatsapp_ingest(uuid4().hex, {"sender": SENDER, "text": "ya"})


# ---- command parse -----------------------------------------------------------

def test_parse_budget_set_variants():
    assert parse_envelope_command("budget hari ini 2 juta") == {"kind": "set", "amount": 2_000_000}
    assert parse_envelope_command("anggaran 500rb") == {"kind": "set", "amount": 500_000}
    assert parse_envelope_command("Budget Rp1.500.000") == {"kind": "set", "amount": 1_500_000}
    assert parse_envelope_command("budget 2,5 juta") == {"kind": "set", "amount": 2_500_000}


def test_parse_leftover_variants():
    assert parse_envelope_command("uang kembali 500 ribu") == {"kind": "leftover", "amount": 500_000}
    assert parse_envelope_command("kembali 50rb") == {"kind": "leftover", "amount": 50_000}
    assert parse_envelope_command("Kembalian 100rb") == {"kind": "leftover", "amount": 100_000}


def test_parse_show_and_invalid():
    assert parse_envelope_command("budget") == {"kind": "show"}
    assert parse_envelope_command("budget?") == {"kind": "show"}
    assert parse_envelope_command("anggaran hari ini") == {"kind": "show"}
    assert parse_envelope_command("budget 999999999 juta") == {"kind": "invalid"}


def test_parse_ignores_normal_messages():
    assert parse_envelope_command("beli cabai 2 kg 25000") is None
    assert parse_envelope_command("kembali") is None  # bare word is never hijacked
    assert parse_envelope_command("harga cabai kembali naik") is None
    assert parse_envelope_command("") is None
    assert parse_envelope_command(None) is None


def test_status_text_is_indonesian_and_names_the_gap():
    text = cash_envelope_status_text({"envelope_idr": 2_000_000, "leftover_idr": 500_000,
                                      "recorded_idr": 250_000, "purchase_count": 1,
                                      "unrecorded_idr": 1_250_000})
    assert "Rp2.000.000" in text and "Rp250.000" in text and "Rp1.250.000" in text
    assert "belum tercatat" in text.casefold()
    empty = cash_envelope_status_text({"envelope_idr": None, "leftover_idr": None,
                                       "recorded_idr": 0, "purchase_count": 0,
                                       "unrecorded_idr": None})
    assert "budget hari ini 2 juta" in empty


# ---- store roundtrip + ingest ------------------------------------------------

def test_ingest_sets_budget_then_leftover(tmp_path):
    client = owner_client(tmp_path)
    store = client.app.state.store
    resto = store.list_restaurants()[0]["id"]
    response = store.whatsapp_ingest(uuid4().hex, {"sender": SENDER, "text": "budget hari ini 2 juta"})
    assert response["result"]["action"] == "envelope_set"
    assert "2.000.000" in response["messages"][-1]["body"]
    response = store.whatsapp_ingest(uuid4().hex, {"sender": SENDER, "text": "uang kembali 500 ribu"})
    assert response["result"]["action"] == "leftover_set"
    status = store.cash_envelope_status(resto)
    assert status["envelope_idr"] == 2_000_000 and status["leftover_idr"] == 500_000


def test_ingest_show_reports_status(tmp_path):
    client = owner_client(tmp_path)
    store = client.app.state.store
    store.whatsapp_ingest(uuid4().hex, {"sender": SENDER, "text": "budget hari ini 2 juta"})
    response = store.whatsapp_ingest(uuid4().hex, {"sender": SENDER, "text": "budget?"})
    assert response["result"]["action"] == "envelope_info"
    assert "Budget: Rp2.000.000" in response["messages"][-1]["body"]


def test_envelope_command_is_never_a_stock_change(tmp_path):
    """A budget sentence must not create drafts, purchases or stock moves."""
    client = owner_client(tmp_path)
    store = client.app.state.store
    resto = store.list_restaurants()[0]["id"]
    store.whatsapp_ingest(uuid4().hex, {"sender": SENDER, "text": "budget hari ini 2 juta"})
    status = store.cash_envelope_status(resto)
    assert status["recorded_idr"] == 0 and status["purchase_count"] == 0


def test_envelope_command_survives_a_waiting_draft(tmp_path):
    client = owner_client(tmp_path)
    store = client.app.state.store
    store.whatsapp_ingest(uuid4().hex, {"sender": SENDER, "text": "bolkus jumbo 10 25000"})
    response = store.whatsapp_ingest(uuid4().hex, {"sender": SENDER, "text": "budget hari ini 2 juta"})
    assert response["result"]["action"] == "envelope_set"
    assert response["thread"]["active_draft_id"]  # the draft is still there


def test_unrecorded_gap_math(tmp_path):
    client = owner_client(tmp_path)
    store = client.app.state.store
    resto = store.list_restaurants()[0]["id"]
    record_purchase(store)
    assert store.recorded_purchase_total(resto)["total_idr"] == 250_000
    store.set_cash_envelope(resto, datetime.now(RESTAURANT_TIMEZONE).date().isoformat(),
                            envelope_idr=2_000_000)
    status = store.cash_envelope_status(resto)
    assert status["unrecorded_idr"] == 1_750_000  # budget − recorded, leftover unknown
    store.set_cash_envelope(resto, status["day"], leftover_idr=500_000)
    status = store.cash_envelope_status(resto)
    assert status["unrecorded_idr"] == 1_250_000  # budget − recorded − leftover


def test_recorded_total_uses_the_wib_day(tmp_path):
    client = owner_client(tmp_path)
    store = client.app.state.store
    resto = store.list_restaurants()[0]["id"]
    record_purchase(store)
    with store.transaction() as conn:
        yesterday = (datetime.now(RESTAURANT_TIMEZONE) - timedelta(days=1)).isoformat()
        conn.execute("UPDATE purchases SET created_at=? WHERE body LIKE '%bolkus%'", (yesterday,))
    assert store.recorded_purchase_total(resto)["total_idr"] == 0
    assert store.recorded_purchase_total(
        resto, yesterday[:10])["total_idr"] == 250_000


def test_cash_envelope_route(tmp_path):
    client = owner_client(tmp_path)
    store = client.app.state.store
    assert client.get("/v1/cash-envelope").json()["data"]["envelope_idr"] is None
    store.whatsapp_ingest(uuid4().hex, {"sender": SENDER, "text": "budget hari ini 2 juta"})
    data = client.get("/v1/cash-envelope").json()["data"]
    assert data["envelope_idr"] == 2_000_000 and data["purchase_count"] == 0


# ---- envelope-aware reminder bodies ------------------------------------------

def test_evening_without_envelope_stays_the_plain_nudge(tmp_path):
    fake = FakeGraphClient(content_json=None)
    client = owner_client(tmp_path, **META, meta_http_client=fake)
    store, resto = set_slot_to_now(client, "evening")
    inbound_hours_ago(store)
    result = client.post("/v1/reminders/check").json()["data"]
    assert result["slot"] == "evening" and result["sent"][0]["ok"] is True
    assert "💰" not in result["body"]


def test_evening_asks_for_the_leftover(tmp_path):
    fake = FakeGraphClient(content_json=None)
    client = owner_client(tmp_path, **META, meta_http_client=fake)
    store, resto = set_slot_to_now(client, "evening")
    store.whatsapp_ingest(uuid4().hex, {"sender": SENDER, "text": "budget hari ini 2 juta"})
    inbound_hours_ago(store)
    result = client.post("/v1/reminders/check").json()["data"]
    assert result["sent"][0]["ok"] is True
    assert "uang kembali" in result["body"] and "Rp2.000.000" in result["body"]


def test_evening_recaps_the_day_when_leftover_is_recorded(tmp_path):
    fake = FakeGraphClient(content_json=None)
    client = owner_client(tmp_path, **META, meta_http_client=fake)
    store, resto = set_slot_to_now(client, "evening")
    record_purchase(store)
    store.whatsapp_ingest(uuid4().hex, {"sender": SENDER, "text": "budget hari ini 2 juta"})
    store.whatsapp_ingest(uuid4().hex, {"sender": SENDER, "text": "uang kembali 500 ribu"})
    inbound_hours_ago(store)
    result = client.post("/v1/reminders/check").json()["data"]
    body = result["body"]
    assert "Rekap kas" in body and "Rp1.250.000" in body  # 2jt − 250rb − 500rb


def test_morning_flags_yesterdays_unrecorded_spend(tmp_path):
    fake = FakeGraphClient(content_json=None)
    client = owner_client(tmp_path, **META, meta_http_client=fake)
    store, resto = set_slot_to_now(client, "morning")
    yesterday = (datetime.now(RESTAURANT_TIMEZONE) - timedelta(days=1)).date().isoformat()
    store.set_cash_envelope(resto, yesterday, envelope_idr=2_000_000, leftover_idr=500_000)
    inbound_hours_ago(store)
    result = client.post("/v1/reminders/check").json()["data"]
    assert result["slot"] == "morning" and result["sent"][0]["ok"] is True
    assert "Kemarin" in result["body"] and "Rp1.500.000" in result["body"]
