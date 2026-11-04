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

## Historical planning context

The October 17, 2026 planning document targeted a midterm on October 29 and a flagship MVP on November 19, 2026. Its original provider-specific schedule and delivery status are historical; the current provider plan is Meta WhatsApp Cloud API as described above. The dates and earlier Twilio/360dialog milestones should not be read as evidence of the current integration status.
