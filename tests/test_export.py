"""CSV export endpoints for purchases and stock."""

import csv
import io
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from service.api import create_app
from service.db import RESTAURANT_TIMEZONE


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path / "export.sqlite3")


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as value:
        from conftest import login_owner
        login_owner(value)
        yield value


def csv_rows(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    return list(csv.reader(io.StringIO(response.text)))


def month_window():
    start = (datetime.now(RESTAURANT_TIMEZONE) - timedelta(days=30)).strftime("%Y-%m-%d")
    today = datetime.now(RESTAURANT_TIMEZONE).strftime("%Y-%m-%d")
    return start, today


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
        from conftest import login_owner
        login_owner(client)
        purchases = csv_rows(client.get("/v1/export/purchases.csv"))
        stock = csv_rows(client.get("/v1/export/stock.csv"))
    assert purchases == [["id", "created_at", "supplier", "item_count", "total_idr", "status"]]
    assert stock[0] == ["item", "unit", "current_stock", "last_unit_price"]
    assert len(stock) == 10
    assert all(row[2] == "0" and row[3] == "" for row in stock[1:])


def test_export_respects_the_demo_boundary(client, app):
    assert client.get("/v1/export/purchases.csv", headers={"X-Demo-Role": "admin"}).status_code == 200
    assert client.get("/v1/export/stock.csv", headers={"Host": "evil.example"}).status_code == 403
    from conftest import login_admin
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as admin:
        login_admin(admin)
        assert admin.get("/v1/export/purchases.csv").status_code == 403
        assert admin.get("/v1/export/purchase-lines.csv").status_code == 403


# --- Line-level purchase export (the "filter bolu kukus in Excel" ask) ---------


def test_purchase_lines_csv_lists_one_row_per_item_line(client):
    start, today = month_window()
    rows = csv_rows(client.get(f"/v1/export/purchase-lines.csv?start={start}&end={today}"))
    assert rows[0] == ["purchase_id", "created_at", "supplier", "item", "quantity", "unit", "unit_price_idr", "line_total_idr"]
    assert len(rows) == 3  # two seeded purchases, one line each
    by_item = {row[3]: row for row in rows[1:]}
    assert set(by_item) == {"Cabai merah", "Telur ayam"}
    cabai = by_item["Cabai merah"]
    assert cabai[2] == "Pasar Pagi"
    assert cabai[4:8] == ["3", "kg", "43000", "129000"]
    assert cabai[1].endswith("+07:00")  # WIB timestamps, day-bounded window


def test_purchase_lines_csv_filters_by_ingredient(client):
    start, today = month_window()
    rows = csv_rows(client.get(f"/v1/export/purchase-lines.csv?start={start}&end={today}&ingredient_id=cabai"))
    assert [row[3] for row in rows[1:]] == ["Cabai merah"]
    assert rows[1][7] == "129000"


def test_purchase_lines_csv_rejects_incomplete_or_unknown_filters(client):
    assert client.get("/v1/export/purchase-lines.csv?start=2026-11-01").status_code == 422
    assert client.get("/v1/export/purchase-lines.csv?start=oops&end=2026-11-18").status_code == 422
    assert client.get("/v1/export/purchase-lines.csv?ingredient_id=not-in-catalog").status_code == 422
