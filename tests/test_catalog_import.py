"""Bulk catalog import: messy-spreadsheet parsing, store upsert, API endpoint.

The sheets mirror real warung files (junk title rows, Indonesian column
labels) without naming any actual warung.
"""

import base64
import io

import openpyxl
import pytest
from fastapi.testclient import TestClient

from conftest import login_owner
from service.api import create_app
from service.catalog_import import CatalogImportError, parse_catalog_file
from service.db import RESTAURANT, Store

TENANT = RESTAURANT["id"]


def build_xlsx(rows):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


CAKE_SHEET = [
    ["Daftar Kue — stok harian"],  # junk title row above the header
    ["Per 1 Nov 2026"],
    ["no", "kode", "nama brg", "suppl", "pp", "csp", "laba", "qty beli", "satuan", "sisa"],
    [1, "K001", "Kue pandan", "Sumber Kue", 15000, 20000, 5000, 24, "pcs", 8],
    [2, "K002", "Bolu choco", "Sumber Kue", 16000, 21000, 5000, 20, "pcs", 11],
    [3, "K003", "Roti sobek", "Roti Enak", 12000, 15000, 3000, 10, "pcs", "2,5"],
]


# --- Parser ---------------------------------------------------------------------


def test_parse_xlsx_skips_title_rows_and_maps_columns():
    parsed = parse_catalog_file("katalog.xlsx", build_xlsx(CAKE_SHEET))
    assert parsed["skipped"] == 0
    assert parsed["items"] == [
        {"name": "Kue pandan", "unit": "pcs", "quantity": "8", "code": "K001", "threshold": None},
        {"name": "Bolu choco", "unit": "pcs", "quantity": "11", "code": "K002", "threshold": None},
        {"name": "Roti sobek", "unit": "pcs", "quantity": "2.5", "code": "K003", "threshold": None},
    ]


def test_parse_csv_variant_maps_synonyms():
    content = "nama,satuan,jumlah,kode,min\nKue pandan,pcs,8,K001,3\nBolu choco,pcs,,K002,\n".encode()
    parsed = parse_catalog_file("katalog.csv", content)
    assert parsed["skipped"] == 0
    assert parsed["items"] == [
        {"name": "Kue pandan", "unit": "pcs", "quantity": "8", "code": "K001", "threshold": "3"},
        {"name": "Bolu choco", "unit": "pcs", "quantity": None, "code": "K002", "threshold": None},
    ]


def test_parse_skips_invalid_and_duplicate_rows():
    long_name = "K" * 61
    content = (
        "nama bahan,satuan,stok\n"
        "Kue pandan,pcs,8\n"
        "   ,pcs,1\n"  # no usable name
        "KUE PANDAN,pcs,5\n"  # duplicate, case-insensitive: first wins
        f"{long_name},pcs,2\n"  # longer than 60 characters
        "Bolu choco,pcs,sepuluh\n"  # quantity not a number -> None
    ).encode()
    parsed = parse_catalog_file("katalog.csv", content)
    assert [item["name"] for item in parsed["items"]] == ["Kue pandan", "Bolu choco"]
    assert parsed["items"][1]["quantity"] is None
    assert parsed["skipped"] == 3


def test_parse_rejects_unsupported_files_and_missing_header():
    with pytest.raises(CatalogImportError):
        parse_catalog_file("katalog.xls", b"old format")
    with pytest.raises(CatalogImportError):
        parse_catalog_file("katalog.xlsx", b"not a workbook")
    rows = build_xlsx([["no", "suppl", "laba"], [1, "Sumber Kue", 5000]])
    with pytest.raises(CatalogImportError):
        parse_catalog_file("katalog.xlsx", rows)


def test_parse_rejects_more_than_cap_items():
    rows = [["nama", "stok"]] + [[f"Bahan ke-{index}", index] for index in range(201)]
    with pytest.raises(CatalogImportError):
        parse_catalog_file("katalog.xlsx", rows)


# --- Store upsert (sqlite) --------------------------------------------------------


def test_import_catalog_upserts_and_sets_inventory(tmp_path):
    store = Store(tmp_path / "import.sqlite3")
    stored = store.import_catalog(TENANT, [
        {"name": "Kue pandan", "unit": "pcs", "quantity": "8", "code": "K001", "threshold": "3"},
        {"name": "Cabai merah", "unit": None, "quantity": "5", "code": None, "threshold": None},
    ])
    assert [item["name"] for item in stored] == ["Kue pandan", "Cabai merah"]
    names = [entry["name"] for entry in store.catalog_list(TENANT)]
    assert "Kue pandan" in names and names.count("Cabai merah") == 1  # base entry, no duplicate
    overview = {row["name"]: row for row in store.overview(TENANT)["inventory"]}
    assert overview["Kue pandan"]["quantity"] == "8" and overview["Kue pandan"]["unit"] == "pcs"
    assert overview["Cabai merah"]["quantity"] == "5" and overview["Cabai merah"]["unit"] == "kg"

    # A second import upserts: quantities refresh, no new rows, cache stays clean.
    store.import_catalog(TENANT, [{"name": "kue PANDAN", "unit": None, "quantity": "11", "code": None, "threshold": None}])
    catalog = store.catalog_list(TENANT)
    assert [entry["name"] for entry in catalog].count("Kue pandan") == 1
    entry = next(item for item in catalog if item["id"].startswith("kue_pandan"))
    overview = {row["name"]: row for row in store.overview(TENANT)["inventory"]}
    assert overview["Kue pandan"]["quantity"] == "11" and entry["reorder_threshold"] == "3"


def test_import_catalog_scoped_per_restaurant(tmp_path):
    store = Store(tmp_path / "import.sqlite3")
    store.import_catalog(TENANT, [{"name": "Kue pandan", "unit": "pcs", "quantity": "8", "code": None, "threshold": None}])
    assert "Kue pandan" not in [entry["name"] for entry in store.catalog_list("other-resto")]
    assert "Kue pandan" in [entry["name"] for entry in store.catalog_list(TENANT)]


# --- API endpoint -------------------------------------------------------------------


def catalog_api(tmp_path):
    app = create_app(tmp_path / "api.sqlite3")
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    login_owner(client)
    return client


def test_catalog_import_endpoint_round_trip(tmp_path):
    client = catalog_api(tmp_path)
    content = build_xlsx(CAKE_SHEET)
    response = client.post("/v1/catalog/import", json={
        "filename": "katalog.xlsx",
        "content_base64": base64.b64encode(content).decode(),
    })
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["imported"] == 3 and data["skipped"] == 0
    assert [item["name"] for item in data["items"]] == ["Kue pandan", "Bolu choco", "Roti sobek"]
    inventory = {row["name"]: row["quantity"] for row in client.get("/v1/overview").json()["data"]["inventory"]}
    assert inventory["Kue pandan"] == "8" and inventory["Roti sobek"] == "2.5"


def test_catalog_import_endpoint_reports_skipped_rows(tmp_path):
    client = catalog_api(tmp_path)
    content = "nama,stok\nKue pandan,8\n,1\n".encode()
    response = client.post("/v1/catalog/import", json={
        "filename": "katalog.csv",
        "content_base64": base64.b64encode(content).decode(),
    })
    assert response.status_code == 200, response.text
    assert response.json()["data"]["imported"] == 1
    assert response.json()["data"]["skipped"] == 1


def test_catalog_import_endpoint_rejects_bad_input(tmp_path):
    client = catalog_api(tmp_path)
    content = build_xlsx([["nama", "stok"]] + [[f"Bahan ke-{index}", index] for index in range(201)])
    too_many = client.post("/v1/catalog/import", json={
        "filename": "katalog.xlsx",
        "content_base64": base64.b64encode(content).decode(),
    })
    assert too_many.status_code == 400
    assert "200" in too_many.json()["error"]["message"]

    bad_base64 = client.post("/v1/catalog/import", json={"filename": "katalog.csv", "content_base64": "!!!bukan-base64!!!"})
    assert bad_base64.status_code == 400 and bad_base64.json()["error"]["message"]

    wrong_type = client.post("/v1/catalog/import", json={
        "filename": "katalog.docx",
        "content_base64": base64.b64encode(b"docx").decode(),
    })
    assert wrong_type.status_code == 400 and ".xlsx" in wrong_type.json()["error"]["message"]

    anonymous = TestClient(create_app(tmp_path / "anon.sqlite3"), base_url="http://127.0.0.1", raise_server_exceptions=False)
    assert anonymous.post("/v1/catalog/import", json={"filename": "katalog.csv", "content_base64": "aGk="}).status_code in (401, 403)
