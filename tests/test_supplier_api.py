"""Supplier API endpoints fase 3: owner-only CRUD, quotes, guards."""
import json
from uuid import uuid4

from fastapi.testclient import TestClient

from conftest import login_admin, login_owner
from service.api import create_app
from service.db import RESTAURANT, Store
from service.suppliers import parse_supplier_command

TENANT = RESTAURANT["id"]


def fresh_app(tmp_path):
    app = create_app(tmp_path / "sup-api.sqlite3")
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    login_owner(client)
    admin = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    login_admin(admin)
    return app, client, admin


def headers(key=None):
    return {"Idempotency-Key": key or uuid4().hex}


def test_supplier_crud_round_trip(tmp_path):
    app, client, _ = fresh_app(tmp_path)
    created = client.post("/v1/suppliers", headers=headers(), json={"name": "Pak Budi", "contact_phone": "628888000777",
                                                                    "contact_platform": "whatsapp", "notes": "ayam"})
    assert created.status_code == 201, created.text
    supplier = created.json()["data"]
    assert supplier["name"] == "Pak Budi"
    listed = client.get("/v1/suppliers").json()["data"]["suppliers"]
    # The seeded demo history also backfills a supplier entity; ours must be present.
    assert "Pak Budi" in [s["name"] for s in listed]
    updated = client.patch(f"/v1/suppliers/{supplier['id']}", headers=headers(),
                           json={"name": "Pak Budi Jaya", "contact_phone": "628999000111"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["name"] == "Pak Budi Jaya"
    removed = client.delete(f"/v1/suppliers/{supplier['id']}", headers=headers())
    assert removed.status_code == 200, removed.text
    # The seeded demo history keeps its own supplier entity.
    names = [s["name"] for s in client.get("/v1/suppliers").json()["data"]["suppliers"]]
    assert "Pak Budi" not in names and "Pasar Pagi" in names


def test_supplier_endpoints_are_owner_only(tmp_path):
    app, client, admin = fresh_app(tmp_path)
    other = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    assert other.post("/v1/suppliers", headers=headers(), json={"name": "X"}).status_code in (401, 403)
    created = client.post("/v1/suppliers", headers=headers(), json={"name": "Pak Budi"})
    supplier_id = created.json()["data"]["id"]
    assert admin.get("/v1/suppliers").status_code in (401, 403)  # admin console is read-only over restos
    assert client.delete(f"/v1/suppliers/{supplier_id}", headers=headers()).status_code == 200


def test_supplier_in_use_cannot_be_removed(tmp_path):
    app, client, _ = fresh_app(tmp_path)
    record = client.post("/v1/suppliers", headers=headers(), json={"name": "Pasar Lama"})
    supplier_id = record.json()["data"]["id"]
    record_purchase(client, "beli cabai 2 kg 100rb dari pasar lama")
    removed = client.delete(f"/v1/suppliers/{supplier_id}", headers=headers())
    assert removed.status_code == 409


def record_purchase(client, text):
    first = client.post("/v1/whatsapp/simulate", headers=headers(), json={"sender": "628123456789", "text": text})
    assert first.status_code == 200, first.text
    approved = client.post("/v1/whatsapp/simulate", headers=headers(), json={"sender": "628123456789", "text": "ya"})
    assert approved.status_code == 200, approved.text
    return approved.json()["data"]["messages"][-1]
