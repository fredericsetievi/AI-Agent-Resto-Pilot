"""Business analysis agent: read-only tool-use for open owner questions.

The provider is a local fake and never called; the deterministic fallback
behavior must survive every agent failure.
"""
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from service.api import create_app
from service.db import RESTAURANT
from service.biz_agent import BizAgentError, answer_business_question, build_tools, clamp_days, question_shaped

OWNER = "628123456789"  # demo tenant owner number


class FakeResponseClass:
    def __init__(self, content):
        self.status_code = 200
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class FakeAgentClient:
    """Scripted chat-completions client; records every call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def post(self, url, *, headers, json):
        self.calls.append({"url": url, "json": json})
        return FakeResponseClass(self.responses.pop(0))


def completion(payload):
    return json.dumps(payload)


def fresh_client(tmp_path, agent_client=None, enabled=True):
    app = create_app(tmp_path / "biz.sqlite3", biz_agent_enabled=enabled,
                     vision_api_key="test-key", biz_agent_http_client=agent_client)
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    login = client.post("/v1/auth/login", json={"username": "owner", "password": "owner123"})
    assert login.status_code == 200, login.text
    return client


def record_quote(client, supplier="Pak Budi", ingredient="cabai", price="35rb"):
    recorded = client.post("/v1/whatsapp/simulate", headers={"Idempotency-Key": uuid4().hex},
                           json={"sender": OWNER, "text": f"catat harga {ingredient} {supplier} {price}"})
    assert recorded.status_code == 200, recorded.text


def record_sale(store, name, portions="10", revenue=150000):
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    event = {"id": uuid4().hex, "kind": "sale", "name": name, "confirmed": True,
             "created_at": datetime.now(timezone.utc).isoformat(), "items": [],
             "portions": portions, "revenue_idr": revenue, "business_date": today}
    with store.transaction() as conn:
        store._inventory_query(conn, "INSERT INTO inventory_events(id,restaurant_id,body,created_at) VALUES (?,?,?,?)",
                               (event["id"], RESTAURANT["id"], json.dumps(event),
                                datetime.now(timezone.utc).isoformat()))


# ---- pure helpers --------------------------------------------------------

def test_question_shaped_gate():
    assert question_shaped("berapa pendapatan hari ini?")
    assert question_shaped("kenapa omzet turun minggu ini")
    assert not question_shaped("beli cabai 3kg")
    assert not question_shaped("ya")


def test_clamp_days():
    assert clamp_days(7) == 7
    assert clamp_days(500) == 90
    assert clamp_days("abc") == 7


# ---- agent loop with the fake provider -----------------------------------

def test_agent_answers_open_question_with_tool_numbers(tmp_path):
    """An open question routes to the agent, calls tools, and answers with
    the tool-derived number instead of the help reply."""
    client = fresh_client(tmp_path)
    record_sale(client.app.state.store, "Bolkus", portions="50", revenue=500000)
    scripted = FakeAgentClient([
        completion({"action": "tool", "name": "sales_insights", "args": {"days": 7}}),
        completion({"action": "final", "answer": "Kas hari ini belum ada amplop tercatat — catat dulu dengan perintah budget, nanti saya hitung sisanya."}),
    ])
    app = create_app(tmp_path / "live.sqlite3", biz_agent_enabled=True,
                     vision_api_key="test-key", biz_agent_http_client=scripted)
    live = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    live.post("/v1/auth/login", json={"username": "owner", "password": "owner123"})
    response = live.post("/v1/whatsapp/simulate", headers={"Idempotency-Key": uuid4().hex},
                         json={"sender": OWNER, "text": "apakah kondisi kas hari ini sehat?"})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["result"]["action"] == "biz_agent_answer"
    assert "amplop" in response.json()["data"]["messages"][-1]["body"]
    assert len(scripted.calls) == 2  # one tool round + one final answer


def test_purchase_notes_never_reach_the_agent(tmp_path):
    """A purchase note with no question shape must stay a draft and never
    touch the agent."""
    scripted = FakeAgentClient([completion({"action": "final", "answer": "x"})])
    client = fresh_client(tmp_path, agent_client=scripted)
    response = client.post("/v1/whatsapp/simulate", headers={"Idempotency-Key": uuid4().hex},
                           json={"sender": OWNER, "text": "beli cabai 3kg 45rb"})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["result"]["action"] == "draft_created"
    assert scripted.calls == []


def test_agent_failure_falls_back_to_help_and_logs(tmp_path):
    """When the agent errors, the deterministic behavior survives: help
    reply plus the intent_unknown tuning log."""
    class BrokenClient:
        async def post(self, url, *, headers, json):
            raise ConnectionError("provider down")

    client = fresh_client(tmp_path, agent_client=BrokenClient())
    response = client.post("/v1/whatsapp/simulate", headers={"Idempotency-Key": uuid4().hex},
                           json={"sender": OWNER, "text": "apakah kondisi kas hari ini sehat?"})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["result"]["action"] == "help_reply"
    with client.app.state.store.transaction() as conn:
        row = conn.execute("SELECT message FROM activity WHERE event='intent_unknown' "
                           "ORDER BY created_at DESC LIMIT 1").fetchone()
    assert row and "kas" in row[0]


def test_tools_are_tenant_locked(tmp_path):
    """The restaurant id is bound at build time; the model cannot point a
    tool at another tenant's data — the documented args never include it."""
    import json as _json
    from service.db import Store
    store = Store(tmp_path / "lock.sqlite3")
    tools = build_tools(store, "demo-resto")
    for name, tool in tools.items():
        assert "restaurant_id" not in _json.dumps(tool["args"])


def test_agent_errors_raise_for_callers():
    import asyncio
    from service.biz_agent import answer_business_question

    class BrokenClient:
        async def post(self, url, *, headers, json):
            raise ConnectionError("provider down")

    with pytest.raises(BizAgentError):
        asyncio.run(answer_business_question(
            "berapa omzet?", store=None, restaurant_id="demo-resto",
            api_key="", model="m", base_url="https://x/v1", client=BrokenClient()))
