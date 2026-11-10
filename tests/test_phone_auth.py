"""Provider-free website phone/password signup, transition and authentication."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from conftest import login_admin, login_owner
from service.api import create_app
from service.db import DomainError
from service.phone_auth import normalize_phone

PHONE = "+628123456789"
PASSWORD = "restaurant-secret-9"


@pytest.fixture
def setup(tmp_path):
    app = create_app(tmp_path / "phone.sqlite3")
    client = TestClient(app, base_url="http://127.0.0.1")
    admin = TestClient(app, base_url="http://127.0.0.1")
    login_admin(admin)
    return app, client, admin


def enroll(admin, phone=PHONE, username="owner", restaurant="demo-resto", **extra):
    return admin.put(f"/v1/admin/restaurants/{restaurant}/owner-phone", json={"username": username, "phone": phone, "ownership_reviewed": True, **extra})


def sign_in(client, phone=PHONE, password=PASSWORD):
    return client.post("/v1/auth/phone/login", json={"phone": phone, "password": password})


def register(client, **extra):
    return client.post("/v1/auth/phone/register", json={"phone": PHONE, "password": PASSWORD, "restaurant_name": "New Kitchen", "display_name": "New Owner", **extra})


def connect(client, **extra):
    return client.post("/v1/auth/phone/connect", json={"username": "owner", "password": "owner123", "phone": PHONE, **extra})


@pytest.mark.parametrize("raw", ["08123456789", "628123456789", "+628123456789", "00628123456789", " (0812) 345-6789 "])
def test_indonesian_normalization(raw):
    assert normalize_phone(raw) == PHONE


@pytest.mark.parametrize("raw,expected", [("+1 (415) 555-2671", "+14155552671"), ("0044 7700 900123", "+447700900123")])
def test_explicit_international_normalization(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["8123456789", "4155552671", "+6208123456789", "+627123456789", "-1234", "++628123456789", "+6281234", "+６２８１２３４５６７８９", "08123456789 ext 3", "123456789", ""])
def test_ambiguous_and_non_phone_identifiers_are_rejected(raw):
    with pytest.raises(DomainError):
        normalize_phone(raw)


def test_signup_creates_isolated_restaurant_and_session_without_messaging(setup):
    app, client, admin = setup
    with app.state.store.transaction() as conn:
        before = {table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table}")] for table in ("inventory", "purchases", "movements", "whatsapp_links")}
    response = register(client, phone="0812 345 6789")
    assert response.status_code == 201, response.text
    user = response.json()["data"]
    assert user["role"] == "owner" and user["restaurant_id"] != "demo-resto"
    assert user["display_name"] == "New Owner" and user["restaurant_name"] == "New Kitchen"
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "samesite=lax" in response.headers["set-cookie"].lower()
    assert client.get("/v1/overview").json()["data"]["restaurant"]["id"] == user["restaurant_id"]
    assert client.get("/v1/admin/restaurants/demo-resto/overview").status_code == 403
    assert client.get("/v1/inventory/workspace").status_code == 200
    with app.state.store.transaction() as conn:
        after = {table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table}")] for table in before}
        assert conn.execute("SELECT verified_at FROM login_phone_identities").fetchone()[0] is None
        assert PASSWORD not in conn.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()[0]
    assert before == after
    # The existing WhatsApp link for this number still belongs to its original restaurant.
    assert app.state.store.restaurant_for_sender(PHONE[1:])["id"] == "demo-resto"


def test_phone_password_login_logout_restart_and_expiry(setup):
    app, client, _ = setup
    registered = register(client).json()["data"]
    client.post("/v1/auth/logout")
    assert client.get("/v1/auth/me").status_code == 401
    assert sign_in(client, "08123456789").json()["data"] == registered
    restarted = TestClient(create_app(app.state.store.path), base_url="http://127.0.0.1")
    restarted.cookies = client.cookies
    assert restarted.get("/v1/auth/me").json()["data"] == registered
    with app.state.store.transaction() as conn:
        conn.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00' WHERE user_id=?", (registered["id"],))
    assert restarted.get("/v1/auth/me").status_code == 401


def test_wrong_password_and_unknown_phone_have_identical_responses(setup):
    _, client, _ = setup
    register(client)
    client.post("/v1/auth/logout")
    wrong = sign_in(client, password="wrong-password")
    unknown = sign_in(client, phone="+628223456789")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert "set-cookie" not in wrong.headers
    assert client.get("/v1/auth/me").status_code == 401


def test_whatsapp_link_does_not_enable_owner_login(setup):
    app, client, _ = setup
    assert app.state.store.restaurant_for_sender(PHONE[1:])
    assert sign_in(client, password="owner123").status_code == 401
    assert client.get("/v1/auth/me").status_code == 401


def test_existing_owner_connects_phone_without_losing_identity_or_data(setup):
    app, client, _ = setup
    original = app.state.store.verify_login("owner", "owner123")
    assert connect(client).json()["data"] == original
    client.post("/v1/auth/logout")
    assert sign_in(client, password="owner123").json()["data"] == original
    assert client.get("/v1/overview").json()["data"]["restaurant"]["id"] == "demo-resto"
    # Legacy recovery remains available.
    assert app.state.store.verify_login("owner", "owner123") == original


def test_connect_requires_correct_existing_credentials_and_rejects_admins(setup):
    _, client, _ = setup
    assert connect(client, password="bad").status_code == 401
    assert connect(client, username="unknown", password="bad").status_code == 401
    assert connect(client, username="admin", password="admin123").status_code == 401
    assert client.get("/v1/auth/me").status_code == 401


def test_signup_retries_never_merge_accounts_or_create_duplicate_tenants(setup):
    app, client, _ = setup
    first = register(client).json()["data"]
    client.post("/v1/auth/logout")
    assert register(client).status_code == 409
    assert client.get("/v1/auth/me").status_code == 401
    assert connect(client).status_code == 409
    assert app.state.store.verify_login("owner", "owner123")["restaurant_id"] == "demo-resto"
    assert sign_in(client).json()["data"] == first
    with app.state.store.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM restaurants").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM login_phone_identities").fetchone()[0] == 1


def test_existing_phone_changes_require_review_and_retries_preserve_sessions(setup):
    _, client, admin = setup
    assert connect(client).status_code == 200
    assert connect(client).status_code == 200
    assert connect(client, phone="+628223456789").status_code == 409
    assert enroll(admin).status_code == 200
    assert client.get("/v1/auth/me").status_code == 200
    assert enroll(admin, "+628223456789").status_code == 200
    assert client.get("/v1/auth/me").status_code == 401
    assert sign_in(client, password="owner123").status_code == 401
    assert sign_in(client, phone="+628223456789", password="owner123").status_code == 200
    assert enroll(admin, None).status_code == 200
    assert client.get("/v1/auth/me").status_code == 401


def test_admin_assignment_requires_review_and_correct_owner(setup):
    _, client, admin = setup
    assert enroll(client).status_code == 401
    login_owner(client)
    assert enroll(client).status_code == 403
    assert enroll(admin, ownership_reviewed=False).status_code == 422
    assert enroll(admin, username="admin").status_code == 404
    assert enroll(admin, restaurant="other").status_code == 404
    assert enroll(admin).status_code == 200
    assert admin.get("/v1/admin/restaurants/demo-resto/owner-phones").json()["data"] == [{"username": "owner", "phone": PHONE, "verified": False}]
    login_owner(client)  # Assignment deliberately revoked the prior owner session.
    assert client.get("/v1/admin/restaurants/demo-resto/owner-phones").status_code == 403


def test_password_reset_changes_phone_password_and_revokes_sessions(setup):
    _, client, admin = setup
    assert connect(client).status_code == 200
    response = admin.post("/v1/admin/restaurants/demo-resto/reset-owner-password", headers={"Idempotency-Key": "reset-phone-password"}, json={"username": "owner", "new_password": PASSWORD})
    assert response.status_code == 200, response.text
    assert client.get("/v1/auth/me").status_code == 401
    assert sign_in(client, password="owner123").status_code == 401
    assert sign_in(client).status_code == 200


def test_phone_and_ip_limits_persist_on_failed_auth_and_restart(setup):
    app, client, _ = setup
    for _ in range(20):
        assert sign_in(client).status_code == 401
    assert sign_in(client).status_code == 429
    restarted = TestClient(create_app(app.state.store.path), base_url="http://127.0.0.1")
    assert sign_in(restarted).status_code == 429
    with app.state.store.transaction() as conn:
        assert conn.execute("SELECT hits FROM login_phone_limits WHERE key=?", ("password-phone:" + PHONE,)).fetchone()[0] == 22


def test_signup_ip_limit_and_phone_normalization_prevent_repeated_registration(setup):
    _, client, _ = setup
    for i in range(5):
        assert register(client, phone=f"+1415555{i:04}").status_code == 201
    assert register(client, phone="+14155559999").status_code == 429


@pytest.mark.parametrize("extra", [{"password": "short"}, {"restaurant_name": " "}, {"display_name": ""}, {"role": "admin"}, {"restaurant_id": "demo-resto"}, {"phone": "telegram:123456789"}])
def test_signup_rejects_invalid_fields_and_existing_tenant_or_role_claims(setup, extra):
    app, client, _ = setup
    response = register(client, **extra)
    assert response.status_code == 422
    assert "set-cookie" not in response.headers
    with app.state.store.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM restaurants").fetchone()[0] == 1


def test_hosted_demo_owner_and_admin_protections_apply_to_phone_transition(setup, monkeypatch):
    app, _, admin = setup
    monkeypatch.delenv("ALLOW_DEMO_OWNER_LOGIN", raising=False)
    assert enroll(admin).status_code == 200
    hosted = create_app(None, hosted=True, public_demo=True, public_base_url="https://phone.example", store_factory=lambda: app.state.store)
    client = TestClient(hosted, base_url="https://phone.example")
    assert sign_in(client, password="owner123").status_code == 401
    assert connect(client).status_code == 401
    assert connect(client, username="admin", password="admin123").status_code == 401
    monkeypatch.setenv("ALLOW_DEMO_OWNER_LOGIN", "1")
    allowed = create_app(None, hosted=True, public_demo=True, public_base_url="https://phone.example", store_factory=lambda: app.state.store)
    client = TestClient(allowed, base_url="https://phone.example")
    response = sign_in(client, password="owner123")
    assert response.status_code == 200
    assert "secure" in response.headers["set-cookie"].lower()


def test_new_hosted_signup_is_provider_independent_and_sets_secure_cookie(setup):
    app, _, _ = setup
    hosted = create_app(None, hosted=True, public_demo=True, public_base_url="https://phone.example", store_factory=lambda: app.state.store)
    client = TestClient(hosted, base_url="https://phone.example")
    response = register(client)
    assert response.status_code == 201
    assert "secure" in response.headers["set-cookie"].lower()
    assert client.get("/v1/auth/me").json()["data"]["role"] == "owner"


def test_concurrent_signup_creates_one_restaurant_account_and_session(setup):
    app, client, _ = setup
    barrier = Barrier(2)

    def attempt(_):
        barrier.wait()
        try:
            app.state.store.register_phone_account(PHONE, PASSWORD, "Concurrent Kitchen", "Owner", "local-test")
            return 201
        except DomainError as exc:
            return exc.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == [201, 409]
    with app.state.store.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM restaurants").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM login_phone_identities").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM sessions s JOIN users u ON s.user_id=u.id WHERE u.role='owner'").fetchone()[0] == 1


def test_passwords_never_appear_in_persistent_auth_metadata_or_logs(setup, caplog):
    app, client, _ = setup
    register(client)
    sign_in(client)
    with app.state.store.transaction() as conn:
        for table in ("login_phone_identities", "login_phone_limits", "activity", "users"):
            assert PASSWORD not in repr([tuple(row) for row in conn.execute(f"SELECT * FROM {table}")])
    assert PASSWORD not in caplog.text
