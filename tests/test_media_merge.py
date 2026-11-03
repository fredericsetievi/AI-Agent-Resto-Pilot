"""Multi-photo intake: Telegram media groups and the dashboard batch endpoint."""

import base64
import json
import json as jsonlib
import time
from uuid import uuid4

from fastapi.testclient import TestClient

from conftest import login_admin, login_owner
from service.api import create_app
from service.db import RESTAURANT

TENANT = RESTAURANT["id"]


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 128


BOT_TOKEN = "123456789:AAExampleExampleExampleExampleExam"
SECRET = "group-secret"
CHAT_ID = "987654321"
HEADERS = {"X-Telegram-Bot-Api-Secret-Token": SECRET, "Content-Type": "application/json"}
SEND_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"


class FakeResponse:
    def __init__(self, status_code=200, body=None, content=b""):
        self.status_code = status_code
        self._body = body if body is not None else {"ok": True}
        self.content = content

    def json(self):
        return self._body


class FakeTelegramAPI:
    def __init__(self):
        self.calls = []

    async def post(self, url, *, headers=None, json=None):
        self.calls.append({"url": url, "json": json})
        return FakeResponse()

    async def get(self, url, *, headers=None, params=None):
        self.calls.append({"method": "GET", "url": url, "params": params})
        if "/file/bot" in url:
            return FakeResponse(body={"ok": True}, content=JPEG)  # the raw photo bytes
        return FakeResponse(body={"ok": True, "result": {"file_path": "notas/nota.jpg"}})


class FakeVisionClient:
    """One queued completion per photo, in arrival order."""

    def __init__(self, completions):
        self.completions = list(completions)
        self.calls = 0

    async def post(self, url, *, headers=None, json=None):
        self.calls += 1
        payload = self.completions[min(self.calls - 1, len(self.completions) - 1)]
        return FakeResponse(body={"choices": [{"message": {"content": jsonlib.dumps(payload)}}]})


def photo_update(update_id, group_id, sender=CHAT_ID):
    message = {"message_id": update_id, "chat": {"id": int(sender)}, "date": 1700000000,
               "media_group_id": group_id,
               "photo": [{"file_id": f"file-{update_id}", "file_size": 900}]}
    return {"update_id": update_id, "message": message}


def text_update(update_id, text, sender=CHAT_ID):
    return {"update_id": update_id, "message": {"message_id": update_id, "chat": {"id": int(sender)}, "date": 1700000000, "text": text}}


NOTA_A = {"supplier": "Pasar A", "items": [{"raw_name": "Cabai Merah", "quantity": "2", "unit": "kg", "line_total_idr": 100000}], "unreadable": []}
NOTA_B = {"supplier": None, "items": [{"raw_name": "Bawang Merah", "quantity": "1", "unit": "kg", "line_total_idr": 50000}], "unreadable": []}


def group_client(tmp_path, vision_completions=(NOTA_A, NOTA_B), **kwargs):
    telegram = FakeTelegramAPI()
    vision = FakeVisionClient(list(vision_completions))
    kwargs.setdefault("vision_api_key", "synthetic-key")
    kwargs.setdefault("telegram_bot_token", BOT_TOKEN)
    kwargs.setdefault("telegram_webhook_secret", SECRET)
    kwargs.setdefault("telegram_http_client", telegram)
    kwargs.setdefault("vision_http_client", vision)
    kwargs.setdefault("telegram_group_seconds", 1.0)
    kwargs.setdefault("seed", False)
    app = create_app(tmp_path / "group.sqlite3", **kwargs)
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    login_owner(client)
    admin = TestClient(app, base_url="http://127.0.0.1")
    login_admin(admin)
    admin.post(f"/v1/admin/restaurants/{RESTAURANT['id']}/numbers", headers={"Idempotency-Key": uuid4().hex},
               json={"whatsapp_number": CHAT_ID, "platform": "telegram", "label": "Owner chat"})
    return client, telegram, vision


def post_update(client, update):
    return client.post("/webhooks/telegram", headers=HEADERS, content=json.dumps(update).encode())


def wait_for_draft(client, timeout_seconds=8):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        drafts = [d for d in client.get("/v1/overview").json()["data"]["drafts"] if d["status"] in ("pending_approval", "needs_clarification")]
        if drafts:
            return drafts[0]
        time.sleep(0.1)
    return None


# --- Telegram media groups ------------------------------------------------------


def test_grouped_photos_become_one_merged_draft(tmp_path):
    client, telegram, vision = group_client(tmp_path)
    with client:
        post_update(client, photo_update(101, "group-1"))
        post_update(client, photo_update(102, "group-1"))
        draft = wait_for_draft(client)
    assert draft is not None, "the merged draft never appeared"
    store = client.app.state.store
    drafts = [d for d in store.overview(TENANT)["drafts"] if d["status"] in ("pending_approval", "needs_clarification")]
    assert len(drafts) == 1
    names = [item["ingredient_id"] for item in draft["items"]]
    assert names == ["cabai", "bawang"]
    assert len(vision.completions) == 2 and vision.calls == 2
    assert sum(1 for call in telegram.calls if call.get("url", "").endswith("/sendMessage")) == 1 and [call for call in telegram.calls if call.get("url", "").endswith("/sendMessage")][0]["json"]["chat_id"] == CHAT_ID


def test_replayed_group_updates_never_duplicate(tmp_path):
    client, telegram, _vision = group_client(tmp_path)
    with client:
        post_update(client, photo_update(201, "group-2"))
        post_update(client, photo_update(202, "group-2"))
        wait_for_draft(client)
        # Telegram retries the same updates: the update-id set absorbs them.
        post_update(client, photo_update(201, "group-2"))
        post_update(client, photo_update(202, "group-2"))
        time.sleep(0.6)
        drafts = [d for d in client.get("/v1/overview").json()["data"]["drafts"] if d["status"] in ("pending_approval", "needs_clarification")]
        assert len(drafts) == 1
        assert sum(1 for call in telegram.calls if call.get("url", "").endswith("/sendMessage")) == 1


def test_grouped_photos_without_vision_get_one_safe_reply(tmp_path):
    client, telegram, _vision = group_client(tmp_path, vision_completions=[], vision_api_key=None)
    with client:
        post_update(client, photo_update(301, "group-3"))
        post_update(client, photo_update(302, "group-3"))
        draft = wait_for_draft(client, timeout_seconds=4)
        assert draft is None
        # One reply total — not one per photo.
        assert sum(1 for call in telegram.calls if call.get("url", "").endswith("/sendMessage")) == 1
        assert "belum diproses" in telegram.calls[0]["json"]["text"] or "Foto" in telegram.calls[0]["json"]["text"]


def test_single_photo_keeps_the_immediate_flow(tmp_path):
    client, telegram, _vision = group_client(tmp_path, vision_completions=[NOTA_A])
    with client:
        post_update(client, {"update_id": 401, "message": {"message_id": 401, "chat": {"id": int(CHAT_ID)},
                    "date": 1700000000, "photo": [{"file_id": "solo", "file_size": 900}]}})  # no media group
        draft = wait_for_draft(client, timeout_seconds=4)
        assert draft is not None and [item["ingredient_id"] for item in draft["items"]] == ["cabai"]


# --- Dashboard batch endpoint ------------------------------------------------------


def batch_client(tmp_path):
    telegram = FakeTelegramAPI()
    vision = FakeVisionClient([NOTA_A, NOTA_B])
    app = create_app(tmp_path / "batch.sqlite3", seed=False, vision_api_key="synthetic-key",
                     vision_http_client=vision)
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=True)
    login_owner(client)
    return client, vision


def image_part():
    return {"image_base64": base64.b64encode(JPEG).decode(), "media_type": "image/jpeg"}


def test_batch_intake_merges_photos_into_one_draft(tmp_path):
    client, vision = batch_client(tmp_path)
    response = client.post("/v1/intake/images", headers={"Idempotency-Key": uuid4().hex},
                           json={"images": [image_part(), image_part()]})
    assert response.status_code == 201, response.text
    draft = response.json()["data"]
    assert [item["ingredient_id"] for item in draft["items"]] == ["cabai", "bawang"]
    assert vision.calls == 2
    drafts = [d for d in client.get("/v1/overview").json()["data"]["drafts"] if d["status"] in ("pending_approval", "needs_clarification")]
    assert len(drafts) == 1


def test_batch_intake_caps_at_five_photos(tmp_path):
    client, _vision = batch_client(tmp_path)
    response = client.post("/v1/intake/images", headers={"Idempotency-Key": uuid4().hex},
                           json={"images": [image_part()] * 6})
    assert response.status_code == 422


def test_batch_intake_requires_vision(tmp_path):
    from service.api import create_app as _create

    app = _create(tmp_path / "novision.sqlite3")
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    login_owner(client)
    response = client.post("/v1/intake/images", headers={"Idempotency-Key": uuid4().hex},
                           json={"images": [image_part()]})
    assert response.status_code == 503
