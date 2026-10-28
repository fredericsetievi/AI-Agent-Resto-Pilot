-- RestoPilot migration 0003: the application authority moves from the local
-- SQLite file to this PostgreSQL database. One row per restaurant (tenant);
-- every operational table carries restaurant_id. RLS stays deny-by-default:
-- only the server-side service role (and the direct postgres connection used
-- by service/pg_store.py) touches these tables.
--
-- Conventions carried over from 0001: uuid PKs, timestamptz, integer rupiah,
-- numeric quantities, NULL for unknown extracted fields. The SQLite-shaped
-- draft/purchase "body" JSON is kept in jsonb so the application logic that
-- reads and projects drafts is unchanged.

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------------
-- Tenants, logins, sessions, WhatsApp allowlist
-- ---------------------------------------------------------------------------

-- The mirror's restaurants table (0001) is the tenant registry. It already
-- holds id/name/created_at; give it the columns the application needs and a
-- text-keyed mapping so SQLite-era ids ("demo-resto") keep working.
alter table public.restaurants
  add column if not exists slug text unique;
alter table public.restaurants
  add column if not exists address text;

-- One canonical row per application tenant id, mapping to the uuid PK.
create table if not exists public.tenants (
    slug          text primary key,             -- application restaurant id (e.g. 'demo-resto')
    restaurant_id uuid not null unique references public.restaurants (id) on delete cascade,
    created_at    timestamptz not null default now()
);

create table if not exists public.app_users (
    id            uuid primary key default gen_random_uuid(),
    username      text not null unique,
    password_hash text not null,                -- pbkdf2_sha256$iter$salt$digest (service.db.hash_password)
    display_name  text not null,
    role          text not null check (role in ('admin', 'owner')),
    slug          text references public.tenants (slug) on delete cascade,
    created_at    timestamptz not null default now()
);

create table if not exists public.app_sessions (
    token      text primary key,
    user_id    uuid not null references public.app_users (id) on delete cascade,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null
);
create index if not exists idx_app_sessions_expiry on public.app_sessions (expires_at);

-- One WhatsApp number serves exactly one restaurant.
create table if not exists public.whatsapp_links (
    id            uuid primary key default gen_random_uuid(),
    slug          text not null references public.tenants (slug) on delete cascade,
    phone         text not null unique,         -- E.164 digits, no '+'
    label         text,
    created_at    timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Operational ledger (mirrors the SQLite v2 schema)
-- ---------------------------------------------------------------------------

create table if not exists public.custom_ingredients (
    id                text primary key,         -- slug of the typed name
    slug              text not null references public.tenants (slug) on delete cascade,
    name              text not null,
    unit              text not null,
    reorder_threshold text not null default '0',
    created_at        timestamptz not null default now(),
    unique (slug, id)
);

create table if not exists public.drafts (
    id             text primary key,
    slug           text not null references public.tenants (slug) on delete cascade,
    version        integer not null check (version > 0),
    status         text not null,
    body           jsonb not null,
    created_at     timestamptz not null default now()
);
create index if not exists idx_drafts_slug_row on public.drafts (slug, created_at desc);

create table if not exists public.purchases (
    id         text primary key,
    slug       text not null references public.tenants (slug) on delete cascade,
    draft_id   text not null unique references public.drafts (id),
    supplier   text,
    created_at timestamptz not null,
    body       jsonb not null
);
create index if not exists idx_purchases_slug_created on public.purchases (slug, created_at desc);
create index if not exists idx_purchases_slug_supplier on public.purchases (slug, supplier, created_at desc);

-- Stock is per restaurant and per ingredient id (catalog slugs, not names —
-- the 0001 inventory mirror keyed by item_name stays a reporting surface).
create table if not exists public.stock (
    slug          text not null references public.tenants (slug) on delete cascade,
    ingredient_id text not null,
    quantity      text not null default '0',    -- exact decimal, as in SQLite
    primary key (slug, ingredient_id)
);

create table if not exists public.movements (
    id             text primary key,
    slug           text not null references public.tenants (slug) on delete cascade,
    purchase_id    text references public.purchases (id),
    ingredient_id  text not null,
    quantity       text not null,
    created_at     timestamptz not null,
    unique (purchase_id, ingredient_id)
);

create table if not exists public.activity (
    id         text primary key,
    slug       text references public.tenants (slug) on delete cascade,  -- null = platform-level
    event      text not null,
    message    text not null,
    created_at timestamptz not null default now()
);
create index if not exists idx_activity_slug_created on public.activity (slug, created_at desc);
create index if not exists idx_activity_event on public.activity (event, created_at desc);

create table if not exists public.idempotency (
    key        text primary key,
    slug       text,                            -- null for admin operations
    fingerprint text not null,
    response   jsonb not null,
    created_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- WhatsApp threads and durable messages
-- ---------------------------------------------------------------------------

create table if not exists public.whatsapp_threads (
    id               text primary key,          -- sha256(channel \0 sender)[:32]
    slug             text references public.tenants (slug) on delete cascade,
    channel          text not null,
    sender           text not null,
    transport        text not null,
    active_draft_id  text references public.drafts (id),
    active_version   integer,
    pending_followup text,
    unique (channel, sender)
);

create table if not exists public.whatsapp_messages (
    id         text primary key,
    thread_id  text not null references public.whatsapp_threads (id) on delete cascade,
    direction  text not null,
    body       text not null,
    created_at timestamptz not null default now(),
    transport  text not null,
    status     text not null,
    draft_id   text references public.drafts (id)
);
create index if not exists idx_wa_messages_thread on public.whatsapp_messages (thread_id, created_at);

create table if not exists public.whatsapp_events (
    channel    text not null,
    event_id   text not null,
    thread_id  text references public.whatsapp_threads (id),
    fingerprint text not null,
    response   jsonb not null,
    primary key (channel, event_id)
);

-- ---------------------------------------------------------------------------
-- Application metadata (schema version, seeded flag)
-- ---------------------------------------------------------------------------

create table if not exists public.app_metadata (
    key   text primary key,
    value text not null
);

-- ---------------------------------------------------------------------------
-- Transactional guard rails the application relies on
-- ---------------------------------------------------------------------------

-- Exact-once event ingest: inserting the same (channel, event_id) twice with
-- a different fingerprint must fail; the app treats the conflict as a replay.
-- (Primary key above already enforces it; this comment documents intent.)

-- Deny-by-default RLS on everything new; the service role bypasses RLS.
do $$
declare t text;
begin
  foreach t in array array[
    'tenants', 'app_users', 'app_sessions', 'whatsapp_links',
    'custom_ingredients', 'drafts', 'purchases', 'stock', 'movements',
    'activity', 'idempotency', 'whatsapp_threads', 'whatsapp_messages',
    'whatsapp_events', 'app_metadata'
  ] loop
    execute format('alter table public.%I enable row level security', t);
  end loop;
end $$;
