"""Verifica que init_schema() migre correctamente una base creada con el
esquema anterior a Fase 4 (sin variant_id en stock_movements/sale_items)
-- reproduce el bug real del 2026-07-26 (ver migrations.py) antes de
confiar en que el fix funciona."""
import sqlite3

import pytest

from libracommerce.db.schema import init_schema


def _old_schema_conn() -> sqlite3.Connection:
    """Una base con el esquema tal cual estaba ANTES de que item_variants
    (Fase 4) agregara variant_id -- mismo shape que cualquier despliegue
    real creado antes de esa migracion."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE catalog_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_type TEXT NOT NULL,
            name TEXT NOT NULL,
            unit_code TEXT
        );

        CREATE TABLE locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL
        );

        CREATE TABLE stock_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL REFERENCES catalog_items(id),
            location_id INTEGER NOT NULL REFERENCES locations(id),
            movement_type TEXT NOT NULL,
            quantity_delta NUMERIC NOT NULL CHECK (quantity_delta <> 0),
            occurred_at TEXT NOT NULL,
            source_type TEXT,
            source_id INTEGER,
            unit_cost NUMERIC,
            lot_code TEXT,
            expires_at TEXT
        );

        CREATE TABLE sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            number TEXT NOT NULL UNIQUE
        );

        CREATE TABLE sale_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL REFERENCES sales(id),
            kind TEXT NOT NULL,
            item_id INTEGER REFERENCES catalog_items(id),
            description_snapshot TEXT NOT NULL,
            quantity NUMERIC NOT NULL,
            unit_price NUMERIC NOT NULL,
            discount_amount NUMERIC NOT NULL DEFAULT 0,
            tax_rate NUMERIC NOT NULL DEFAULT 0,
            tax_amount NUMERIC NOT NULL DEFAULT 0,
            unit_cost_snapshot NUMERIC,
            CHECK (kind != 'product' OR item_id IS NOT NULL)
        );
        """
    )
    conn.execute("INSERT INTO catalog_items (item_type, name, unit_code) VALUES ('product', 'Yerba', 'kg')")
    conn.execute("INSERT INTO locations (name) VALUES ('Deposito')")
    conn.execute(
        "INSERT INTO stock_movements (item_id, location_id, movement_type, quantity_delta, occurred_at) "
        "VALUES (1, 1, 'purchase', 10, '2026-01-01T00:00:00')"
    )
    conn.execute("INSERT INTO sales (number) VALUES ('V-0001')")
    conn.execute(
        "INSERT INTO sale_items (sale_id, kind, item_id, description_snapshot, quantity, unit_price) "
        "VALUES (1, 'product', 1, 'Yerba', 2, 1500)"
    )
    conn.commit()
    return conn


def test_init_schema_migrates_old_database_without_crashing():
    conn = _old_schema_conn()
    init_schema(conn)  # no debe levantar sqlite3.OperationalError


def test_migration_preserves_existing_stock_movement_data():
    conn = _old_schema_conn()
    init_schema(conn)
    row = conn.execute(
        "SELECT item_id, location_id, quantity_delta, variant_id FROM stock_movements WHERE id = 1"
    ).fetchone()
    assert row == (1, 1, 10, None)


def test_migration_preserves_existing_sale_item_data():
    conn = _old_schema_conn()
    init_schema(conn)
    row = conn.execute(
        "SELECT sale_id, item_id, description_snapshot, quantity, unit_price, variant_id "
        "FROM sale_items WHERE id = 1"
    ).fetchone()
    assert row == (1, 1, "Yerba", 2, 1500, None)


def test_migration_adds_stock_variant_index_and_column():
    conn = _old_schema_conn()
    init_schema(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(stock_movements)").fetchall()}
    assert "variant_id" in columns
    conn.execute(
        "INSERT INTO stock_movements (item_id, location_id, movement_type, quantity_delta, occurred_at, variant_id) "
        "VALUES (1, 1, 'adjustment', 1, '2026-01-02T00:00:00', 5)"
    )


def test_migration_still_enforces_product_requires_item_id_check():
    conn = _old_schema_conn()
    init_schema(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sale_items (sale_id, kind, item_id, description_snapshot, quantity, unit_price) "
            "VALUES (1, 'product', NULL, 'Sin catalogar', 1, 100)"
        )


def test_migration_enforces_variant_requires_item_id_check():
    conn = _old_schema_conn()
    init_schema(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sale_items "
            "(sale_id, kind, item_id, variant_id, description_snapshot, quantity, unit_price) "
            "VALUES (1, 'service', NULL, 1, 'No deberia poder pasar esto', 1, 100)"
        )


def test_init_schema_is_idempotent_on_already_migrated_database():
    conn = _old_schema_conn()
    init_schema(conn)
    init_schema(conn)  # no debe fallar ni duplicar la migracion
    applied = conn.execute("SELECT version FROM schema_migrations").fetchall()
    assert applied == [(1,)]


def test_init_schema_on_a_fresh_database_records_migration_as_applied():
    conn = sqlite3.connect(":memory:")
    init_schema(conn)
    applied = conn.execute("SELECT version, name FROM schema_migrations").fetchall()
    assert applied == [(1, "add_variant_id_to_stock_movements_and_sale_items")]
