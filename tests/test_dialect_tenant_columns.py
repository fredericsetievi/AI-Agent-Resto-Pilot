"""Pin the tenant column of every dual-store purchases query to its dialect.

The hosted Postgres schema is mixed: purchases/movements/drafts/stock key on
``slug`` while inventory_events/inventory_recipes/inventory keep
``restaurant_id``. Any purchases SQL that hardcodes ``restaurant_id`` 500s on
the hosted deployment (same failure class as supplier_for_phone, fixed in
PR #26). These tests capture the SQL each mixin builds on both dialects.
"""
import contextlib

from service.menu_insights import MenuInsightsMixin
from service.purchase_edits import PurchaseEditsMixin
from service.suppliers import SupplierMixin


class _DialectStub:
    """Captures the SQL each mixin sends, without touching a database."""

    postgres = None
    captured_sql = None

    def __init__(self, postgres):
        self.inventory_postgres = postgres
        self.captured_sql = None

    def transaction(self):
        return contextlib.nullcontext(None)

    def _inventory_query(self, conn, sql, params):
        self.captured_sql = sql

        class _Cursor:
            def fetchone(self):
                return None

            def fetchall(self):
                return []

        return _Cursor()


class _SupplierStub(_DialectStub, SupplierMixin):
    pass


class _MenuStub(_DialectStub, MenuInsightsMixin, SupplierMixin):
    pass


class _EditsStub(_DialectStub, PurchaseEditsMixin, SupplierMixin):
    pass


def _assert_dialect(stub):
    if stub.inventory_postgres:
        assert "slug" in stub.captured_sql
        assert "restaurant_id" not in stub.captured_sql
    else:
        assert "restaurant_id" in stub.captured_sql


def test_supplier_purchase_price_selects_dialect_tenant_column():
    for postgres in (True, False):
        stub = _SupplierStub(postgres)
        assert stub.supplier_purchase_price(None, "demo-resto", "cabai") is None
        _assert_dialect(stub)


def test_menu_ingredient_unit_prices_selects_dialect_tenant_column():
    for postgres in (True, False):
        stub = _MenuStub(postgres)
        assert stub._ingredient_unit_prices(None, "demo-resto", days=7) == {}
        _assert_dialect(stub)


def test_purchase_edit_rows_select_dialect_tenant_column():
    for postgres in (True, False):
        stub = _EditsStub(postgres)
        assert stub._edit_purchase_rows(None, "demo-resto") == []
        _assert_dialect(stub)


def test_purchase_edit_load_selects_dialect_tenant_column():
    for postgres in (True, False):
        stub = _EditsStub(postgres)
        assert stub._edit_load_purchase(None, "demo-resto", "a1b2c3d4") is None
        _assert_dialect(stub)
