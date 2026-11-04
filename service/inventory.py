"""Reviewed inventory operations shared by SQLite and PostgreSQL authorities.

Forecasts use explicit owner-supplied daily consumption rates, never purchase
frequency or incompletely observed days. All stock arithmetic uses Decimal.
"""
import json
import re
import uuid
from decimal import Decimal, InvalidOperation
from datetime import date, datetime


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
            if not isinstance(item, dict):
                invalid("Each ingredient line must be an object.")
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

    @staticmethod
    def _inventory_business_date(value=None):
        from .db import RESTAURANT_TIMEZONE
        today = datetime.now(RESTAURANT_TIMEZONE).date()
        if value is None:
            return today.isoformat()
        try:
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError()
            parsed = date.fromisoformat(value)
            if parsed > today:
                raise ValueError()
            return parsed.isoformat()
        except ValueError:
            invalid("Use a valid business date YYYY-MM-DD no later than today in Asia/Jakarta.")

    @staticmethod
    def _inventory_event_date(event):
        from .db import RESTAURANT_TIMEZONE
        from datetime import timezone
        if event.get("business_date"):
            return event["business_date"]
        stamp = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(RESTAURANT_TIMEZONE).date().isoformat()

    def _prepare_inventory_batch_conn(self, conn, payloads, restaurant_id, *, posting=False):
        """Validate and simulate the complete batch before posting any movement.

        Preview does SELECTs only. Posting locks recipes before the sorted union
        of stock rows, using the same stock lock order as incoming purchases.
        The caller owns commit/rollback and its own approval idempotency key.
        """
        from .db import decimal_string
        self._inventory_schema(conn)
        if not isinstance(payloads, list) or not 1 <= len(payloads) <= 100:
            invalid("Provide 1 to 100 inventory operations.")
        recipes = {}
        recipe_ids = set()
        for payload in payloads:
            if not isinstance(payload, dict):
                invalid("Each inventory operation must be an object.")
            if payload.get("recipe_id"):
                if not isinstance(payload["recipe_id"], str):
                    invalid("Choose a saved recipe from this restaurant.")
                recipe_ids.add(payload["recipe_id"])
        for recipe_id in sorted(recipe_ids):
            suffix = " FOR SHARE" if self.inventory_postgres and posting else ""
            row = self._inventory_query(conn, "SELECT body FROM inventory_recipes WHERE restaurant_id=? AND id=?" + suffix, (restaurant_id, recipe_id)).fetchone()
            if not row:
                invalid("Recipe not found in this restaurant.", "not_found", 404)
            recipes[recipe_id] = body(row[0])
        prepared = []
        for payload in payloads:
            kind = payload.get("kind")
            if kind not in {"usage", "waste", "count", "sale"} or (posting and payload.get("confirmed") is not True):
                invalid("Choose an operation and explicitly confirm its stock effect.")
            note = str(payload.get("note") or "").strip()
            if not note or len(note) > 500:
                invalid("Describe the entry in 1 to 500 characters.")
            revenue = payload.get("revenue_idr")
            if revenue is not None and (kind != "sale" or isinstance(revenue, bool) or not isinstance(revenue, int) or not 0 <= revenue <= 1_000_000_000_000):
                invalid("Sales revenue must be nonnegative whole rupiah.")
            if kind != "sale" and (payload.get("recipe_id") or payload.get("portions") is not None):
                invalid("Recipes and portions apply only to sales.")
            history_only = payload.get("history_only", False)
            if not isinstance(history_only, bool) or (history_only and kind == "count"):
                invalid("History-only recording applies to consumption or sales, never a physical count.")
            event = {"kind": kind, "note": note, "business_date": self._inventory_business_date(payload.get("business_date")), "items": []}
            if history_only:
                event["history_only"] = True
            for field in ("operation_id", "source_id", "report_id", "source_digest", "coverage"):
                if payload.get(field) is not None:
                    value = payload[field]
                    if not isinstance(value, str) or not value.strip() or len(value) > 500:
                        invalid("Source references must contain 1 to 500 characters.")
                    if field == "source_digest" and not re.fullmatch(r"[0-9a-fA-F]{64}", value):
                        invalid("Source digest must be a SHA-256 hexadecimal digest.")
                    event[field] = value
            items = payload.get("items") or []
            if kind == "sale":
                event["revenue_idr"] = revenue
                if payload.get("recipe_id"):
                    if items:
                        invalid("Choose recipe consumption or explicit usage, not both.")
                    recipe = recipes[payload["recipe_id"]]
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
            prepared.append((event, lines))
        ingredients = sorted({ingredient for _, lines in prepared for ingredient, _, _ in lines})
        stock_effects = {ingredient for event, lines in prepared if not event.get("history_only") for ingredient, _, _ in lines}
        table, tenant = ("stock", "slug") if self.inventory_postgres else ("inventory", "restaurant_id")
        balances = {}
        for ingredient in ingredients:
            if posting and ingredient in stock_effects:
                balances[ingredient] = self._inventory_balance(conn, restaurant_id, ingredient)
            else:
                suffix = " FOR UPDATE" if self.inventory_postgres and posting else ""
                row = self._inventory_query(conn, f"SELECT quantity FROM {table} WHERE {tenant}=? AND ingredient_id=?" + suffix, (restaurant_id, ingredient)).fetchone()
                balances[ingredient] = Decimal(row[0]) if row else Decimal(0)
        original_balances = dict(balances)
        # Query after acquiring stock locks so concurrent physical counts cannot
        # disappear between reconciliation validation and posting.
        counts = [body(row[0]) for row in self._inventory_query(conn, "SELECT body FROM inventory_events WHERE restaurant_id=?", (restaurant_id,)).fetchall()]
        latest_counts = {}
        for count in counts:
            if count["kind"] == "count":
                for item in count["items"]:
                    ingredient = item["ingredient_id"]
                    latest_counts[ingredient] = max(latest_counts.get(ingredient, ""), self._inventory_event_date(count))
        counted = set()
        # Counts reconcile the final state, regardless of their input position.
        for event, lines in sorted(prepared, key=lambda entry: entry[0]["kind"] == "count"):
            kind = event["kind"]
            for ingredient, amount, original in sorted(lines, key=lambda item: item[0]):
                before = balances[ingredient]
                if kind == "count":
                    if ingredient in counted:
                        invalid("Include only one final physical count per ingredient in a batch.")
                    counted.add(ingredient)
                    if original.get("expected_quantity") is None or quantity(original["expected_quantity"], zero=True) != original_balances[ingredient]:
                        invalid("Stock changed since this count was prepared. Refresh and review the count.", "stock_conflict", 409)
                    after = amount
                elif event.get("history_only"):
                    after = before
                else:
                    if latest_counts.get(ingredient, "") > event["business_date"]:
                        invalid("A later physical count may already include this consumption. Review history-only recording or reconcile stock explicitly.", "reconciliation_required", 409)
                    after = before - amount
                if after < 0:
                    invalid("Recorded consumption exceeds estimated stock. Reconcile a physical count first.", "insufficient_stock", 409)
                delta = after - before
                line = {"ingredient_id": ingredient, "quantity": decimal_string(amount), "delta": decimal_string(delta), "before": decimal_string(before), "after": decimal_string(after)}
                if event.get("history_only"):
                    line["planned_quantity"] = decimal_string(amount)
                event["items"].append(line)
                balances[ingredient] = after
        return [event for event, _ in prepared]

    def _preview_inventory_batch_conn(self, conn, payloads, restaurant_id):
        return self._prepare_inventory_batch_conn(conn, payloads, restaurant_id)

    def _post_inventory_batch_conn(self, conn, payloads, restaurant_id, *, expected_preview=None):
        from .db import encode, now
        events = self._prepare_inventory_batch_conn(conn, payloads, restaurant_id, posting=True)
        if expected_preview is not None and events != expected_preview:
            invalid("Stock or reviewed calculations changed. Review a fresh preview before confirming.", "reviewed_stock_conflict", 409)
        table, tenant = ("stock", "slug") if self.inventory_postgres else ("inventory", "restaurant_id")
        for event in sorted(events, key=lambda item: item["kind"] == "count"):
            event.update(id=uuid.uuid4().hex, created_at=now())
            for item in event["items"]:
                if event.get("history_only"):
                    continue
                self._inventory_query(conn, f"UPDATE {table} SET quantity=? WHERE {tenant}=? AND ingredient_id=?", (item["after"], restaurant_id, item["ingredient_id"]))
                self._inventory_query(conn, f"INSERT INTO movements (id,{tenant},purchase_id,ingredient_id,quantity,created_at,inventory_event_id) VALUES (?,?,NULL,?,?,?,?)", (uuid.uuid4().hex, restaurant_id, item["ingredient_id"], item["delta"], event["created_at"], event["id"]))
            self._inventory_query(conn, "INSERT INTO inventory_events(id,restaurant_id,body,created_at) VALUES (?,?,?,?)", (event["id"], restaurant_id, encode(event), event["created_at"]))
            self._audit(conn, "inventory_" + event["kind"], f"{event['kind'].capitalize()} recorded: {event['note']} (entry {event['id']}).", restaurant_id)
        return events

    def record_inventory_event(self, key, payload, restaurant_id):
        def action(conn):
            return self._post_inventory_batch_conn(conn, [payload], restaurant_id)[0]
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
        with self.transaction() as conn:
            return self._inventory_workspace_conn(conn, restaurant_id)

    def _inventory_workspace_conn(self, conn, restaurant_id):
        from .db import decimal_string
        self._inventory_schema(conn)
        recipes = [body(row[0]) for row in self._inventory_query(conn, "SELECT body FROM inventory_recipes WHERE restaurant_id=? ORDER BY id", (restaurant_id,)).fetchall()]
        events = [body(row[0]) for row in self._inventory_query(conn, "SELECT body FROM inventory_events WHERE restaurant_id=? ORDER BY created_at DESC,id DESC LIMIT 100", (restaurant_id,)).fetchall()]
        row = self._inventory_query(conn, "SELECT body FROM inventory_settings WHERE restaurant_id=?", (restaurant_id,)).fetchone()
        settings = body(row[0]) if row else {"lead_days": 2, "safety_days": 1, "daily_usage": {}}
        table, tenant = ("stock", "slug") if self.inventory_postgres else ("inventory", "restaurant_id")
        balances = dict(self._inventory_query(conn, f"SELECT ingredient_id,quantity FROM {table} WHERE {tenant}=?", (restaurant_id,)).fetchall())
        history = []
        if settings.get("history_complete"):
            # A report posted today may cover yesterday. Inspect business dates
            # over the complete tenant history, never the 100-row UI window.
            history = [body(row[0]) for row in self._inventory_query(conn, "SELECT body FROM inventory_events WHERE restaurant_id=?", (restaurant_id,)).fetchall()]
            history = [event for event in history if settings["history_start"] <= self._inventory_event_date(event) <= settings["history_end"]]
        recorded = {}
        for event in history:
            if event["kind"] in {"usage", "waste", "sale"}:
                for item in event["items"]:
                    consumed = Decimal(item.get("planned_quantity", "0")) if event.get("history_only") else -Decimal(item["delta"])
                    if consumed > 0:
                        recorded[item["ingredient_id"]] = recorded.get(item["ingredient_id"], Decimal(0)) + consumed
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
