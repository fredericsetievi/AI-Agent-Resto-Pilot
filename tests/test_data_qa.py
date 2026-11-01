"""Data questions beyond stock: spending totals and last prices, ledger-locked."""

import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from conftest import login_owner
from service.api import create_app
from service.data_qa import (
    StepFunTextError,
    build_answer,
    format_idr,
    is_price_question,
    is_spend_question,
    validate_data_reply,
)
from service.db import RESTAURANT, Store
from service.extraction import extract_purchase


TENANT = RESTAURANT["id"]
SPEND_QUESTION = "berapa pengeluaran minggu ini?"
PRICE_QUESTION = "harga telur terakhir berapa?"


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {"ok": True}

    def json(self):
        return self._body


class FakeLLM:
    def __init__(self, content):
        self.content = content
        self.calls = []

    async def post(self, url, *, headers=None, json=None):
        self.calls.append({"url": url, "json": json})
        return FakeResponse(200, {"choices": [{"message": {"content": self.content}}]})


# --- Detection ----------------------------------------------------------------


def test_detection_splits_spend_price_and_purchases():
    assert is_spend_question(SPEND_QUESTION)
    assert is_spend_question("belanja minggu ini habis berapa?")
    assert not is_spend_question("beli cabai 3 kg 150rb")  # a purchase, not a question
    assert not is_spend_question("berapa stok cabai?")
    assert is_price_question("harga telur terakhir berapa?")
    assert is_price_question("cabai naik berapa persen dari minggu lalu?")
    assert not is_price_question("tolong catat harga cabai 50rb")  # purchase verb guard
    assert not is_price_question("berapa stok cabai?")
    assert not is_spend_question("halo siang")


# --- Facts from the seed ledger -------------------------------------------------


def seeded_store(tmp_path):
    return Store(tmp_path / "qa.sqlite3")


def test_spend_fact_sums_the_seed_purchases(tmp_path):
    import re

    store = seeded_store(tmp_path)
    result = store.data_facts(SPEND_QUESTION, TENANT)
    assert result and result["facts"]
    fact = result["facts"][0]
    assert fact["total_idr"] == 559000 and fact["purchase_count"] == 2  # cabai 129rb + telur 430rb
    assert any(re.search(pattern, "Rp559.000") for pattern in fact["_require"])


def test_spend_question_with_no_purchases_answers_empty(tmp_path):
    store = seeded_store(tmp_path)
    result = store.data_facts(SPEND_QUESTION, "empty-resto")
    assert result == {"question": SPEND_QUESTION, "facts": [],
                      "empty_text": "Belum ada pembelian tercatat dalam 7 hari terakhir."}


def test_price_fact_reads_last_and_previous_prices(tmp_path):
    store = seeded_store(tmp_path)
    # Seed: cabai 3 kg @43.000. Add a second cabai purchase @50.000 via the normal flow.
    draft = store.intake(uuid4().hex, {"text": "beli cabai 2 kg 100rb"}, extract_purchase("beli cabai 2 kg 100rb"), TENANT)
    store.decision(draft["id"], uuid4().hex, {"expected_version": draft["version"], "decision": "approve", "received": True}, TENANT)

    result = store.data_facts("cabai sekarang naik berapa persen?", TENANT)
    fact = result["facts"][0]
    assert fact["name"] == "Cabai merah"
    assert fact["last_price_idr"] == 50000 and fact["previous_price_idr"] == 43000
    assert fact["change_percent"] == "16.3"
    assert any("16,3" in pattern for pattern in fact["_require"])
    # Telur has a single purchase: a price but no percentage to claim.
    telur = store.data_facts(PRICE_QUESTION, TENANT)["facts"][0]
    assert telur["last_price_idr"] == 43000 and "previous_price_idr" not in telur


def test_questions_without_a_match_stay_none(tmp_path):
    store = seeded_store(tmp_path)
    assert store.data_facts(SPEND_QUESTION, None) is None
    assert store.data_facts("harga bahan yang belum pernah dibeli naik?", TENANT) is None  # no catalog match
    assert store.data_facts("berapa stok cabai?", TENANT) is None  # stock question, not data


# --- Deterministic answer text ---------------------------------------------------


def test_build_answer_spend_and_price():
    spend = build_answer([{"kind": "spend", "days": 7, "total_idr": 559000, "purchase_count": 2,
                           "top_item": {"name": "Telur ayam", "quantity": "10", "unit": "kg"}}])
    assert spend == "Pengeluaran 7 hari terakhir: Rp559.000 dari 2 pembelian. Terbanyak: Telur ayam 10 kg."
    price = build_answer([{"kind": "price", "name": "Cabai merah", "unit": "kg", "last_price_idr": 50000,
                           "previous_price_idr": 43000, "change_percent": "16.3"}])
    assert price == "Harga terakhir Cabai merah: Rp50.000/kg (naik 16,3% dari Rp43.000)"


# --- Anti-hallucination validation -----------------------------------------------


def test_validate_data_reply_two_way_figure_lock():
    facts = [{"kind": "spend", "total_idr": 559000, "purchase_count": 2,
              "_require": [r"Rp\s?559\.000", r"Telur\s?ayam"]}]
    assert validate_data_reply("Pengeluaranmu Rp559.000, terbanyak Telur ayam.", facts)
    for bad in (
        "Pengeluaranmu Rp559.000.",  # missing the top item name
        "Pengeluaranmu Rp559.000, naik dari Rp100.000 kemarin.",  # invented figure
        "Pengeluaranmu Rp559.00, terbanyak Telur ayam.",  # corrupted amount
    ):
        with pytest.raises(StepFunTextError):
            validate_data_reply(bad, facts)


# --- End to end through the simulator ---------------------------------------------


def qa_client(tmp_path, llm_content=None):
    kwargs = {}
    fake = None
    if llm_content is not None:
        fake = FakeLLM(llm_content)
        kwargs.update(vision_api_key="synthetic-key", stock_llm_enabled=True, stock_llm_http_client=fake)
    app = create_app(tmp_path / "demo.sqlite3", **kwargs)
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    login_owner(client)
    return client, fake


def simulate(client, text, key=None):
    return client.post("/v1/whatsapp/simulate", headers={"Idempotency-Key": key or uuid4().hex}, json={"sender": "628123456789", "text": text})


def test_simulator_answers_spending_from_the_ledger(tmp_path):
    client, _fake = qa_client(tmp_path)
    result = client.app.state.store
    response = simulate(client, SPEND_QUESTION)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["result"]["action"] == "stock_answer"
    body = data["messages"][-1]["body"]
    assert "Rp559.000" in body and "2 pembelian" in body


def test_simulator_answers_last_price_and_price_move(tmp_path):
    client, _fake = qa_client(tmp_path)
    # A second cabai purchase gives the price history to compare against.
    draft = simulate(client, "beli cabai 2 kg 100rb").json()["data"]["draft"]
    decision = client.post(f"/v1/drafts/{draft['id']}/decision", headers={"Idempotency-Key": uuid4().hex},
                           json={"expected_version": draft["version"], "decision": "approve", "received": True})
    assert decision.status_code == 200, decision.text

    answer = simulate(client, "harga cabai terakhir berapa?").json()["data"]["messages"][-1]["body"]
    assert "Rp50.000/kg" in answer and "naik 16,3% dari Rp43.000" in answer

    move = simulate(client, "cabai naik berapa persen?").json()["data"]["messages"][-1]["body"]
    assert "16,3%" in move


def test_llm_rephrase_keeps_or_drops_figures(tmp_path):
    good, _ = qa_client(tmp_path, llm_content="Pengeluaran 7 hari terakhir ini Rp559.000 dari 2 pembelian, terbanyak Telur ayam ya.")
    result = simulate(good, SPEND_QUESTION).json()["data"]["messages"][-1]["body"]
    assert result.startswith("Pengeluaran 7 hari terakhir ini")

    bad, fake = qa_client(tmp_path, llm_content="Pengeluaran minggu ini Rp999.000.")
    result = simulate(bad, SPEND_QUESTION).json()["data"]["messages"][-1]["body"]
    assert "Rp999.000" not in result and "Rp559.000" in result  # deterministic fallback
    assert len(fake.calls) == 1


def test_format_idr_indonesian_style():
    assert format_idr(559000) == "559.000"
    assert format_idr(1234567) == "1.234.567"
