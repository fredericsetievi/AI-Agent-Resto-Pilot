"""Durable WhatsApp reviews exercise the real SQLite ledger, without providers."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
from uuid import uuid4

import pytest

from conftest import SENDER_NUMBER
from service.db import RESTAURANT_TIMEZONE, Store
from service.operation_extraction import parse_operations_text


TENANT = "demo-resto"


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "whatsapp-operations.sqlite3", seed=False)
    with result.transaction() as conn:
        for ingredient, amount in (("beras", "30"), ("telur", "20"), ("minyak", "20"), ("cabai", "5")):
            conn.execute("UPDATE inventory SET quantity=? WHERE restaurant_id=? AND ingredient_id=?", (amount, TENANT, ingredient))
    return result


def recipe(store, name="Nasi goreng", ingredient="beras", amount="0.1"):
    return store.save_inventory_recipe(uuid4().hex, {"name": name, "items": [{"ingredient_id": ingredient, "quantity": amount}]}, TENANT)


def ingest(store, text, *, key=None, event=None, candidate=None, source=None, sender=SENDER_NUMBER, **kwargs):
    payload = {"sender": sender, "text": text, "event_id": event}
    if candidate is not None:
        payload["operation_candidate"] = candidate
    if source is not None:
        payload["operation_source"] = source
    return store.whatsapp_ingest(key or uuid4().hex, payload, **kwargs)


def op(response):
    return response["result"]["operation"]


def approve(store, response, **kwargs):
    operation = op(response)
    return ingest(store, f"YA {operation['id'][:8]} V{operation['version']}", **kwargs)


def balances(store):
    with store.transaction() as conn:
        return {row[0]: Decimal(row[1]) for row in conn.execute("SELECT ingredient_id,quantity FROM inventory WHERE restaurant_id=?", (TENANT,))}


def events(store):
    return store.inventory_workspace(TENANT)["events"]


def final_reply(response):
    return response["messages"][-1]["body"]


def test_two_dish_review_calculations_require_explicit_version_before_atomic_post(store):
    rice = recipe(store)
    eggs = recipe(store, "Mie goreng", "telur", "0.2")
    before = balances(store)
    review = ingest(store, "Hari ini terjual30 nasi goreng dan15 mie goreng")
    assert op(review)["status"] == "review"
    assert balances(store) == before and events(store) == []
    reply = final_reply(review)
    assert "30 × 0.1 = 3" in reply and "15 × 0.2 = 3" in reply
    assert rice["name"] in reply and eggs["name"] in reply
    assert op(ingest(store, "YA"))["status"] == "review"
    assert events(store) == []
    posted = approve(store, review)
    assert posted["result"]["action"] == "operation_posted"
    assert balances(store)["beras"] == 27 and balances(store)["telur"] == 17
    assert len(events(store)) == 2
    assert {e["id"] for e in events(store)} == {e["id"] for e in op(posted)["events"]}
    assert all(e["business_date"] == datetime.now(RESTAURANT_TIMEZONE).date().isoformat() for e in events(store))


def test_revenue_only_records_no_portions_and_no_stock_deduction(store):
    before = balances(store)
    review = ingest(store, "Penjualan hari ini Rp2 juta")
    assert op(review)["status"] == "review"
    assert "tanpa pengurangan stok" in final_reply(review)
    posted = approve(store, review)
    assert posted["result"]["action"] == "operation_posted"
    assert balances(store) == before
    assert len(events(store)) == 1
    assert events(store)[0]["revenue_idr"] == 2_000_000
    assert events(store)[0].get("portions") is None
    assert events(store)[0]["items"] == []


def test_unknown_recipe_requires_named_resolution_or_explicit_sales_only(store):
    before = balances(store)
    unknown = ingest(store, "Terjual 10 Nasi spesial")
    assert op(unknown)["status"] == "clarification"
    assert "Resep" in final_reply(unknown)
    assert events(store) == []
    assert op(approve(store, unknown))["status"] == "clarification"
    review = ingest(store, "TANPA RESEP")
    assert op(review)["status"] == "review"
    assert op(approve(store, review))["status"] == "posted"
    assert balances(store) == before
    assert events(store)[0]["portions"] == "10"
    assert events(store)[0]["items"] == []


def test_explicit_recipe_mapping_uses_tenant_recipe_and_keeps_candidate_dish(store):
    recipe(store, "Nasi standar")
    unknown = ingest(store, "Terjual nasi spesial 4 porsi")
    assert op(unknown)["status"] == "clarification"
    review = ingest(store, "RESEP Nasi standar")
    assert op(review)["status"] == "review"
    assert op(review)["candidate"]["lines"][0]["name"] == "nasi spesial"
    approve(store, review)
    assert balances(store)["beras"] == Decimal("29.6")


def test_half_unknown_package_clarifies_then_review_persists_mapping_only_on_approval(store):
    pending = ingest(store, "Pakai setengah dus minyak")
    assert op(pending)["status"] == "clarification"
    assert "berisi berapa" in final_reply(pending)
    with store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM inventory_packages").fetchone()[0] == 0
    review = ingest(store, "1 dus = 12 l")
    assert op(review)["status"] == "review"
    assert "0.5 × 12 l" in final_reply(review)
    assert balances(store)["minyak"] == 20
    approve(store, review)
    assert balances(store)["minyak"] == 14
    with store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM inventory_packages").fetchone()[0] == 1
    next_review = ingest(store, "Pakai setengah dus minyak")
    assert op(next_review)["status"] == "review"
    assert op(next_review)["preview"][0]["items"][0]["after"] == "8"


def test_count_statement_sets_absolute_and_question_is_read_only(store):
    question = ingest(store, "Berapa sisa beras?")
    assert "operation" not in question["result"]
    assert balances(store)["beras"] == 30 and events(store) == []
    review = ingest(store, "Sisa beras5kg malam ini")
    assert op(review)["status"] == "review"
    assert op(review)["preview"][0]["items"][0]["delta"] == "-25"
    approve(store, review)
    assert balances(store)["beras"] == 5
    assert events(store)[0]["kind"] == "count"


def test_plain_stock_assertion_proposes_absolute_count_while_question_reads_only(store):
    question = ingest(store, "Berapa stok beras?")
    assert "operation" not in question["result"]
    review = ingest(store, "Stok beras 5 kg")
    assert op(review)["status"] == "review"
    assert op(review)["candidate"]["document_type"] == "count"
    assert balances(store)["beras"] == 30 and events(store) == []
    assert "30 → 5 kg" in final_reply(review)
    approve(store, review)
    assert balances(store)["beras"] == 5


def test_restart_preserves_pending_question_and_focused_answer(store):
    clarification = ingest(store, "Pakai beras")
    assert op(clarification)["status"] == "clarification"
    assert events(store) == []
    reopened = Store(store.path, seed=False)
    assert reopened.has_active_operation(SENDER_NUMBER)
    review = ingest(reopened, "2 kg")
    assert op(review)["id"] == op(clarification)["id"]
    assert op(review)["version"] == op(clarification)["version"] + 1
    assert op(review)["status"] == "review"
    approve(reopened, review)
    assert balances(store)["beras"] == 28


def test_transport_retry_double_confirmation_and_failed_outbound_do_not_repost(store):
    review = ingest(store, "Pakai beras2kg", event="wamid.operation")
    assert ingest(store, "Pakai beras2kg", event="wamid.operation") == review
    posted = approve(store, review, event="wamid.approval")
    outbound = next(message for message in reversed(posted["messages"]) if message["direction"] == "outbound")
    store.set_whatsapp_message_status(outbound["id"], "failed")
    operation = op(review)
    text = f"YA {operation['id'][:8]} V{operation['version']}"
    retry = ingest(store, text, event="wamid.approval")
    assert op(retry)["id"] == operation["id"]
    repeated = ingest(store, text, event="wamid.approval.again")
    assert repeated["result"]["action"] == "operation_duplicate"
    assert balances(store)["beras"] == 28
    assert len(events(store)) == 1


def test_byte_identical_image_resubmission_uses_digest_even_with_new_message(store):
    recipe(store)
    candidate = parse_operations_text("Terjual nasi goreng10porsi")
    source = {"digest": hashlib.sha256(b"photo bytes").hexdigest(), "media_kind": "image", "media_id": "first-id"}
    review = ingest(store, "Laporan", candidate=candidate, source=source, event="image-1")
    posted = approve(store, review)
    duplicate = ingest(store, "Foto ulang", candidate=candidate, source={**source, "media_id": "second-id"}, event="image-2")
    assert duplicate["result"]["action"] == "operation_duplicate"
    assert op(duplicate)["id"] == op(posted)["id"]
    assert balances(store)["beras"] == 29 and len(events(store)) == 1


def test_individual_receipt_then_daily_image_requires_explicit_distinct_coverage(store):
    recipe(store)
    first = ingest(store, "Struk pelanggan nasi goreng10porsi shift pagi")
    approve(store, first)
    candidate = parse_operations_text("Penjualan hari ini nasi goreng5porsi")
    pending = ingest(store, "Foto laporan harian", candidate=candidate, source={"digest": "a" * 64, "media_kind": "image"})
    assert op(pending)["status"] == "clarification"
    assert "TERPISAH" in final_reply(pending)
    assert balances(store)["beras"] == 29
    review = ingest(store, "TERPISAH shift malam")
    assert op(review)["status"] == "review"
    assert op(review)["candidate"]["coverage"] == "shift malam"
    approve(store, review)
    assert balances(store)["beras"] == Decimal("28.5")
    assert len(events(store)) == 2


def test_already_included_daily_report_cancels_without_deduction(store):
    recipe(store)
    approve(store, ingest(store, "Struk pelanggan nasi goreng10porsi"))
    pending = ingest(store, "Penjualan hari ini nasi goreng10porsi")
    assert op(pending)["status"] == "clarification"
    cancelled = ingest(store, "SUDAH TERMASUK")
    assert op(cancelled)["status"] == "cancelled"
    assert not store.has_active_operation(SENDER_NUMBER)
    assert balances(store)["beras"] == 29 and len(events(store)) == 1


def test_stale_recipe_requires_new_review_and_version_confirmation(store):
    saved = recipe(store)
    review = ingest(store, "Terjual nasi goreng10porsi")
    store.save_inventory_recipe(uuid4().hex, {**saved, "items": [{"ingredient_id": "beras", "quantity": "0.2"}]}, TENANT)
    stale = approve(store, review)
    assert op(stale)["status"] == "clarification"
    assert len(events(store)) == 0 and balances(store)["beras"] == 30
    refreshed = ingest(store, "TINJAU ULANG")
    assert op(refreshed)["status"] == "review"
    assert op(refreshed)["version"] > op(review)["version"]
    assert op(approve(store, review))["status"] == "review"
    assert len(events(store)) == 0
    approve(store, refreshed)
    assert balances(store)["beras"] == 28


def test_stale_count_cannot_overwrite_new_receipt_balance_without_rereview(store):
    review = ingest(store, "Sisa beras25kg")
    # A separate ledger operation represents intervening kitchen/receipt work.
    store.record_inventory_event(uuid4().hex, {"kind": "usage", "note": "Separate kitchen usage", "items": [{"ingredient_id": "beras", "quantity": "1"}], "confirmed": True}, TENANT)
    stale = approve(store, review)
    assert op(stale)["status"] == "clarification"
    assert balances(store)["beras"] == 29
    assert len(events(store)) == 1
    refreshed = ingest(store, "TINJAU ULANG")
    assert op(refreshed)["status"] == "review"
    approve(store, refreshed)
    assert balances(store)["beras"] == 25
    assert len(events(store)) == 2


def test_mixed_report_applies_final_count_after_recipe_usage_and_waste(store):
    recipe(store)
    review = ingest(store, "Sisa beras25kg; terjual nasi goreng10porsi; pakai beras2kg; buang beras0,5kg")
    assert op(review)["status"] == "review"
    assert balances(store)["beras"] == 30
    posted = approve(store, review)
    assert op(posted)["status"] == "posted"
    assert balances(store)["beras"] == 25 and len(events(store)) == 4
    count = next(event for event in events(store) if event["kind"] == "count")
    assert count["items"][0]["before"] == "26.5"
    assert count["items"][0]["after"] == "25"


def test_refunds_unclear_net_and_mismatched_totals_block_approval(store):
    recipe(store)
    for text in ("Penjualan nasi goreng10porsi refund Rp10000", "Penjualan nasi goreng10porsi gross", "Penjualan nasi goreng10porsi Rp100000; total Rp90000"):
        pending = ingest(store, text)
        assert op(pending)["status"] == "clarification"
        assert op(approve(store, pending))["status"] == "clarification"
        assert events(store) == [] and balances(store)["beras"] == 30
        ingest(store, "BATAL")


def test_shortage_in_second_dish_prevents_every_deduction(store):
    recipe(store)
    recipe(store, "Mie goreng", "telur", "10")
    before = balances(store)
    blocked = ingest(store, "Terjual nasi goreng10porsi dan mie goreng3porsi")
    assert op(blocked)["status"] == "clarification"
    assert balances(store) == before and events(store) == []
    approve(store, blocked)
    assert balances(store) == before and events(store) == []


def test_backdated_usage_after_later_count_requires_explicit_history_only(store):
    approve(store, ingest(store, "Sisa beras25kg hari ini"))
    old = ingest(store, "Pakai beras2kg kemarin")
    assert op(old)["status"] == "clarification"
    assert "RIWAYAT SAJA" in final_reply(old)
    history = ingest(store, "RIWAYAT SAJA")
    assert op(history)["status"] == "review"
    approve(store, history)
    assert balances(store)["beras"] == 25
    event = next(event for event in events(store) if event["kind"] == "usage")
    assert event["items"][0]["delta"] == "0"
    assert event["items"][0]["planned_quantity"] == "2"


@pytest.mark.parametrize("answer,expected_balance,expected_delta", [
    ("SETELAH HITUNGAN", Decimal("23"), "-2"),
    ("RIWAYAT SAJA", Decimal("25"), "0"),
])
def test_same_day_count_requires_timing_answer_then_separate_review_confirmation(store, answer, expected_balance, expected_delta):
    approve(store, ingest(store, "Sisa beras25kg hari ini"))
    pending = ingest(store, "Pakai beras2kg hari ini")
    assert op(pending)["status"] == "clarification"
    assert "SETELAH HITUNGAN" in final_reply(pending)
    assert "RIWAYAT SAJA" in final_reply(pending)
    assert op(approve(store, pending))["status"] == "clarification"
    assert balances(store)["beras"] == 25 and len(events(store)) == 1

    review = ingest(store, answer)
    assert op(review)["status"] == "review"
    assert op(review)["version"] > op(pending)["version"]
    assert balances(store)["beras"] == 25 and len(events(store)) == 1
    assert op(approve(store, pending))["status"] == "review"  # Stale approval remains harmless.
    assert balances(store)["beras"] == 25 and len(events(store)) == 1

    posted = approve(store, review)
    assert op(posted)["status"] == "posted"
    assert balances(store)["beras"] == expected_balance
    recorded = next(event for event in events(store) if event["kind"] == "usage")
    assert recorded["items"][0]["delta"] == expected_delta
    if answer == "RIWAYAT SAJA":
        assert recorded["items"][0]["planned_quantity"] == "2"


def test_mixed_report_total_attaches_first_sale_when_consumption_line_is_first(store):
    recipe(store)
    review = ingest(store, "Pakai beras1kg; terjual nasi goreng2porsi; total Rp50000")
    assert op(review)["status"] == "review"
    assert [line["kind"] for line in op(review)["candidate"]["lines"]] == ["usage", "sale"]
    assert op(review)["payloads"][0].get("revenue_idr") is None
    assert op(review)["payloads"][1]["revenue_idr"] == 50000
    assert "Total pendapatan laporan Rp50,000; dicatat sekali." in final_reply(review)
    assert balances(store)["beras"] == 30 and events(store) == []
    approve(store, review)
    assert balances(store)["beras"] == Decimal("28.8")
    assert sum(event.get("revenue_idr") or 0 for event in events(store)) == 50000
    assert next(event for event in events(store) if event["kind"] == "usage").get("revenue_idr") is None


def test_partial_line_revenues_cannot_hide_unallocated_report_total(store):
    recipe(store)
    recipe(store, "Mie goreng", "telur", "0.1")
    before = balances(store)
    pending = ingest(store, "Terjual nasi goreng2porsi Rp50000; mie goreng3porsi; total Rp75000")
    assert op(pending)["status"] == "clarification"
    assert "Total pendapatan dan rincian belum cocok/lengkap" in final_reply(pending)
    assert op(approve(store, pending))["status"] == "clarification"
    assert balances(store) == before and events(store) == []


def test_document_type_answer_preserves_existing_lines_until_date_is_clarified(store):
    recipe(store)
    candidate = parse_operations_text("Terjual nasi goreng2porsi")
    candidate.update(document_type="ambiguous", business_date=None,
                     unresolved=["Jenis dokumen belum jelas.", "Tanggal usaha belum terbaca."])
    pending = ingest(store, "Foto belum jelas", candidate=candidate, source={"digest": "f" * 64, "media_kind": "image"})
    assert op(pending)["status"] == "clarification"
    original_lines = op(pending)["candidate"]["lines"]
    resolved_type = ingest(store, "ini penjualan")
    assert op(resolved_type)["id"] == op(pending)["id"]
    assert op(resolved_type)["candidate"]["document_type"] == "sales_report"
    assert op(resolved_type)["candidate"]["business_date"] is None
    assert op(resolved_type)["candidate"]["lines"] == original_lines
    assert op(resolved_type)["status"] == "clarification"
    assert "Tanggal laporan ini kapan" in final_reply(resolved_type)
    assert events(store) == []
    review = ingest(store, "hari ini")
    assert op(review)["status"] == "review"
    assert len(op(review)["candidate"]["lines"]) == 1
    assert op(review)["candidate"]["lines"][0]["quantity"] == "2"
    assert balances(store)["beras"] == 30 and events(store) == []
    approve(store, review)
    assert balances(store)["beras"] == Decimal("29.8")


def test_oversized_review_refuses_confirmation_instead_of_hiding_breakdown(store):
    name = "Nasi " + "spesial " * 18
    recipe(store, name.strip(), amount="0.01")
    text = "Laporan penjualan hari ini:\n" + "\n".join(f"{name}1 porsi" for _ in range(30))
    assert len(text) <= 8000
    pending = ingest(store, text)
    operation = op(pending)
    assert len(operation["candidate"]["lines"]) == 30
    assert len(operation["preview"]) == 30
    assert operation["status"] == "clarification"
    assert "Rincian terlalu panjang" in final_reply(pending)
    assert "Balas BATAL" in final_reply(pending)
    assert "Balas YA" not in final_reply(pending)
    assert len(final_reply(pending)) < 3900
    assert op(approve(store, pending))["status"] == "clarification"
    assert balances(store)["beras"] == 30 and events(store) == []


def test_concurrent_distinct_approval_messages_post_one_operation(store):
    review = ingest(store, "Pakai beras2kg")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: approve(store, review), range(2)))
    assert {response["result"]["action"] for response in results} == {"operation_posted", "operation_duplicate"}
    assert balances(store)["beras"] == 28 and len(events(store)) == 1


def test_active_purchase_cannot_be_approved_by_operation_confirmation(store):
    purchase = ingest(store, "Beli cabai 2 kg Rp100000")
    assert purchase["result"]["action"] == "draft_created"
    blocked = ingest(store, "Pakai beras2kg")
    assert op(blocked)["status"] == "collision"
    assert ingest(store, "YA")["result"]["action"] == "operation_collision"
    assert events(store) == [] and balances(store)["beras"] == 30
    assert len(store.overview(TENANT)["purchases"]) == 0
    cancelled = ingest(store, "BATAL OPERASI")
    assert op(cancelled)["status"] == "cancelled"
    assert cancelled["thread"]["active_draft_id"] == purchase["draft"]["id"]


def test_read_only_stock_and_forecast_questions_preserve_review_version(store):
    review = ingest(store, "Pakai beras 2 kg")
    operation = op(review)
    for text in ("Berapa stok beras?", "Stok cukup berapa hari?", "Apa yang perlu dipesan?"):
        answer = ingest(store, text)
        assert answer["result"]["action"] in {"stock_answer", "operation_forecast"}
        assert store.has_active_operation(SENDER_NUMBER)
        with store.transaction() as conn:
            import json
            saved = json.loads(conn.execute("SELECT body FROM whatsapp_operations WHERE id=?", (operation["id"],)).fetchone()[0])
        assert saved["version"] == operation["version"] and saved["preview"] == operation["preview"]
    assert op(approve(store, review))["status"] == "posted"
    assert balances(store)["beras"] == 28


def test_purchase_order_verb_is_not_hijacked_as_forecast(store):
    response = ingest(store, "pesan beras 2 kg 30rb")
    assert response["result"]["action"] != "operation_forecast"
    assert response.get("draft") is not None
