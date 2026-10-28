"""Durable WhatsApp operations over real disposable PostgreSQL, never Supabase.

Uses the local-only database fixture shared with inventory concurrency tests.
Run with TEST_LOCAL_PG_DSN='host=/private/tmp port=55439 dbname=postgres'.
"""
import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from service.db import DomainError
from service.operation_extraction import parse_operations_text
from test_inventory_postgres import local_database, pg_inventory, balance, parallel, query, usage


pytestmark = pytest.mark.skipif(not os.environ.get("TEST_LOCAL_PG_DSN"), reason="requires explicit local PostgreSQL DSN")
ALPHA = "628111111111"
ALPHA_OTHER = "628222222222"
BETA = "628333333333"


@pytest.fixture
def operations_store(pg_inventory):
    store = pg_inventory
    with store.transaction() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS whatsapp_links (
                id text PRIMARY KEY, slug text REFERENCES tenants(slug), phone text UNIQUE,
                label text, created_at timestamptz DEFAULT now());
            CREATE TABLE IF NOT EXISTS whatsapp_threads (
                id text PRIMARY KEY, slug text REFERENCES tenants(slug), channel text,
                sender text, transport text, active_draft_id text REFERENCES drafts(id),
                active_version integer, pending_followup text, UNIQUE(channel,sender));
            CREATE TABLE IF NOT EXISTS whatsapp_messages (
                id text PRIMARY KEY, thread_id text REFERENCES whatsapp_threads(id),
                direction text, body text, created_at timestamptz, transport text,
                status text, draft_id text REFERENCES drafts(id));
            CREATE TABLE IF NOT EXISTS whatsapp_events (
                channel text, event_id text, thread_id text REFERENCES whatsapp_threads(id),
                fingerprint text, response jsonb, PRIMARY KEY(channel,event_id));
        """)
        cur.execute((Path(__file__).parents[1] / "supabase/migrations/0005_whatsapp_operations.sql").read_text())
        cur.execute("TRUNCATE whatsapp_operations, inventory_packages, whatsapp_events, whatsapp_messages, whatsapp_threads, whatsapp_links CASCADE")
        cur.executemany("INSERT INTO whatsapp_links(id,slug,phone) VALUES (%s,%s,%s)", [(uuid4().hex, "alpha", ALPHA), (uuid4().hex, "alpha", ALPHA_OTHER), (uuid4().hex, "beta", BETA)])
    return store


def send(store, text, *, sender=ALPHA, event_id=None, candidate=None, source=None):
    payload = {"sender": sender, "text": text, "event_id": event_id or uuid4().hex, "num_media": 1 if source else 0}
    if candidate is not None:
        payload["operation_candidate"] = candidate
    if source is not None:
        payload["operation_source"] = source
    return store.whatsapp_ingest(uuid4().hex, payload, restaurant_id="beta" if sender == BETA else "alpha")


def op(response):
    return response["result"]["operation"]


def confirm(store, operation, **kwargs):
    return send(store, f"YA {operation['id'][:8]} V{operation['version']}", **kwargs)


def recipes(store, tenant="alpha"):
    first = store.save_inventory_recipe(uuid4().hex, {"name": "Nasi goreng", "items": [{"ingredient_id": "beras", "quantity": "0.5"}, {"ingredient_id": "telur", "quantity": "1"}]}, tenant)
    second = store.save_inventory_recipe(uuid4().hex, {"name": "Mie goreng", "items": [{"ingredient_id": "beras", "quantity": "0.25"}, {"ingredient_id": "telur", "quantity": "0.5"}]}, tenant)
    return first, second


def test_natural_two_recipe_sales_review_then_confirm_matches_dashboard(operations_store):
    store = operations_store
    first, second = recipes(store)
    reviewed = send(store, "Hari ini terjual 4 nasi goreng dan 3 mie goreng")
    draft = op(reviewed)
    assert draft["status"] == "review", reviewed
    assert [e["recipe"]["id"] for e in draft["preview"]] == [first["id"], second["id"]]
    assert balance(store) == 10 and balance(store, ingredient="telur") == 10
    assert query(store, "SELECT count(*) FROM inventory_events") == [(0,)]
    assert send(store, "YA")["result"]["action"] == "operation_review_required"
    posted = confirm(store, draft)
    assert posted["result"]["action"] == "operation_posted", posted
    assert balance(store) == Decimal("7.25")
    assert balance(store, ingredient="telur") == Decimal("4.5")
    assert {e["id"] for e in op(posted)["events"]} == {e["id"] for e in store.inventory_workspace("alpha")["events"]}
    assert query(store, "SELECT count(*) FROM purchases") == [(0,)]


def test_concurrent_distinct_confirmation_events_post_batch_once(operations_store):
    store = operations_store
    recipes(store)
    draft = op(send(store, "Terjual 4 nasi goreng dan 3 mie goreng hari ini"))
    responses = parallel([lambda: confirm(store, draft) for _ in range(5)])
    assert sum(r["result"]["action"] == "operation_posted" for r in responses) == 1
    assert all(r["result"]["action"] in {"operation_posted", "operation_duplicate"} for r in responses)
    assert query(store, "SELECT count(*) FROM inventory_events") == [(2,)]
    assert query(store, "SELECT count(*) FROM movements") == [(4,)]
    assert balance(store) == Decimal("7.25")


def test_same_digest_other_linked_sender_replays_posted_report(operations_store):
    store = operations_store
    candidate = parse_operations_text("Pakai beras 1 kg hari ini")
    source = {"digest": hashlib.sha256(b"same-photo").hexdigest(), "media_kind": "image"}
    first = op(send(store, "", candidate=candidate, source=source))
    assert confirm(store, first)["result"]["action"] == "operation_posted"
    replay = send(store, "", sender=ALPHA_OTHER, candidate=candidate, source=source)
    assert replay["result"]["action"] == "operation_duplicate"
    assert op(replay)["id"] == first["id"]
    assert balance(store) == 9
    assert query(store, "SELECT count(*) FROM inventory_events") == [(1,)]


def test_same_digest_pending_for_two_senders_cannot_post_twice(operations_store):
    store = operations_store
    candidate = parse_operations_text("Pakai beras 1 kg hari ini")
    source = {"digest": hashlib.sha256(b"pending-same-photo").hexdigest(), "media_kind": "image"}
    first = op(send(store, "", candidate=candidate, source=source))
    second = op(send(store, "", sender=ALPHA_OTHER, candidate=candidate, source=source))
    assert confirm(store, first)["result"]["action"] == "operation_posted"
    response = confirm(store, second, sender=ALPHA_OTHER)
    assert response["result"]["action"] == "operation_duplicate", response
    assert balance(store) == 9
    assert query(store, "SELECT count(*) FROM inventory_events") == [(1,)]


def test_count_changed_since_review_requires_fresh_review(operations_store):
    store = operations_store
    draft = op(send(store, "Sisa beras 7 kg malam ini"))
    assert draft["status"] == "review", draft
    store.record_inventory_event("other_usage", usage("1"), "alpha")
    blocked = confirm(store, draft)
    assert blocked["result"]["action"] == "operation_review_required"
    assert balance(store) == 9
    refreshed = op(send(store, "TINJAU ULANG"))
    assert refreshed["status"] == "review"
    assert refreshed["preview"][0]["items"][0]["before"] == "9"
    assert confirm(store, refreshed)["result"]["action"] == "operation_posted"
    assert balance(store) == 7


def test_recipe_changed_since_review_does_not_deduct_old_recipe(operations_store):
    store = operations_store
    rice, _ = recipes(store)
    draft = op(send(store, "Terjual 2 nasi goreng hari ini"))
    store.save_inventory_recipe("changed_recipe", {"id": rice["id"], "name": rice["name"], "items": [{"ingredient_id": "beras", "quantity": "1"}]}, "alpha")
    blocked = confirm(store, draft)
    assert blocked["result"]["action"] == "operation_review_required"
    assert balance(store) == 10
    refreshed = op(send(store, "TINJAU ULANG"))
    assert refreshed["status"] == "review"
    assert refreshed["preview"][0]["recipe"]["version"] == 2
    assert confirm(store, refreshed)["result"]["action"] == "operation_posted"
    assert balance(store) == 8


def test_concurrent_stock_change_between_resolve_and_post_requires_review(operations_store, monkeypatch):
    store = operations_store
    draft = op(send(store, "Pakai beras 2 kg hari ini"))
    original = store._post_inventory_batch_conn
    raced = False

    def competing_post(conn, payloads, restaurant_id, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            # Separate committed transaction after fresh preview but before
            # approval acquires ingredient locks.
            store.record_inventory_event("racing_usage", usage("1"), "alpha")
        return original(conn, payloads, restaurant_id, **kwargs)

    monkeypatch.setattr(store, "_post_inventory_batch_conn", competing_post)
    blocked = confirm(store, draft)
    assert blocked["result"]["action"] == "operation_review_required", blocked
    assert balance(store) == 9
    assert query(store, "SELECT count(*) FROM inventory_events") == [(1,)]


def test_sources_recipes_and_confirmation_ids_are_tenant_scoped(operations_store):
    store = operations_store
    recipes(store)
    candidate = parse_operations_text("Pakai beras 1 kg hari ini")
    source = {"digest": hashlib.sha256(b"same-bytes-different-tenant").hexdigest(), "media_kind": "image"}
    alpha = op(send(store, "", candidate=candidate, source=source))
    assert confirm(store, alpha)["result"]["action"] == "operation_posted"
    beta = op(send(store, "", sender=BETA, candidate=candidate, source=source))
    assert beta["id"] != alpha["id"] and beta["restaurant_id"] == "beta"
    assert confirm(store, beta, sender=BETA)["result"]["action"] == "operation_posted"
    assert balance(store) == 9 and balance(store, "beta") == 19
    unknown = op(send(store, "Terjual 2 nasi goreng hari ini", sender=BETA))
    assert unknown["status"] == "clarification"
    assert "recipe_id" not in unknown["candidate"]["lines"][0]
    with pytest.raises(DomainError) as error:
        store.whatsapp_ingest(uuid4().hex, {"sender": BETA, "text": "YA", "event_id": uuid4().hex}, restaurant_id="alpha")
    assert error.value.code == "sender_not_registered"
    assert query(store, "SELECT count(*) FROM inventory_events") == [(2,)]


def save_package(store, size, version):
    package = {"ingredient_id": "beras", "package_name": "dus", "quantity": size, "version": version}
    with store.transaction() as conn:
        store._execute(conn, "INSERT INTO inventory_packages(restaurant_id,ingredient_id,package_name,body) VALUES (%s,%s,%s,%s) ON CONFLICT(restaurant_id,ingredient_id,package_name) DO UPDATE SET body=excluded.body", ("alpha", "beras", "dus", json.dumps(package)))
    return package


def test_saved_package_usage_posts_without_rewriting_implicitly_reused_definition(operations_store):
    store = operations_store
    package = save_package(store, "12", 1)
    draft = op(send(store, "Pakai setengah dus beras hari ini"))
    assert draft["status"] == "review", draft
    assert draft["packages"][0]["explicit"] is False
    assert confirm(store, draft)["result"]["action"] == "operation_posted"
    assert balance(store) == 4
    assert json.loads(query(store, "SELECT body FROM inventory_packages WHERE restaurant_id='alpha'")[0][0]) == package


def test_changed_saved_package_requires_review_of_new_size_without_reverting_definition(operations_store):
    store = operations_store
    save_package(store, "12", 1)
    draft = op(send(store, "Pakai setengah dus beras hari ini"))
    package = save_package(store, "10", 2)
    blocked = confirm(store, draft)
    assert blocked["result"]["action"] == "operation_review_required"
    refreshed = op(blocked)
    assert refreshed["status"] == "review"
    assert refreshed["version"] > draft["version"]
    assert refreshed["preview"][0]["items"][0]["quantity"] == "5"
    assert balance(store) == 10
    assert confirm(store, refreshed)["result"]["action"] == "operation_posted"
    assert balance(store) == 5
    assert json.loads(query(store, "SELECT body FROM inventory_packages WHERE restaurant_id='alpha'")[0][0]) == package


def test_corrected_photo_preserves_original_and_replacement_digest_for_replays(operations_store):
    store = operations_store
    original_source = {"digest": hashlib.sha256(b"original-photo").hexdigest(), "media_kind": "image"}
    corrected_source = {"digest": hashlib.sha256(b"corrected-photo").hexdigest(), "media_kind": "image"}
    original = op(send(store, "", candidate=parse_operations_text("Pakai beras 2 kg hari ini"), source=original_source))
    corrected = op(send(store, "", candidate=parse_operations_text("Pakai beras 1 kg hari ini"), source=corrected_source))
    assert original["id"] == corrected["id"]
    assert set(corrected["source_digests"]) == {original_source["digest"], corrected_source["digest"]}
    assert confirm(store, corrected)["result"]["action"] == "operation_posted"
    for source in (original_source, corrected_source):
        replay = send(store, "", sender=ALPHA_OTHER, candidate=parse_operations_text("Pakai beras 1 kg hari ini"), source=source)
        assert replay["result"]["action"] == "operation_duplicate"
    assert balance(store) == 9
    assert query(store, "SELECT count(*) FROM inventory_events") == [(1,)]
