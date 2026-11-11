"""Supplier agent fase 2b: the price-inquiry cycle over Telegram.

The owner asks once ("tanya harga cabai ke supplier"); the agent messages
every supplier with a Telegram contact, records the inquiry, and the
suppliers' replies are parsed into price quotes — never purchases, never
approvals. Suppliers have no authority: their replies only feed the
price ledger, and owner numbers are never treated as supplier contacts.
"""
import json
from uuid import uuid4

from fastapi.testclient import TestClient

from service.api import create_app
from conftest import login_admin

from service.suppliers import _MONEY_IDR


def _MONEY(line):
    return _MONEY_IDR(line)


def _money_idr(line):
    from service.suppliers import _money_idr as _fn
    match = __import__('re').search(r'(?i)(?:rp\.?\s*)?(\d{1,9}(?:[.,]\d{3})*(?:[.,]\d{1,2})?)\s*(rb|ribu|jt|juta|k)?', str(line or ''))
    return _fn(match.group(0)) if match else None
from service.db import RESTAURANT, Store
from service.suppliers import parse_supplier_command, supplier_inquiry_send

TENANT = RESTAURANT["id"]
BOT_TOKEN = "123456789:AAExampleExampleExampleExampleExam"
SECRET = "inquiry-secret"
OWNER_CHAT = "628888000111"   # the owner's Telegram chat (linked)
SUPPLIER_CHAT = "628777000666"  # the supplier's Telegram contact
HEADERS = {"X-Telegram-Bot-Api-Secret-Token": SECRET, "Content-Type": "application/json"}


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {"ok": True}

    def json(self):
        return self._body


class FakeTelegramAPI:
    """Records every Bot API call; sendVoice/sendText/sendContact all land here."""

    def __init__(self):
        self.calls = []

    async def post(self, url, *, headers=None, json=None, data=None, files=None):
        self.calls.append({"url": url, "json": json, "data": data, "files": files})
        return FakeResponse()

    async def get(self, url, *, headers=None, params=None):
        self.calls.append({"method": "GET", "url": url})
        return FakeResponse(body={"ok": True, "result": {"file_path": "x.jpg"}})


def client_with(tmp_path, *, telegram=None, **kwargs):
    app = create_app(tmp_path / "inquiry-app.sqlite3", seed=False,
                     telegram_bot_token=BOT_TOKEN, telegram_webhook_secret=SECRET,
                     telegram_http_client=telegram or FakeTelegramAPI(), telegram_group_seconds=0, **kwargs)
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=True)
    # Webhook access follows the public-demo rule; loopback needs PUBLIC_DEMO off ✓.
    admin = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    login_admin(admin)
    rid = RESTAURANT["id"]
    store = client.app.state.store
    admin.post(f"/v1/admin/restaurants/{rid}/numbers", headers={"Idempotency-Key": uuid4().hex},
               json={"whatsapp_number": OWNER_CHAT, "platform": "telegram", "label": "Owner chat"})
    store.supplier_add(uuid4().hex, {"name": "Pak Budi", "contact_phone": SUPPLIER_CHAT,
                                     "contact_platform": "telegram", "notes": "ayam"}, rid)
    return client, store, rid


def post_update(client, update):
    return client.post("/webhooks/telegram", headers=HEADERS, content=json.dumps(update).encode())


def update(text, *, update_id=700, sender=SUPPLIER_CHAT):
    return {"update_id": update_id, "message": {"message_id": update_id, "chat": {"id": int(sender)},
                                                "date": 1700000000, "text": text}}


def test_inquiry_reaches_telegram_suppliers_and_ack_lands(tmp_path):
    telegram = FakeTelegramAPI()
    client, store, rid = client_with(tmp_path, telegram=telegram)
    response = post_update(client, update("tanya harga cabai ke supplier", update_id=701, sender=OWNER_CHAT))
    assert response.status_code == 200, response.text
    inquiry_calls = [c for c in telegram.calls if "sendmessage" in c["url"].casefold()]
    to_supplier = [c for c in inquiry_calls if c["json"]["chat_id"] == SUPPLIER_CHAT]
    assert to_supplier and "harga cabai" in to_supplier[0]["json"]["text"]
    inquiries = store.supplier_inquiry_open(rid)
    assert len(inquiries) == 1 and "cabai" in inquiries[0]["request"]


def test_supplier_reply_stores_quotes_never_purchases(tmp_path):
    telegram = FakeTelegramAPI()
    client, store, rid = client_with(tmp_path, telegram=telegram)
    post_update(client, update("tanya harga cabai ke supplier", update_id=710, sender=OWNER_CHAT))
    reply_update = update("cabai hari ini 44rb per kg ya", update_id=711)
    assert post_update(client, reply_update).status_code == 200
    quotes = store.supplier_quotes_recent(rid, "cabai")
    assert quotes and quotes[0]["unit_price_idr"] == 44000, quotes
    # A supplier reply never creates purchases, drafts or approvals.
    with store.transaction() as conn:
        purchases = conn.execute("SELECT COUNT(*) FROM purchases WHERE restaurant_id=?", (rid,)).fetchone()[0]
        drafts = conn.execute("SELECT COUNT(*) FROM drafts WHERE restaurant_id=?", (rid,)).fetchone()[0]
    assert purchases == 0 and drafts == 0


def test_supplier_reply_with_unreadable_text_is_reported(tmp_path):
    telegram = FakeTelegramAPI()
    client, store, rid = client_with(tmp_path, telegram=telegram)
    post_update(client, update("tanya harga cabai ke supplier", update_id=720))
    post_update(client, update("sori lupa cek", update_id=721))
    quotes = store.supplier_quotes_recent(rid, "ayam")
    assert quotes == []  # nothing invented from an unreadable reply


def test_owner_numbers_are_never_supplier_contacts(tmp_path):
    store = Store(tmp_path / "disjoint.sqlite3")
    created = store.create_restaurant(uuid4().hex, {
        "name": "W", "owner_username": "u" + uuid4().hex[:6],
        "owner_password": "rahasia-123", "whatsapp_number": OWNER_CHAT,
    })
    try:
        store.supplier_add(uuid4().hex, {"name": "Pemilik", "contact_phone": OWNER_CHAT}, created["id"])
        raise AssertionError("owner-linked phone must not become a supplier contact")
    except Exception as exc:
        assert getattr(exc, "status", None) == 409 and "pemilik" in str(exc)


def test_whatsapp_supplier_gets_template_and_telegram_gets_text(tmp_path):
    import asyncio
    from service.suppliers import supplier_inquiry_send

    class StubStore:
        def supplier_inquiry_create(self, conn, restaurant_id, request, detail=None):
            return "inq-1", 2

        def supplier_inquiry_targets(self, restaurant_id):
            return [
                {"id": "s-tg", "name": "Pasar Baru", "contact_phone": "628111000222",
                 "contact_platform": "telegram"},
                {"id": "s-wa", "name": "Pak Budi", "contact_phone": "628222000333",
                 "contact_platform": "whatsapp"},
            ]

    store_stub = StubStore()
    tg_calls, wa_calls = [], []

    async def tg_fn(name, phone, text):
        tg_calls.append((name, phone, text))

    async def wa_fn(name, phone, text):
        wa_calls.append((name, phone, text))

    command = parse_supplier_command("tanya harga cabai ke supplier")
    asyncio.run(supplier_inquiry_send(store_stub, tg_fn, wa_fn, "demo-rid", command))
    assert len(tg_calls) == 1 and "Pasar Baru" in tg_calls[0][0]
    assert len(wa_calls) == 1 and "Pak Budi" in wa_calls[0][0]
