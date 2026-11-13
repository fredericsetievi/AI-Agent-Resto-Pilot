-- Daily reminder engine (V33): morning/evening nudges per restaurant,
-- adjustable from chat ("pengingat pagi jadi jam 8"). Quiet hours 22:00-07:00
-- are enforced in code; the engine itself never fires inside the band.
-- Additive; apply explicitly with the corresponding service release.
begin;
create table if not exists public.reminder_settings (
    slug text primary key references public.tenants(slug) on delete cascade,
    morning_hour integer not null default 9,
    evening_hour integer not null default 21,
    enabled boolean not null default true,
    last_morning_date text,
    last_evening_date text,
    updated_at timestamptz not null default now()
);
alter table public.reminder_settings enable row level security;
commit;
