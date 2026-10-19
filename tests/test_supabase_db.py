import uuid

import pytest

from service import supabase_db
from service.db import Store


@pytest.fixture(autouse=True)
def clean_client_cache():
    supabase_db.get_supabase_client.cache_clear()
    yield
    supabase_db.get_supabase_client.cache_clear()


def test_missing_configuration_fails_fast(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    monkeypatch.setattr(supabase_db, "ENV_PATH", "/nonexistent/.env")
    with pytest.raises(RuntimeError, match="Supabase is not configured"):
        supabase_db.get_supabase_client()


def test_env_file_configuration_creates_cached_client(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("SUPABASE_URL=https://demo.supabase.co\nSUPABASE_SERVICE_KEY=service-key\n", encoding="utf-8")
    monkeypatch.setattr(supabase_db, "ENV_PATH", env)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    client = supabase_db.get_supabase_client()
    assert client is supabase_db.get_supabase_client()


def _purchase_fixture():
    """Shape mirrors service.db purchase bodies (uuid4().hex ids, IDR ints)."""
    return {
        "id": "a" * 32,
        "draft_id": "b" * 32,
        "supplier": "Pasar Pagi",
        "total_idr": 175000,
        "created_at": "2026-10-19T09:00:00+00:00",
        "items": [
            {
                "ingredient_id": "telur", "raw_name": "Telur",
                "canonical_quantity": "3.000", "canonical_unit": "kg",
                "unit_price_idr": "50000.0000", "line_total_idr": 150000,
                "comparison": {"status": "price_increase", "change_percent": 16.3},
            },
            {
                "ingredient_id": "cabai", "raw_name": "Cabai",
                "canonical_quantity": None, "canonical_unit": None,
                "unit_price_idr": None, "line_total_idr": None,
                "comparison": {"status": "insufficient_history"},
            },
        ],
    }


def test_mirror_payloads_map_spike_evidence_and_nulls():
    payloads = supabase_db.build_mirror_payloads(_purchase_fixture())
    invoice = payloads["invoice"]
    assert invoice["status"] == "approved" and invoice["total_amount"] == 175000
    assert str(uuid.UUID(invoice["id"])) == invoice["id"]  # valid uuid, dashed
    spike, unclear = payloads["items"]
    assert float(spike["price_spike_pct"]) == 16.3
    assert unclear["quantity"] is None and unclear["price_spike_pct"] is None
    assert spike["id"] != unclear["id"]
    assert payloads["inventory"][0]["item_name"] == "Telur"
    assert float(payloads["inventory"][0]["_quantity"]) == 3.0
    assert payloads["inventory"][0]["last_unit_price"] == "50000.0000"


def test_mirror_item_ids_are_deterministic():
    first = supabase_db.build_mirror_payloads(_purchase_fixture())["items"]
    second = supabase_db.build_mirror_payloads(_purchase_fixture())["items"]
    assert [item["id"] for item in first] == [item["id"] for item in second]


def test_mirror_disabled_by_default(monkeypatch):
    monkeypatch.delenv("SUPABASE_MIRROR", raising=False)
    monkeypatch.setattr(supabase_db, "ENV_PATH", "/nonexistent/.env")
    assert supabase_db.mirror_enabled() is False


def test_mirror_enabled_with_flag(monkeypatch):
    monkeypatch.setenv("SUPABASE_MIRROR", "1")
    monkeypatch.setattr(supabase_db, "ENV_PATH", "/nonexistent/.env")
    assert supabase_db.mirror_enabled() is True


def test_store_mirrors_approved_purchase(monkeypatch):
    calls = []
    monkeypatch.setattr(supabase_db, "mirror_enabled", lambda: True)
    monkeypatch.setattr(supabase_db, "mirror_purchase", lambda purchase: calls.append(purchase) or {"status": "mirrored", "invoice_id": purchase["id"]})
    store = Store(":memory:", seed=False)
    draft = store.intake("key-1", {"text": "Telur 10 kg 430rb"})
    store.decision(draft["id"], "key-2", {"expected_version": draft["version"], "decision": "approve", "received": True})
    assert len(calls) == 1
    assert calls[0]["items"][0]["ingredient_id"] == "telur"
    assert calls[0]["total_idr"] == 430000


def test_store_skips_mirror_when_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr(supabase_db, "mirror_enabled", lambda: False)
    monkeypatch.setattr(supabase_db, "mirror_purchase", lambda purchase: calls.append(purchase))
    store = Store(":memory:", seed=False)
    draft = store.intake("key-1", {"text": "Telur 10 kg 430rb"})
    store.decision(draft["id"], "key-2", {"expected_version": draft["version"], "decision": "approve", "received": True})
    assert calls == []


def test_store_survives_mirror_failure(monkeypatch):
    monkeypatch.setattr(supabase_db, "mirror_enabled", lambda: True)
    def boom(_purchase):
        raise RuntimeError("network down")
    monkeypatch.setattr(supabase_db, "mirror_purchase", boom)
    store = Store(":memory:", seed=False)
    draft = store.intake("key-1", {"text": "Telur 10 kg 430rb"})
    recorded = store.decision(draft["id"], "key-2", {"expected_version": draft["version"], "decision": "approve", "received": True})
    assert recorded["status"] == "recorded"  # demo unaffected
    events = [event["event"] for event in store.overview()["activity"]]
    assert "cloud_mirror_failed" in events
