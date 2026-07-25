import sqlite3

from libracommerce.db.schema import init_schema


def test_schema_enables_foreign_keys_and_creates_core_tables():
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"parties", "catalog_items", "locations", "stock_movements"} <= tables
