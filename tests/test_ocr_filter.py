"""OCR invoice (vision) path: non-kitchen goods from a nota photo get the
same filter treatment as typed text."""

import base64
import json as jsonlib
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from conftest import login_owner
from service.api import create_app


class FakeResponse:
    def __init__(self, body):
        self.status_code = 200
        self._body = body

    def json(self):
        return self._body


class FakeVision:
    """Serves the invoice OCR reply."""

    def __init__(self, invoice):
        self.invoice = invoice
        self.calls = []

    async def post(self, url, *, headers=None, json=None):
        self.calls.append(json)
        return FakeResponse({"choices": [{"message": {"content": jsonlib.dumps(self.invoice)}}]})


class FakeFilter:
    """Serves the kitchen-filter classification reply."""

    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = []

    async def post(self, url, *, headers=None, json=None):
        self.calls.append(json)
        return FakeResponse({"choices": [{"message": {"content": jsonlib.dumps(self.verdict)}}]})


def photo_body():
    return {"image_base64": base64.b64encode(bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"0" * 64).decode(),
            "media_type": "image/jpeg"}


INVOICE = {"supplier": "Toko Campur",
           "items": [
               {"raw_name": "telur", "quantity": "1", "unit": "kg", "line_total_idr": 28000, "confidence": "high"},
               {"raw_name": "pepsodent", "quantity": "1", "unit": "pack", "line_total_idr": 15000, "confidence": "high"},
           ],
           "unreadable": []}


@pytest.fixture
def client(tmp_path):
    fake_vision = FakeVision(INVOICE)
    # The filter payload holds only the catalog-unknown names re-indexed from
    # 0 (telur is catalog-mapped and skips the model), so the model answers in
    # that 0-based payload space: pepsodent is payload item 0. classify_items
    # maps the verdict back to the global draft indices.
    fake_filter = FakeFilter({"kitchen": [], "non_kitchen": [0]})
    app = create_app(tmp_path / "ocr.sqlite3", vision_api_key="synthetic-key",
                     vision_http_client=fake_vision, stock_llm_http_client=fake_filter,
                     kitchen_filter_enabled=True)
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as value:
        login_owner(value)
        yield value, fake_filter


def test_ocr_invoice_flags_non_kitchen_and_blocks_approval(client):
    client, fake_filter = client
    draft = client.post("/v1/intake/image", headers={"Idempotency-Key": uuid4().hex},
                        json=photo_body()).json()["data"]
    assert draft["status"] == "needs_clarification"
    assert [(item["raw_name"], bool(item.get("non_kitchen"))) for item in draft["items"]] == [
        ("telur", False), ("pepsodent", True)]
    assert any("bukan bahan masak" in warning and "pepsodent" in warning for warning in draft["warnings"])
    # The filter model only saw the unmapped pepsodent name.
    assert "pepsodent" in fake_filter.calls[0]["messages"][0]["content"]
    blocked = client.post(f"/v1/drafts/{draft['id']}/decision", headers={"Idempotency-Key": uuid4().hex},
                          json={"expected_version": draft["version"], "decision": "approve", "received": True})
    assert blocked.status_code == 422
