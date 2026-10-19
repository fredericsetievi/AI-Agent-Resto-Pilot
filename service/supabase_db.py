"""Supabase PostgreSQL client for RestoPilot's cloud data path.

Reads SUPABASE_URL and SUPABASE_SERVICE_KEY from the process environment or a
local .env file (.env is git-ignored; see .env.example for placeholders).

The service-role key bypasses row level security, so this client is strictly
server-side: keep it out of browser bundles, logs, and any code path that
handles untrusted input. Direct client-side table mutations stay disabled
(row level security is enabled with no public policies in the migrations).

The local SQLite demo (service.db.Store) remains the default data path until
routes are explicitly wired to this client.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from supabase import Client, create_client

from .db import RESTAURANT

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

# Deterministic tenant UUID so repeated mirrors upsert onto the same row.
DEMO_RESTAURANT_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, f"restopilot:{RESTAURANT['id']}"))


@lru_cache(maxsize=1)
def get_supabase_client() -> Client:
    """Return the shared service-role client, or fail fast if unconfigured."""
    load_dotenv(ENV_PATH)
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError(
            "Supabase is not configured: set SUPABASE_URL and SUPABASE_SERVICE_KEY "
            "in the environment or a local .env file (see .env.example)."
        )
    return create_client(url, key)


def mirror_enabled() -> bool:
    """The cloud mirror is opt-in: set SUPABASE_MIRROR=1 in .env or the environment."""
    load_dotenv(ENV_PATH)
    return os.environ.get("SUPABASE_MIRROR", "").strip() == "1"


def _item_uuid(purchase_id: str, index: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"restopilot:item:{purchase_id}:{index}"))


def build_mirror_payloads(purchase: dict) -> dict:
    """Map a recorded purchase (service.db purchase body) onto the cloud schema.

    Pure function so tests can verify the mapping without any network access.
    Uncertain extraction fields stay NULL; price_spike_pct is set only when the
    recorded comparison evidence says "price_increase".
    """
    invoice = {
        "id": str(uuid.UUID(purchase["id"])),
        "restaurant_id": DEMO_RESTAURANT_ID,
        "supplier_name": purchase.get("supplier"),
        "raw_image_url": None,
        "status": "approved",
        "total_amount": purchase.get("total_idr"),
        "created_at": purchase["created_at"],
    }
    items, stock_deltas, last_prices = [], {}, {}
    for index, item in enumerate(purchase.get("items", [])):
        comparison = item.get("comparison") or {}
        items.append({
            "id": _item_uuid(purchase["id"], index),
            "invoice_id": invoice["id"],
            "item_name": item.get("raw_name") or item.get("ingredient_id"),
            "quantity": item.get("canonical_quantity"),
            "unit": item.get("canonical_unit"),
            "unit_price": item.get("unit_price_idr"),
            "total_price": item.get("line_total_idr"),
            "price_spike_pct": comparison.get("change_percent") if comparison.get("status") == "price_increase" else None,
        })
        name, unit = items[-1]["item_name"], item.get("canonical_unit")
        quantity = item.get("canonical_quantity")
        if name and unit and quantity:
            key = (name, unit)
            stock_deltas[key] = stock_deltas.get(key, Decimal(0)) + Decimal(quantity)
            if item.get("unit_price_idr"):
                last_prices[key] = item["unit_price_idr"]
    inventory = [
        {"restaurant_id": DEMO_RESTAURANT_ID, "item_name": name, "unit": unit,
         "_quantity": str(delta), "last_unit_price": last_prices.get((name, unit))}
        for (name, unit), delta in stock_deltas.items()
    ]
    return {
        "restaurant": {"id": DEMO_RESTAURANT_ID, "name": RESTAURANT["name"], "phone_number": f"demo+{RESTAURANT['id']}"},
        "invoice": invoice,
        "items": items,
        "inventory": inventory,
    }


def mirror_purchase(purchase: dict) -> dict:
    """Best-effort cloud mirror of one recorded purchase. Never raises."""
    try:
        sb = get_supabase_client()
        payloads = build_mirror_payloads(purchase)
        sb.table("restaurants").upsert(payloads["restaurant"]).execute()
        # Exactly-once stock effect: if the invoice was already mirrored, stop here.
        # Note: maybe_single().execute() returns None itself when zero rows match.
        existing = sb.table("invoices").select("id").eq("id", payloads["invoice"]["id"]).maybe_single().execute()
        if existing is not None and getattr(existing, "data", None):
            return {"status": "already_mirrored", "invoice_id": payloads["invoice"]["id"]}
        sb.table("invoices").upsert(payloads["invoice"]).execute()
        if payloads["items"]:
            sb.table("invoice_items").upsert(payloads["items"]).execute()
        for row in payloads["inventory"]:
            current = sb.table("inventory").select("id, current_stock") \
                .eq("restaurant_id", row["restaurant_id"]).eq("item_name", row["item_name"]) \
                .maybe_single().execute()
            current = current.data if current is not None else None
            if current:
                new_stock = Decimal(current["current_stock"]) + Decimal(row["_quantity"])
                sb.table("inventory").update(
                    {"current_stock": format(new_stock.normalize(), "f"), "last_unit_price": row["last_unit_price"]}
                ).eq("id", current["id"]).execute()
            else:
                sb.table("inventory").insert({
                    "restaurant_id": row["restaurant_id"], "item_name": row["item_name"],
                    "current_stock": row["_quantity"], "unit": row["unit"],
                    "last_unit_price": row["last_unit_price"],
                }).execute()
        return {"status": "mirrored", "invoice_id": payloads["invoice"]["id"]}
    except Exception as error:  # noqa: BLE001 - mirror failures must not break the demo
        return {"status": "failed", "error": str(error)[:300]}
