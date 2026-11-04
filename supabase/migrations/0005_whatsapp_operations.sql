-- Durable WhatsApp reviews and explicit package mappings. No stock is changed.
-- Apply explicitly with the corresponding service release; startup stays read-only.
begin;
create table if not exists public.whatsapp_operations (
    id text primary key,
    restaurant_id text not null references public.tenants(slug),
    thread_id text not null references public.whatsapp_threads(id),
    status text not null,
    body text not null,
    created_at text not null,
    updated_at text not null
);
create index if not exists whatsapp_operations_tenant_thread_status
    on public.whatsapp_operations(restaurant_id, thread_id, status, updated_at desc);
create index if not exists whatsapp_operations_tenant_status
    on public.whatsapp_operations(restaurant_id, status, updated_at desc);
create table if not exists public.inventory_packages (
    restaurant_id text not null references public.tenants(slug),
    ingredient_id text not null,
    package_name text not null,
    body text not null,
    primary key (restaurant_id, ingredient_id, package_name)
);
create table if not exists public.whatsapp_purchase_sources (
    restaurant_id text not null references public.tenants(slug),
    digest text not null,
    draft_id text not null references public.drafts(id),
    primary key (restaurant_id, digest)
);
alter table public.whatsapp_operations enable row level security;
alter table public.inventory_packages enable row level security;
alter table public.whatsapp_purchase_sources enable row level security;
commit;
