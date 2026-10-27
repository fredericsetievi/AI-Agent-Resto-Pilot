"""Stock answers: deterministic facts from SQLite, optional model phrasing.

The acceptance question "berapa total cabai yang tersisa saat ini?" must be
answered from the inventory ledger. The provider is a local fake and never
called for real; every number asserted here is one the seed wrote.
"""

import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from service.api import create_app
from service.db import Store
from service.stock_llm import StepFunTextError, phrase_stock_answer, validate_stock_reply
from service.whatsapp import is_stock_question, stock_reply


QUESTION = "berapa total cabai yang tersisa saat ini?"
MODEL_URL = "https://api.stepfun.com/v1/chat/completions"


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


class FakeStockClient:
    def __init__(self):
        self.calls = []
        self.responses = []

    async def post(self, url, *, headers, json):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.responses.pop(0)


def completion(content):
    return FakeResponse(body={"choices": [{"message": {"content": content}}]})


# --- Deterministic detection and reply building -----------------------------


def test_is_stock_question_accepts_questions_and_rejects_purchases():
    assert is_stock_question(QUESTION)
    assert is_stock_question("Stok bawang putih masih ada?")
    assert is_stock_question("sisa telur berapa?")
    assert is_stock_question("stok apa saja yang tersisa?")
    assert not is_stock_question("beli cabai 3 kg 150rb")
    assert not is_stock_question("tolong catat beli beras 2 kg")
    assert not is_stock_question("ya")
    assert not is_stock_question("halo")
    assert not is_stock_question("")
    assert not is_stock_question(None)


def test_stock_reply_only_prints_database_facts():
    reply = stock_reply([
        {"ingredient_id": "cabai", "name": "Cabai merah", "quantity": "3", "unit": "kg", "reorder_threshold": "5", "low_stock": True},
        {"ingredient_id": "telur", "name": "Telur ayam", "quantity": "10", "unit": "kg", "reorder_threshold": "5", "low_stock": False},
    ])
    assert reply == (
        "Stok saat ini:\n"
        "• Cabai merah — 3 kg (di bawah ambang pesan ulang 5 kg)\n"
        "• Telur ayam — 10 kg"
    )


# --- Store facts ------------------------------------------------------------


def facts_for(store, text):
    result = store.stock_facts(text)
    return {fact["ingredient_id"]: fact for fact in result["facts"]} if result else None


def test_stock_facts_answers_the_acceptance_question_from_the_seed(tmp_path):
    store = Store(tmp_path / "stock.sqlite3")
    result = store.stock_facts(QUESTION)
    assert result is not None and result["question"] == QUESTION
    assert len(result["facts"]) == 1
    cabai = result["facts"][0]
    assert cabai["ingredient_id"] == "cabai" and cabai["name"] == "Cabai merah"
    assert cabai["quantity"] == "3" and cabai["unit"] == "kg"
    assert cabai["low_stock"] is True  # 3 kg is below the 5 kg reorder threshold


def test_stock_facts_rejects_non_questions_and_purchase_notes(tmp_path):
    store = Store(tmp_path / "stock.sqlite3")
    assert store.stock_facts("beli cabai 3 kg 150rb") is None
    assert store.stock_facts("halo siang") is None
    assert store.stock_facts("") is None


def test_stock_facts_named_ingredient_beats_the_whole_catalog(tmp_path):
    store = Store(tmp_path / "stock.sqlite3")
    named = facts_for(store, "berapa stok bawang putih?")
    assert set(named) == {"bawang_putih"}
    general = facts_for(store, "stok apa saja yang tersisa?")
    assert set(general) >= {"cabai", "telur", "beras", "minyak", "bawang"}


def test_stock_facts_covers_aliases_and_multiple_ingredients(tmp_path):
    store = Store(tmp_path / "stock.sqlite3")
    assert set(facts_for(store, "sisa cabe masih berapa?")) == {"cabai"}
    assert set(facts_for(store, "berapa stok cabai dan telur?")) == {"cabai", "telur"}


# --- Anti-hallucination validation ------------------------------------------


CABAI_FACT = {"ingredient_id": "cabai", "name": "Cabai merah", "quantity": "3", "unit": "kg", "reorder_threshold": "5", "low_stock": True}


def test_validate_stock_reply_accepts_exact_numbers():
    reply = "Stok Cabai merah sekarang 3 kg, sudah di bawah ambang pesan ulang 5 kg."
    assert validate_stock_reply(reply, [CABAI_FACT]) == reply


def test_validate_stock_reply_rejects_a_fabricated_or_missing_quantity():
    for reply in (
        "Stok Cabai merah sekarang 30 kg.",  # invented digit
        "Stok Cabai merah masih banyak.",  # no number
        "Stok cabai sekarang 3 kg.",  # display name missing
        "Stok Cabai merah sekarang 3.",  # unit missing
    ):
        with pytest.raises(StepFunTextError):
            validate_stock_reply(reply, [CABAI_FACT])


def test_validate_stock_reply_rejects_empty_and_oversized_replies():
    for reply in ("", "   ", "x" * 5000):
        with pytest.raises(StepFunTextError):
            validate_stock_reply(reply, [CABAI_FACT])


def test_phrase_stock_answer_posts_facts_and_checks_the_reply():
    client = FakeStockClient()
    client.responses.append(completion("Stok Cabai merah saat ini 3 kg ya, di bawah ambang 5 kg."))
    reply = asyncio.run(phrase_stock_answer(
        QUESTION, [CABAI_FACT],
        api_key="synthetic-stepfun-key",
        client=client,
    ))
    assert "3 kg" in reply
    call = client.calls[0]
    assert call["url"] == MODEL_URL
    assert call["headers"]["Authorization"] == "Bearer synthetic-stepfun-key"
    sent = call["json"]["messages"][0]["content"]
    assert QUESTION in sent and '"quantity": "3"' in sent


def test_phrase_stock_answer_requires_a_key():
    with pytest.raises(StepFunTextError) as error:
        asyncio.run(phrase_stock_answer(QUESTION, [CABAI_FACT], api_key=" ", client=FakeStockClient()))
    assert error.value.status == 503


# --- End to end through the simulator ---------------------------------------


def simulate(client, text, *, sender="demo-owner", key=None):
    return client.post("/v1/whatsapp/simulate", headers={"Idempotency-Key": key or uuid4().hex}, json={"sender": sender, "text": text})


def data(response):
    assert response.status_code == 200, response.text
    return response.json()["data"]


def overview(client):
    return client.get("/v1/overview").json()["data"]


def test_simulator_answers_the_acceptance_question_without_a_model(tmp_path):
    client = TestClient(create_app(tmp_path / "demo.sqlite3"), base_url="http://127.0.0.1")
    before = overview(client)
    result = data(simulate(client, QUESTION))
    assert result["result"]["action"] == "stock_answer"
    assert "draft" not in result
    body = result["messages"][-1]["body"]
    assert "Cabai merah — 3 kg" in body and "5 kg" in body
    assert result["messages"][0]["body"] == QUESTION
    # Read-only: no purchase, no draft, and the ledger is untouched.
    after = overview(client)
    assert after["purchases"] == before["purchases"]
    assert after["drafts"] == before["drafts"]
    assert next(row["quantity"] for row in after["inventory"] if row["ingredient_id"] == "cabai") == "3"


def test_stock_question_with_an_active_draft_answers_and_keeps_the_draft(tmp_path):
    client = TestClient(create_app(tmp_path / "demo.sqlite3"), base_url="http://127.0.0.1")
    draft = data(simulate(client, "beli telur 2 kg 60rb"))["draft"]
    result = data(simulate(client, QUESTION))
    assert result["result"]["action"] == "stock_answer"
    assert "Cabai merah — 3 kg" in result["messages"][-1]["body"]
    assert result["draft"]["id"] == draft["id"] and result["draft"]["status"] == "pending_approval"
    assert len(overview(client)["purchases"]) == 2  # the seed purchases only


def test_purchase_wording_is_never_mistaken_for_a_stock_question(tmp_path):
    client = TestClient(create_app(tmp_path / "demo.sqlite3"), base_url="http://127.0.0.1")
    result = data(simulate(client, "beli cabai 3 kg 150rb"))
    assert result["result"]["action"] == "draft_created"
    assert result["draft"]["status"] == "pending_approval"


def test_custom_ingredient_becomes_askable_after_approval(tmp_path):
    client = TestClient(create_app(tmp_path / "demo.sqlite3"), base_url="http://127.0.0.1")
    draft = data(simulate(client, "beli sambal 2 kg 20rb"))["draft"]
    response = client.post(f"/v1/drafts/{draft['id']}/decision", headers={"Idempotency-Key": uuid4().hex}, json={
        "expected_version": draft["version"], "decision": "approve", "received": True,
    })
    assert response.status_code == 200, response.text
    result = data(simulate(client, "berapa stok sambal yang tersisa?"))
    assert result["result"]["action"] == "stock_answer"
    # The approved purchase posted 2 kg, so the ledger says 2, not 0.
    assert "Sambal — 2 kg" in result["messages"][-1]["body"]


# --- Optional model phrasing, numbers still from SQLite ----------------------


def test_model_phrasing_is_used_only_when_it_repeats_the_database_numbers(tmp_path):
    fake = FakeStockClient()
    fake.responses.append(completion("Stok Cabai merah sekarang tinggal 3 kg ya, di bawah ambang pesan ulang 5 kg."))
    app = create_app(
        tmp_path / "demo.sqlite3",
        vision_api_key="synthetic-stepfun-key",
        stock_llm_enabled=True,
        stock_llm_http_client=fake,
    )
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    result = data(simulate(client, QUESTION))
    assert result["result"]["action"] == "stock_answer"
    assert result["messages"][-1]["body"].startswith("Stok Cabai merah sekarang tinggal 3 kg")
    assert len(fake.calls) == 1


def test_a_fabricated_model_number_falls_back_to_the_deterministic_reply(tmp_path):
    fake = FakeStockClient()
    fake.responses.append(completion("Stok Cabai merah masih 30 kg, aman!"))
    app = create_app(
        tmp_path / "demo.sqlite3",
        vision_api_key="synthetic-stepfun-key",
        stock_llm_enabled=True,
        stock_llm_http_client=fake,
    )
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    result = data(simulate(client, QUESTION))
    assert result["result"]["action"] == "stock_answer"
    assert "Cabai merah — 3 kg" in result["messages"][-1]["body"]
    assert "30 kg" not in result["messages"][-1]["body"]


def test_a_model_error_falls_back_to_the_deterministic_reply(tmp_path):
    fake = FakeStockClient()
    fake.responses.append(FakeResponse(status_code=500, body={"error": {"message": "boom"}}))
    app = create_app(
        tmp_path / "demo.sqlite3",
        vision_api_key="synthetic-stepfun-key",
        stock_llm_enabled=True,
        stock_llm_http_client=fake,
    )
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    result = data(simulate(client, QUESTION))
    assert result["result"]["action"] == "stock_answer"
    assert "Cabai merah — 3 kg" in result["messages"][-1]["body"]
