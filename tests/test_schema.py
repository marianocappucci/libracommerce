import sqlite3

import pytest

from libracommerce.db.schema import init_schema


def test_schema_enables_foreign_keys_and_creates_core_tables():
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "parties",
        "categories",
        "units",
        "catalog_items",
        "item_codes",
        "locations",
        "stock_movements",
        "sales",
        "sale_items",
        "purchase_orders",
        "purchase_order_items",
        "purchase_receipts",
        "purchase_receipt_items",
    } <= tables


def test_schema_rejects_stock_movement_for_unknown_item():
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO stock_movements
                (item_id, location_id, movement_type, quantity_delta, occurred_at)
            VALUES (999, 999, 'purchase', 1, '2026-07-25T00:00:00')
            """
        )


def test_schema_rejects_product_sale_item_without_catalog_link():
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    conn.execute("INSERT INTO sales (number) VALUES ('V-0001')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO sale_items
                (sale_id, kind, item_id, description_snapshot, quantity, unit_price)
            VALUES (1, 'product', NULL, 'Sin catalogar', 1, 100)
            """
        )


def test_schema_allows_service_sale_item_without_catalog_link():
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    conn.execute("INSERT INTO sales (number) VALUES ('V-0001')")
    conn.execute(
        """
        INSERT INTO sale_items
            (sale_id, kind, item_id, description_snapshot, quantity, unit_price)
        VALUES (1, 'service', NULL, 'Consulta ad-hoc', 1, 5000)
        """
    )
    count = conn.execute("SELECT COUNT(*) FROM sale_items").fetchone()[0]
    assert count == 1


def _make_item(conn: sqlite3.Connection) -> int:
    conn.execute("INSERT INTO units (code, name) VALUES ('u', 'Unidad')")
    conn.execute(
        "INSERT INTO catalog_items (item_type, name, unit_code) VALUES ('product', 'Fideos', 'u')"
    )
    return conn.execute("SELECT id FROM catalog_items").fetchone()[0]


def test_schema_rejects_duplicate_code_within_same_type():
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    item_id = _make_item(conn)
    conn.execute(
        "INSERT INTO item_codes (item_id, code_type, code) VALUES (?, 'barcode', '7791234567890')",
        (item_id,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO item_codes (item_id, code_type, code) VALUES (?, 'barcode', '7791234567890')",
            (item_id,),
        )


def test_schema_rejects_second_primary_code_for_same_item():
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    item_id = _make_item(conn)
    conn.execute(
        "INSERT INTO item_codes (item_id, code_type, code, is_primary) VALUES (?, 'internal', 'A1', 1)",
        (item_id,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO item_codes (item_id, code_type, code, is_primary) VALUES (?, 'barcode', '7791234567890', 1)",
            (item_id,),
        )
