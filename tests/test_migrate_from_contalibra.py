"""Tests del script de migracion P7 (Fase 1) contra un fixture sintetico
con la forma real de Contalibra -- mismo esquema que
tests/test_contalibra_adapter.py, pero acá se corre `migrate()` completo y
se verifica el resultado escrito en las tablas de LibraCommerce, no solo
los objetos de dominio en memoria que devuelve el adapter.
"""
import json
import sqlite3

import pytest

from libracommerce.scripts.migrate_from_contalibra import (
    PROVEEDOR_ID_OFFSET,
    migrate,
)
from libracommerce.scripts.verify_contalibra_migration import verify


@pytest.fixture
def contalibra_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            cuit_dni TEXT,
            email TEXT,
            phone TEXT,
            activo INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE proveedores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            cuit_dni TEXT DEFAULT '',
            email TEXT DEFAULT '',
            phone TEXT DEFAULT ''
        );

        CREATE TABLE categorias_producto (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL UNIQUE
        );

        CREATE TABLE productos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo TEXT UNIQUE,
            nombre TEXT NOT NULL,
            descripcion TEXT DEFAULT '',
            precio_venta REAL NOT NULL DEFAULT 0,
            precio_costo REAL NOT NULL DEFAULT 0,
            unidad TEXT NOT NULL DEFAULT 'u',
            categoria TEXT DEFAULT '',
            activo INTEGER NOT NULL DEFAULT 1,
            stock_minimo REAL NOT NULL DEFAULT 0,
            estacion TEXT DEFAULT '',
            vendible INTEGER NOT NULL DEFAULT 1,
            tipo TEXT NOT NULL DEFAULT 'producto'
        );

        CREATE TABLE depositos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            descripcion TEXT DEFAULT '',
            activo INTEGER NOT NULL DEFAULT 1,
            es_default INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE movimientos_stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            producto_id INTEGER NOT NULL,
            tipo TEXT NOT NULL,
            cantidad REAL NOT NULL,
            referencia TEXT DEFAULT '',
            venta_id INTEGER,
            usuario_id INTEGER,
            fecha TEXT NOT NULL,
            deposito_id INTEGER
        );

        CREATE TABLE ventas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero TEXT NOT NULL UNIQUE,
            fecha TEXT NOT NULL,
            cliente_id INTEGER,
            cliente_nombre TEXT DEFAULT '',
            items TEXT NOT NULL,
            subtotal REAL NOT NULL DEFAULT 0,
            descuento REAL NOT NULL DEFAULT 0,
            total REAL NOT NULL DEFAULT 0,
            estado TEXT NOT NULL DEFAULT 'cobrada',
            created_at TEXT DEFAULT (datetime('now'))
        );
        """
    )
    return conn


def _seed_realistic_dataset(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO clients (id, name, cuit_dni, activo) VALUES (1, 'Ana Gomez', '20304050607', 1)")
    conn.execute("INSERT INTO proveedores (id, nombre, cuit_dni) VALUES (1, 'Distribuidora SA', '30111222333')")
    conn.execute("INSERT INTO categorias_producto (id, nombre) VALUES (1, 'Almacen')")
    conn.execute(
        """INSERT INTO productos (id, nombre, precio_venta, precio_costo, unidad, categoria, tipo, stock_minimo)
           VALUES (1, 'Yerba', 1500, 900, 'kg', 'Almacen', 'producto', 5)"""
    )
    conn.execute(
        """INSERT INTO productos (id, nombre, precio_venta, precio_costo, unidad, tipo)
           VALUES (2, 'Consulta', 0, 0, 'u', 'servicio')"""
    )
    conn.execute(
        "INSERT INTO depositos (id, nombre, descripcion, es_default) VALUES (1, 'Origen', 'Principal', 1)"
    )
    conn.execute("INSERT INTO depositos (id, nombre) VALUES (2, 'Destino')")
    conn.executemany(
        "INSERT INTO movimientos_stock (producto_id, tipo, cantidad, fecha, deposito_id, venta_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, "entrada", 20, "2026-07-01", 1, None),
            (1, "transferencia_salida", -5, "2026-07-02", 1, None),
            (1, "transferencia_entrada", 5, "2026-07-02", 2, None),
            (1, "venta", -2, "2026-07-03", 1, 1),
        ],
    )
    items_venta_1 = json.dumps(
        [{"nombre": "Yerba", "qty": 2, "precio": 1500, "subtotal": 3000, "producto_id": 1}]
    )
    conn.execute(
        """INSERT INTO ventas (numero, fecha, cliente_id, items, subtotal, descuento, total, estado)
           VALUES ('V-0001', '2026-07-03', 1, ?, 3000, 0, 3000, 'cobrada')""",
        (items_venta_1,),
    )
    items_venta_2 = json.dumps(
        [{"nombre": "Empanada", "qty": 1, "precio": 500, "subtotal": 500, "producto_id": None}]
    )
    conn.execute(
        """INSERT INTO ventas (numero, fecha, cliente_id, items, subtotal, descuento, total, estado)
           VALUES ('V-0002', '2026-07-04', NULL, ?, 500, 0, 500, 'cobrada')""",
        (items_venta_2,),
    )


def test_migrate_produces_matching_counts(contalibra_conn):
    _seed_realistic_dataset(contalibra_conn)
    report = migrate(contalibra_conn)

    assert report.parties_from_clients == 1
    assert report.parties_from_proveedores == 1
    assert report.catalog_items == 2
    assert report.locations == 2
    assert report.stock_movements == 4
    assert report.sales == 2
    assert report.sale_items == 2
    assert report.skipped_sale_items == []


def test_migrate_offsets_proveedor_ids_away_from_clients(contalibra_conn):
    conn = contalibra_conn
    conn.execute("INSERT INTO clients (id, name) VALUES (1, 'Ana')")
    conn.execute("INSERT INTO proveedores (id, nombre) VALUES (1, 'Mismo ID que el cliente')")
    migrate(conn)
    rows = conn.execute("SELECT id, display_name FROM parties ORDER BY id").fetchall()
    assert rows == [(1, "Ana"), (1 + PROVEEDOR_ID_OFFSET, "Mismo ID que el cliente")]


def test_migrate_preserves_transferencia_between_depositos(contalibra_conn):
    _seed_realistic_dataset(contalibra_conn)
    migrate(contalibra_conn)
    rows = contalibra_conn.execute(
        "SELECT movement_type, location_id, quantity_delta FROM stock_movements "
        "WHERE movement_type IN ('transfer_out', 'transfer_in') ORDER BY movement_type"
    ).fetchall()
    assert rows == [("transfer_in", 2, 5), ("transfer_out", 1, -5)]


def test_migrate_maps_ad_hoc_sale_item_as_service_without_item_id(contalibra_conn):
    _seed_realistic_dataset(contalibra_conn)
    migrate(contalibra_conn)
    row = contalibra_conn.execute(
        "SELECT kind, item_id, description_snapshot FROM sale_items "
        "WHERE description_snapshot = 'Empanada'"
    ).fetchone()
    assert row == ("service", None, "Empanada")


def test_migrate_preserves_catalog_item_ids_and_type(contalibra_conn):
    _seed_realistic_dataset(contalibra_conn)
    migrate(contalibra_conn)
    rows = contalibra_conn.execute(
        "SELECT id, item_type FROM catalog_items ORDER BY id"
    ).fetchall()
    assert rows == [(1, "product"), (2, "service")]


def test_migrate_preserves_stock_minimo_and_deposito_default(contalibra_conn):
    _seed_realistic_dataset(contalibra_conn)
    migrate(contalibra_conn)
    # min_stock tiene afinidad NUMERIC: SQLite guarda "5.0" como el entero 5.
    assert contalibra_conn.execute(
        "SELECT min_stock FROM catalog_items WHERE id = 1"
    ).fetchone()[0] == 5
    assert contalibra_conn.execute(
        "SELECT name, description, is_default FROM locations ORDER BY id"
    ).fetchall() == [("Origen", "Principal", 1), ("Destino", "", 0)]


def test_verify_reports_no_discrepancies_on_clean_migration(contalibra_conn):
    _seed_realistic_dataset(contalibra_conn)
    migrate(contalibra_conn)
    report = verify(contalibra_conn)
    assert report.ok, report.discrepancies


def test_verify_catches_a_real_discrepancy(contalibra_conn):
    _seed_realistic_dataset(contalibra_conn)
    migrate(contalibra_conn)
    # Corrompemos a mano un stock_movement migrado, para confirmar que
    # verify() realmente compara y no siempre da "ok" por construccion.
    contalibra_conn.execute("UPDATE stock_movements SET quantity_delta = '999' WHERE id = 1")
    report = verify(contalibra_conn)
    assert not report.ok
    assert any(d.check == "stock_por_producto_deposito" for d in report.discrepancies)


def test_migrate_is_not_meant_to_run_twice_against_the_same_target(contalibra_conn):
    _seed_realistic_dataset(contalibra_conn)
    migrate(contalibra_conn)
    with pytest.raises(sqlite3.IntegrityError):
        migrate(contalibra_conn)
