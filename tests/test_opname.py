"""Physical stock counts (stok opname) over chat: parse, confirm, apply."""

from uuid import uuid4

from fastapi.testclient import TestClient

from conftest import login_owner
from service.api import create_app
from service.db import RESTAURANT, Store


TENANT = RESTAURANT["id"]
OPNAME = "stok opname cabai 2 kg"


class FakeResponse:
    def __init__(self, body):
        self.status_code = 200
        self._body = body

    def json(self):
        return self._body


class FakeLLM:
    def __init__(self, content):
        self.content = content
        self.calls = []

    async def post(self, url, *, headers=None, json=None):
        self.calls.append({"json": json})
        return FakeResponse({"choices": [{"message": {"content": self.content}}]})


def owner_client(tmp_path, **kwargs):
    app = create_app(tmp_path / "opname.sqlite3", **kwargs)
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    login_owner(client)
    return client


def simulate(client, text, key=None):
    return client.post("/v1/whatsapp/simulate", headers={"Idempotency-Key": key or uuid4().hex}, json={"sender": "628123456789", "text": text})


def data(response):
    assert response.status_code == 200, response.text
    return response.json()["data"]


def inventory_quantity(store, ingredient_id="cabai"):
    with store.transaction() as conn:
        row = conn.execute("SELECT quantity FROM inventory WHERE restaurant_id=? AND ingredient_id=?", (TENANT, ingredient_id)).fetchone()
    return row[0] if row else None


# --- Deterministic parsing -----------------------------------------------------


def test_parse_opname_reads_quantities_against_the_ledger(tmp_path):
    store = Store(tmp_path / "op.sqlite3")
    parsed = store.parse_opname(OPNAME, TENANT)
    assert parsed["invalid"] == []
    item = parsed["items"][0]
    assert item["name"] == "Cabai merah"
    assert item["estimated_quantity"] == "3" and item["counted_quantity"] == "2" and item["delta"] == "-1"


def test_parse_opname_rejects_what_it_cannot_read(tmp_path):
    store = Store(tmp_path / "op.sqlite3")
    # No quantity: stays invalid, the reply asks for the format.
    parsed = store.parse_opname("stok opname cabai", TENANT)
    assert parsed["items"] == [] and parsed["invalid"][0]["reason"] == "jumlah tidak terbaca"
    # Unknown ingredient: never guessed into a correction.
    parsed = store.parse_opname("stok opname terasi 2 kg", TENANT)
    assert parsed["items"] == [] and parsed["invalid"][0]["reason"] == "bahan tidak ada di katalog"
    # No marker at all: not an opname.
    assert store.parse_opname("beli cabai 2 kg", TENANT) is None
    assert store.parse_opname(OPNAME, None) is None


def test_parse_opname_handles_multiple_ingredients(tmp_path):
    store = Store(tmp_path / "op.sqlite3")
    parsed = store.parse_opname("stok opname cabai 2 kg dan bawang 1 kg", TENANT)
    assert {item["ingredient_id"] for item in parsed["items"]} == {"cabai", "bawang"}


# --- Proposal and confirmation over chat ----------------------------------------


def test_proposal_shows_both_figures_and_waits(tmp_path):
    client = owner_client(tmp_path)
    before = inventory_quantity(client.app.state.store)
    result = data(simulate(client, OPNAME))
    assert result["result"]["action"] == "opname_proposed"
    body = result["messages"][-1]["body"]
    assert "catatan sistem 3 kg" in body and "hitung fisik 2 kg" in body and "selisih -1 kg" in body
    assert inventory_quantity(client.app.state.store) == before  # nothing applied yet


def test_ya_applies_the_counted_quantity(tmp_path):
    client = owner_client(tmp_path)
    simulate(client, OPNAME)
    result = data(simulate(client, "ya"))
    assert result["result"]["action"] == "opname_applied"
    assert "3 → 2" in result["messages"][-1]["body"]
    store = client.app.state.store
    assert inventory_quantity(store) == "2"
    # The correction is a signed movement without a purchase id, plus audits.
    with store.transaction() as conn:
        movement = conn.execute("SELECT quantity, purchase_id FROM movements WHERE restaurant_id=? AND ingredient_id='cabai' AND purchase_id IS NULL", (TENANT,)).fetchone()
        counts = conn.execute("SELECT status FROM stock_counts WHERE restaurant_id=?", (TENANT,)).fetchall()
    assert movement["quantity"] == "-1"
    assert all(row["status"] == "applied" for row in counts)
    events = [row["event"] for row in store.overview(TENANT)["activity"]]
    assert "opname_applied" in events


def test_applied_reply_reports_derived_usage_and_overview_shows_trend(tmp_path):
    client = owner_client(tmp_path)
    simulate(client, OPNAME)
    result = data(simulate(client, "ya"))
    # The count dropped below the estimate, so the reply says how much left.
    assert "terpakai ±1 kg" in result["messages"][-1]["body"]
    trend = client.app.state.store.overview(TENANT)["charts"]["usage_trend"]
    cabai = next(item for item in trend if item["ingredient_id"] == "cabai")
    assert cabai["unit"] == "kg"
    assert cabai["total_used"] == 1.0
    assert sum(day["used"] for day in cabai["daily"]) == 1.0


def test_batal_cancels_and_keeps_the_ledger(tmp_path):
    client = owner_client(tmp_path)
    simulate(client, OPNAME)
    result = data(simulate(client, "batal"))
    assert result["result"]["action"] == "opname_cancelled"
    assert inventory_quantity(client.app.state.store) == "3"
    # A later YA finds nothing pending and answers the standard refusal.
    assert data(simulate(client, "ya"))["result"]["action"] == "no_active_draft"


def test_a_second_opname_replaces_the_pending_one(tmp_path):
    client = owner_client(tmp_path)
    simulate(client, OPNAME)
    result = data(simulate(client, "stok opname telur 8 kg"))
    assert result["result"]["action"] == "opname_proposed"
    data(simulate(client, "ya"))
    store = client.app.state.store
    assert inventory_quantity(store, "telur") == "8"
    assert inventory_quantity(store, "cabai") == "3"  # the replaced proposal never applied
    with store.transaction() as conn:
        statuses = [row["status"] for row in conn.execute("SELECT status FROM stock_counts ORDER BY rowid")]
    assert statuses.count("cancelled") == 1 and statuses.count("applied") == 1


def test_opname_is_blocked_while_a_purchase_draft_is_active(tmp_path):
    client = owner_client(tmp_path)
    draft = data(simulate(client, "beli telur 2 kg 60rb"))["draft"]
    result = data(simulate(client, OPNAME))
    assert result["draft"]["id"] == draft["id"]  # the waiting draft is untouched
    assert result["result"]["action"] == "active_draft_exists"
    assert inventory_quantity(client.app.state.store) == "3"


# --- Optional LLM phrasing, numbers locked --------------------------------------


def test_fabricated_llm_opname_falls_back_to_the_database_text(tmp_path):
    fake = FakeLLM("Stok cabai 30 kg, semua aman!")
    client = owner_client(tmp_path, vision_api_key="synthetic-key", stock_llm_enabled=True, stock_llm_http_client=fake)
    result = data(simulate(client, OPNAME))
    body = result["messages"][-1]["body"]
    assert "catatan sistem 3 kg" in body and "hitung fisik 2 kg" in body and "30 kg" not in body
    assert len(fake.calls) == 1


def test_valid_llm_rephrase_is_used(tmp_path):
    fake = FakeLLM("Cabai merah: sistem catat 3 kg, hitungan fisik kamu 2 kg. Balas YA untuk terapkan ya.")
    client = owner_client(tmp_path, vision_api_key="synthetic-key", stock_llm_enabled=True, stock_llm_http_client=fake)
    result = data(simulate(client, OPNAME))
    assert result["messages"][-1]["body"].startswith("Cabai merah: sistem catat 3 kg")


# --- Dashboard integration --------------------------------------------------------


def test_history_reaches_the_dashboard(tmp_path):
    client = owner_client(tmp_path)
    simulate(client, OPNAME)
    data(simulate(client, "ya"))
    counts = client.get("/v1/stock-counts").json()["data"]["counts"]
    assert counts and counts[0]["name"] == "Cabai merah"
    assert counts[0]["estimated_quantity"] == "3" and counts[0]["counted_quantity"] == "2"
    overview = client.get("/v1/overview").json()["data"]
    assert overview["stock_counts"] == counts[:5]


# --- Dashboard stock count form -------------------------------------------------


def test_dashboard_stock_count_applies_and_records(tmp_path):
    client = owner_client(tmp_path)
    response = client.post("/v1/stock-counts", headers={"Idempotency-Key": uuid4().hex},
                           json={"ingredient_id": "cabai", "counted_quantity": "2,5", "note": "hitung fisik pagi"})
    assert response.status_code == 201, response.text
    record = response.json()["data"]
    assert record["estimated_quantity"] == "3" and record["counted_quantity"] == "2.5"
    assert record["delta"] == "-0.5"
    store = client.app.state.store
    assert inventory_quantity(store) == "2.5"
    with store.transaction() as conn:
        movement = conn.execute("SELECT quantity FROM movements WHERE restaurant_id=? AND ingredient_id='cabai' AND purchase_id IS NULL", (TENANT,)).fetchone()
        assert movement["quantity"] == "-0.5"
    counts = client.get("/v1/stock-counts").json()["data"]["counts"]
    assert counts and counts[0]["counted_quantity"] == "2.5"
    overview = client.get("/v1/overview").json()["data"]
    assert overview["stock_counts"] == counts[:5]


def test_dashboard_stock_count_validations(tmp_path):
    client = owner_client(tmp_path)
    unknown = client.post("/v1/stock-counts", headers={"Idempotency-Key": uuid4().hex},
                          json={"ingredient_id": "bukan-bahan", "counted_quantity": "1"})
    assert unknown.status_code == 404
    negative = client.post("/v1/stock-counts", headers={"Idempotency-Key": uuid4().hex},
                           json={"ingredient_id": "cabai", "counted_quantity": "-1"})
    assert negative.status_code == 422
    not_number = client.post("/v1/stock-counts", headers={"Idempotency-Key": uuid4().hex},
                             json={"ingredient_id": "cabai", "counted_quantity": "banyak"})
    assert not_number.status_code == 422
    # Nothing changed by the failed attempts.
    assert inventory_quantity(client.app.state.store) == "3"


def test_zero_delta_dashboard_count_is_still_recorded(tmp_path):
    client = owner_client(tmp_path)
    # Counted exactly what the system says: recorded as physical evidence, no change.
    response = client.post("/v1/stock-counts", headers={"Idempotency-Key": uuid4().hex},
                           json={"ingredient_id": "cabai", "counted_quantity": "3"})
    assert response.status_code == 201, response.text
    assert response.json()["data"]["delta"] == "0"
    assert inventory_quantity(client.app.state.store) == "3"
