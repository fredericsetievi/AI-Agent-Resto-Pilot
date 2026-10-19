-- RestoPilot migration 0002: fields required by user-research findings.
-- Sources: F&B owner interviews — stock alerts, category profitability,
-- budget cross-check. All additions are nullable backfill-friendly columns;
-- no existing data is rewritten.

-- Daily spending budget the owner wants purchases cross-checked against.
-- Nullable until the owner sets one.
alter table public.restaurants
  add column if not exists daily_budget bigint;

-- Reorder threshold for low-stock alerts.
-- Nullable: not every item has one yet.
alter table public.inventory
  add column if not exists min_stock numeric(12, 3);

-- Category labels for per-category profit analysis. Applied to both the
-- cloud mirror of purchases and the live stock view.
alter table public.invoice_items
  add column if not exists category text;

alter table public.inventory
  add column if not exists category text;

-- Helpful for category rollups once data exists.
create index if not exists invoice_items_category_idx
  on public.invoice_items (category);
