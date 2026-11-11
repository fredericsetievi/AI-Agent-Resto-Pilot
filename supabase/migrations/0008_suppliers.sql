-- Supplier contacts, price intelligence, and purchase linkage.
-- Supplier names already live on purchases (canonicalized); this gives them
-- a stable entity with contact details, links purchases to it, and adds
-- price quotes — market prices recorded without buying.
-- Apply explicitly with the corresponding service release; startup stays read-only.
begin;
create table if not exists public.suppliers (
    id text primary key,
    slug text not null references public.tenants(slug) on delete cascade,
    name text not null,
    contact_phone text,
    contact_platform text,
    notes text,
    created_at timestamptz not null default now(),
    unique (slug, contact_phone)
);
alter table public.suppliers enable row level security;
-- One supplier entity per distinct purchase supplier name, per restaurant.
insert into public.suppliers (id, slug, name)
select distinct on (p.slug, lower(trim(p.supplier)))
    gen_random_uuid()::text, p.slug, trim(p.supplier)
from public.purchases p
where p.supplier is not null and trim(p.supplier) <> ''
order by p.slug, lower(trim(p.supplier)), p.created_at;
alter table public.purchases add column if not exists supplier_id text references public.suppliers(id);
update public.purchases p
set supplier_id = s.id
from public.suppliers s
where p.supplier_id is null and s.slug = p.slug and lower(trim(s.name)) = lower(trim(p.supplier));
create table if not exists public.supplier_price_quotes (
    id text primary key,
    slug text not null references public.tenants(slug) on delete cascade,
    supplier_id text not null references public.suppliers(id) on delete cascade,
    ingredient_id text,
    ingredient_name text,
    unit_price_idr bigint,
    note text,
    raw_message text,
    source text not null default 'manual',
    inquiry_id text,
    created_at timestamptz not null default now()
);
create index if not exists supplier_price_quotes_tenant_ingredient
    on public.supplier_price_quotes(slug, ingredient_id, created_at desc);
alter table public.supplier_price_quotes enable row level security;
create table if not exists public.supplier_inquiries (
    id text primary key,
    slug text not null references public.tenants(slug) on delete cascade,
    request text not null,
    status text not null default 'open',
    detail jsonb,
    created_at timestamptz not null default now()
);
alter table public.supplier_inquiries enable row level security;
commit;
