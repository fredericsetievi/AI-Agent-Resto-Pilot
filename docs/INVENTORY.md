# Stock usage, sales, and order planning

The Inventory page closes the receipt-to-stock loop. Purchases received and approved through WhatsApp or the dashboard add stock as before. These new owner tools record what happens afterwards; the server stores them in the same restaurant ledger.

## Daily workflow

1. Check starting balances. Use **Count remaining** if the recorded stock differs from what is physically present. Enter the total remaining amount, not the amount used. Counts reconcile the balance and preserve an audit record.
2. Use **Record usage** for ingredients consumed in the kitchen, or **Record waste** for spoilage. Enter a canonical quantity, or an explicit package conversion: `0.5` packages × `12` kg/package removes `6 kg`. The app does not guess a box's contents.
3. If you track menu sales, first create a recipe under **Manage recipes**. Recipe quantities are per sold portion, in each ingredient's stock unit: `0.15 kg` rice per portion, for example. **Log sales** with that recipe and `20` portions deducts `3 kg` rice. Revenue is optional and records sales value, not payment collection or profit.
4. Sales without a recipe can record sold portions and/or revenue while leaving stock unchanged. Record actual kitchen usage separately in this case. With a recipe, do not record the same consumption again as usage. A later physical count can reconcile recipe estimates.
5. Every stock change requires a note and confirmation. History shows balances before and after the entry. Ingredient totals are never silently made negative; a shortage blocks the whole entry until corrected. A count prepared against an old balance is rejected and must be reopened after refreshing.

Recipe edits affect future sales only. A sale captures the reviewed recipe version and is rejected if that recipe changes before confirmation; reopen it to review the new amounts. Existing sales retain their original recipe quantities. Historical entries remain immutable; use a new, explained physical count to correct the stock balance. This version does not reverse sales revenue or calculate profit.

## Reorder estimates

Open **Order planning** to enter supplier lead time and safety days, plus either:

- Your expected daily use for individual ingredients. Blank means unknown; an explicit zero disables a consumption projection for that ingredient.
- A completed history period of 3–90 days ending before today (restaurant time: Asia/Jakarta). Confirm that all consumption throughout the period was recorded. The app averages usage, waste, and recipe/explicit-ingredient sales over **every calendar day** in the period. Physical counts and purchases are excluded.

Manual ingredient estimates override recorded averages. History-based estimates show their coverage dates; update the period as the business changes. Missing/incomplete history is never treated as evidence of demand. A selected period with no consumption for an ingredient yields no projection.

`days left = current stock / daily use`

`order in = max(0, days left - supplier lead days - safety days)`

The dashboard displays **Order now** when the planned buffer is reached. These are estimates; they do not place orders, send automatic WhatsApp reminders, or deduct stock on a timer. Daily recording and periodic counts improve their usefulness. The lead/safety settings currently apply restaurant-wide.

## Deployment and data compatibility

SQLite creates the new tables and movement-reference column additively on startup. Existing stock and purchases are retained.

For Supabase, apply [0004_inventory_operations.sql](../supabase/migrations/0004_inventory_operations.sql) explicitly, then deploy the current `service/` and `frontend/` through the existing Atoms packaging flow. The migration adds three tables (`inventory_events`, `inventory_recipes`, `inventory_settings`) and a nullable `movements.inventory_event_id`; it does not update existing balances, purchases, users, or WhatsApp links. New tables have RLS enabled, with no client policies; only the existing privileged backend connects.

Hosted startup still performs no migrations. Before migration, the new inventory endpoints return `503 migration_required`; existing purchase routes remain usable. Rolling application code back leaves the additive tables and history intact. Do not drop them to roll back the UI.

## API and verification

Owner-only endpoints, scoped by the authenticated restaurant:

- `GET /v1/inventory/workspace`
- `POST /v1/inventory/events` (`usage`, `waste`, `count`, `sale`)
- `POST /v1/inventory/recipes`
- `POST /v1/inventory/settings`

All writes require `Idempotency-Key`. Event changes, signed stock movements, audit activity, and the saved retry response commit atomically. Exact decimal arithmetic is used on the server. PostgreSQL locks stock rows in a consistent order shared with purchase posting, so concurrent receipts and reductions retain both changes.

Run the regular suite with `python -m pytest`. `tests/test_inventory.py` verifies the real isolated SQLite API; `tests/test_inventory_postgres.py` documents a disposable local PostgreSQL setup for real SQL/concurrency tests. The local PostgreSQL tests require an explicit `TEST_LOCAL_PG_DSN` and never read Supabase credentials. Four older live Supabase tests remain opt-in via `TEST_PG=1`.
