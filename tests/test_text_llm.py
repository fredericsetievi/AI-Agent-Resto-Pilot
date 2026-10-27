"""StepFun text rescue; the provider is a local fake and never called."""

import asyncio
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from service.api import create_app
from service.text_llm import StepFunTextError, clean_extraction, extract_message

MODEL_URL = "https://api.stepfun.com/v1/chat/completions"
FLEXIBLE = "tolong catat saya tadi beli 150 ribu cabai 3kg nih"


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


class FakeTextClient:
    def __init__(self):
        self.calls = []
        self.responses = []

    async def post(self, url, *, headers, json):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.responses.pop(0)


def completion(payload):
    return FakeResponse(body={"choices": [{"message": {"content": json.dumps(payload)}}]})


CABAI_ITEM = {"raw_name": "cabai", "quantity": "3", "unit": "kg", "line_total_idr": 150000}


def run_message(text=FLEXIBLE, client=None, **kwargs):
    return asyncio.run(extract_message(
        text,
        api_key=kwargs.pop("api_key", "synthetic-stepfun-key"),
        client=client,
        **kwargs,
    ))


def rescue_app(tmp_path, fake_client, **kwargs):
    kwargs.setdefault("vision_api_key", "synthetic-stepfun-key")
    app = create_app(
        tmp_path / "demo.sqlite3",
        text_llm_enabled=True,
        text_llm_http_client=fake_client,
        **kwargs,
    )
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    from conftest import login_owner
    login_owner(client)
    return client


def intake(client, text):
    return client.post("/v1/intake", headers={"Idempotency-Key": uuid4().hex}, json={"text": text})


def test_extract_message_maps_a_free_form_sentence():
    client = FakeTextClient()
    client.responses.append(completion({"items": [CABAI_ITEM]}))
    result = run_message(client=client)

    assert result == {"items": [
        {"ingredient_id": "cabai", "raw_name": "cabai", "quantity": "3", "unit": "kg", "line_total_idr": 150000},
    ], "warnings": []}

    call = client.calls[0]
    assert call["url"] == MODEL_URL
    assert call["headers"]["Authorization"] == "Bearer synthetic-stepfun-key"
    assert call["json"]["model"] == "step-3.7-flash"
    assert call["json"]["temperature"] == 0
    assert "Message to read:" in call["json"]["messages"][0]["content"]


def test_extract_message_strips_a_code_fence():
    client = FakeTextClient()
    content = '```json\n{"items": []}\n```'
    client.responses.append(FakeResponse(body={"choices": [{"message": {"content": content}}]}))
    result = run_message(client=client)
    assert result == {"items": [], "warnings": []}


def test_extract_message_rejects_unreadable_json():
    client = FakeTextClient()
    client.responses.append(FakeResponse(body={"choices": [{"message": {"content": "not json"}}]}))
    with pytest.raises(StepFunTextError) as error:
        run_message(client=client)
    assert error.value.status == 502


def test_extract_message_rejects_a_provider_error_without_leaking_the_key():
    client = FakeTextClient()
    client.responses.append(FakeResponse(status_code=401, body={"error": {"message": "bad key"}}))
    with pytest.raises(StepFunTextError) as error:
        run_message(client=client)
    assert error.value.status == 502
    assert "synthetic-stepfun-key" not in str(error.value)
    assert "bad key" not in str(error.value)


def test_extract_message_requires_a_key():
    with pytest.raises(StepFunTextError) as error:
        run_message(client=FakeTextClient(), api_key="")
    assert error.value.status == 503


def test_clean_extraction_keeps_unstated_values_empty():
    result = clean_extraction({"items": [{"raw_name": "cabai", "quantity": None, "unit": None, "line_total_idr": None}]})
    item = result["items"][0]
    assert item["ingredient_id"] == "cabai" and item["raw_name"] == "cabai"
    assert item["quantity"] is None and item["unit"] is None and item["line_total_idr"] is None
    assert len(result["warnings"]) == 3


def test_clean_extraction_keeps_new_ingredients_approvable():
    # Like the rules parser: an unknown name is not a warning. The draft line
    # shows the raw name and approval adds it to the catalog.
    result = clean_extraction({"items": [{"raw_name": "terasi", "quantity": "1", "unit": "kg", "line_total_idr": 49000}]})
    item = result["items"][0]
    assert item["ingredient_id"] is None and item["raw_name"] == "terasi"
    assert result["warnings"] == []


def test_clean_extraction_passes_an_unknown_unit_through_untouched():
    # Like the vision path: an unsupported package is kept as written so the
    # owner picks a catalog pack; the module never invents a conversion.
    result = clean_extraction({"items": [dict(CABAI_ITEM, unit="kardus")]})
    item = result["items"][0]
    assert item["unit"] == "kardus"
    assert result["warnings"] == []


def test_clean_extraction_rejects_more_than_twenty_items():
    items = [{"raw_name": f"item {index}", "quantity": "1", "unit": "kg", "line_total_idr": 1000} for index in range(21)]
    result = clean_extraction({"items": items})
    assert result["items"] == []
    assert "more than 20 items" in result["warnings"][0]


def test_clean_extraction_rejects_a_non_purchase_object():
    with pytest.raises(StepFunTextError):
        clean_extraction(["items"])
    with pytest.raises(StepFunTextError):
        clean_extraction({"lines": []})


def test_intake_rescues_a_free_form_sentence(tmp_path):
    fake = FakeTextClient()
    fake.responses.append(completion({"items": [CABAI_ITEM]}))
    client = rescue_app(tmp_path, fake)

    response = intake(client, FLEXIBLE)
    assert response.status_code == 201, response.text
    draft = response.json()["data"]
    assert draft["status"] == "pending_approval"
    assert draft["items"][0]["ingredient_id"] == "cabai"
    assert draft["items"][0]["quantity"] == "3"
    assert draft["items"][0]["line_total_idr"] == 150000
    assert draft["warnings"] == []
    assert len(fake.calls) == 1


def test_intake_keeps_the_rules_result_for_canonical_text(tmp_path):
    fake = FakeTextClient()
    client = rescue_app(tmp_path, fake)

    draft = intake(client, "beli cabai 3 kg 150rb").json()["data"]
    assert draft["status"] == "pending_approval"
    assert draft["items"][0]["ingredient_id"] == "cabai"
    assert fake.calls == []


def test_intake_falls_back_to_rules_when_the_model_fails(tmp_path):
    fake = FakeTextClient()
    fake.responses.append(FakeResponse(status_code=503, body={"error": {"message": "overloaded"}}))
    client = rescue_app(tmp_path, fake)

    draft = intake(client, FLEXIBLE).json()["data"]
    assert draft["status"] == "needs_clarification"
    assert draft["items"][0]["raw_name"] == FLEXIBLE
    assert any("quantity is missing" in warning for warning in draft["warnings"])


def test_intake_falls_back_when_the_model_returns_no_items(tmp_path):
    fake = FakeTextClient()
    fake.responses.append(completion({"items": []}))
    client = rescue_app(tmp_path, fake)

    draft = intake(client, FLEXIBLE).json()["data"]
    assert draft["status"] == "needs_clarification"
    assert draft["items"][0]["raw_name"] == FLEXIBLE


def test_intake_without_the_flag_never_calls_the_model(tmp_path):
    fake = FakeTextClient()
    app = create_app(tmp_path / "demo.sqlite3", vision_api_key="synthetic-stepfun-key")
    client = TestClient(app, base_url="http://127.0.0.1")
    from conftest import login_owner
    login_owner(client)

    draft = intake(client, FLEXIBLE).json()["data"]
    assert draft["status"] == "needs_clarification"
    assert fake.calls == []


def test_simulate_rescues_free_form_text_but_not_commands(tmp_path):
    fake = FakeTextClient()
    fake.responses.append(completion({"items": [dict(CABAI_ITEM, raw_name="bawang", quantity="2", line_total_idr=80000)]}))
    client = rescue_app(tmp_path, fake)

    def simulate(text):
        return client.post(
            "/v1/whatsapp/simulate",
            headers={"Idempotency-Key": uuid4().hex},
            json={"sender": "628123456789", "text": text},
        ).json()["data"]

    result = simulate("tadi saya beli bawang 2 kilo harganya 80 ribu ya")
    assert result["result"]["action"] == "draft_created"
    assert result["draft"]["items"][0]["ingredient_id"] == "bawang"
    assert len(fake.calls) == 1

    command = simulate("ya")
    assert command["result"]["action"] == "approved"
    assert len(fake.calls) == 1


def test_rescued_new_ingredient_approves_and_creates_catalog_entry(tmp_path):
    fake = FakeTextClient()
    fake.responses.append(completion({"items": [{"raw_name": "terasi", "quantity": "1", "unit": "kg", "line_total_idr": 49000}]}))
    client = rescue_app(tmp_path, fake)

    draft = intake(client, FLEXIBLE).json()["data"]
    assert draft["status"] == "pending_approval"
    assert draft["items"][0]["ingredient_id"] is None
    assert draft["items"][0]["raw_name"] == "terasi"
    assert draft["warnings"] == []

    decision = client.post(
        f"/v1/drafts/{draft['id']}/decision",
        headers={"Idempotency-Key": uuid4().hex},
        json={"expected_version": draft["version"], "decision": "approve", "received": True},
    )
    assert decision.status_code == 200, decision.text
    data = decision.json()["data"]
    assert data["status"] == "recorded"
    assert data["new_ingredients"] == ["Terasi"]

    inventory = client.get("/v1/overview").json()["data"]["inventory"]
    assert any(row["ingredient_id"] == "terasi" and str(row["quantity"]) == "1" for row in inventory)


def test_clean_extraction_marks_spelling_recoveries_for_confirmation():
    result = clean_extraction({"items": [{"raw_name": "bwang putih", "quantity": "2", "unit": "kg", "line_total_idr": 30000}]})
    item = result["items"][0]
    assert item["ingredient_id"] == "bawang_putih"
    assert item["matched_from"] == "bwang putih"
    assert result["warnings"] == []
