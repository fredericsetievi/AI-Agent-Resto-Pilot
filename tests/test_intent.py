"""Bounded intent routing: chatty commands, exact commands, and the guards."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from conftest import login_owner
from service.api import create_app
from service.db import APPROVE_COMMANDS, REJECT_COMMANDS
from service.intent_llm import StepFunTextError, classify_intent


class FakeResponse:
    def __init__(self, body):
        self.status_code = 200
        self._body = body

    def json(self):
        return self._body


class FakeIntentClient:
    def __init__(self, content='{"intent": "none"}'):
        self.content = content
        self.calls = []

    async def post(self, url, *, headers=None, json=None):
        self.calls.append({"url": url, "json": json})
        return FakeResponse({"choices": [{"message": {"content": self.content}}]})

    async def aclose(self):
        pass


def intent_completion(intent):
    return f'{{"intent": "{intent}"}}'


def routed_client(tmp_path, fake=None, **kwargs):
    kwargs.setdefault("vision_api_key", "synthetic-key")
    kwargs.setdefault("intent_llm_enabled", True)
    kwargs.setdefault("intent_llm_http_client", fake)
    kwargs.setdefault("seed", False)  # no seeded pending draft in the way
    app = create_app(tmp_path / "intent.sqlite3", **kwargs)
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    login_owner(client)
    return client


def simulate(client, text, key=None):
    return client.post("/v1/whatsapp/simulate", headers={"Idempotency-Key": key or uuid4().hex}, json={"sender": "628123456789", "text": text})


def data(response):
    assert response.status_code == 200, response.text
    return response.json()["data"]


def start_draft(client, text="beli telur 2 kg 60rb"):
    draft = data(simulate(client, text))["draft"]
    assert draft["status"] == "pending_approval"
    return draft


# --- Deterministic synonym expansion (no LLM involved) -----------------------


def test_expanded_synonym_commands(tmp_path):
    client = routed_client(tmp_path, vision_api_key=None, intent_llm_enabled=False)
    start_draft(client, "beli telur 2 kg 60rb")
    assert data(simulate(client, "gas"))["result"]["action"] == "approved"
    start_draft(client, "beli beras 2 kg 50rb")
    assert data(simulate(client, "gajadi"))["result"]["action"] == "rejected"
    start_draft(client, "beli gula 1 kg 15rb")
    assert data(simulate(client, "tidak jadi"))["result"]["action"] == "rejected"
    start_draft(client, "beli minyak 2 l 40rb")
    assert data(simulate(client, "setuju semua"))["result"]["action"] == "approved"


def test_the_classic_commands_still_work(tmp_path):
    assert "ya" in APPROVE_COMMANDS and "batal" in REJECT_COMMANDS
    client = routed_client(tmp_path, vision_api_key=None, intent_llm_enabled=False)
    start_draft(client)
    assert data(simulate(client, "ya"))["result"]["action"] == "approved"
    start_draft(client)
    assert data(simulate(client, "batal"))["result"]["action"] == "rejected"


# --- LLM routing for the chatty words -----------------------------------------


def test_chatty_approval_routes_to_approve(tmp_path):
    fake = FakeIntentClient(intent_completion("approve"))
    client = routed_client(tmp_path, fake)
    start_draft(client, "beli telur 2 kg 60rb")
    result = data(simulate(client, "setuju banget deh, gaskeun"))
    assert result["result"]["action"] == "approved"
    assert len(fake.calls) == 1
    assert "setuju banget deh" in fake.calls[0]["json"]["messages"][0]["content"]


def test_chatty_rejection_routes_to_reject(tmp_path):
    fake = FakeIntentClient(intent_completion("reject"))
    client = routed_client(tmp_path, fake)
    start_draft(client, "beli telur 2 kg 60rb")
    assert data(simulate(client, "batalin ahh deh"))["result"]["action"] == "rejected"


def test_chatty_summary_routes_to_summary(tmp_path):
    # "rekap"-style wording is answered by the spending feature before intent
    # routing; a report request in other words reaches the summary route.
    fake = FakeIntentClient(intent_completion("summary"))
    client = routed_client(tmp_path, fake)
    result = data(simulate(client, "laporan mingguan dong"))
    assert result["result"]["action"] == "weekly_summary"


# --- The guards ----------------------------------------------------------------


def test_ambiguous_message_stays_help_even_when_routed_none(tmp_path):
    fake = FakeIntentClient(intent_completion("none"))
    client = routed_client(tmp_path, fake)
    result = data(simulate(client, "zaman sekarang mahal mahal"))
    assert result["result"]["action"] == "help_reply"


def test_an_invalid_classifier_answer_falls_back_to_the_old_flow(tmp_path):
    fake = FakeIntentClient("hambalaje tanpa json")
    client = routed_client(tmp_path, fake)
    result = data(simulate(client, "setuju banget deh"))
    assert result["result"]["action"] == "help_reply"


def test_purchase_notes_are_never_routed(tmp_path):
    fake = FakeIntentClient(intent_completion("approve"))
    client = routed_client(tmp_path, fake)
    # An approval word rides along, but the purchase signal wins: the note
    # becomes a draft and the classifier is never consulted.
    result = data(simulate(client, "setuju, beli cabai 3 kg 150rb"))
    assert result["result"]["action"] == "draft_created"
    assert result["draft"]["status"] == "pending_approval"
    assert fake.calls == []


def test_disabled_flag_keeps_the_old_behavior(tmp_path):
    fake = FakeIntentClient(intent_completion("approve"))
    client = routed_client(tmp_path, fake, intent_llm_enabled=False)
    start_draft(client, "beli telur 2 kg 60rb")
    result = data(simulate(client, "setuju banget deh"))
    # The old flow reminds the owner about the waiting draft instead of guessing.
    assert result["result"]["action"] == "active_draft_exists"
    assert fake.calls == []


def test_an_approve_hint_without_a_draft_is_still_refused(tmp_path):
    fake = FakeIntentClient(intent_completion("approve"))
    client = routed_client(tmp_path, fake)
    result = data(simulate(client, "setuju banget deh"))
    assert result["result"]["action"] == "no_active_draft"


# --- The classifier module itself ------------------------------------------------


def test_classify_intent_parses_fenced_json_and_rejects_unknown():
    import asyncio

    async def run(content):
        class R:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content": content}}]}

        class C:
            async def post(self, url, *, headers=None, json=None):
                return R()

            async def aclose(self):
                pass

        return await classify_intent("gas", api_key="synthetic-key", client=C())

    assert asyncio.run(run('```json\n{"intent": "approve"}\n```')) == "approve"
    with pytest.raises(StepFunTextError):
        asyncio.run(run('{"intent": "cancel_everything"}'))
    with pytest.raises(StepFunTextError):
        asyncio.run(run("no json here"))
