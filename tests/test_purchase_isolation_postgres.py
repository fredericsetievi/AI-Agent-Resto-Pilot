"""Purchase retry isolation on disposable local PostgreSQL, never Supabase."""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from service.db import DomainError
from test_inventory_postgres import local_database, pg_inventory, query


pytestmark = pytest.mark.skipif(not os.environ.get("TEST_LOCAL_PG_DSN"), reason="requires explicit local PostgreSQL DSN")


@pytest.mark.parametrize("legacy", [False, True], ids=["scoped-cache", "legacy-cache"])
@pytest.mark.parametrize("kind", ["intake", "intake_image", "edit", "decision"])
def test_purchase_retry_cache_cannot_cross_restaurants(pg_inventory, monkeypatch, kind, legacy):
    store = pg_inventory
    payload = {"text": "Beli beras 2 kg 20000", "supplier": "Private alpha supplier"}
    extracted = {"items": [{"ingredient_id": "beras", "raw_name": "Beras", "quantity": "2", "unit": "kg", "line_total_idr": 20000}], "warnings": []}
    draft = None
    if kind in {"edit", "decision"}:
        draft = store.intake("prepare-draft", payload, extracted=extracted, restaurant_id="alpha")

    def invoke(tenant):
        if kind == "intake":
            return store.intake("same-client-key", payload, extracted=extracted, restaurant_id=tenant)
        if kind == "intake_image":
            return store.intake_image("same-client-key", "a" * 64, "Private photo", payload["supplier"], extracted["items"], [], restaurant_id=tenant)
        if kind == "edit":
            return store.edit(draft["id"], "same-client-key", {
                "expected_version": draft["version"], "supplier": "Reviewed private supplier", "items": extracted["items"],
            }, restaurant_id=tenant)
        return store.decision(draft["id"], "same-client-key", {
            "expected_version": draft["version"], "decision": "approve", "received": True,
        }, restaurant_id=tenant)

    if legacy:
        # Seed exactly the old raw-key behavior for the first response, then
        # restore the new implementation to exercise deployment-era retries.
        with monkeypatch.context() as old_release:
            old_release.setattr("service.pg_store.purchase_idempotent", lambda s, k, op, p, action, tenant: s._idempotent(k, op, p, action))
            first = invoke("alpha")
    else:
        first = invoke("alpha")

    before = {
        "purchases": query(store, "SELECT count(*) FROM purchases"),
        "movements": query(store, "SELECT count(*) FROM movements"),
        "stock": query(store, "SELECT slug,ingredient_id,quantity FROM stock ORDER BY slug,ingredient_id"),
    }
    assert invoke("alpha") == first
    assert invoke("alpha") == first
    if kind in {"intake", "intake_image"}:
        second = invoke("beta")
        assert second["id"] != first["id"]
        assert invoke("beta") == second
        with pytest.raises(DomainError) as denied:
            store.get_draft(first["id"], "beta")
    else:
        with pytest.raises(DomainError) as denied:
            invoke("beta")
    assert denied.value.code == "not_found" and denied.value.status == 404
    assert query(store, "SELECT count(*) FROM purchases") == before["purchases"]
    assert query(store, "SELECT count(*) FROM movements") == before["movements"]
    assert query(store, "SELECT slug,ingredient_id,quantity FROM stock ORDER BY slug,ingredient_id") == before["stock"]


def test_retry_waits_for_inflight_legacy_request_instead_of_creating_another_draft(pg_inventory, monkeypatch):
    store = pg_inventory
    key = "inflight-legacy-request"
    payload = {"text": "Beli beras 2 kg 20000"}
    items = [{"ingredient_id": "beras", "raw_name": "Beras", "quantity": "2", "unit": "kg", "line_total_idr": 20000}]
    legacy_created, scoped_at_legacy_lock, release_legacy = Event(), Event(), Event()
    execute = store._execute

    def observed_execute(conn, sql, params=()):
        if "pg_advisory_xact_lock" in sql and params == (key,) and legacy_created.is_set():
            scoped_at_legacy_lock.set()
        return execute(conn, sql, params)

    monkeypatch.setattr(store, "_execute", observed_execute)

    def old_action(conn):
        result = store._present(conn, store._new_draft(conn, payload["text"], None, items, [], restaurant_id="alpha"), "alpha")
        legacy_created.set()
        assert release_legacy.wait(timeout=15)
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        original = pool.submit(store._idempotent, key, "intake", payload, old_action)
        try:
            assert legacy_created.wait(timeout=10)
            retry = pool.submit(store.intake, key, payload, {"items": items, "warnings": []}, "alpha")
            assert scoped_at_legacy_lock.wait(timeout=10), "retry must synchronize with the in-flight legacy cache writer"
        finally:
            release_legacy.set()
        assert retry.result(timeout=15) == original.result(timeout=15)
    assert query(store, "SELECT count(*) FROM drafts") == [(1,)]
    assert query(store, "SELECT count(*) FROM purchases") == [(0,)]
