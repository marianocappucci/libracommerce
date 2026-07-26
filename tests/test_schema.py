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
        "item_variants",
        "price_lists",
        "item_prices",
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


def test_schema_rejects_second_default_price_list():
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    conn.execute("INSERT INTO price_lists (name, is_default) VALUES ('Mayorista', 1)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO price_lists (name, is_default) VALUES ('Minorista', 1)")


def test_schema_rejects_item_price_with_valid_until_not_after_valid_from():
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    item_id = _make_item(conn)
    conn.execute("INSERT INTO price_lists (name) VALUES ('General')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO item_prices (item_id, price_list_id, amount, valid_from, valid_until)
            VALUES (?, 1, 1000, '2026-01-01T00:00:00', '2026-01-01T00:00:00')
            """,
            (item_id,),
        )


def test_schema_rejects_duplicate_variant_sku():
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    item_id = _make_item(conn)
    conn.execute("INSERT INTO item_variants (item_id, sku, name) VALUES (?, 'REM-M', 'M')", (item_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO item_variants (item_id, sku, name) VALUES (?, 'REM-M', 'M otra vez')", (item_id,))


def test_schema_rejects_sale_item_with_variant_but_no_item_id():
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    item_id = _make_item(conn)
    conn.execute("INSERT INTO sales (number) VALUES ('V-0001')")
    conn.execute("INSERT INTO item_variants (item_id, sku, name) VALUES (?, 'REM-M', 'M')", (item_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO sale_items
                (sale_id, kind, item_id, variant_id, description_snapshot, quantity, unit_price)
            VALUES (1, 'service', NULL, 1, 'No deberia poder pasar esto', 1, 100)
            """
        )
