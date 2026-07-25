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
        "locations",
        "stock_movements",
        "sales",
        "sale_items",
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
