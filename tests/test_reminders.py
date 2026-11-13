"""Reminder engine: chat-adjustable schedule, once-a-day nudges, silence-aware.

The Meta test account cannot reach Indonesian numbers, so the pilot runs on
Telegram — but the send path is shared with the proactive sweep and covered
there (test_proactive_templates). Here the deterministic pieces are tested:
the command parse, the per-restaurant schedule, the ingest branch, and the
slot/silence/once-per-day logic of run_reminder_check via the check route.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from conftest import login_owner
from service.api import create_app
from service.db import RESTAURANT_TIMEZONE
from service.reminders import parse_reminder_command, reminder_command_reply
from test_tenant_isolation import META, FakeGraphClient

SENDER = "628123456789"  # the demo restaurant's linked number


def wib(hour, minute=0):
    return datetime(2026, 11, 13, hour, minute, tzinfo=RESTAURANT_TIMEZONE)


def inbound_hours_ago(store, hours=8.0):
    """An inbound message 8h back: inside WhatsApp's 24-hour window (free-form
    send works) yet outside the 6-hour silence window (the nudge may fire)."""
    store.whatsapp_ingest(uuid4().hex, {"sender": SENDER, "text": "berapa sisa cabai?"})
    with store.transaction() as conn:
        conn.execute(
            "UPDATE whatsapp_messages SET created_at=? WHERE direction='inbound'",
            ((datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(),))


def owner_client(tmp_path, **kwargs):
    app = create_app(tmp_path / "reminders.sqlite3", reminder_check_interval_seconds=0, **kwargs)
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    login_owner(client)
    return client


# ---- command parse ---------------------------------------------------------

def test_parse_sets_morning_and_evening_hours():
    assert parse_reminder_command("pengingat pagi jadi jam 8") == {"kind": "set", "slot": "morning", "hour": 8}
    assert parse_reminder_command("pengingat malam jadi jam 9") == {"kind": "set", "slot": "evening", "hour": 21}
    assert parse_reminder_command("pengingat sore jam 6") == {"kind": "set", "slot": "evening", "hour": 18}
    assert parse_reminder_command("pengingat malam jadi jam 21") == {"kind": "set", "slot": "evening", "hour": 21}
    assert parse_reminder_command("pengingat pagi jadi jam 21") == {"kind": "set", "slot": "morning", "hour": 21}


def test_parse_bare_hour_asks_which_slot():
    assert parse_reminder_command("pengingat jadi jam 8") == {"kind": "clarify"}


def test_parse_on_off_and_show():
    assert parse_reminder_command("pengingat mati") == {"kind": "off"}
    assert parse_reminder_command("pengingat berhenti deh") == {"kind": "off"}
    assert parse_reminder_command("pengingat nyala") == {"kind": "on"}
    assert parse_reminder_command("pengingat") == {"kind": "show"}
    assert parse_reminder_command("pengingat jam berapa?") == {"kind": "show"}


def test_parse_rejects_quiet_hours_and_bad_hours():
    assert parse_reminder_command("pengingat malam jadi jam 23") == {"kind": "quiet"}
    assert parse_reminder_command("pengingat pagi jadi jam 3") == {"kind": "quiet"}
    assert parse_reminder_command("pengingat pagi jadi jam 25") == {"kind": "invalid"}


def test_parse_ignores_non_reminder_text():
    assert parse_reminder_command("beli cabai 2 kg 25000") is None
    assert parse_reminder_command("pengingatkan saya beli cabai") is None  # not sentence-initial
    assert parse_reminder_command("") is None
    assert parse_reminder_command(None) is None


def test_replies_are_indonesian_and_name_the_slot():
    settings = {"morning_hour": 8, "evening_hour": 21, "enabled": True,
                "last_morning_date": None, "last_evening_date": None}
    reply = reminder_command_reply({"kind": "set", "slot": "morning", "hour": 8}, settings)
    assert "pagi" in reply and "jam 8" in reply
    quiet = reminder_command_reply({"kind": "quiet"}, settings)
    assert "jam 7" in quiet and "jam 21" in quiet


# ---- schedule roundtrip + ingest -------------------------------------------

def test_settings_default_and_update_roundtrip(tmp_path):
    client = owner_client(tmp_path)
    store = client.app.state.store
    resto = store.list_restaurants()[0]["id"]
    settings = store.reminder_settings(resto)
    assert settings["morning_hour"] == 9 and settings["evening_hour"] == 21 and settings["enabled"] is True

    with store.transaction() as conn:
        reply, result = store.apply_reminder_command(conn, resto, {"kind": "set", "slot": "evening", "hour": 20})
    assert result["action"] == "reminder_updated" and "jam 20" in reply
    assert store.reminder_settings(resto)["evening_hour"] == 20


def test_ingest_sets_the_schedule(tmp_path):
    client = owner_client(tmp_path)
    store = client.app.state.store
    resto = store.list_restaurants()[0]["id"]
    response = store.whatsapp_ingest(uuid4().hex, {"sender": SENDER, "text": "pengingat pagi jadi jam 7"})
    assert response["result"]["action"] == "reminder_updated"
    assert "jam 7" in response["messages"][-1]["body"]
    assert store.reminder_settings(resto)["morning_hour"] == 7


def test_ingest_draft_survives_a_reminder_command(tmp_path):
    """A schedule command while a draft waits leaves the draft untouched."""
    client = owner_client(tmp_path)
    store = client.app.state.store
    store.whatsapp_ingest(uuid4().hex, {"sender": SENDER, "text": "bolkus jumbo 10 25000"})
    response = store.whatsapp_ingest(uuid4().hex, {"sender": SENDER, "text": "pengingat pagi jadi jam 7"})
    assert response["result"]["action"] == "reminder_updated"
    assert response["thread"]["active_draft_id"]  # the draft is still there


# ---- run_reminder_check via the route --------------------------------------

def test_morning_reminder_sends_once_per_day(tmp_path):
    fake = FakeGraphClient(content_json=None)
    client = owner_client(tmp_path, **META, meta_http_client=fake)
    check = lambda: client.post("/v1/reminders/check", json=None).json()["data"]
    # The seeded schedule is the defaults (9/21); dry_run shows what would go.
    dry = client.post("/v1/reminders/check?dry_run=true").json()["data"]
    assert dry["slot"] is None  # 09:00 WIB does not match the test run's real clock
    # Drive the clock directly: schedule the morning slot to now's hour.
    store = client.app.state.store
    resto = store.list_restaurants()[0]["id"]
    hour = datetime.now(RESTAURANT_TIMEZONE).hour or 9
    if not 7 <= hour <= 21:
        hour = 9
    store.set_reminder_settings(resto, morning_hour=hour)
    inbound_hours_ago(store)
    result = check()
    assert result["slot"] == "morning" and result["sent"][0]["ok"] is True
    posts = [call["json"] for call in fake.calls if call["method"] == "POST"]
    assert len(posts) == 1 and "Selamat pagi" in posts[0]["text"]["body"]
    # Second tick inside the same hour must not repeat the day's nudge.
    again = check()
    assert again["slot"] is None and len(fake.calls) == 1


def test_evening_reminder_and_disabled_schedule(tmp_path):
    fake = FakeGraphClient(content_json=None)
    client = owner_client(tmp_path, **META, meta_http_client=fake)
    store = client.app.state.store
    resto = store.list_restaurants()[0]["id"]
    hour = datetime.now(RESTAURANT_TIMEZONE).hour
    if not 7 <= hour <= 21:
        hour = 21
    store.set_reminder_settings(resto, evening_hour=hour)
    inbound_hours_ago(store)
    result = client.post("/v1/reminders/check").json()["data"]
    assert result["slot"] == "evening" and result["sent"][0]["ok"] is True
    assert "belum tercatat" in result["body"]
    # Off means off, even inside the hour.
    store.set_reminder_settings(resto, enabled=False)
    assert client.post("/v1/reminders/check").json()["data"]["slot"] is None


def test_silence_aware_skip_after_recent_interaction(tmp_path):
    fake = FakeGraphClient(content_json=None)
    client = owner_client(tmp_path, **META, meta_http_client=fake)
    store = client.app.state.store
    resto = store.list_restaurants()[0]["id"]
    hour = datetime.now(RESTAURANT_TIMEZONE).hour
    if not 7 <= hour <= 21:
        hour = 9
    store.set_reminder_settings(resto, morning_hour=hour)
    # A fresh inbound message: the owner is at the chat, no nudge needed.
    store.whatsapp_ingest(uuid4().hex, {"sender": SENDER, "text": "berapa sisa cabai?"})
    result = client.post("/v1/reminders/check").json()["data"]
    assert result["skipped"] == "owner_recently_active" and fake.calls == []
    # The skipped day is not spent: the nudge still goes once they go quiet
    # (8h back keeps the WhatsApp window open for the free-form send).
    inbound_hours_ago(store)
    result = client.post("/v1/reminders/check").json()["data"]
    assert result["sent"][0]["ok"] is True


def test_quiet_hours_never_send(tmp_path):
    client = owner_client(tmp_path, **META, meta_http_client=FakeGraphClient(content_json=None))
    store = client.app.state.store
    resto = store.list_restaurants()[0]["id"]
    store.set_reminder_settings(resto, morning_hour=23)  # rejected by chat, forced here
    result = client.post("/v1/reminders/check").json()["data"]
    assert result["slot"] is None  # the engine refuses a schedule row inside the quiet band
