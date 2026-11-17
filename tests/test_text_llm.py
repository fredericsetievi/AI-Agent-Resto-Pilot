"""StepFun text rescue; the provider is a local fake and never called."""

import asyncio
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from service.api import create_app
from service.text_llm import StepFunTextError, clean_extraction, extract_message

MODEL_URL = "https://api.stepfun.com/v1/chat/completions"
FLEXIBLE = "tolong catat saya tadi beli 150 ribu cabai 3kg nih"


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


class FakeTextClient:
    def __init__(self):
        self.calls = []
        self.responses = []

    async def post(self, url, *, headers, json):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.responses.pop(0)


def completion(payload):
    return FakeResponse(body={"choices": [{"message": {"content": json.dumps(payload)}}]})


CABAI_ITEM = {"raw_name": "cabai", "quantity": "3", "unit": "kg", "line_total_idr": 150000}


def run_message(text=FLEXIBLE, client=None, **kwargs):
    return asyncio.run(extract_message(
        text,
        api_key=kwargs.pop("api_key", "synthetic-stepfun-key"),
        client=client,
        **kwargs,
    ))


def rescue_app(tmp_path, fake_client, **kwargs):
    kwargs.setdefault("vision_api_key", "synthetic-stepfun-key")
    app = create_app(
        tmp_path / "demo.sqlite3",
        text_llm_enabled=True,
        text_llm_http_client=fake_client,
        **kwargs,
    )
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    from conftest import login_owner
    login_owner(client)
    return client


def intake(client, text):
    return client.post("/v1/intake", headers={"Idempotency-Key": uuid4().hex}, json={"text": text})


def test_extract_message_maps_a_free_form_sentence():
    client = FakeTextClient()
    client.responses.append(completion({"items": [CABAI_ITEM]}))
    result = run_message(client=client)

    assert result == {"items": [
        {"ingredient_id": "cabai", "raw_name": "cabai", "quantity": "3", "unit": "kg", "line_total_idr": 150000},
    ], "warnings": []}

    call = client.calls[0]
    assert call["url"] == MODEL_URL
    assert call["headers"]["Authorization"] == "Bearer synthetic-stepfun-key"
    assert call["json"]["model"] == "step-3.7-flash"
    assert call["json"]["temperature"] == 0
    assert "Message to read:" in call["json"]["messages"][0]["content"]


def test_extract_message_strips_a_code_fence():
    client = FakeTextClient()
    content = '```json\n{"items": []}\n```'
    client.responses.append(FakeResponse(body={"choices": [{"message": {"content": content}}]}))
    result = run_message(client=client)
    assert result == {"items": [], "warnings": []}


def test_extract_message_rejects_unreadable_json():
    client = FakeTextClient()
    client.responses.append(FakeResponse(body={"choices": [{"message": {"content": "not json"}}]}))
    with pytest.raises(StepFunTextError) as error:
        run_message(client=client)
    assert error.value.status == 502


def test_extract_message_rejects_a_provider_error_without_leaking_the_key():
    client = FakeTextClient()
    client.responses.append(FakeResponse(status_code=401, body={"error": {"message": "bad key"}}))
    with pytest.raises(StepFunTextError) as error:
        run_message(client=client)
    assert error.value.status == 502
    assert "synthetic-stepfun-key" not in str(error.value)
    assert "bad key" not in str(error.value)


def test_extract_message_requires_a_key():
    with pytest.raises(StepFunTextError) as error:
        run_message(client=FakeTextClient(), api_key="")
    assert error.value.status == 503


def test_clean_extraction_keeps_unstated_values_empty():
    result = clean_extraction({"items": [{"raw_name": "cabai", "quantity": None, "unit": None, "line_total_idr": None}]})
    item = result["items"][0]
    assert item["ingredient_id"] == "cabai" and item["raw_name"] == "cabai"
    assert item["quantity"] is None and item["unit"] is None and item["line_total_idr"] is None
    assert len(result["warnings"]) == 3


def test_clean_extraction_keeps_new_ingredients_approvable():
    # Like the rules parser: an unknown name is not a warning. The draft line
    # shows the raw name and approval adds it to the catalog.
    result = clean_extraction({"items": [{"raw_name": "terasi", "quantity": "1", "unit": "kg", "line_total_idr": 49000}]})
    item = result["items"][0]
    assert item["ingredient_id"] is None and item["raw_name"] == "terasi"
    assert result["warnings"] == []


def test_clean_extraction_passes_an_unknown_unit_through_untouched():
    # Like the vision path: an unsupported package is kept as written so the
    # owner picks a catalog pack; the module never invents a conversion.
    result = clean_extraction({"items": [dict(CABAI_ITEM, unit="kardus")]})
    item = result["items"][0]
    assert item["unit"] == "kardus"
    assert result["warnings"] == []


def test_clean_extraction_rejects_more_than_twenty_items():
    items = [{"raw_name": f"item {index}", "quantity": "1", "unit": "kg", "line_total_idr": 1000} for index in range(21)]
    result = clean_extraction({"items": items})
    assert result["items"] == []
    assert "more than 20 items" in result["warnings"][0]


def test_clean_extraction_rejects_a_non_purchase_object():
    with pytest.raises(StepFunTextError):
        clean_extraction(["items"])
    with pytest.raises(StepFunTextError):
        clean_extraction({"lines": []})


def test_intake_rescues_a_free_form_sentence(tmp_path):
    fake = FakeTextClient()
    fake.responses.append(completion({"items": [CABAI_ITEM]}))
    client = rescue_app(tmp_path, fake)

    response = intake(client, FLEXIBLE)
    assert response.status_code == 201, response.text
    draft = response.json()["data"]
    assert draft["status"] == "pending_approval"
    assert draft["items"][0]["ingredient_id"] == "cabai"
    assert draft["items"][0]["quantity"] == "3"
    assert draft["items"][0]["line_total_idr"] == 150000
    assert draft["warnings"] == []
    assert len(fake.calls) == 1


def test_intake_keeps_the_rules_result_for_canonical_text(tmp_path):
    fake = FakeTextClient()
    client = rescue_app(tmp_path, fake)

    draft = intake(client, "beli cabai 3 kg 150rb").json()["data"]
    assert draft["status"] == "pending_approval"
    assert draft["items"][0]["ingredient_id"] == "cabai"
    assert fake.calls == []


def test_intake_falls_back_to_rules_when_the_model_fails(tmp_path):
    fake = FakeTextClient()
    fake.responses.append(FakeResponse(status_code=503, body={"error": {"message": "overloaded"}}))
    client = rescue_app(tmp_path, fake)

    draft = intake(client, FLEXIBLE).json()["data"]
    assert draft["status"] == "needs_clarification"
    # The rules fallback keeps the flexible sentence as one unmodelled item;
    # the "tolong" order-verb prefix is stripped, the rest stays verbatim.
    assert draft["items"][0]["raw_name"] == FLEXIBLE.removeprefix("tolong ")
    assert any("quantity is missing" in warning for warning in draft["warnings"])


def test_intake_falls_back_when_the_model_returns_no_items(tmp_path):
    fake = FakeTextClient()
    fake.responses.append(completion({"items": []}))
    client = rescue_app(tmp_path, fake)

    draft = intake(client, FLEXIBLE).json()["data"]
    assert draft["status"] == "needs_clarification"
    assert draft["items"][0]["raw_name"] == FLEXIBLE.removeprefix("tolong ")


def test_intake_without_the_flag_never_calls_the_model(tmp_path):
    fake = FakeTextClient()
    app = create_app(tmp_path / "demo.sqlite3", vision_api_key="synthetic-stepfun-key")
    client = TestClient(app, base_url="http://127.0.0.1")
    from conftest import login_owner
    login_owner(client)

    draft = intake(client, FLEXIBLE).json()["data"]
    assert draft["status"] == "needs_clarification"
    assert fake.calls == []


def test_simulate_rescues_free_form_text_but_not_commands(tmp_path):
    fake = FakeTextClient()
    fake.responses.append(completion({"items": [dict(CABAI_ITEM, raw_name="bawang", quantity="2", line_total_idr=80000)]}))
    client = rescue_app(tmp_path, fake)

    def simulate(text):
        return client.post(
            "/v1/whatsapp/simulate",
            headers={"Idempotency-Key": uuid4().hex},
            json={"sender": "628123456789", "text": text},
        ).json()["data"]

    result = simulate("tadi saya beli bawang 2 kilo harganya 80 ribu ya")
    assert result["result"]["action"] == "draft_created"
    assert result["draft"]["items"][0]["ingredient_id"] == "bawang"
    assert len(fake.calls) == 1

    command = simulate("ya")
    assert command["result"]["action"] == "approved"
    assert len(fake.calls) == 1


def test_rescued_new_ingredient_approves_and_creates_catalog_entry(tmp_path):
    fake = FakeTextClient()
    fake.responses.append(completion({"items": [{"raw_name": "terasi", "quantity": "1", "unit": "kg", "line_total_idr": 49000}]}))
    client = rescue_app(tmp_path, fake)

    draft = intake(client, FLEXIBLE).json()["data"]
    assert draft["status"] == "pending_approval"
    assert draft["items"][0]["ingredient_id"] is None
    assert draft["items"][0]["raw_name"] == "terasi"
    assert draft["warnings"] == []

    decision = client.post(
        f"/v1/drafts/{draft['id']}/decision",
        headers={"Idempotency-Key": uuid4().hex},
        json={"expected_version": draft["version"], "decision": "approve", "received": True},
    )
    assert decision.status_code == 200, decision.text
    data = decision.json()["data"]
    assert data["status"] == "recorded"
    assert data["new_ingredients"] == ["Terasi"]

    inventory = client.get("/v1/overview").json()["data"]["inventory"]
    assert any(row["ingredient_id"] == "terasi" and str(row["quantity"]) == "1" for row in inventory)


def test_clean_extraction_marks_spelling_recoveries_for_confirmation():
    result = clean_extraction({"items": [{"raw_name": "bwang putih", "quantity": "2", "unit": "kg", "line_total_idr": 30000}]})
    item = result["items"][0]
    assert item["ingredient_id"] == "bawang_putih"
    assert item["matched_from"] == "bwang putih"
    assert result["warnings"] == []


def test_multi_line_forward_gets_the_model_first(tmp_path):
    # A forwarded PO whose greeting line parses into a clean-looking but
    # wrong item ("Selasa kirim 2 gas" -> "selasa") goes to the model even
    # though the rules result carries no warnings.
    fake = FakeTextClient()
    fake.responses.append(completion({"items": [{"raw_name": "gas", "quantity": "2", "unit": None, "line_total_idr": None}]}))
    client = rescue_app(tmp_path, fake)

    draft = intake(client, "Pagi mas.\nSelasa kirim 2 gas").json()["data"]
    assert len(fake.calls) == 1
    assert draft["items"][0]["raw_name"] == "gas"
    assert draft["items"][0]["quantity"] == "2"


def test_single_line_clean_text_stays_rules_only(tmp_path):
    fake = FakeTextClient()
    client = rescue_app(tmp_path, fake)

    draft = intake(client, "beli cabai 3 kg 150rb").json()["data"]
    assert fake.calls == []
    assert draft["items"][0]["ingredient_id"] == "cabai"
    assert draft["items"][0]["quantity"] == "3"


def test_fully_priced_rules_draft_skips_the_model(tmp_path):
    # V34.4 rules read the estimated range as the lower bound with a warning;
    # the model trip used to replace it with a weaker answer that dropped the
    # count. A complete rules draft is never rescued.
    fake = FakeTextClient()
    client = rescue_app(tmp_path, fake)

    draft = intake(client, "beli cabai sekitar 3-4 kg 150rb").json()["data"]
    assert fake.calls == []
    assert draft["items"][0]["ingredient_id"] == "cabai"
    assert draft["items"][0]["quantity"] == "3"
    assert draft["items"][0]["line_total_idr"] == 150000


def test_pack_size_draft_skips_the_model(tmp_path):
    # The owner-quoted pack conversion ("@2ltr") is information the model
    # schema cannot express; the rules draft carries it, so no rescue.
    fake = FakeTextClient()
    client = rescue_app(tmp_path, fake)

    draft = intake(client, "PO Sunco @2ltr 10 dus ya🙏").json()["data"]
    assert fake.calls == []
    assert draft["items"][0]["pack_size"] == "2"
    assert draft["items"][0]["pack_unit"] == "l"


def test_model_tie_keeps_the_rules_result(tmp_path):
    # A supplier price quote ("cabai minggu ini 52rb/kg") — the rules read a
    # priced but unquantified line; a model answer with no more information
    # must not replace it (no phantom drafts on ties).
    fake = FakeTextClient()
    fake.responses.append(completion({"items": [{"raw_name": "cabai", "quantity": None, "unit": None, "line_total_idr": None}]}))
    client = rescue_app(tmp_path, fake)

    draft = intake(client, "cabai minggu ini 52rb/kg").json()["data"]
    assert len(fake.calls) == 1
    assert draft["items"][0]["ingredient_id"] == "cabai"
    assert draft["items"][0]["line_total_idr"] == 52000
    assert draft["items"][0]["quantity"] is None


def test_strictly_stronger_model_answer_still_wins(tmp_path):
    # Rules read a price-only fragment with no count; the model adds the
    # quantity, which is strictly stronger and replaces the rules result.
    fake = FakeTextClient()
    fake.responses.append(completion({"items": [CABAI_ITEM]}))
    client = rescue_app(tmp_path, fake)

    draft = intake(client, FLEXIBLE).json()["data"]
    assert len(fake.calls) == 1
    assert draft["items"][0]["quantity"] == "3"
    assert draft["items"][0]["line_total_idr"] == 150000


# A real forwarded supplier list (pilot fixture): 25 non-empty lines, one over
# the deterministic 20-line cap, no prices anywhere.
DAD_LIST_2 = "\n".join([
    "#2 Sayuran & protein:",
    "2kg daun bawang",
    "5kg terong ungu",
    "4 labu acar",
    "4 labu besar yg muda",
    "5kg wortel",
    "2kg kacang panjang",
    "2kg buncis",
    "6kg kol",
    "2kg sawi putih",
    "1kg caisim",
    "2kg cuciwis",
    "2kg brokoli",
    "2kg kembang kol",
    "10 kg jagung",
    "10kg madu",
    "10kg kentang",
    "5kg cabe merah",
    "10kg bawang merah",
    "2kg daging sapi",
    "10 ikan cue keranjang",
    "30 potong tongkol asap",
    "30 potong paha ayam boiler",
    "2 kg fillet dada ayam boiler",
    "15 ayam pejantan",
])


def _chunk_items(count, offset):
    return [
        {"raw_name": f"bahan {offset + index}", "quantity": "2", "unit": "kg", "line_total_idr": None}
        for index in range(1, count + 1)
    ]


def router_app(tmp_path, fake_client, **kwargs):
    kwargs.setdefault("vision_api_key", "synthetic-stepfun-key")
    app = create_app(
        tmp_path / "demo.sqlite3",
        text_llm_enabled=True,
        text_llm_http_client=fake_client,
        forwarded_router_enabled=True,
        **kwargs,
    )
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    from conftest import login_owner
    login_owner(client)
    return client


def test_forwarded_router_reads_a_long_list_in_chunks(tmp_path):
    # LLM_FORWARDED_ROUTER on: the 25-line forward is split into cap-sized
    # chunks, read through the same rescue, and merged into one flagged draft.
    fake = FakeTextClient()
    fake.responses.append(completion({"items": _chunk_items(20, 0)}))
    fake.responses.append(completion({"items": _chunk_items(5, 20)}))
    client = router_app(tmp_path, fake)

    draft = intake(client, DAD_LIST_2).json()["data"]
    assert len(fake.calls) == 2
    assert len(draft["items"]) == 25
    # The rescue never invents values: no prices were stated, so every row
    # stays empty on money and the whole draft waits flagged for confirmation.
    assert all(item["line_total_idr"] is None for item in draft["items"])
    assert draft["status"] == "needs_clarification"
    first, second = fake.calls
    assert "2kg daun bawang" in first["json"]["messages"][0]["content"]
    assert "15 ayam pejantan" not in first["json"]["messages"][0]["content"]
    assert "15 ayam pejantan" in second["json"]["messages"][0]["content"]
    # The tenant rule rides along on every chunk.
    assert "Warung Bu Sari" in second["json"]["messages"][0]["content"]


def test_forwarded_router_failure_keeps_the_rules_refusal(tmp_path):
    # A provider error must never turn a refusal into a partial draft.
    fake = FakeTextClient()
    fake.responses.append(FakeResponse(status_code=503, body={"error": {"message": "overloaded"}}))
    client = router_app(tmp_path, fake)

    draft = intake(client, DAD_LIST_2).json()["data"]
    assert len(fake.calls) == 1
    assert draft["items"] == []
    assert any("20-item limit" in warning for warning in draft["warnings"])


def test_forwarded_router_disabled_keeps_the_refusal_and_skips_the_model(tmp_path):
    fake = FakeTextClient()
    app = create_app(tmp_path / "demo.sqlite3", vision_api_key="synthetic-stepfun-key", text_llm_enabled=True, text_llm_http_client=fake)
    client = TestClient(app, base_url="http://127.0.0.1")
    from conftest import login_owner
    login_owner(client)

    draft = intake(client, DAD_LIST_2).json()["data"]
    assert fake.calls == []
    assert draft["items"] == []
    assert any("20-item limit" in warning for warning in draft["warnings"])


def test_forwarded_router_ignores_a_long_chatty_message(tmp_path):
    # A long forward without numbers (chat log, article) is not an itemized
    # list; no model trip, the refusal stands.
    fake = FakeTextClient()
    client = router_app(tmp_path, fake)

    chat = "\n".join(f"baris cerita tanpa angka nomor {word}" for word in ["satu", "dua", "tiga", "empat", "lima"] * 5)
    draft = intake(client, chat).json()["data"]
    assert fake.calls == []
    assert draft["items"] == []


def test_forwarded_router_caps_one_merged_draft(tmp_path):
    # A runaway forward cannot produce an unusable wall of rows: the merged
    # draft stops at the forwarded cap and says so.
    fake = FakeTextClient()
    for offset in (0, 20, 40):
        fake.responses.append(completion({"items": _chunk_items(20, offset)}))
    client = router_app(tmp_path, fake)

    long_list = "\n".join(f"{index}kg bahan nomor {index}" for index in range(1, 61))
    draft = intake(client, long_list).json()["data"]
    assert len(fake.calls) == 3
    assert len(draft["items"]) == 50
    assert any("more than 50 items" in warning for warning in draft["warnings"])


def test_dads_single_line_supplier_list_drafts_flagged(tmp_path):
    # The first pilot fixture: one slash-separated line, no prices. The rules
    # parser reads fragments; the rescue turns it into item rows whose money
    # fields stay empty for the owner to fill.
    fake = FakeTextClient()
    fake.responses.append(completion({"items": [
        {"raw_name": "telor ayam", "quantity": "2", "unit": None, "line_total_idr": None},
        {"raw_name": "gula merah", "quantity": "2", "unit": None, "line_total_idr": None},
        {"raw_name": "kemiri", "quantity": "5", "unit": "kg", "line_total_idr": None},
    ]}))
    client = rescue_app(tmp_path, fake)

    draft = intake(client, "#1 Raw materials: 2 telor ayam / 2 gula merah / 5kg kemiri").json()["data"]
    assert len(fake.calls) == 1
    assert [item["raw_name"] for item in draft["items"]] == ["telor ayam", "gula merah", "kemiri"]
    assert draft["items"][2]["quantity"] == "5"
    assert all(item["line_total_idr"] is None for item in draft["items"])
    assert draft["status"] == "needs_clarification"


# ---- context gate (LLM_CONTEXT_GATE) -----------------------------------------

CHAT_TIMER = "besok tutup toko jam 8 ya"  # parses as "besok tutup toko jam — 8 pcs"


def gate_app(tmp_path, fake_client, **kwargs):
    kwargs.setdefault("vision_api_key", "synthetic-stepfun-key")
    app = create_app(
        tmp_path / "demo.sqlite3",
        text_llm_enabled=True,
        text_llm_http_client=fake_client,
        context_gate_enabled=True,
        **kwargs,
    )
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    from conftest import login_owner, SENDER_NUMBER
    login_owner(client)
    return client


def sim(client, text):
    from conftest import SENDER_NUMBER
    return client.post("/v1/whatsapp/simulate", headers={"Idempotency-Key": uuid4().hex},
                       json={"sender": SENDER_NUMBER, "text": text}).json()["data"]


def test_context_gate_drops_a_chat_draft_and_explains(tmp_path):
    from service.extraction import extract_purchase, has_uncontextual_quantity_items
    assert has_uncontextual_quantity_items(extract_purchase(CHAT_TIMER))  # fixture sanity

    fake = FakeTextClient()
    fake.responses.append(completion({"is_purchase": False, "intent": "reminder", "summary": "pengingat tutup toko"}))
    client = gate_app(tmp_path, fake)

    data = sim(client, CHAT_TIMER)
    assert len(fake.calls) == 1  # the gate ran; no rescue followed, no draft
    assert data["result"]["action"] == "help_reply"
    reply = data["messages"][-1]["body"]
    assert "bukan catatan pembelian" in reply
    assert "pengingat tutup toko" in reply
    assert "pengingat pagi jadi jam 8" in reply  # the deterministic next step
    assert not data.get("draft")


def test_context_gate_passes_real_purchases_through(tmp_path):
    fake = FakeTextClient()
    fake.responses.append(completion({"is_purchase": True, "intent": "purchase", "summary": "beli bahan masak"}))
    fake.responses.append(completion({"items": [CABAI_ITEM]}))
    client = gate_app(tmp_path, fake)

    data = sim(client, "besok saya mau ke pasar beli bahan jam 7 pagi")
    assert len(fake.calls) == 2  # gate said purchase, the rescue ran as usual
    assert data["draft"]["items"]


def test_context_gate_failure_keeps_the_rules_behavior(tmp_path):
    # Fail-open: a provider error must never change what the parser did.
    # The gate call and the later rescue both fail; the rules draft stands.
    fake = FakeTextClient()
    fake.responses.append(FakeResponse(status_code=503, body={"error": {"message": "overloaded"}}))
    fake.responses.append(FakeResponse(status_code=503, body={"error": {"message": "overloaded"}}))
    client = gate_app(tmp_path, fake)

    data = sim(client, CHAT_TIMER)
    assert len(fake.calls) == 2
    assert data["draft"]["items"][0]["raw_name"] == "besok tutup toko jam"
    assert data["draft"]["items"][0]["quantity"] == "8"


def test_context_gate_off_never_calls_the_model(tmp_path):
    fake = FakeTextClient()
    fake.responses.append(completion({"items": []}))  # only the rescue may call
    app = create_app(tmp_path / "demo.sqlite3", vision_api_key="synthetic-stepfun-key", text_llm_enabled=True, text_llm_http_client=fake)
    client = TestClient(app, base_url="http://127.0.0.1")
    from conftest import login_owner, SENDER_NUMBER
    login_owner(client)

    client.post("/v1/whatsapp/simulate", headers={"Idempotency-Key": uuid4().hex},
                json={"sender": SENDER_NUMBER, "text": CHAT_TIMER})
    assert len(fake.calls) == 1  # the rescue ran; the gate never fired


def test_classify_context_verdicts():
    import asyncio

    from service.intent_llm import classify_context
    from service.stock_llm import StepFunTextError

    fake = FakeTextClient()
    fake.responses.append(completion({"is_purchase": False, "intent": "question", "summary": "tanya tutup toko"}))
    verdict = asyncio.run(classify_context("tanya", api_key="k", client=fake))
    assert verdict == {"is_purchase": False, "intent": "question", "summary": "tanya tutup toko"}

    fake = FakeTextClient()
    fake.responses.append(completion({"intent": "question"}))  # no is_purchase
    try:
        asyncio.run(classify_context("tanya", api_key="k", client=fake))
        raise AssertionError("missing is_purchase must fail")
    except StepFunTextError:
        pass


def test_context_note_reply_covers_all_intents():
    from service.whatsapp import chat_note_reply

    assert "pengingat pagi jadi jam 8" in chat_note_reply("reminder")
    assert "stok" in chat_note_reply("question")
    assert "bukan catatan pembelian" in chat_note_reply("chat")
    assert "(cek pasar)" in chat_note_reply("other", "cek pasar")


def test_an_unset_text_model_falls_back_to_the_default(tmp_path):
    # Live regression risk: STEPFUN_TEXT_MODEL unset sent "model": null to
    # StepFun, which answers 404, and every text-LLM path silently failed
    # open to the deterministic parser. The app must fall back to the
    # documented default model instead.
    fake = FakeTextClient()
    fake.responses.append(completion({"items": [CABAI_ITEM]}))
    app = create_app(tmp_path / "demo.sqlite3", vision_api_key="synthetic-stepfun-key",
                     text_llm_enabled=True, text_llm_http_client=fake, text_llm_model=None)
    client = TestClient(app, base_url="http://127.0.0.1")
    from conftest import login_owner
    login_owner(client)
    intake(client, "cabai 3 kg, ada peringatan")
    from service.text_llm import DEFAULT_MODEL
    assert fake.calls[0]["json"]["model"] == DEFAULT_MODEL
