"""Admin console: create restaurants, manage WhatsApp numbers and owner logins."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from conftest import login, login_admin, login_owner, owner_client
from service.api import create_app


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path / "admin.sqlite3")


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as value:
        login_admin(value)
        yield value


def key():
    return {"Idempotency-Key": uuid4().hex}


def create_payload(**changes):
    payload = {
        "name": "Warung Kita",
        "address": "Jl. Mawar 4",
        "owner_username": "kitaku",
        "owner_password": "rahasia123",
        "whatsapp_number": "628999000111",
        "number_label": "Pemilik",
    }
    payload.update(changes)
    return {name: value for name, value in payload.items() if value is not None}


def create_restaurant(client, **changes):
    return client.post("/v1/admin/restaurants", headers=key(), json=create_payload(**changes))


def test_create_restaurant_provisions_owner_login_and_number(client):
    response = create_restaurant(client)
    assert response.status_code == 201, response.text
    detail = response.json()["data"]
    assert detail["name"] == "Warung Kita"
    assert [number["phone"] for number in detail["whatsapp_numbers"]] == ["628999000111"]
    assert [user["username"] for user in detail["users"]] == ["kitaku"]
    assert detail["recorded_purchases"] == 0

    # The owner login works and sees an empty, isolated dashboard.
    owner = login(client, "kitaku", "rahasia123")
    assert owner.get("/v1/auth/me").json()["data"]["restaurant_name"] == "Warung Kita"
    assert owner.get("/v1/overview").status_code == 200


def test_create_restaurant_validation_and_uniqueness(client):
    assert create_restaurant(client, name="  ").status_code == 422
    assert create_restaurant(client, owner_username="ab").status_code == 422
    assert create_restaurant(client, owner_username="bad name!").status_code == 422
    assert create_restaurant(client, owner_password="short").status_code == 422
    assert create_restaurant(client, whatsapp_number="12345").status_code == 422

    assert create_restaurant(client).status_code == 201
    assert create_restaurant(client, owner_username="kitaku", whatsapp_number="628999000222").status_code == 409
    assert create_restaurant(client, owner_username="other", whatsapp_number="628999000111").status_code == 409


def test_restaurant_list_detail_and_update(client):
    create_restaurant(client, name="Kedai Dua", owner_username="kedai2", whatsapp_number="628999000333")
    listing = client.get("/v1/admin/restaurants").json()["data"]
    names = {item["name"] for item in listing["restaurants"]}
    assert {"Warung Bu Sari", "Kedai Dua"} <= names
    assert listing["rejected_attempts"] == []

    resto_id = next(item["id"] for item in listing["restaurants"] if item["name"] == "Kedai Dua")
    detail = client.get(f"/v1/admin/restaurants/{resto_id}").json()["data"]
    assert detail["address"] == "Jl. Mawar 4"

    updated = client.patch(f"/v1/admin/restaurants/{resto_id}", headers=key(), json={"name": "Kedai Dua Baru", "address": "Jl. Melati 7"})
    assert updated.status_code == 200
    assert updated.json()["data"]["name"] == "Kedai Dua Baru"
    assert client.get("/v1/admin/restaurants/missing-id").status_code == 404


def test_add_and_remove_whatsapp_numbers(client):
    resto_id = create_restaurant(client, whatsapp_number="628999000444").json()["data"]["id"]

    added = client.post(f"/v1/admin/restaurants/{resto_id}/numbers", headers=key(), json={"whatsapp_number": "628999000555", "label": "Karyawan"})
    assert added.status_code == 200
    assert [row["phone"] for row in added.json()["data"]["whatsapp_numbers"]] == ["628999000444", "628999000555"]

    # Same restaurant twice, or a number held elsewhere, both conflict.
    assert client.post(f"/v1/admin/restaurants/{resto_id}/numbers", headers=key(), json={"whatsapp_number": "628999000555"}).status_code == 409
    assert client.post(f"/v1/admin/restaurants/{resto_id}/numbers", headers=key(), json={"whatsapp_number": "628123456789"}).status_code == 409
    assert client.post(f"/v1/admin/restaurants/missing/numbers", headers=key(), json={"whatsapp_number": "628999000666"}).status_code == 404

    removed = client.delete(f"/v1/admin/restaurants/{resto_id}/numbers/628999000555", headers=key())
    assert removed.status_code == 200
    assert len(removed.json()["data"]["whatsapp_numbers"]) == 1
    assert client.delete(f"/v1/admin/restaurants/{resto_id}/numbers/628999000555", headers=key()).status_code == 404


def test_reset_owner_password_signs_old_sessions_out(client, app):
    create_restaurant(client)
    owner = owner_client(app)
    login(owner, "kitaku", "rahasia123")
    assert owner.get("/v1/overview").status_code == 200

    listing = client.get("/v1/admin/restaurants").json()["data"]
    resto_id = next(item["id"] for item in listing["restaurants"] if item["name"] == "Warung Kita")
    reset = client.post(f"/v1/admin/restaurants/{resto_id}/reset-owner-password", headers=key(), json={"username": "kitaku", "new_password": "kunci-rahasia"})
    assert reset.status_code == 200

    # The old session is gone; the new password works.
    assert owner.get("/v1/overview").status_code == 401
    relogin = owner_client(app)
    assert relogin.post("/v1/auth/login", json={"username": "kitaku", "password": "rahasia123"}).status_code == 401
    fresh = owner_client(app)
    login(fresh, "kitaku", "kunci-rahasia")
    assert fresh.get("/v1/overview").status_code == 200
    assert client.post(f"/v1/admin/restaurants/{resto_id}/reset-owner-password", headers=key(), json={"username": "admin", "new_password": "kunci-rahasia"}).status_code == 404


def test_role_walls_around_the_admin_console(client, app):
    assert client.get("/v1/admin/restaurants").status_code == 200
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as anon:
        assert anon.get("/v1/admin/restaurants").status_code == 401
        assert anon.post("/v1/admin/restaurants", headers=key(), json=create_payload()).status_code == 401
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as owner:
        login_owner(owner)
        assert owner.get("/v1/admin/restaurants").status_code == 403
        assert owner.post("/v1/admin/restaurants", headers=key(), json=create_payload()).status_code == 403
        assert owner.get("/v1/admin/restaurants/demo-resto/overview").status_code == 403
