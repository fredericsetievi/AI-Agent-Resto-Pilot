"""Chat unit-edit commands: "ganti satuan bolkus jumbo jadi pcs".

Deterministic catalog operations — no model call, no credits. The bare
"ganti cabai merah jadi kg" form only fires when the final word is a
known unit, so general "ganti penjual jadi pasar" sentences never
become unit edits. Catalog-only: runs while a purchase draft waits
without touching the draft (the sentence often names a draft item, and
the follow-up merge must not swallow it).
"""
from uuid import uuid4

from service.api import create_app
from service.db import RESTAURANT, parse_unit_command

TENANT = RESTAURANT["id"]
SENDER = "628123456789"


def make_store(tmp_path):
    app = create_app(tmp_path / "units.sqlite3")
    return app.state.store


def import_items(store, rows):
    """rows: list of (name, unit)."""
    return store.import_catalog(TENANT, [
        {"name": name, "unit": unit, "quantity": None, "code": None, "threshold": None}
        for name, unit in rows
    ])


def catalog_unit(store, name):
    item = next(entry for entry in store.catalog_list(TENANT) if entry["name"].casefold() == name.casefold())
    return item["unit"]


def ingest(store, text):
    return store.whatsapp_ingest(uuid4().hex, {"sender": SENDER, "text": text})


def reply_text(response):
    """The bot's answer to this message (newest outbound first)."""
    outbound = [message["body"] for message in response["messages"] if message["direction"] == "outbound"]
    return outbound[0] if outbound else ""


# --- parser ---------------------------------------------------------------------


def test_parse_unit_command_variants():
    assert parse_unit_command("ganti satuan bolkus jumbo jadi pcs") == {
        "scope": "single", "target": "bolkus jumbo", "unit": "pcs", "explicit": True}
    assert parse_unit_command("ganti semua satuan kue jadi pcs") == {
        "scope": "all", "target": "kue", "unit": "pcs", "explicit": True}
    assert parse_unit_command("ubah unit cabai merah jadi kg.") == {
        "scope": "single", "target": "cabai merah", "unit": "kg", "explicit": True}
    # Bare form only fires on a known unit word.
    assert parse_unit_command("ganti cabai merah jadi kg") == {
        "scope": "single", "target": "cabai merah", "unit": "kg", "explicit": False}
    assert parse_unit_command("ganti penjual jadi pasar senen") is None
    assert parse_unit_command("beli cabai 2 kg 20rb") is None
    # Explicit satuan keyword with an unknown unit: parsed, unit missing.
    parsed = parse_unit_command("ganti satuan telur jadi kilo")
    assert parsed["explicit"] and parsed["unit"] is None and parsed["target"] == "telur"


# --- chat flow ------------------------------------------------------------------


def test_chat_unit_edit_single(tmp_path):
    store = make_store(tmp_path)
    import_items(store, [("Bolkus jumbo", "kg")])
    result = ingest(store, "ganti satuan bolkus jumbo jadi pcs")
    assert result["result"]["action"] == "units_updated", result
    assert "Bolkus jumbo" in reply_text(result) and "pcs" in reply_text(result)
    assert catalog_unit(store, "Bolkus jumbo") == "pcs"


def test_chat_unit_edit_bare_form(tmp_path):
    store = make_store(tmp_path)
    import_items(store, [("Cabai rawit merah", "pcs")])  # "Cabai merah" is a base-catalog name
    result = ingest(store, "ganti cabai rawit merah jadi kg")
    assert result["result"]["action"] == "units_updated", result
    assert catalog_unit(store, "Cabai rawit merah") == "kg"


def test_chat_unit_edit_bulk_semua(tmp_path):
    store = make_store(tmp_path)
    import_items(store, [("Bolu panggang", "kg"), ("Bolu kismis", "kg"), ("Bolkus jumbo", "kg")])
    result = ingest(store, "ganti semua satuan bolu jadi pcs")
    assert result["result"]["action"] == "units_updated", result
    # Substring matching: "bolu" hits the two Bolu items but not "Bolkus".
    assert catalog_unit(store, "Bolu panggang") == "pcs"
    assert catalog_unit(store, "Bolu kismis") == "pcs"
    assert catalog_unit(store, "Bolkus jumbo") == "kg"
    assert "2 bahan" in reply_text(result)


def test_chat_unit_edit_ambiguous_asks_to_specify(tmp_path):
    store = make_store(tmp_path)
    import_items(store, [("Bolu kismis", "kg"), ("Bolu choco", "kg")])
    result = ingest(store, "ganti satuan bolu jadi pcs")
    assert result["result"]["action"] == "unit_edit_rejected", result
    assert "cocok 2 bahan" in reply_text(result)
    assert catalog_unit(store, "Bolu kismis") == "kg"


def test_chat_unit_edit_rejects_base_catalog(tmp_path):
    store = make_store(tmp_path)
    base_unit = catalog_unit(store, "Telur ayam")  # base-catalog name
    result = ingest(store, "ganti satuan telur ayam jadi pcs")
    assert result["result"]["action"] == "unit_edit_rejected", result
    assert "bawaan katalog" in reply_text(result)
    assert catalog_unit(store, "Telur ayam") == base_unit


def test_chat_unit_edit_unknown_unit_lists_options(tmp_path):
    store = make_store(tmp_path)
    import_items(store, [("Bolkus jumbo", "kg")])
    result = ingest(store, "ganti satuan bolkus jumbo jadi kilo")
    assert result["result"]["action"] == "unit_edit_rejected", result
    assert "pcs" in reply_text(result)  # lists the accepted units


def test_chat_unit_edit_runs_while_draft_waits(tmp_path):
    store = make_store(tmp_path)
    import_items(store, [("Bolkus jumbo", "kg")])
    draft = ingest(store, "beli cabai 2 kg 129rb")
    assert draft["result"]["action"] in ("draft_created", "review_required"), draft
    edit = ingest(store, "ganti satuan bolkus jumbo jadi pcs")
    assert edit["result"]["action"] == "units_updated", edit
    assert catalog_unit(store, "Bolkus jumbo") == "pcs"
    # The earlier draft is still alive and can still be approved.
    approved = ingest(store, "ya")
    assert approved["result"]["action"] == "approved", approved
