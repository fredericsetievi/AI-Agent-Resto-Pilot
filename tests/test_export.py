"""CSV export endpoints for purchases and stock."""

import csv
import io

import pytest
from fastapi.testclient import TestClient

from service.api import create_app


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path / "export.sqlite3")


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as value:
        yield value


def csv_rows(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    return list(csv.reader(io.StringIO(response.text)))


def test_purchases_csv_lists_recorded_purchases(client):
    rows = csv_rows(client.get("/v1/export/purchases.csv"))
    assert rows[0] == ["id", "created_at", "supplier", "item_count", "total_idr", "status"]
    assert len(rows) == 3
    assert {row[2] for row in rows[1:]} == {"Pasar Pagi"}
    assert {row[3] for row in rows[1:]} == {"1"}
    assert [row[4] for row in rows[1:]] == ["430000", "129000"]
    assert {row[5] for row in rows[1:]} == {"unknown"}


def test_stock_csv_lists_the_catalog_with_last_recorded_prices(client):
    rows = csv_rows(client.get("/v1/export/stock.csv"))
    assert rows[0] == ["item", "unit", "current_stock", "last_unit_price"]
    assert len(rows) == 10
    by_item = {row[0]: row for row in rows[1:]}
    assert by_item["Telur ayam"] == ["Telur ayam", "kg", "10", "43000"]
    assert by_item["Cabai merah"] == ["Cabai merah", "kg", "3", "43000"]
    assert by_item["Beras"] == ["Beras", "kg", "12", ""]
    assert by_item["Kecombrang"] == ["Kecombrang", "kg", "0", ""]


def test_export_respects_an_empty_database(tmp_path):
    with TestClient(create_app(tmp_path / "empty.sqlite3", seed=False), base_url="http://127.0.0.1") as client:
        purchases = csv_rows(client.get("/v1/export/purchases.csv"))
        stock = csv_rows(client.get("/v1/export/stock.csv"))
    assert purchases == [["id", "created_at", "supplier", "item_count", "total_idr", "status"]]
    assert stock[0] == ["item", "unit", "current_stock", "last_unit_price"]
    assert len(stock) == 10
    assert all(row[2] == "0" and row[3] == "" for row in stock[1:])


def test_export_respects_the_demo_boundary(client):
    assert client.get("/v1/export/purchases.csv", headers={"X-Demo-Role": "admin"}).status_code == 403
    assert client.get("/v1/export/stock.csv", headers={"Host": "evil.example"}).status_code == 403
