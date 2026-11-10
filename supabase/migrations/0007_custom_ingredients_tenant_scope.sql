-- Tenant-scope custom ingredient identities. The single-column primary key on
-- id made the first restaurant to auto-create a name own it globally: any other
-- restaurant approving a purchase with the same new ingredient hit a unique
-- violation (HTTP 500) and a stuck draft. Key by (slug, id) instead — every
-- other reference (stock, movements, draft/purchase JSON) is already per
-- tenant, so no data rewrite is needed.
ALTER TABLE custom_ingredients DROP CONSTRAINT IF EXISTS custom_ingredients_pkey;
ALTER TABLE custom_ingredients DROP CONSTRAINT IF EXISTS custom_ingredients_slug_id_key;
ALTER TABLE custom_ingredients ADD PRIMARY KEY (slug, id);
