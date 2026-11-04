-- Multi-platform channel links, agent rules, proactive alerts, and
-- physical stock counts: the Postgres-side schema for the features that
-- shipped on the SQLite authority (chat stock opname, owner rules,
-- proactive low-stock alerts, platform-aware channel links).

-- One phone may serve WhatsApp and Telegram; the same digits may belong
-- to different tenants on different platforms.
alter table public.whatsapp_links drop constraint if exists whatsapp_links_phone_key;
alter table public.whatsapp_links add column if not exists platform text not null default 'whatsapp';
alter table public.whatsapp_links drop constraint if exists whatsapp_links_phone_platform_key;
alter table public.whatsapp_links
    add constraint whatsapp_links_phone_platform_key unique (phone, platform);

-- A physical count proposal waiting on the owner's YA/BATAL.
alter table public.whatsapp_threads
    add column if not exists pending_opname_id text;

-- Per-tenant rules the assistant obeys (threshold overrides, mutes,
-- phrasing context). Scoped by tenant; never shared across restaurants.
create table if not exists public.restaurant_rules (
    id         uuid primary key default gen_random_uuid(),
    slug       text not null references public.tenants (slug) on delete cascade,
    rule       text not null,
    enabled    boolean not null default true,
    created_at timestamptz not null default now()
);
create index if not exists idx_restaurant_rules_slug
    on public.restaurant_rules (slug, created_at);

-- Cooldown ledger for proactive low-stock alerts: an ingredient re-alerts
-- only when its quantity changed and the previous alert is older than the
-- sweep interval.
create table if not exists public.proactive_alerts (
    slug            text not null references public.tenants (slug) on delete cascade,
    ingredient_id   text not null,
    last_alerted_at timestamptz not null,
    last_quantity   text not null,
    primary key (slug, ingredient_id)
);

-- Applied physical counts (the audit trail behind the inventory history).
create table if not exists public.stock_counts (
    id                 uuid primary key default gen_random_uuid(),
    group_id           text not null,
    slug               text not null references public.tenants (slug) on delete cascade,
    ingredient_id      text not null,
    estimated_quantity text not null,
    counted_quantity   text not null,
    delta              text not null,
    status             text not null default 'pending',
    source             text not null default 'chat',
    created_at         timestamptz not null default now(),
    applied_at         timestamptz
);
create index if not exists idx_stock_counts_slug
    on public.stock_counts (slug, created_at);
