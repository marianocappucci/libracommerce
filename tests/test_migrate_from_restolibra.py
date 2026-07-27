"""Tests del script de migracion P8 (Fase 1) contra un fixture sintetico
con la forma real de Restolibra -- mismo esquema base que
tests/test_migrate_from_contalibra.py (Restolibra es un fork con el mismo
schema de libracore), mas las tablas exclusivas recetas/receta_items.
"""
import json
import sqlite3

import pytest

from libracommerce.scripts.migrate_from_restolibra import migrate
from libracommerce.scripts.verify_restolibra_migration import verify


@pytest.fixture
def restolibra_conn() -> sqlite3.Connection:
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
            tipo TEXT NOT NULL DEFAULT 'producto',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE depositos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            descripcion TEXT DEFAULT '',
            activo INTEGER NOT NULL DEFAULT 1,
            es_default INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
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
            deposito_id INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
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
            created_at TEXT DEFAULT (datetime('now')),
            usuario_id INTEGER,
            observaciones TEXT DEFAULT '',
            factura_id INTEGER,
            remito_id INTEGER,
            turno_id INTEGER,
            mp_order_id TEXT DEFAULT '',
            mp_payment_id TEXT DEFAULT ''
        );

        CREATE TABLE listas_precio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            descripcion TEXT DEFAULT '',
            es_default INTEGER NOT NULL DEFAULT 0,
            activa INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE lista_precio_items (
            lista_id INTEGER NOT NULL,
            producto_id INTEGER NOT NULL,
            precio REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (lista_id, producto_id)
        );

        CREATE TABLE recetas (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            producto_id     INTEGER NOT NULL UNIQUE REFERENCES productos(id) ON DELETE CASCADE,
            rinde           REAL NOT NULL DEFAULT 1,
            rinde_unidad    TEXT NOT NULL DEFAULT 'u',
            rendimiento_pct REAL NOT NULL DEFAULT 100,
            activo          INTEGER NOT NULL DEFAULT 1,
            notas           TEXT DEFAULT '',
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE receta_items (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            receta_id      INTEGER NOT NULL REFERENCES recetas(id) ON DELETE CASCADE,
            ingrediente_id INTEGER NOT NULL REFERENCES productos(id) ON DELETE CASCADE,
            cantidad       REAL NOT NULL DEFAULT 0,
            created_at     TEXT DEFAULT (datetime('now'))
        );
        """
    )
    return conn


def _seed_realistic_dataset(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO clients (id, name, cuit_dni, activo) VALUES (1, 'Ana Gomez', '20304050607', 1)")
    conn.execute(
        """INSERT INTO productos (id, nombre, precio_venta, precio_costo, unidad, categoria, tipo,
                                  estacion, vendible)
           VALUES (1, 'Papa', 0, 300, 'kg', 'Insumos', 'producto', '', 0)"""
    )
    conn.execute(
        """INSERT INTO productos (id, nombre, precio_venta, precio_costo, unidad, categoria, tipo,
                                  estacion, vendible)
           VALUES (2, 'Papas Fritas', 4000, 0, 'plato', 'Cocina', 'producto', 'cocina', 1)"""
    )
    conn.execute(
        """INSERT INTO productos (id, nombre, precio_venta, precio_costo, unidad, tipo)
           VALUES (3, 'Gaseosa', 2000, 1200, 'u', 'producto')"""
    )
    conn.execute(
        "INSERT INTO depositos (id, nombre, descripcion, es_default) VALUES (1, 'Cocina', 'Principal', 1)"
    )
    conn.executemany(
        "INSERT INTO movimientos_stock (producto_id, tipo, cantidad, fecha, deposito_id, venta_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, "entrada", 50, "2026-07-01", 1, None),
            (1, "merma", -2, "2026-07-02", 1, None),
            (2, "produccion", 10, "2026-07-02", 1, None),
            (1, "venta", -3, "2026-07-03", 1, 1),
        ],
    )
    items_venta_1 = json.dumps(
        [{"nombre": "Papas Fritas", "qty": 1, "precio": 4000, "subtotal": 4000, "producto_id": 2}]
    )
    conn.execute(
        """INSERT INTO ventas (numero, fecha, cliente_id, items, subtotal, descuento, total, estado)
           VALUES ('V-0001', '2026-07-03', 1, ?, 4000, 0, 4000, 'cobrada')""",
        (items_venta_1,),
    )
    conn.execute(
        "INSERT INTO recetas (id, producto_id, rinde, rinde_unidad) VALUES (1, 2, 4, 'porciones')"
    )
    conn.execute(
        "INSERT INTO receta_items (receta_id, ingrediente_id, cantidad) VALUES (1, 1, 1.2)"
    )


def test_migrate_reuses_base_counts(restolibra_conn):
    _seed_realistic_dataset(restolibra_conn)
    report = migrate(restolibra_conn)

    assert report.parties_from_clients == 1
    assert report.catalog_items == 3
    assert report.locations == 1
    assert report.stock_movements == 4
    assert report.sales == 1
    assert report.sale_items == 1


def test_migrate_maps_merma_and_produccion_types(restolibra_conn):
    _seed_realistic_dataset(restolibra_conn)
    migrate(restolibra_conn)
    rows = restolibra_conn.execute(
        "SELECT movement_type, reason_code FROM stock_movements ORDER BY id"
    ).fetchall()
    assert ("waste", "merma") in rows
    assert ("adjustment", "produccion") in rows


def test_migrate_repoints_recetas_and_receta_items_to_catalog_items(restolibra_conn):
    _seed_realistic_dataset(restolibra_conn)
    report = migrate(restolibra_conn)

    assert report.recetas_repointed is True
    assert report.receta_items_repointed is True

    fks_recetas = restolibra_conn.execute("PRAGMA foreign_key_list(recetas)").fetchall()
    assert any(row[2] == "catalog_items" for row in fks_recetas)
    fks_items = restolibra_conn.execute("PRAGMA foreign_key_list(receta_items)").fetchall()
    assert any(row[2] == "catalog_items" for row in fks_items)

    # Los datos (IDs) se preservan tal cual -- solo cambio el constraint.
    receta = restolibra_conn.execute(
        "SELECT producto_id, rinde, rinde_unidad FROM recetas WHERE id=1"
    ).fetchone()
    assert receta == (2, 4, "porciones")
    item = restolibra_conn.execute(
        "SELECT receta_id, ingrediente_id, cantidad FROM receta_items"
    ).fetchone()
    assert item == (1, 1, 1.2)

    # Y la FK ahora sirve de verdad contra catalog_items (mismo ID que productos).
    joined = restolibra_conn.execute(
        """SELECT c.name FROM receta_items ri
           JOIN catalog_items c ON c.id = ri.ingrediente_id
           WHERE ri.receta_id = 1"""
    ).fetchone()
    assert joined == ("Papa",)


def test_migrate_is_idempotent_guard_fails_loudly_on_second_run(restolibra_conn):
    _seed_realistic_dataset(restolibra_conn)
    migrate(restolibra_conn)
    with pytest.raises(sqlite3.IntegrityError):
        migrate(restolibra_conn)


def test_verify_reports_no_discrepancies_on_clean_migration(restolibra_conn):
    _seed_realistic_dataset(restolibra_conn)
    migrate(restolibra_conn)
    report = verify(restolibra_conn)
    assert report.ok, report.discrepancies


def test_verify_catches_a_broken_receta_cost(restolibra_conn):
    _seed_realistic_dataset(restolibra_conn)
    migrate(restolibra_conn)
    # Corrompemos a mano el costo migrado del insumo (catalog_items), para
    # confirmar que verify() compara de verdad el costeo de la receta y no
    # da "ok" por construccion.
    restolibra_conn.execute("UPDATE catalog_items SET default_cost = '999' WHERE id = 1")
    report = verify(restolibra_conn)
    assert not report.ok
    assert any(d.check == "costo_receta" for d in report.discrepancies)


def test_verify_catches_orphaned_receta_item(restolibra_conn):
    _seed_realistic_dataset(restolibra_conn)
    migrate(restolibra_conn)
    restolibra_conn.execute("PRAGMA foreign_keys = OFF")
    restolibra_conn.execute("DELETE FROM catalog_items WHERE id = 1")
    report = verify(restolibra_conn)
    assert not report.ok
    assert any(d.check == "receta_items_huerfanos" for d in report.discrepancies)
