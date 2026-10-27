"""Live Supabase Postgres store tests. Opt-in: TEST_PG=1 pytest tests/test_pg_store.py

Writes real rows into the configured project's tables; skipped by default so
the ordinary suite stays hermetic.
"""

import os

import pytest

# Live-cloud module, opt-in via TEST_PG=1. The suite-wide FORCE_SQLITE=1 set
# in conftest is flipped per-test here (with teardown) so no other test file
# ever touches the cloud.
os.environ.setdefault("TEST_PG", "1")


@pytest.fixture(autouse=True)
def _live_postgres(monkeypatch):
    monkeypatch.setenv("FORCE_SQLITE", "0")
from fastapi.testclient import TestClient

from conftest import login_owner
from service.api import create_app

pytestmark = pytest.mark.skipif(
    os.environ.get("TEST_PG", "").strip().lower() not in {"1", "true", "yes", "on"},
    reason="live Supabase writes; set TEST_PG=1 to include",
)


@pytest.fixture(scope="module")
def app():
    os.environ["FORCE_SQLITE"] = "0"
    try:
        yield create_app("data/pg-test-ignored.sqlite3")
    finally:
        os.environ["FORCE_SQLITE"] = "1"


@pytest.fixture(scope="module")
def client(app):
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as value:
        login_owner(value)
        yield value


def test_ready_reports_supabase(client):
    assert client.get("/health/ready").json()["data"]["storage"] == "supabase"


def test_login_and_overview_over_postgres(client):
    me = client.get("/v1/auth/me").json()["data"]
    assert me["role"] == "owner" and me["restaurant_id"] == "demo-resto"
    state = client.get("/v1/overview").json()["data"]
    assert state["mode"]["storage"] == "supabase"
    assert state["restaurant"]["name"] == "Warung Bu Sari"


def test_intake_approve_and_whatsapp_flow_persist(client):
    draft = client.post("/v1/intake", headers={"Idempotency-Key": "pg-live-1"}, json={"text": "Beli gula merah 1 kg 18rb"}).json()["data"]
    assert draft["status"] == "pending_approval"
    decided = client.post(f"/v1/drafts/{draft['id']}/decision", headers={"Idempotency-Key": "pg-live-2"},
                          json={"expected_version": draft["version"], "decision": "approve", "received": True}).json()["data"]
    assert decided["status"] == "recorded"
    # Idempotent replay returns the same draft id, not a new one.
    replay = client.post("/v1/intake", headers={"Idempotency-Key": "pg-live-1"}, json={"text": "Beli gula merah 1 kg 18rb"}).json()["data"]
    assert replay["id"] == draft["id"]
    sim = client.post("/v1/whatsapp/simulate", headers={"Idempotency-Key": "pg-live-3"},
                      json={"sender": "628123456789", "text": "beli minyak 1 l 20rb"}).json()["data"]
    assert sim["result"]["action"] in ("draft_created", "active_draft_exists")  # cloud state may hold an older draft
    approved = client.post("/v1/whatsapp/simulate", headers={"Idempotency-Key": "pg-live-4"},
                           json={"sender": "628123456789", "text": "batal"}).json()["data"]
    assert approved["result"]["action"] in ("approved", "rejected", "no_active_draft")


def test_unregistered_sender_is_rejected_over_postgres(client):
    # 628123456789 is seeded as linked; the simulator gate must refuse it for a
    # different restaurant. The admin ensures a probe number is unlinked first.
    client.post("/v1/auth/login", json={"username": "admin", "password": "admin123"})
    client.delete("/v1/admin/restaurants/demo-resto/numbers/628000111222", headers={"Idempotency-Key": "pg-live-unlink"})
    client.post("/v1/auth/login", json={"username": "owner", "password": "owner123"})
    result = client.post("/v1/whatsapp/simulate", headers={"Idempotency-Key": "pg-live-5"},
                         json={"sender": "628000111222", "text": "beli cabai 1 kg"})
    assert result.status_code == 403  # not one of this restaurant's numbers
