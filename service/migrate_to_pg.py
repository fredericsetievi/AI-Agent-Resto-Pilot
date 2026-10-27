"""One-shot migration: copy a local SQLite authority into Supabase Postgres.

Run once after the 0003 migration is applied:

    .venv/Scripts/python -m service.migrate_to_pg [--db data/restopilot.sqlite3]

Copies tenants (the demo restaurant plus any created on this file), users,
WhatsApp links, drafts, purchases, stock, movements, custom ingredients,
activity, WhatsApp threads/messages/events, and idempotency records. Rows are
upserted, so re-running is safe. The SQLite file is left untouched.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import uuid
from pathlib import Path

from .db import RESTAURANT
from .pg import db_config as pg_config
from .pg_store import PostgresStore


def _sqlite_rows(path, table, tenant_default=None):
    """Read a SQLite table, stamping the demo tenant when the file predates it."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        rows = []
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid"):
            item = dict(row)
            if tenant_default and "restaurant_id" not in item:
                item["restaurant_id"] = tenant_default
            yield item
    finally:
        conn.close()


def migrate(db_path):
    import psycopg2

    config = pg_config()
    # Boot the Postgres store first so tenants/users/links/stock base exist.
    store = PostgresStore(seed=False)
    store.initialize(seed=False)

    conn = psycopg2.connect(sslmode="require", connect_timeout=15, **config)
    counts = {}

    def run(sql, params=()):
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur

    def upsert(table, columns, rows, key):
        if not rows:
            counts[table] = 0
            return
        placeholders = ", ".join(["%s"] * len(columns))
        updates = ", ".join(f"{column}=excluded.{column}" for column in columns if column != key)
        jsonb = {"body", "response"}
        for row in rows:
            values = [json.dumps(row.get(column)) if column in jsonb else row.get(column) for column in columns]
            run(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
                f"ON CONFLICT ({key}) DO UPDATE SET {updates}",
                values,
            )
        counts[table] = len(rows)

    # Ensure every restaurant_id seen in the file has a tenant row.
    run(
        "INSERT INTO tenants (slug, restaurant_id) "
        "SELECT %s, (SELECT restaurant_id FROM tenants WHERE slug=%s) ON CONFLICT (slug) DO NOTHING",
        (RESTAURANT["id"], RESTAURANT["id"]),
    )
    # Every SQLite-era restaurant_id lands on the demo tenant's cloud uuid
    # mapping; one-tenant files are the common case.
    known = {RESTAURANT["id"]}
    try:
        conn2 = sqlite3.connect(db_path)
        conn2.row_factory = sqlite3.Row
        tables = {row[0] for row in conn2.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "restaurants" in tables:
            known |= {row["id"] for row in conn2.execute("SELECT id FROM restaurants")}
        conn2.close()
    except Exception:
        pass
    for slug in sorted(known):
        run("INSERT INTO tenants (slug, restaurant_id) SELECT %s, restaurant_id FROM tenants WHERE slug=%s ON CONFLICT (slug) DO NOTHING",
            (slug, RESTAURANT["id"]))
    conn.commit()

    # Custom ingredients first (drafts/purchases reference catalog ids only in JSON).
    try:
        upsert("custom_ingredients",
               ["id", "slug", "name", "unit", "reorder_threshold", "created_at"],
               [{"id": row["id"], "slug": row.get("restaurant_id") or RESTAURANT["id"], "name": row["name"],
                 "unit": row["unit"], "reorder_threshold": row["reorder_threshold"], "created_at": row["created_at"]}
                for row in _sqlite_rows(db_path, "custom_ingredients", RESTAURANT["id"])],
               "id")
    except Exception:
        counts["custom_ingredients"] = 0
    conn.commit()

    upsert("drafts",
           ["id", "slug", "version", "status", "body", "created_at"],
           [{"id": row["id"], "slug": row["restaurant_id"], "version": row["version"], "status": row["status"],
             "body": json.loads(row["body"]), "created_at": row.get("created_at") or json.loads(row["body"]).get("created_at")}
            for row in _sqlite_rows(db_path, "drafts", RESTAURANT["id"])],
           "id")
    conn.commit()

    upsert("purchases",
           ["id", "slug", "draft_id", "supplier", "created_at", "body"],
           [{"id": row["id"], "slug": row["restaurant_id"], "draft_id": row["draft_id"], "supplier": row["supplier"],
             "created_at": row["created_at"], "body": json.loads(row["body"])}
            for row in _sqlite_rows(db_path, "purchases", RESTAURANT["id"])],
           "id")
    conn.commit()

    for row in _sqlite_rows(db_path, "inventory", RESTAURANT["id"]):
        run(
            "INSERT INTO stock (slug, ingredient_id, quantity) VALUES (%s, %s, %s) "
            "ON CONFLICT (slug, ingredient_id) DO UPDATE SET quantity=excluded.quantity",
            (row["restaurant_id"], row["ingredient_id"], row["quantity"]),
        )
    counts["stock"] = sum(1 for _ in _sqlite_rows(db_path, "inventory", RESTAURANT["id"]))
    conn.commit()

    upsert("movements",
           ["id", "slug", "purchase_id", "ingredient_id", "quantity", "created_at"],
           [{"id": row["id"], "slug": row["restaurant_id"], "purchase_id": row["purchase_id"],
             "ingredient_id": row["ingredient_id"], "quantity": row["quantity"], "created_at": row["created_at"]}
            for row in _sqlite_rows(db_path, "movements", RESTAURANT["id"])],
           "id")
    conn.commit()

    upsert("activity",
           ["id", "slug", "event", "message", "created_at"],
           [{"id": row["id"], "slug": row["restaurant_id"], "event": row["event"],
             "message": row["message"], "created_at": row["created_at"]}
            for row in _sqlite_rows(db_path, "activity", RESTAURANT["id"])],
           "id")
    conn.commit()

    upsert("whatsapp_threads",
           ["id", "slug", "channel", "sender", "transport", "active_draft_id", "active_version", "pending_followup"],
           [{"id": row["id"], "slug": row["restaurant_id"], "channel": row["channel"], "sender": row["sender"],
             "transport": row["transport"], "active_draft_id": row["active_draft_id"],
             "active_version": row["active_version"], "pending_followup": row["pending_followup"]}
            for row in _sqlite_rows(db_path, "whatsapp_threads", RESTAURANT["id"])],
           "id")
    conn.commit()

    upsert("whatsapp_messages",
           ["id", "thread_id", "direction", "body", "created_at", "transport", "status", "draft_id"],
           [{"id": row["id"], "thread_id": row["thread_id"], "direction": row["direction"], "body": row["body"],
             "created_at": row["created_at"], "transport": row["transport"], "status": row["status"],
             "draft_id": row["draft_id"]}
            for row in _sqlite_rows(db_path, "whatsapp_messages")],
           "id")
    conn.commit()

    for row in _sqlite_rows(db_path, "whatsapp_events"):
        run(
            "INSERT INTO whatsapp_events (channel, event_id, thread_id, fingerprint, response) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (channel, event_id) DO UPDATE SET response=excluded.response",
            (row["channel"], row["event_id"], row["thread_id"], row["fingerprint"], row["response"]),
        )
    counts["whatsapp_events"] = sum(1 for _ in _sqlite_rows(db_path, "whatsapp_events"))
    conn.commit()

    upsert("idempotency",
           ["key", "fingerprint", "response"],
           [{"key": row["key"], "fingerprint": row["fingerprint"], "response": json.loads(row["response"])}
            for row in _sqlite_rows(db_path, "idempotency")],
           "key")
    conn.commit()

    conn.close()
    print("Migration complete:", ", ".join(f"{table}={count}" for table, count in counts.items()))
    print(f"SQLite file kept untouched at {db_path}.")


def main():
    parser = argparse.ArgumentParser(description="Copy the local SQLite authority into Supabase Postgres")
    parser.add_argument("--db", type=Path, default=Path("data/restopilot.sqlite3"))
    args = parser.parse_args()
    migrate(str(args.db))


if __name__ == "__main__":
    main()
