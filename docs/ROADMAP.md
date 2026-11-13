# RestoPilot roadmap

## Current milestone

The local purchase loop is implemented with SQLite, deterministic text extraction, an editable draft, explicit owner approval, estimated stock movement and activity history. A WhatsApp simulator remains available for rehearsal. The live provider adapter targets Meta WhatsApp Cloud API.

Automated fake-provider contract tests are available for the Meta integration. A separate real development-mode end-to-end test passed on October 20, 2026, including signed inbound webhooks, delivered outbound replies, explicit `YA` approval, one persisted purchase and one stock movement. See [Meta WhatsApp setup and test](META_WHATSAPP.md).

## Next delivery work

The inventory extension now implements confirmed usage/waste, stock-count reconciliation, recipe-based sales, and reorder planning. See [the inventory guide](INVENTORY.md). Hosted rollout requires migration `0004_inventory_operations.sql` and the updated application; no automatic ordering or outbound reminders are included.

1. Replace the temporary tunnel and development token with stable deployment configuration when the project moves beyond local rehearsal.
2. Evaluate the parser against reviewed Indonesian purchase examples and improve aliases or explicit correction prompts.
3. Prepare PostgreSQL persistence, managed identity and restaurant memberships; port concurrency and tenant-access checks.
4. Add private receipt photo storage, bounded extraction, retained source evidence and mandatory correction/approval.
5. Add durable delivery jobs, restart/retry behavior, audited purchase/sales reversals and backup/restore rehearsal.

Voice, marketing, supplier outreach, payments and POS connectors remain outside the current purchase-loop scope.

## Pilot feedback — Warung Bu Cemong (November 13, 2026)

First live contact with the target users (owner's family) surfaced four items. Items 1–2 are already handled; the rest shape the next milestone.

1. **Hex draft references read as gibberish.** "Draf 72ce40fb dibatalkan" meant nothing to the owner. Fixed: approve/cancel replies now name the items (`Draf 'Bolkus jumbo' dibatalkan`), with long lists collapsed to a count. See PR #52.
2. **Version gap confusion.** Owners hit V31 parser limits (no `total` keyword, `kg` defaults, the insecure-context link bug). All fixed in the V32 package awaiting publish.
3. **Cash planning is the core job.** They buy ~50 cakes per market trip across ~5 shops (mostly WhatsApp preorders) and use the list to decide how much cash to bring. Copy-pasting a messy multi-line order into the chat already produces a priced draft whose total is the cash to bring; validate with real preorders.
4. **Two-app navigation is friction.** The owners do not use Telegram and found the dashboard/Telegram split confusing. The dashboard chat widget moves from backlog into the next milestone.

Rejected directions (with reasons, so they are not re-proposed):

- **WhatsApp channel for this pilot.** The Meta WhatsApp test account cannot send to Indonesian numbers, so the owners' WhatsApp cannot be onboarded on the current account. The channel code stays for other deployments.
- **Excel/Word order intake.** If the input is a spreadsheet, the product is a spreadsheet. The differentiator is the assistant's reply (cash-to-bring, opname reconciliation, price-spike alerts), not the input format. Catalog import remains the only file flow.

## Next milestone (V33)

1. **Dashboard chat widget.** One web surface where owners read, approve, and type purchases — removes the Telegram-learning barrier for WhatsApp-preorder copy-paste. Phased: chat transcript + draft decisions first, typed purchases second.
2. **Reminder engine.** Morning/evening nudges (defaults 09:00/21:00, quiet hours 22:00–07:00), silence-aware, adjustable by chat (`pengingat jadi jam 8`).
3. **Daily cash envelope.** `budget hari ini 2 juta` → evening `uang kembali berapa?` → unrecorded spend = envelope − recorded purchases − leftover cash; folds into the reminder engine and flags forgotten purchases instead of assuming theft.
4. Validate messy WhatsApp preorder copy-paste against real cake-market order lists and tune the parser aliases.

## Historical planning context

The October 17, 2026 planning document targeted a midterm on October 29 and a flagship MVP on November 19, 2026. Its original provider-specific schedule and delivery status are historical; the current provider plan is Meta WhatsApp Cloud API as described above. The dates and earlier Twilio/360dialog milestones should not be read as evidence of the current integration status.
