"""Caller-owned inventory batches use one ledger and deterministic previews."""
import json
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from service.db import DomainError, RESTAURANT_TIMEZONE, Store


TENANT = "demo-resto"


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "batch.sqlite3", seed=False)
    with value.transaction() as conn:
        conn.execute("UPDATE inventory SET quantity='30' WHERE restaurant_id=? AND ingredient_id='beras'", (TENANT,))
        conn.execute("UPDATE inventory SET quantity='20' WHERE restaurant_id=? AND ingredient_id='telur'", (TENANT,))
    return value


def usage(amount="1", **changes):
    return {"kind": "usage", "note": "Daily report", "items": [{"ingredient_id": "beras", "quantity": amount}], "confirmed": True, **changes}


def count(amount="20", expected="30", **changes):
    return usage(kind="count", items=[{"ingredient_id": "beras", "quantity": amount, "expected_quantity": expected}], **changes)


def balance(store, ingredient="beras"):
    with store.transaction() as conn:
        row = conn.execute("SELECT quantity FROM inventory WHERE restaurant_id=? AND ingredient_id=?", (TENANT, ingredient)).fetchone()
        return Decimal(row[0]) if row else Decimal(0)


def post(store, payloads):
    with store.transaction() as conn:
        return store._post_inventory_batch_conn(conn, payloads, TENANT)


def recipe(store, name, ingredient, amount):
    return store.save_inventory_recipe(uuid4().hex, {"name": name, "items": [{"ingredient_id": ingredient, "quantity": amount}]}, TENANT)


def sale(recipe, portions="2"):
    return usage(kind="sale", items=[], recipe_id=recipe["id"], expected_recipe_version=recipe["version"], portions=portions)


def test_preview_is_read_only_deterministic_and_does_not_create_missing_stock(store):
    with store.transaction() as conn:
        conn.execute("DELETE FROM inventory WHERE restaurant_id=? AND ingredient_id='cabai'", (TENANT,))
        payload = usage(kind="count", items=[{"ingredient_id": "cabai", "quantity": "2", "expected_quantity": "0"}], confirmed=False)
        denied = []

        def authorize(action, *unused):
            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                denied.append(action)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorize)
        first = store._preview_inventory_batch_conn(conn, [payload], TENANT)
        assert store._preview_inventory_batch_conn(conn, [payload], TENANT) == first
        conn.set_authorizer(None)
        assert not denied
        assert "id" not in first[0] and "created_at" not in first[0]
        assert conn.execute("SELECT count(*) FROM inventory WHERE restaurant_id=? AND ingredient_id='cabai'", (TENANT,)).fetchone()[0] == 0


def test_mixed_batch_counts_last_with_original_balance_guard_and_recipe_snapshots(store):
    rice = recipe(store, "Nasi", "beras", "0.5")
    eggs = recipe(store, "Telur", "telur", "1")
    payloads = [count("24"), sale(rice, "4"), sale(eggs, "3"), usage("1"), usage("0.5", kind="waste")]
    with store.transaction() as conn:
        preview = store._preview_inventory_batch_conn(conn, payloads, TENANT)
        events = store._post_inventory_batch_conn(conn, payloads, TENANT)
    assert [{k: v for k, v in event.items() if k not in {"id", "created_at"}} for event in events] == preview
    assert events[0]["items"][0] == {"ingredient_id": "beras", "quantity": "24", "delta": "-2.5", "before": "26.5", "after": "24"}
    assert events[1]["recipe"] == rice
    assert balance(store) == 24 and balance(store, "telur") == 17
    with store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM inventory_events").fetchone()[0] == 5
        assert conn.execute("SELECT count(*) FROM movements WHERE inventory_event_id IS NOT NULL").fetchone()[0] == 5


def test_shortage_late_in_batch_rolls_back_all_events_and_stock(store):
    first = recipe(store, "Rice", "beras", "1")
    second = recipe(store, "Egg", "telur", "21")
    with pytest.raises(DomainError) as error:
        post(store, [sale(first), sale(second)])
    assert error.value.code == "insufficient_stock"
    assert balance(store) == 30 and balance(store, "telur") == 20
    assert store.inventory_workspace(TENANT)["events"] == []


def test_caller_rollback_undoes_entire_batch(store):
    with pytest.raises(RuntimeError):
        with store.transaction() as conn:
            store._post_inventory_batch_conn(conn, [usage("2"), usage("1", kind="waste")], TENANT)
            raise RuntimeError("Simulated failure saving the operation approval")
    assert balance(store) == 30
    assert store.inventory_workspace(TENANT)["events"] == []


def test_post_revalidates_recipe_and_count_versions(store):
    rice = recipe(store, "Nasi", "beras", "0.5")
    with store.transaction() as conn:
        store._preview_inventory_batch_conn(conn, [sale(rice)], TENANT)
    store.save_inventory_recipe(uuid4().hex, {**rice, "items": [{"ingredient_id": "beras", "quantity": "1"}]}, TENANT)
    with pytest.raises(DomainError) as error:
        post(store, [sale(rice)])
    assert error.value.code == "recipe_conflict"
    post(store, [usage()])
    with pytest.raises(DomainError) as error:
        post(store, [count()])
    assert error.value.code == "stock_conflict"
    assert balance(store) == 29


def test_post_requires_every_confirmation_and_rejects_multiple_final_counts(store):
    for payloads in ([usage(), usage(confirmed=False)], [count(), count("21")], [count(history_only=True)]):
        with pytest.raises(DomainError):
            post(store, payloads)
    assert balance(store) == 30
    assert store.inventory_workspace(TENANT)["events"] == []


def test_expected_preview_rechecked_after_stock_lock_before_posting(store):
    with store.transaction() as conn:
        preview = store._preview_inventory_batch_conn(conn, [usage("2")], TENANT)
    post(store, [usage("1")])
    with pytest.raises(DomainError) as error:
        with store.transaction() as conn:
            store._post_inventory_batch_conn(conn, [usage("2")], TENANT, expected_preview=preview)
    assert error.value.code == "reviewed_stock_conflict"
    assert balance(store) == 29
    assert len(store.inventory_workspace(TENANT)["events"]) == 1


@pytest.mark.parametrize("business_date", ["2026-1-01", "2026-02-30", "tomorrow", True, "9999-12-31"])
def test_invalid_or_future_business_dates_are_rejected(store, business_date):
    with pytest.raises(DomainError):
        post(store, [usage(business_date=business_date)])
    assert balance(store) == 30


def test_later_count_requires_explicit_history_only_without_reducing_stock(store):
    post(store, [count("25")])
    yesterday = (datetime.now(RESTAURANT_TIMEZONE).date() - timedelta(days=1)).isoformat()
    with pytest.raises(DomainError) as error:
        post(store, [usage("3", business_date=yesterday)])
    assert error.value.code == "reconciliation_required"
    event = post(store, [usage("3", business_date=yesterday, history_only=True)])[0]
    assert event["items"][0]["delta"] == "0"
    assert event["items"][0]["planned_quantity"] == "3"
    assert balance(store) == 25
    with store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM movements WHERE inventory_event_id=?", (event["id"],)).fetchone()[0] == 0


def test_source_allowlist_and_business_date_survive_restart(store):
    fields = {"operation_id": "draft:1", "source_id": "meta:1", "report_id": "POS-001", "source_digest": "a" * 64, "coverage": "evening shift"}
    event = post(store, [usage(**fields, provider_secret="must not persist")])[0]
    assert all(event[key] == value for key, value in fields.items())
    assert "provider_secret" not in event
    assert event["business_date"] == datetime.now(RESTAURANT_TIMEZONE).date().isoformat()
    reopened = Store(store.path, seed=False)
    assert reopened.inventory_workspace(TENANT)["events"][0] == event


def test_forecast_uses_business_dates_full_history_and_history_only_quantities(store):
    today = datetime.now(RESTAURANT_TIMEZONE).date()
    first, last = today - timedelta(days=3), today - timedelta(days=1)
    post(store, [count("30")])
    events = post(store, [usage("0.03", business_date=first.isoformat(), history_only=True) for _ in range(100)])
    # Simulate a legacy event at 18:00 UTC: its Jakarta business date is the next day.
    with store.transaction() as conn:
        stamp = f"{first.isoformat()}T18:00:00+00:00"
        legacy = {**events[0], "id": uuid4().hex, "created_at": stamp}
        legacy.pop("business_date")
        conn.execute("INSERT INTO inventory_events VALUES (?,?,?,?)", (legacy["id"], TENANT, json.dumps(legacy), stamp))
    store.save_inventory_settings(uuid4().hex, {"history_start": first.isoformat(), "history_end": last.isoformat(), "history_complete": True}, TENANT)
    workspace = store.inventory_workspace(TENANT)
    forecast = next(row for row in workspace["forecasts"] if row["ingredient_id"] == "beras")
    assert len(workspace["events"]) == 100
    assert Decimal(forecast["daily_usage"]) == Decimal("1.01")
    assert forecast["evidence_days"] == 3 and forecast["source"] == "recorded"
    assert balance(store) == 30
