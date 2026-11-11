-- Purchase corrections from chat: 'ubah pembelian' / 'batal pembelian'.
-- A pending proposal is stored on the sending thread (one at a time, like
-- pending_opname_id) and voided purchases keep their history with a
-- timestamp instead of being deleted.
alter table public.whatsapp_threads add column if not exists pending_edit text;
alter table public.purchases add column if not exists voided_at timestamptz;
