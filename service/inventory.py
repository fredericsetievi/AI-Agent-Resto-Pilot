"""Reviewed inventory operations shared by SQLite and PostgreSQL authorities.

Forecasts use explicit owner-supplied daily consumption rates, never purchase
frequency or incompletely observed days. All stock arithmetic uses Decimal.
"""
import json
import uuid
from decimal import Decimal, InvalidOperation
from datetime import date, datetime, timedelta


SQLITE_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS inventory_events (id TEXT PRIMARY KEY, restaurant_id TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS inventory_events_tenant ON inventory_events(restaurant_id, created_at)",
    "CREATE TABLE IF NOT EXISTS inventory_recipes (id TEXT NOT NULL, restaurant_id TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(restaurant_id,id))",
    "CREATE TABLE IF NOT EXISTS inventory_settings (restaurant_id TEXT PRIMARY KEY, body TEXT NOT NULL)",
)


def invalid(message, code="invalid_inventory", status=422):
    from .db import DomainError
    raise DomainError(code, message, status)


def quantity(value, *, zero=False):
    try:
        if isinstance(value, bool) or len(str(value)) > 40:
            raise ValueError()
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or (number == 0 and not zero) or number > Decimal("1000000000") or number.as_tuple().exponent < -9:
            raise ValueError()
        return number
    except (InvalidOperation, ValueError, TypeError):
        invalid("Use a finite quantity between " + ("0" if zero else "greater than 0") + " and 1,000,000,000, with at most 9 decimal places.")


def body(value):
    return json.loads(value) if isinstance(value, str) else value


class InventoryMixin:
    inventory_postgres = False

    def _inventory_query(self, conn, sql, params=()):
        if self.inventory_postgres:
            return self._execute(conn, sql.replace("?", "%s"), params)
        return conn.execute(sql, params)

    def _inventory_schema(self, conn):
        if self.inventory_postgres:
            row = self._inventory_query(conn, "SELECT to_regclass('public.inventory_events'), to_regclass('public.inventory_recipes'), to_regclass('public.inventory_settings'), EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='movements' AND column_name='inventory_event_id')").fetchone()
            if not all(row):
                invalid("Inventory operations require database migration 0004_inventory_operations.sql.", "migration_required", 503)

    def _inventory_transaction(self, key, operation, payload, restaurant_id, action):
        # The namespace prevents cross-tenant replay, even with the same client key.
        def checked(conn):
            self._inventory_schema(conn)
            return action(conn)
        return self._idempotent(f"inventory:{restaurant_id}:{key}", operation + ":" + restaurant_id, payload, checked)

    def _inventory_lines(self, items, restaurant_id, *, zero=False, packaging=True):
        if not isinstance(items, list) or not 1 <= len(items) <= 100:
            invalid("Provide 1 to 100 ingredient lines.")
        catalog = self._catalog_for(restaurant_id)["by_id"]
        result, seen = [], set()
        for item in items:
            ingredient = item.get("ingredient_id")
            if ingredient not in catalog:
                invalid("Select an ingredient from this restaurant.")
            if ingredient in seen:
                invalid("Each ingredient can appear only once.")
            seen.add(ingredient)
            amount = quantity(item.get("quantity"), zero=zero)
            size = quantity(item.get("pack_size", "1")) if packaging else Decimal(1)
            total = quantity(amount * size, zero=zero)
            result.append((ingredient, total, item))
        return result

    def _inventory_balance(self, conn, restaurant_id, ingredient):
        table, tenant = ("stock", "slug") if self.inventory_postgres else ("inventory", "restaurant_id")
        self._inventory_query(conn, f"INSERT INTO {table} ({tenant},ingredient_id,quantity) VALUES (?,?,'0') ON CONFLICT DO NOTHING", (restaurant_id, ingredient))
        suffix = " FOR UPDATE" if self.inventory_postgres else ""
        row = self._inventory_query(conn, f"SELECT quantity FROM {table} WHERE {tenant}=? AND ingredient_id=?" + suffix, (restaurant_id, ingredient)).fetchone()
        return Decimal(row[0])

    def record_inventory_event(self, key, payload, restaurant_id):
        from .db import decimal_string, encode, now
        kind = payload.get("kind")
        if kind not in {"usage", "waste", "count", "sale"} or payload.get("confirmed") is not True:
            invalid("Choose an operation and explicitly confirm its stock effect.")
        note = str(payload.get("note") or "").strip()
        if not note or len(note) > 500:
            invalid("Describe the entry in 1 to 500 characters.")
        revenue = payload.get("revenue_idr")
        if revenue is not None and (kind != "sale" or isinstance(revenue, bool) or not isinstance(revenue, int) or not 0 <= revenue <= 1_000_000_000_000):
            invalid("Sales revenue must be nonnegative whole rupiah.")
        if kind != "sale" and (payload.get("recipe_id") or payload.get("portions") is not None):
            invalid("Recipes and portions apply only to sales.")

        def action(conn):
            event = {"id": uuid.uuid4().hex, "kind": kind, "note": note, "created_at": now(), "items": []}
            items = payload.get("items") or []
            if kind == "sale":
                event["revenue_idr"] = revenue
                if payload.get("recipe_id"):
                    if items:
                        invalid("Choose recipe consumption or explicit usage, not both.")
                    row = self._inventory_query(conn, "SELECT body FROM inventory_recipes WHERE restaurant_id=? AND id=?", (restaurant_id, payload["recipe_id"])).fetchone()
                    if not row:
                        invalid("Recipe not found in this restaurant.", "not_found", 404)
                    recipe = body(row[0])
                    if payload.get("expected_recipe_version") != recipe.get("version", 1):
                        invalid("This recipe changed since the sale was prepared. Reopen the sale and review its quantities.", "recipe_conflict", 409)
                    portions = quantity(payload.get("portions"))
                    items = [{"ingredient_id": item["ingredient_id"], "quantity": decimal_string(quantity(item["quantity"]) * portions)} for item in recipe["items"]]
                    event.update(recipe_id=recipe["id"], recipe=recipe, portions=decimal_string(portions))
                elif payload.get("portions") is not None:
                    event["portions"] = decimal_string(quantity(payload["portions"]))
                if not items and revenue is None and "portions" not in event:
                    invalid("A sale needs a sold quantity, revenue, or ingredient consumption.")
            lines = self._inventory_lines(items, restaurant_id, zero=kind == "count") if items else []
            if not lines and kind != "sale":
                invalid("Provide at least one ingredient.")
            table, tenant = ("stock", "slug") if self.inventory_postgres else ("inventory", "restaurant_id")
            # Identical lock order to receipt posting prevents deadlocks and lost updates.
            for ingredient, amount, original in sorted(lines, key=lambda item: item[0]):
                before = self._inventory_balance(conn, restaurant_id, ingredient)
                if kind == "count":
                    if original.get("expected_quantity") is None or quantity(original["expected_quantity"], zero=True) != before:
                        invalid("Stock changed since this count was prepared. Refresh and review the count.", "stock_conflict", 409)
                    after = amount
                else:
                    after = before - amount
                if after < 0:
                    invalid("Recorded consumption exceeds estimated stock. Reconcile a physical count first.", "insufficient_stock", 409)
                delta = after - before
                self._inventory_query(conn, f"UPDATE {table} SET quantity=? WHERE {tenant}=? AND ingredient_id=?", (decimal_string(after), restaurant_id, ingredient))
                self._inventory_query(conn, f"INSERT INTO movements (id,{tenant},purchase_id,ingredient_id,quantity,created_at,inventory_event_id) VALUES (?,?,NULL,?,?,?,?)", (uuid.uuid4().hex, restaurant_id, ingredient, decimal_string(delta), event["created_at"], event["id"]))
                event["items"].append({"ingredient_id": ingredient, "quantity": decimal_string(amount), "delta": decimal_string(delta), "before": decimal_string(before), "after": decimal_string(after)})
            self._inventory_query(conn, "INSERT INTO inventory_events(id,restaurant_id,body,created_at) VALUES (?,?,?,?)", (event["id"], restaurant_id, encode(event), event["created_at"]))
            self._audit(conn, "inventory_" + kind, f"{kind.capitalize()} recorded: {note} (entry {event['id']}).", restaurant_id)
            return event
        return self._inventory_transaction(key, "inventory_event", payload, restaurant_id, action)

    def save_inventory_recipe(self, key, payload, restaurant_id):
        from .db import decimal_string, encode
        name = str(payload.get("name") or "").strip()
        if not 1 <= len(name) <= 200:
            invalid("Recipe name must contain 1 to 200 characters.")
        lines = self._inventory_lines(payload.get("items"), restaurant_id, packaging=False)
        def action(conn):
            recipe_id = payload.get("id") or uuid.uuid4().hex
            version = 1
            if payload.get("id"):
                suffix = " FOR UPDATE" if self.inventory_postgres else ""
                existing = self._inventory_query(conn, "SELECT body FROM inventory_recipes WHERE restaurant_id=? AND id=?" + suffix, (restaurant_id, recipe_id)).fetchone()
                if not existing:
                    invalid("Recipe not found in this restaurant.", "not_found", 404)
                version = body(existing[0]).get("version", 1) + 1
            recipe = {"id": recipe_id, "version": version, "name": name, "items": [{"ingredient_id": i, "quantity": decimal_string(q)} for i, q, _ in lines]}
            self._inventory_query(conn, "INSERT INTO inventory_recipes(id,restaurant_id,body) VALUES (?,?,?) ON CONFLICT(restaurant_id,id) DO UPDATE SET body=excluded.body", (recipe_id, restaurant_id, encode(recipe)))
            self._audit(conn, "inventory_recipe_saved", f"Recipe saved: {name}.", restaurant_id)
            return recipe
        return self._inventory_transaction(key, "inventory_recipe", payload, restaurant_id, action)

    def save_inventory_settings(self, key, payload, restaurant_id):
        from .db import decimal_string, encode
        settings = {}
        for name, default in (("lead_days", 2), ("safety_days", 1)):
            value = payload.get(name, default)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 365:
                invalid("Lead and safety days must be whole days between 0 and 365.")
            settings[name] = value
        rates = payload.get("daily_usage", {})
        if not isinstance(rates, dict) or len(rates) > 1000:
            invalid("Daily usage must map ingredients to quantities.")
        start, end = payload.get("history_start"), payload.get("history_end")
        if start or end:
            from .db import RESTAURANT_TIMEZONE
            try:
                first, last = date.fromisoformat(start), date.fromisoformat(end)
                days = (last - first).days + 1
                # Today is still incomplete, so only completed local days qualify.
                if not 3 <= days <= 90 or last >= datetime.now(RESTAURANT_TIMEZONE).date():
                    raise ValueError()
            except (ValueError, TypeError):
                invalid("Choose 3 to 90 complete calendar days, ending before today, and confirm all consumption was recorded.")
            if payload.get("history_complete") is not True:
                invalid("Confirm that all consumption was recorded throughout the selected period.")
            settings.update(history_start=first.isoformat(), history_end=last.isoformat(), history_complete=True)
        elif payload.get("history_complete"):
            invalid("Choose both dates for the complete consumption history.")
        catalog = self._catalog_for(restaurant_id)["by_id"]
        settings["daily_usage"] = {}
        for ingredient, rate in rates.items():
            if ingredient not in catalog:
                invalid("Select an ingredient from this restaurant.")
            settings["daily_usage"][ingredient] = decimal_string(quantity(rate, zero=True))
        def action(conn):
            self._inventory_query(conn, "INSERT INTO inventory_settings(restaurant_id,body) VALUES (?,?) ON CONFLICT(restaurant_id) DO UPDATE SET body=excluded.body", (restaurant_id, encode(settings)))
            self._audit(conn, "inventory_settings_saved", "Reorder planning assumptions updated.", restaurant_id)
            return settings
        return self._inventory_transaction(key, "inventory_settings", payload, restaurant_id, action)

    def inventory_workspace(self, restaurant_id):
        from .db import decimal_string
        with self.transaction() as conn:
            self._inventory_schema(conn)
            recipes = [body(row[0]) for row in self._inventory_query(conn, "SELECT body FROM inventory_recipes WHERE restaurant_id=? ORDER BY id", (restaurant_id,)).fetchall()]
            events = [body(row[0]) for row in self._inventory_query(conn, "SELECT body FROM inventory_events WHERE restaurant_id=? ORDER BY created_at DESC,id DESC LIMIT 100", (restaurant_id,)).fetchall()]
            row = self._inventory_query(conn, "SELECT body FROM inventory_settings WHERE restaurant_id=?", (restaurant_id,)).fetchone()
            settings = body(row[0]) if row else {"lead_days": 2, "safety_days": 1, "daily_usage": {}}
            table, tenant = ("stock", "slug") if self.inventory_postgres else ("inventory", "restaurant_id")
            balances = dict(self._inventory_query(conn, f"SELECT ingredient_id,quantity FROM {table} WHERE {tenant}=?", (restaurant_id,)).fetchall())
            history = []
            if settings.get("history_complete"):
                from .db import RESTAURANT_TIMEZONE
                first = date.fromisoformat(settings["history_start"])
                last = date.fromisoformat(settings["history_end"])
                start_time = datetime.combine(first, datetime.min.time(), RESTAURANT_TIMEZONE).isoformat()
                end_time = datetime.combine(last + timedelta(days=1), datetime.min.time(), RESTAURANT_TIMEZONE).isoformat()
                # SQLite stores UTC ISO strings; normalize bounds before comparing.
                from datetime import timezone
                start_time = datetime.fromisoformat(start_time).astimezone(timezone.utc).isoformat()
                end_time = datetime.fromisoformat(end_time).astimezone(timezone.utc).isoformat()
                history = [body(row[0]) for row in self._inventory_query(conn, "SELECT body FROM inventory_events WHERE restaurant_id=? AND created_at>=? AND created_at<?", (restaurant_id, start_time, end_time)).fetchall()]
        recorded = {}
        for event in history:
            if event["kind"] in {"usage", "waste", "sale"}:
                for item in event["items"]:
                    delta = Decimal(item["delta"])
                    if delta < 0:
                        recorded[item["ingredient_id"]] = recorded.get(item["ingredient_id"], Decimal(0)) - delta
        forecasts = []
        for ingredient in self._catalog_for(restaurant_id)["list"]:
            ingredient_id = ingredient["id"]
            rate = Decimal(settings["daily_usage"].get(ingredient_id, "0"))
            forecast = {"ingredient_id": ingredient_id, "daily_usage": None, "days_left": None, "reorder_in_days": None, "status": "insufficient_history", "source": None, "evidence_days": 0}
            source, evidence_days = "manual", 0
            if ingredient_id not in settings["daily_usage"] and recorded.get(ingredient_id, 0) > 0:
                evidence_days = (date.fromisoformat(settings["history_end"]) - date.fromisoformat(settings["history_start"])).days + 1
                rate = recorded[ingredient_id] / evidence_days
                source = "recorded"
                forecast.update(history_start=settings["history_start"], history_end=settings["history_end"])
            if rate > 0:
                days = Decimal(balances.get(ingredient_id, "0")) / rate
                reorder = days - settings["lead_days"] - settings["safety_days"]
                forecast.update(daily_usage=decimal_string(rate), days_left=decimal_string(days.quantize(Decimal("0.01"))), reorder_in_days=decimal_string(max(Decimal(0), reorder).quantize(Decimal("0.01"))), status="reorder_now" if reorder <= 0 else "planned", source=source, evidence_days=evidence_days)
            forecasts.append(forecast)
        return {"recipes": recipes, "events": events, "forecasts": forecasts, "settings": settings}
