-- RestoPilot baseline schema for the Supabase (PostgreSQL) cloud data path.
--
-- Conventions (per restopilot-technical-plan.md, "Minimum data model"):
--   * opaque UUID primary keys, UTC timestamptz timestamps
--   * IDR money as integer rupiah (bigint); unit prices and quantities as
--     numeric (never binary floats), so derived unit prices are not rounded
--     before price-spike comparison
--   * uncertain/ambiguous extracted fields stay NULL rather than guessed
--   * row level security enabled with no public policies: anon/authenticated
--     keys are denied by default and only the server-side service-role key
--     (used by service/supabase_db.py) may read or write these tables

create extension if not exists pgcrypto;

create table if not exists restaurants (
    id           uuid primary key default gen_random_uuid(),
    name         text not null,
    phone_number text not null unique,
    created_at   timestamptz not null default now()
);

create table if not exists invoices (
    id            uuid primary key default gen_random_uuid(),
    restaurant_id uuid not null references restaurants (id) on delete cascade,
    supplier_name text,
    raw_image_url text,
    status        text not null default 'draft'
                  check (status in ('draft', 'approved', 'flagged')),
    total_amount  bigint check (total_amount is null or total_amount >= 0),
    created_at    timestamptz not null default now()
);

create table if not exists invoice_items (
    id             uuid primary key default gen_random_uuid(),
    invoice_id     uuid not null references invoices (id) on delete cascade,
    item_name      text not null,
    quantity       numeric(12, 3) check (quantity is null or quantity > 0),
    unit           text,
    unit_price     numeric(14, 4) check (unit_price is null or unit_price >= 0),
    total_price    bigint check (total_price is null or total_price >= 0),
    price_spike_pct numeric(6, 2),
    created_at     timestamptz not null default now()
);

create table if not exists inventory (
    id              uuid primary key default gen_random_uuid(),
    restaurant_id   uuid not null references restaurants (id) on delete cascade,
    item_name       text not null,
    current_stock   numeric(12, 3) not null default 0 check (current_stock >= 0),
    unit            text not null,
    last_unit_price numeric(14, 4) check (last_unit_price is null or last_unit_price >= 0),
    updated_at      timestamptz not null default now(),
    unique (restaurant_id, item_name)
);

create index if not exists idx_invoices_restaurant_created
    on invoices (restaurant_id, created_at desc);

create index if not exists idx_invoice_items_invoice
    on invoice_items (invoice_id);

create index if not exists idx_invoice_items_name
    on invoice_items (item_name);

-- Keep updated_at truthful on every inventory mutation.
create or replace function restopilot_set_updated_at() returns trigger
language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists trg_inventory_updated_at on inventory;
create trigger trg_inventory_updated_at
    before update on inventory
    for each row execute function restopilot_set_updated_at();

-- Deny-by-default for anon/authenticated keys; the service role bypasses RLS.
alter table restaurants    enable row level security;
alter table invoices       enable row level security;
alter table invoice_items  enable row level security;
alter table inventory      enable row level security;
