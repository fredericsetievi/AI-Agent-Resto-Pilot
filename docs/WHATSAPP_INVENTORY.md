# WhatsApp stock and sales reporting

Published on 2026-10-29 as **Atoms V9** on the existing
https://restopilot-dashboard.pub.atoms.world/ domain, from source
`1c27af32cca570d27648a71082af4f9a69ad1515`. Migration0005 was explicitly applied
to the existing Supabase project. It added three RLS-enabled tables without
changing existing stock, purchases, users, links, inventory history or settings.
This includes the earlier dashboard inventory release (V7).

## Owner experience

Send ordinary Indonesian text, a receipt/report photo, or a transcribed voice
note. Existing restaurant selection comes first for new numbers. Examples:

- `Hari ini terjual30 nasi goreng dan15 mie goreng`
- `Pakai setengah dus minyak`
- `Buang cabai 0,5 kg karena busuk`
- `Sisa beras5kg malam ini` or `stok beras 5 kg`
- `Penjualan hari ini Rp2 juta` (revenue only; no stock deduction)
- `Apa yang perlu dipesan?` (read-only planning information)

The assistant resolves only this restaurant's catalog, saved recipes and
explicit package definitions. Unknown recipes, quantities, units, dates and
document types block stock changes. It asks one focused question at a time.

For unknown dishes, answer `RESEP <exact saved recipe name>` or `TANPA RESEP`.
Create new recipes in the existing dashboard Inventory page. For an unknown
package size, answer an explicit equation such as `1 dus = 12 l`; the mapping
is saved only when the reviewed operation is approved. Kilograms and litres
are never converted using an assumed density.

The review shows the business date, coverage, sold quantities, recipe versions,
package math, ingredient deductions and resulting balances. For example,
30 portions × 0.1 kg rice = 3 kg, and 15 portions × 0.2 kg egg = 3 kg.
No stock has changed at this point.

Approve with the exact instruction shown, such as **`YA a1b2c3d4 V2`**.
A bare `YA` reprints the review; it does not approve an operation. This protects
against a delayed confirmation approving corrected quantities. `BATAL` cancels.
Send a complete corrected report to replace the pending facts and get a new
review version. Drafts and clarification answers survive process restarts.

An existing purchase draft is kept separate. A collision explains how to cancel
the operation attempt and finish the purchase first; one bare confirmation
cannot approve both. Inventory reporting preserves a pending purchase-payment
follow-up. Stock/planning questions do not change a pending operation version.

## Photos, document uncertainty and limits

Photos are classified before purchase extraction. Only an explicit supplier
purchase invoice enters the existing purchase flow. POS daily reports and
customer sales receipts enter the sales review. Uncertain or unreadable
documents ask for clarification; they never silently become purchases.

Supported photos/screenshots: JPEG, PNG and WebP, up to 5 MiB. Text input is
bounded to 8,000 characters and a report to 30 rows. Oversized reports and
reviews too long to display fully are held without posting; send smaller
sections with explicit separate coverage. PDF/XLS/CSV intake is not supported.
Provider timeouts, bad JSON and invalid media return a useful no-write response.

Voice uses the existing optional transcription configuration and then the same
review pipeline. Actual provider quality and live WhatsApp delivery have not
been exercised for this extension; tests use controlled provider responses.

Extracted facts, captions and source digests are durable. Image/audio bytes are
transient; permanent receipt-original storage/viewing is not promised.

## Duplicate reports and reconciliation

- Repeated Meta events, request keys, approval messages and failed outgoing
  replies cannot repost stock.
- Identical photo/audio digests and report IDs are checked within the restaurant,
  including two senders preparing the same source before either approves.
  Corrected-photo drafts retain both original and replacement source identities.
- Supplier receipt reuploads share a durable purchase-source identity, including
  pending, approved and rejected drafts. Reclassification of the same photo
  between purchase and operations does not create a second stock effect.
- A customer receipt and a daily sales total on the same date trigger overlap
  review. `SUDAH TERMASUK` cancels the additional report. `TERPISAH shift malam`
  declares a specifically separate shift/channel before a fresh confirmation.
  Replacing an approved total is held for reconciliation rather than posting
  both full totals. There is no automatic reversal of sales/revenue.
- A report predating a later physical count offers `RIWAYAT SAJA`: recorded
  historical consumption, with current stock unchanged. Same-day count/usage
  uncertainty asks whether consumption was already included or occurred
  `SETELAH HITUNGAN`. Both paths produce a fresh review before confirmation.
- Mixed sales, extra usage/waste and final counts post atomically, with counts
  applied last. Recipe usage must not also be reported as extra kitchen usage.
- Refunds/voids, gross versus net quantities, and unresolved financial totals
  block approval. A refund never automatically returns ingredients to stock.

Recipes, packages, count balances and complete calculations are revalidated at
approval. Changed facts require a new reviewed version. Insufficient stock
rejects the entire batch. PostgreSQL thread/restaurant locks and sorted stock
locks serialize competing approvals and preserve concurrent purchase receipts.

## Shared dashboard and planning

Events and signed movements use the existing inventory ledger. Dashboard history
shows the WhatsApp operation reference, business date, coverage and history-only
status. Forecasts use consumption business dates, with Jakarta-local posting
dates for older events. Confirmed complete calendar coverage and manual rates
remain required; purchases and count corrections are excluded.

Successful replies include the recorded event references, updated balances and
available reorder advice. There is no scheduled reminder service or automatic
purchase ordering.

## Release contents and verification

Review these source changes:

- `service/operations.py`: durable clarification, versioned review and approval.
- `service/operation_extraction.py`: conservative text and typed photo candidates.
- `service/purchase_sources.py`: supplier-photo source identity.
- `service/inventory.py`: read-only previews and caller-owned atomic batch posting.
- `service/api.py`, `service/whatsapp.py`, both stores: routing and transport wiring.
- `frontend/app.js`: business-date/source/history-only audit display.
- `supabase/migrations/0005_whatsapp_operations.sql`: three additive RLS-enabled
  tables: `whatsapp_operations`, `inventory_packages`, `whatsapp_purchase_sources`.
  No stock rewrite, data reset, seed or automatic startup migration.

Migration0004 is already present in the live project and was verified during
the dashboard publication. Do not rerun or edit it. Hosted startup stays
read-only and fails closed. Missing0005 produces a safe operations/photo-intake
response while text purchase functionality stays available.

The approved rollout committed/pushed the source, applied0005, imported the
pinned release into the existing native Atoms scaffold, re-vendored the backend,
checked external preview, then published V9 on the same domain. Keys,
history/branches, open restaurant selection and the existing Meta test
callback/account were retained. The native V7 startup/proxy repair was not
overwritten. GitHub synchronization and Atoms publication remain separate;
see [the workflow guide](ATOMS_GITHUB_WORKFLOW.md).

Verification completed locally: **374 Python tests passed**, with 34 skipped
(30 opt-in local PostgreSQL tests plus four legacy live tests). All **30 local
PostgreSQL tests passed separately**, against disposable databases restricted
to a local socket. **25 JavaScript tests passed**. These local tests used isolated
databases and did not send real WhatsApp messages.

Final release audit added an unchanged-balance physical-count race regression
and a count-evidence check under the posting locks. All **31 local PostgreSQL
tests** and **28 SQLite operation-flow tests** passed after that fix.

Atoms' Python 3.10 runtime needed a portable test cleanup callback instead of
`set_authorizer(None)`. After that test-only correction, its complete source
suite passed **371 tests, 35 skipped** (the three GitHub-layout adapter tests
were excluded). Native frontend tests passed22/22; lint/build passed.

Independent post-publication verification confirmed:

- Health/live and readiness200 with hosted Supabase storage; signed-out
  inventory401; demo-owner login and inventory readback200.
- Published `assets/app.js` exactly matches the reviewed source file.
- A simulator usage message produced a versioned review; retrying its key
  returned the identical response, then `BATAL` cancelled it.
- All seven pre-existing business-table counts and fingerprints stayed equal.
  No purchase, stock movement or inventory event was posted by verification.

One cancelled QA simulator operation is deliberately retained for audit:
`b306a87f9d7745b18ba78c26eabc4f19`. Its simulator messages were retained; no real
WhatsApp message was sent. The sanitized verification report is ignored under
`tmp/whatsapp-release-check-20261028T180828Z.json`.
