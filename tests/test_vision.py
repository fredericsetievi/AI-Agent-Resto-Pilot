"""StepFun vision extraction; the provider is a local fake and never called."""

import asyncio
import base64
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from service.api import create_app
from service.vision import StepFunVisionError, extract_invoice

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 128
MODEL_URL = "https://api.stepfun.com/v1/chat/completions"


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


class FakeVisionClient:
    def __init__(self):
        self.calls = []
        self.responses = []

    async def post(self, url, *, headers, json):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.responses.pop(0)


def completion(payload):
    return FakeResponse(body={"choices": [{"message": {"content": json.dumps(payload)}}]})


def run(image=JPEG, media_type="image/jpeg", client=None, **kwargs):
    return asyncio.run(extract_invoice(
        image,
        media_type=media_type,
        api_key=kwargs.pop("api_key", "synthetic-stepfun-key"),
        client=client,
        **kwargs,
    ))


def test_extract_invoice_maps_printed_lines():
    client = FakeVisionClient()
    client.responses.append(completion({
        "supplier": "Petik Segar",
        "items": [
            {"raw_name": "Cabai Merah", "quantity": "3", "unit": "kg", "line_total_idr": 150000, "confidence": "high"},
            {"raw_name": "kecombrang", "quantity": 0.5, "unit": "kg", "line_total_idr": 31000},
        ],
        "unreadable": [],
    }))
    result = run(client=client)

    assert result["supplier"] == "Petik Segar"
    assert result["items"] == [
        {"ingredient_id": "cabai", "raw_name": "cabai merah", "quantity": "3", "unit": "kg", "line_total_idr": 150000},
        {"ingredient_id": None, "raw_name": "kecombrang", "quantity": "0.5", "unit": "kg", "line_total_idr": 31000},
    ]
    assert result["warnings"] == ["Line 2: unknown ingredient 'kecombrang'; select a catalog item manually."]

    call = client.calls[0]
    assert call["url"] == MODEL_URL
    assert call["headers"]["Authorization"] == "Bearer synthetic-stepfun-key"
    assert call["json"]["model"] == "step-1v"
    assert call["json"]["temperature"] == 0
    image_part = call["json"]["messages"][0]["content"][1]
    assert image_part["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_extract_invoice_leaves_unreadable_values_empty():
    client = FakeVisionClient()
    client.responses.append(completion({
        "supplier": None,
        "items": [{"raw_name": "sereh", "quantity": None, "unit": None, "line_total_idr": None, "confidence": "low"}],
        "unreadable": ["fold over the price column"],
    }))
    result = run(client=client)

    item = result["items"][0]
    assert item["quantity"] is None and item["unit"] is None and item["line_total_idr"] is None
    assert len(result["warnings"]) == 6
    assert any("fold over the price column" in text for text in result["warnings"])
    assert any("verify this line" in text for text in result["warnings"])


def test_extract_invoice_strips_a_code_fence():
    client = FakeVisionClient()
    client.responses.append(FakeResponse(body={"choices": [{"message": {"content": '```json\n{"items": [], "supplier": null}\n```'}}]}))
    result = run(client=client)
    assert result == {"supplier": None, "items": [], "warnings": []}


def test_extract_invoice_rejects_more_than_twenty_lines():
    client = FakeVisionClient()
    client.responses.append(completion({
        "items": [{"raw_name": f"item {index}", "quantity": "1", "unit": "kg", "line_total_idr": 1000} for index in range(21)],
    }))
    result = run(client=client)
    assert result["items"] == []
    assert "more than 20 lines" in result["warnings"][0]


def test_extract_invoice_rejects_a_provider_error_without_leaking_the_key():
    client = FakeVisionClient()
    client.responses.append(FakeResponse(status_code=401, body={"error": {"message": "bad key"}}))
    with pytest.raises(StepFunVisionError) as error:
        run(client=client)
    assert error.value.status == 502
    assert "synthetic-stepfun-key" not in str(error.value)
    assert "bad key" not in str(error.value)


def test_extract_invoice_rejects_unreadable_json():
    client = FakeVisionClient()
    client.responses.append(FakeResponse(body={"choices": [{"message": {"content": "not json"}}]}))
    with pytest.raises(StepFunVisionError) as error:
        run(client=client)
    assert error.value.status == 502


def test_extract_invoice_requires_a_key():
    with pytest.raises(StepFunVisionError) as error:
        run(client=FakeVisionClient(), api_key="")
    assert error.value.status == 503


def test_extract_invoice_rejects_a_photo_that_is_not_an_image():
    client = FakeVisionClient()
    with pytest.raises(StepFunVisionError) as error:
        run(image=b"%PDF-1.7 not a photo", client=client)
    assert error.value.status == 415
    assert client.calls == []


def image_body(**overrides):
    body = {"image_base64": base64.b64encode(JPEG).decode("ascii"), "media_type": "image/jpeg"}
    body.update(overrides)
    return body


@pytest.fixture
def provider():
    return FakeVisionClient()


@pytest.fixture
def client(provider, tmp_path):
    app = create_app(tmp_path / "vision.sqlite3", vision_api_key="synthetic-stepfun-key", vision_http_client=provider)
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as value:
        yield value


def headers():
    return {"Idempotency-Key": uuid4().hex}


def test_image_intake_is_disabled_without_a_key(tmp_path):
    app = create_app(tmp_path / "demo.sqlite3")
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as value:
        response = value.post("/v1/intake/image", headers=headers(), json=image_body())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "vision_not_configured"


def test_image_intake_creates_a_draft_for_owner_approval(client, provider):
    provider.responses.append(completion({
        "supplier": "Petik Segar",
        "items": [{"raw_name": "cabai", "quantity": "3", "unit": "kg", "line_total_idr": 150000, "confidence": "high"}],
    }))
    response = client.post("/v1/intake/image", headers=headers(), json=image_body())
    assert response.status_code == 201, response.text

    draft = response.json()["data"]
    assert draft["status"] == "pending_approval"
    assert draft["supplier"] == "Petik Segar"
    assert draft["source_text"] == "Foto nota"
    assert draft["items"][0]["ingredient_id"] == "cabai"
    assert draft["items"][0]["canonical_quantity"] == "3"


def test_image_intake_routes_an_unreadable_photo_to_clarification(client, provider):
    provider.responses.append(completion({
        "supplier": "Petik Segar",
        "items": [{"raw_name": "kacang panjang", "quantity": None, "unit": None, "line_total_idr": None}],
    }))
    response = client.post("/v1/intake/image", headers=headers(), json=image_body())
    assert response.status_code == 201, response.text
    draft = response.json()["data"]
    assert draft["status"] == "needs_clarification"
    assert draft["items"][0]["line_total_idr"] is None


def test_image_intake_rejects_bad_base64(client, provider):
    response = client.post("/v1/intake/image", headers=headers(), json=image_body(image_base64="not base64!"))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "vision_failed"
    assert provider.calls == []


def test_image_intake_rejects_a_photo_that_lies_about_its_type(client, provider):
    response = client.post("/v1/intake/image", headers=headers(), json=image_body(media_type="image/png"))
    assert response.status_code == 415


def test_image_intake_deduplicates_the_same_photo_key(client, provider):
    reply = completion({
        "supplier": "Petik Segar",
        "items": [{"raw_name": "cabai", "quantity": "3", "unit": "kg", "line_total_idr": 150000}],
    })
    provider.responses.extend([reply, reply])
    key = headers()
    first = client.post("/v1/intake/image", headers=key, json=image_body())
    second = client.post("/v1/intake/image", headers=key, json=image_body())
    assert first.status_code == second.status_code == 201
    assert first.json()["data"]["id"] == second.json()["data"]["id"]
