"""Operation candidates remain facts/clarifications; every provider is a fake."""

import asyncio
from datetime import date, datetime, timezone
import json

import httpx
import pytest

from service.operation_extraction import clean_operations_document, extract_operations_document, parse_operations_text
from service.vision import MAX_IMAGE_BYTES, StepFunVisionError


TODAY = date(2026, 10, 29)
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


def parse(text, today=TODAY):
    return parse_operations_text(text, today=today)


@pytest.mark.parametrize("text", [
    "Beli beras 5 kg 30rb", "Nota pembelian dari supplier", "Beli stok beras5kg",
    "Berapa sisa beras?", "sisa beras berapa", "berapa penjualan kemarin?",
    "Cek stok sisa beras", "YA", "BATAL", "halo", "stok cukup berapa hari?",
    "1 dus = 12 l", "RESEP Penjualan spesial", "BAHAN sisa ayam", "TERPISAH penjualan shift malam",
    "TERPISAH shift malam", "RIWAYAT SAJA", "TANPA RESEP", "TINJAU ULANG", "SUDAH TERMASUK",
])
def test_purchases_questions_and_legacy_commands_remain_legacy(text):
    assert parse(text) is None


def test_two_dishes_with_missing_chat_spaces():
    result = parse("Hari ini terjual30 nasi goreng dan15 mie goreng")
    assert result["document_type"] == "sales_report"
    assert result["business_date"] == "2026-10-29"
    assert [(line["name"], line["quantity"], line["unit"]) for line in result["lines"]] == [
        ("nasi goreng", "30", "porsi"), ("mie goreng", "15", "porsi"),
    ]
    assert result["unresolved"] == []
    assert all(not line["sales_only"] for line in result["lines"])


def test_multiline_report_heading_is_metadata_not_an_unreadable_item():
    result = parse("Laporan penjualan hari ini:\nNasi goreng 30 porsi\nMie goreng 15 porsi")
    assert len(result["lines"]) == 2
    assert result["unresolved"] == []
    bounded = parse("Laporan penjualan hari ini:\n" + "\n".join("Nasi goreng 1 porsi" for _ in range(30)))
    assert len(bounded["lines"]) == 30
    assert bounded["unresolved"] == []


def test_revenue_never_becomes_portions_or_ingredients():
    result = parse("Penjualan hari ini Rp2 juta")
    assert result["revenue_idr"] == 2_000_000
    assert result["lines"] == [{
        "kind": "sale", "name": "penjualan", "quantity": None, "unit": None,
        "pack_size": None, "pack_unit": None, "revenue_idr": 2_000_000, "sales_only": True,
    }]


def test_printed_text_total_is_metadata_and_never_a_second_sale():
    result = parse("Penjualan nasi goreng 10 porsi Rp150.000; mie goreng 5 porsi Rp50.000; total Rp200.000")
    assert len(result["lines"]) == 2
    assert result["revenue_idr"] == 200000
    assert result["unresolved"] == []
    mismatch = parse("Penjualan nasi goreng 10 porsi Rp150.000; total Rp100.000")
    assert len(mismatch["lines"]) == 1
    assert mismatch["unresolved"]


def test_uncertain_mutation_cannot_become_a_purchase():
    result = parse("Pakai beras2kg?")
    assert result is not None
    assert result["unresolved"]
    assert parse("Sisa beras2kg?") is None
    for text in ("Jangan pakai beras2kg", "Belum terjual nasi goreng2porsi", "Nanti pakai beras2kg"):
        assert parse(text)["unresolved"]
    assert parse("Kepakai beras2kg")["document_type"] == "usage"


def test_sales_measured_by_mass_are_not_treated_as_portions():
    assert parse("Terjual nasi goreng2kg")["unresolved"]
    assert extract(document(lines=[{"kind": "sale", "name": "Nasi goreng", "quantity": "2", "unit": "kg"}], revenue_idr=None))["unresolved"]


def test_exact_decimal_half_package_contents_are_facts_not_conversions():
    unknown = parse("Pakai setengah dus minyak")["lines"][0]
    assert (unknown["quantity"], unknown["unit"], unknown["pack_size"], unknown["pack_unit"]) == ("0.5", "dus", None, None)
    explicit = parse("Pakai setengah dus minyak isi 12 liter")["lines"][0]
    assert (explicit["name"], explicit["quantity"], explicit["pack_size"], explicit["pack_unit"]) == ("minyak", "0.5", "12", "l")
    assert "canonical_quantity" not in explicit


def test_waste_and_physical_remaining_count_are_distinct():
    waste = parse("Buang cabai0,5kg karena busuk")
    count = parse("Sisa beras5kg malam ini")
    assert waste["document_type"] == "waste"
    assert waste["lines"][0]["name"] == "cabai"
    assert waste["lines"][0]["quantity"] == "0.5"
    assert count["document_type"] == "count"
    assert count["lines"][0]["quantity"] == "5"
    assert parse("Sisa beras0,0kg")["lines"][0]["quantity"] == "0"


def test_mixed_report_preserves_all_kinds_and_order():
    result = parse("Terjual nasi goreng20porsi; pakai beras2kg; buang cabai0,5kg; sisa beras5kg")
    assert result["document_type"] == "mixed"
    assert [line["kind"] for line in result["lines"]] == ["sale", "usage", "waste", "count"]
    assert [line["quantity"] for line in result["lines"]] == ["20", "2", "0.5", "5"]


def test_uncertain_quantity_units_and_multiple_numbers_require_clarification():
    missing = parse("Pakai minyak")
    assert missing["lines"][0]["quantity"] is None
    assert missing["lines"][0]["unit"] is None
    assert len(missing["unresolved"]) == 2
    conflicting = parse("Terjual nasi goreng 10 porsi 20 porsi")
    assert conflicting["lines"][0]["quantity"] is None
    assert conflicting["unresolved"]
    for text in ("Pakai beras -1kg", "Pakai beras0kg", "Pakai beras0,0000001kg", "Pakai beras1000001kg"):
        result = parse(text)
        assert result["lines"][0]["quantity"] is None
        assert result["unresolved"]


def test_dates_use_restaurant_calendar_and_never_amounts():
    # 17:30 UTC is already the next restaurant business day.
    result = parse("Kemarin terjual 3 mie goreng", datetime(2026, 10, 28, 17, 30, tzinfo=timezone.utc))
    assert result["business_date"] == "2026-10-28"
    assert result["lines"][0]["quantity"] == "3"
    absolute = parse("Penjualan tanggal 2026-10-27: nasi goreng 12 porsi")
    assert absolute["business_date"] == "2026-10-27"
    assert absolute["lines"][0]["quantity"] == "12"
    assert parse("Pemakaian 27/10/2026 beras 2 kg")["business_date"] == "2026-10-27"
    assert parse("Pemakaian 2026-02-31 beras 2 kg")["unresolved"]
    assert parse("Penjualan kemarin dan hari ini 3 nasi goreng")["business_date"] is None
    assert parse("Penjualan besok 3 nasi goreng")["unresolved"]


def test_coverage_document_identity_and_customer_receipt():
    result = parse("Laporan penjualan id: POS-42 kemarin shift malam grabfood; nasi goreng 5 porsi")
    assert result["report_id"] == "pos-42"
    assert result["coverage"] == "shift malam; grabfood"
    assert result["lines"][0]["name"] == "nasi goreng"
    assert parse("Struk pelanggan nasi goreng 2 porsi")["document_type"] == "sale_receipt"


@pytest.mark.parametrize("word", ["refund", "void", "diskon", "gross", "net", "pajak"])
def test_adjustments_never_silently_resolve_gross_net_quantities(word):
    result = parse(f"Penjualan nasi goreng 10 porsi; {word} 2")
    assert result["unresolved"]
    assert any("refund/void" in note for note in result["unresolved"])


def test_ambiguous_operation_and_mixed_purchase_cannot_fall_through():
    physical = parse("Stok beras5kg")
    assert physical["document_type"] == "count"
    assert physical["lines"][0]["name"] == "beras"
    assert physical["lines"][0]["quantity"] == "5"
    assert parse("Berapa stok beras5kg?") is None
    assert parse("Stok beras") is None
    result = parse("Beli beras 5kg dan pakai beras 2kg")
    assert result["document_type"] == "ambiguous"
    assert result["lines"] == []
    assert result["unresolved"]


def test_limits_do_not_partially_parse_a_report():
    for text in ("Penjualan " + "x" * 8000, "Penjualan\n" + "\n".join("nasi goreng 1 porsi" for _ in range(31))):
        result = parse(text)
        assert result["lines"] == []
        assert result["unresolved"]
        assert len(result["evidence"]) <= 8000


class FakeResponse:
    def __init__(self, status=200, content=None, payload=None):
        self.status_code = status
        self.payload = payload
        self.content = content

    def json(self):
        if self.payload is not None:
            return self.payload
        return {"choices": [{"message": {"content": self.content}}]}


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


def document(kind="sales_report", **overrides):
    result = {"document_type": kind, "business_date": "2026-10-28", "coverage": "shift malam",
              "report_id": "POS-9", "lines": [{"kind": "sale", "name": "Nasi Goreng", "quantity": "30",
              "unit": "porsi", "pack_size": None, "pack_unit": None, "revenue_idr": 450000, "sales_only": False}],
              "revenue_idr": 450000, "unresolved": [], "evidence": "Penjualan bersih, 28 Oktober 2026, 30 nasi goreng",
              "quantity_basis": "net"}
    result.update(overrides)
    return result


def extract(data=None, *, client=None, image=JPEG, **kwargs):
    client = client or FakeClient(FakeResponse(content=json.dumps(document() if data is None else data)))
    return asyncio.run(extract_operations_document(image, api_key="synthetic-key", client=client, **kwargs))


def test_pos_photo_stays_sales_and_provider_request_is_bounded():
    client = FakeClient(FakeResponse(content=json.dumps(document())))
    result = extract(client=client, caption="Penjualan kemarin", restaurant="Warung")
    assert result["document_type"] == "sales_report"
    assert result["lines"][0]["name"] == "nasi goreng"
    assert result["lines"][0]["quantity"] == "30"
    assert result["unresolved"] == []
    url, request = client.calls[0]
    assert url == "https://api.stepfun.com/v1/chat/completions"
    assert request["json"]["max_tokens"] == 6000
    prompt = request["json"]["messages"][0]["content"][0]["text"]
    assert '"caption": "Penjualan kemarin"' in prompt
    assert '"restaurant": "Warung"' in prompt
    assert "No stock changes are authorized" in prompt


def test_supplier_classification_only_permits_existing_purchase_extractor():
    result = extract({"document_type": "purchase_invoice", "lines": [], "unresolved": [], "evidence": "Supplier invoice"})
    assert result["document_type"] == "purchase_invoice"
    assert result["lines"] == []
    assert result["unresolved"] == []
    assert "items" not in result  # No unvalidated purchase items are produced.
    conflict = extract({"document_type": "purchase_invoice", "lines": []}, caption="Penjualan hari ini")
    assert conflict["document_type"] == "ambiguous"
    assert conflict["unresolved"]


def test_ambiguous_and_unreadable_documents_have_no_automatic_purchase_path():
    result = extract({"document_type": "unknown-paper", "lines": [], "evidence": "Unreadable columns"})
    assert result["document_type"] == "ambiguous"
    assert result["unresolved"]
    missing = document()
    missing["business_date"] = None
    missing["lines"][0].update(quantity=None, unit=None, revenue_idr=None, confidence="low")
    result = extract(missing)
    assert result["lines"][0]["quantity"] is None
    assert result["lines"][0]["unit"] is None
    assert len(result["unresolved"]) >= 4


def test_image_revenue_only_never_infers_portions():
    result = extract(document(lines=[{"kind": "sale", "name": "Penjualan", "quantity": None, "unit": None,
                                      "revenue_idr": 450000, "sales_only": True}]))
    assert result["lines"][0]["quantity"] is None
    assert result["lines"][0]["sales_only"] is True
    assert result["unresolved"] == []


def test_printed_totals_checked_without_altering_quantities():
    result = extract(document(revenue_idr=400000))
    assert result["lines"][0]["quantity"] == "30"
    assert any("Total baris" in note for note in result["unresolved"])
    explained = extract(document(revenue_idr=400000, totals={"discount_idr": 50000}))
    assert explained["unresolved"] == []
    refund = extract(document(revenue_idr=400000, totals={"refund_idr": 50000}, quantity_basis="gross"))
    assert any("refund/void" in note for note in refund["unresolved"])


@pytest.mark.parametrize("mutate", [
    {"document_type": ["sale"]},
    {"business_date": "yesterday"},
    {"business_date": "2026-02-31"},
    {"quantity_basis": ["net"]},
    {"lines": [{"kind": ["sale"], "name": "nasi"}]},
    {"lines": [{"kind": "execute", "name": "nasi"}]},
    {"lines": [{"kind": "sale", "name": "nasi", "quantity": True, "unit": "porsi", "revenue_idr": 10000.5}]},
])
def test_untrusted_invalid_values_are_held_for_clarification(mutate):
    result = extract(document(**mutate))
    assert result["unresolved"]
    assert all(line["quantity"] is None or not isinstance(line["quantity"], bool) for line in result["lines"])


@pytest.mark.parametrize("data", [
    [], {"document_type": "sales_report", "lines": "bad"},
    document(lines=[{"kind": "sale", "name": "a" * 201}]),
    document(lines=[None]), document(unresolved="not a list"),
    document(lines=[{"kind": "sale", "name": "nasi", "sales_only": "true"}]),
    document(lines=[{"kind": "sale", "name": "nasi"}] * 31),
])
def test_invalid_shapes_and_oversized_reports_raise_safe_errors(data):
    with pytest.raises(StepFunVisionError):
        extract(data)


@pytest.mark.parametrize("response", [
    FakeResponse(status=401, payload={"error": "secret provider response"}),
    FakeResponse(status=429), FakeResponse(content="not json"),
    FakeResponse(content="x" * 50001), FakeResponse(payload={"choices": []}),
])
def test_provider_errors_never_leak_response_or_credentials(response):
    with pytest.raises(StepFunVisionError) as caught:
        extract(client=FakeClient(response))
    assert "synthetic-key" not in str(caught.value)
    assert "secret provider response" not in str(caught.value)


def test_provider_timeout_is_safe():
    with pytest.raises(StepFunVisionError):
        extract(client=FakeClient(error=httpx.ReadTimeout("private provider detail")))


@pytest.mark.parametrize("image,media_type", [
    (b"%PDF-1.7", "application/pdf"), (b"not jpeg", "image/jpeg"),
    (JPEG, "image/png"), (b"RIFFxxxxNOPE", "image/webp"),
    (JPEG + b"x" * MAX_IMAGE_BYTES, "image/jpeg"),
])
def test_media_limits_checked_before_provider(image, media_type):
    client = FakeClient()
    with pytest.raises(StepFunVisionError):
        extract(client=client, image=image, media_type=media_type)
    assert client.calls == []


def test_model_json_code_fence_and_strict_decimal_normalization():
    data = document(document_type="count", lines=[{"kind": "count", "name": "Beras", "quantity": "0,0", "unit": "kg"}], revenue_idr=None)
    result = extract(client=FakeClient(FakeResponse(content="```json\n" + json.dumps(data) + "\n```")))
    assert result["lines"][0]["quantity"] == "0"
    assert result["unresolved"] == []
