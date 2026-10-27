"""Shared fixtures: keep the suite hermetic against a developer's local .env."""
import os

import pytest
from fastapi.testclient import TestClient

from service import supabase_db

# The suite is hermetic: it always runs on the local SQLite store unless a
# test module opts into the live cloud (tests/test_pg_store.py sets TEST_PG
# and is skipped by default). FORCE_SQLITE makes create_app ignore a
# developer's SUPABASE_DB_PASSWORD.
os.environ.setdefault("FORCE_SQLITE", "1")
from service.db import DEMO_NUMBERS, DEMO_USERS


@pytest.fixture(autouse=True)
def isolate_cloud_environment(monkeypatch):
    """No test may talk to a real Supabase project via the local .env.

    mirror_enabled() loads the repo .env into os.environ at call time, so
    point it at a nonexistent path and strip Supabase variables for every
    test. Tests that exercise the mirror stub mirror_enabled/mirror_purchase
    explicitly (tests/test_supabase_db.py).
    """
    monkeypatch.setattr(supabase_db, "ENV_PATH", "/nonexistent/.env")
    for key in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_MIRROR"):
        monkeypatch.delenv(key, raising=False)


# Seeded credentials (service.db.DEMO_USERS); TestClient keeps the session
# cookie in its jar, so one login authenticates every later request.
OWNER = next(user for user in DEMO_USERS if user["role"] == "owner")
ADMIN = next(user for user in DEMO_USERS if user["role"] == "admin")
# The WhatsApp number seeded onto the demo restaurant; simulator tests send as it.
SENDER_NUMBER = DEMO_NUMBERS[0]["phone"]


def login(client, username, password):
    response = client.post("/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return client


def owner_client(app):
    """A fresh TestClient logged in as the seeded demo owner (own cookie jar)."""
    value = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    login(value, OWNER["username"], OWNER["password"])
    return value


def login_owner(client):
    return login(client, OWNER["username"], OWNER["password"])


def login_admin(client):
    return login(client, ADMIN["username"], ADMIN["password"])
