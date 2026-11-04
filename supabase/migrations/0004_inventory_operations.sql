-- Additive inventory operations. Existing purchases and balances are preserved.
-- Apply explicitly before enabling inventory tools on the hosted app.
begin;
create table if not exists public.inventory_events (
    id text primary key,
    restaurant_id text not null references public.tenants(slug),
    body jsonb not null,
    created_at timestamptz not null
);
create index if not exists inventory_events_tenant on public.inventory_events(restaurant_id, created_at desc);
create table if not exists public.inventory_recipes (
    id text not null,
    restaurant_id text not null references public.tenants(slug),
    body jsonb not null,
    primary key (restaurant_id,id)
);
create table if not exists public.inventory_settings (
    restaurant_id text primary key references public.tenants(slug),
    body jsonb not null
);
-- Deferred reference allows movements and their event to commit atomically.
alter table public.movements add column if not exists inventory_event_id text
    references public.inventory_events(id) deferrable initially deferred;
create index if not exists movements_inventory_event on public.movements(inventory_event_id);
alter table public.inventory_events enable row level security;
alter table public.inventory_recipes enable row level security;
alter table public.inventory_settings enable row level security;
commit;
