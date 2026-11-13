-- Daily cash envelope (V33): the cash brought to the market ("budget hari ini
-- 2 juta"), the change brought back ("uang kembali 500 ribu"), one row per
-- restaurant per WIB day. Unrecorded spend is computed in code from the
-- purchases ledger and is report-only — never a stock change.
-- Additive; apply explicitly with the corresponding service release.
begin;
create table if not exists public.cash_envelopes (
    slug text not null references public.tenants(slug) on delete cascade,
    day text not null,
    envelope_idr bigint,
    leftover_idr bigint,
    recorded_at timestamptz not null default now(),
    primary key (slug, day)
);
alter table public.cash_envelopes enable row level security;
commit;
