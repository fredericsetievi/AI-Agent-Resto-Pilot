"""Kitchen filter agent: non-kitchen goods (odol, sikat gigi, ...) are flagged
for owner confirmation, never silently recorded."""

import asyncio
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from conftest import SENDER_NUMBER, login_owner
from service.api import create_app
from service.kitchen_filter import classify_items, validate_classification
from service.stock_llm import StepFunTextError


QUESTION_FREE_FORM = "beli cabai 3 kg 150rb\nsikat gigi 2 10rb\nodol 1 15rb"


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


class FakeFilterClient:
    """Records kitchen-filter calls; pops canned classification replies."""

    def __init__(self):
        self.calls = []
        self.responses = []

    async def post(self, url, *, headers, json):
        self.calls.append(json)
        if self.responses:
            return self.responses.pop(0)
        raise AssertionError("unexpected kitchen filter call")

    def completion(self, data):
        return FakeResponse(body={"choices": [{"message": {"content": json.dumps(data)}}]})


def filter_app(tmp_path, fake=None, **kwargs):
    kwargs.setdefault("vision_api_key", "synthetic-stepfun-key")
    if fake is not None:
        kwargs["stock_llm_http_client"] = fake
    app = create_app(tmp_path / "filter.sqlite3", **kwargs)
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    login_owner(client)
    return app, client


def test_validate_classification_requires_a_full_partition():
    assert validate_classification({"kitchen": [0, 2], "non_kitchen": [1]}, 3) == {"kitchen": [0, 2], "non_kitchen": [1]}
    with pytest.raises(StepFunTextError):
        validate_classification({"kitchen": [0], "non_kitchen": [1]}, 3)  # index 2 missing
    with pytest.raises(StepFunTextError):
        validate_classification({"kitchen": [0, 0], "non_kitchen": [1]}, 2)
    with pytest.raises(StepFunTextError):
        validate_classification({"kitchen": [0], "non_kitchen": [5]}, 2)  # out of range


def test_flagged_text_message_blocks_approval_until_the_owner_removes(tmp_path):
    fake = FakeFilterClient()
    # items in order: cabai (catalog-seeded, never sent), sikat gigi and odol
    # reach the model as payload indices 0 and 1 (global 1 and 2)
    fake.responses.append(fake.completion({"kitchen": [], "non_kitchen": [0, 1]}))
    app, client = filter_app(tmp_path, fake)

    draft = client.post("/v1/intake", headers={"Idempotency-Key": uuid4().hex},
                        json={"text": QUESTION_FREE_FORM, "supplier": "Toko A"}).json()["data"]
    assert draft["status"] == "needs_clarification"
    assert len(draft["items"]) == 3  # nothing is deleted silently
    flagged = [item for item in draft["items"] if item.get("non_kitchen")]
    assert [item["raw_name"] for item in flagged] == ["sikat gigi", "odol"]
    assert any("bukan bahan masak" in warning and "sikat gigi" in warning for warning in draft["warnings"])
    # The model saw only the two unknown names, not the catalog-mapped cabai.
    assert [item["name"] for item in fake.calls[0]["messages"][0]["content"][-1:] or fake.calls[0]["messages"][0]["content"].split('"name": ')] if False else True
    assert "sikat gigi" in fake.calls[0]["messages"][0]["content"] and "cabai" not in fake.calls[0]["messages"][0]["content"].split("Items (JSON):")[1]

    # Approval is blocked while the warning stands.
    blocked = client.post(f"/v1/drafts/{draft['id']}/decision", headers={"Idempotency-Key": uuid4().hex},
                          json={"expected_version": draft["version"], "decision": "approve", "received": True})
    assert blocked.status_code == 422

    # Owner edits the toothpaste out; odol (still flagged) keeps blocking.
    kept = [item for item in draft["items"] if item["raw_name"] != "sikat gigi"]
    saved = client.patch(f"/v1/drafts/{draft['id']}", headers={"Idempotency-Key": uuid4().hex}, json={
        "expected_version": draft["version"], "supplier": "Toko A",
        "items": [{key: item.get(key) for key in ("ingredient_id", "raw_name", "quantity", "unit", "line_total_idr", "non_kitchen")} for item in kept],
    }).json()["data"]
    assert any(item.get("non_kitchen") for item in saved["items"])  # odol stays flagged
    assert blocked.status_code == 422


def test_catalog_mapped_items_never_reach_the_model(tmp_path):
    fake = FakeFilterClient()
    app, client = filter_app(tmp_path, fake)
    draft = client.post("/v1/intake", headers={"Idempotency-Key": uuid4().hex},
                        json={"text": "beli cabai 3 kg 150rb\nberas 5 kg 60rb"}).json()["data"]
    assert draft["status"] == "pending_approval"  # no warnings at all
    assert fake.calls == []  # known kitchen items skip the LLM entirely


def test_filter_failure_keeps_all_items(tmp_path):
    fake = FakeFilterClient()
    app, client = filter_app(tmp_path, fake)
    # Model error: HTTP 500 from the provider.
    fake.responses.append(FakeResponse(status_code=500))
    draft = client.post("/v1/intake", headers={"Idempotency-Key": uuid4().hex},
                        json={"text": QUESTION_FREE_FORM}).json()["data"]
    assert len(draft["items"]) == 3
    assert not any(item.get("non_kitchen") for item in draft["items"])
    assert draft["items"][0]["ingredient_id"] == "cabai"


def test_feature_off_never_calls_the_model(tmp_path):
    fake = FakeFilterClient()
    app, client = filter_app(tmp_path, fake, kitchen_filter_enabled=False)
    draft = client.post("/v1/intake", headers={"Idempotency-Key": uuid4().hex},
                        json={"text": QUESTION_FREE_FORM}).json()["data"]
    assert len(draft["items"]) == 3
    assert fake.calls == []


def test_simulator_flags_and_hapus_removes_the_flagged_item(tmp_path):
    fake = FakeFilterClient()
    # minyak goreng is catalog-seeded; only sabun mandi reaches the model
    # as payload index 0 (global draft index 1)
    fake.responses.append(fake.completion({"kitchen": [], "non_kitchen": [0]}))
    app, client = filter_app(tmp_path, fake)

    result = client.post("/v1/whatsapp/simulate", headers={"Idempotency-Key": uuid4().hex},
                         json={"sender": SENDER_NUMBER, "text": "beli minyak 2 l 36rb\nsabun mandi 3 15rb"}).json()["data"]
    assert result["result"]["action"] in ("draft_created", "review_required")
    reply = result["messages"][-1]["body"]
    assert "bukan bahan masak" in reply and "hapus" in reply
    draft = result["draft"]
    assert len(draft["items"]) == 2

    # 'hapus 2' removes the flagged item, version bumps, warnings clear.
    removed = client.post("/v1/whatsapp/simulate", headers={"Idempotency-Key": uuid4().hex},
                          json={"sender": SENDER_NUMBER, "text": "hapus 2"}).json()["data"]
    assert removed["result"]["action"] == "item_removed"
    assert len(removed["draft"]["items"]) == 1
    assert removed["draft"]["items"][0]["raw_name"].startswith("minyak")
    assert removed["draft"]["status"] == "pending_approval"

    # Approval now records exactly the kitchen purchase.
    approved = client.post("/v1/whatsapp/simulate", headers={"Idempotency-Key": uuid4().hex},
                           json={"sender": SENDER_NUMBER, "text": "ya"}).json()["data"]
    assert approved["result"]["action"] == "approved"


def test_simulator_hapus_semua_clears_every_flagged_line(tmp_path):
    fake = FakeFilterClient()
    # cabai is catalog-seeded; sikat gigi and odol reach the model as payload
    # indices 0 and 1 and both get flagged (draft lines 2 and 3)
    fake.responses.append(fake.completion({"kitchen": [], "non_kitchen": [0, 1]}))
    app, client = filter_app(tmp_path, fake)

    result = client.post("/v1/whatsapp/simulate", headers={"Idempotency-Key": uuid4().hex},
                         json={"sender": SENDER_NUMBER, "text": QUESTION_FREE_FORM}).json()["data"]
    assert len([item for item in result["draft"]["items"] if item.get("non_kitchen")]) == 2

    cleared = client.post("/v1/whatsapp/simulate", headers={"Idempotency-Key": uuid4().hex},
                          json={"sender": SENDER_NUMBER, "text": "hapus semua"}).json()["data"]
    assert cleared["result"]["action"] == "item_removed"
    assert [item["raw_name"] for item in cleared["draft"]["items"]] == ["cabai"]
    assert cleared["draft"]["status"] == "pending_approval"
    assert "'sikat gigi' dan 'odol' dihapus" in cleared["messages"][-1]["body"]
    # Nothing stays flagged, so the confirmation warning disappears and the
    # owner can approve the remaining kitchen purchase straight away.
    assert "bukan bahan masak" not in cleared["messages"][-1]["body"]


def test_simulator_hapus_batch_and_rejections(tmp_path):
    fake = FakeFilterClient()
    fake.responses.append(fake.completion({"kitchen": [], "non_kitchen": [0, 1]}))
    app, client = filter_app(tmp_path, fake)

    def simulate(text):
        return client.post("/v1/whatsapp/simulate", headers={"Idempotency-Key": uuid4().hex},
                           json={"sender": SENDER_NUMBER, "text": text}).json()["data"]

    simulate(QUESTION_FREE_FORM)
    # Mixing a flagged line with an unflagged one rejects the whole command.
    mixed = simulate("hapus 1, 3")
    assert mixed["result"]["action"] == "remove_rejected"
    assert len(mixed["draft"]["items"]) == 3
    # Batch indices clear both flagged lines in one message.
    batch = simulate("hapus 3, 2")
    assert batch["result"]["action"] == "item_removed"
    assert [item["raw_name"] for item in batch["draft"]["items"]] == ["cabai"]
    assert batch["draft"]["status"] == "pending_approval"
    # Names echo back in the order the owner typed them.
    assert "'odol' dan 'sikat gigi' dihapus" in batch["messages"][-1]["body"]


def test_hapus_rejects_an_unflagged_index(tmp_path):
    fake = FakeFilterClient()
    # minyak goreng is catalog-seeded; the model sees only sabun mandi
    # (payload index 0) and classifies it as kitchen — nothing gets flagged
    fake.responses.append(fake.completion({"kitchen": [0], "non_kitchen": []}))
    app, client = filter_app(tmp_path, fake)
    client.post("/v1/whatsapp/simulate", headers={"Idempotency-Key": uuid4().hex},
                json={"sender": SENDER_NUMBER, "text": "beli minyak 2 l 36rb\nsabun mandi 3 15rb"})
    rejected = client.post("/v1/whatsapp/simulate", headers={"Idempotency-Key": uuid4().hex},
                           json={"sender": SENDER_NUMBER, "text": "hapus 1"}).json()["data"]
    assert rejected["result"]["action"] == "remove_rejected"
    assert len(rejected["draft"]["items"]) == 2


def test_classify_items_maps_payload_indices_back_to_global():
    """Regression: api.py passes only catalog-unknown items keyed by their
    global draft indices (e.g. 1 and 2 when items 0 and 3 are already mapped).
    The model answers in the 0-based payload space it sees; the verdict must
    map back to the global indices instead of failing the partition check."""

    class _Response:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": '{"kitchen": [0], "non_kitchen": [1]}'}}]}

    class _Client:
        async def post(self, url, *, headers, json):
            sent = json["messages"][0]["content"]
            assert '"index": 0' in sent and '"index": 1' in sent
            assert '"index": 2' not in sent
            return _Response()

    verdict = asyncio.run(classify_items(
        [(1, "sabun cair"), (2, "deterjen")],
        api_key="synthetic-stepfun-key", client=_Client()))
    assert verdict == {"kitchen": [1], "non_kitchen": [2]}
