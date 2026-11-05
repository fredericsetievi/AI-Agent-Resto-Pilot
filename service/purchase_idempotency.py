"""Tenant-scoped purchase retries, including safe replay of pre-scope caches."""

import hashlib

from .inventory import body


def purchase_idempotent(store, key, operation, payload, action, restaurant_id):
    from .db import DomainError, encode

    # Hash the tuple so separators in a client-controlled key cannot create
    # another restaurant's namespace. Keep one key across purchase operations
    # so changing the request still produces the existing conflict response.
    scoped_key = "purchase:" + hashlib.sha256(encode([restaurant_id, key]).encode()).hexdigest()
    scoped_payload = {"restaurant_id": restaurant_id, "request": payload}
    legacy_fingerprint = hashlib.sha256(encode({"operation": operation, "payload": payload}).encode()).hexdigest()

    def checked(conn):
        # Old releases stored raw keys globally. Preserve an owner's retries
        # without trusting a cached body to assert its own restaurant identity.
        if store.inventory_postgres:
            # Wait for an in-flight pre-fix request to commit its legacy cache
            # before deciding whether this retry needs a new action.
            store._inventory_query(conn, "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))", (key,))
        legacy = store._inventory_query(conn, "SELECT fingerprint,response FROM idempotency WHERE key=?", (key,)).fetchone()
        if legacy and restaurant_id:
            response = body(legacy[1])
            draft_id = response.get("id") if isinstance(response, dict) else None
            column = "slug" if store.inventory_postgres else "restaurant_id"
            owner = store._inventory_query(conn, f"SELECT {column} FROM drafts WHERE id=?", (draft_id,)).fetchone()
            if owner and owner[0] == restaurant_id:
                if legacy[0] != legacy_fingerprint:
                    raise DomainError("idempotency_conflict", "This idempotency key was already used for a different request.")
                return response
        return action(conn)

    return store._idempotent(scoped_key, operation, scoped_payload, checked)
