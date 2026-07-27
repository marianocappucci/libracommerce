"""Tests for libracommerce.adapters.contalibra against a fixture schema that
mirrors the relevant tables of Contalibra's real schema (verbatim column
names/types from libracore/libracore/db/schema.py's init_core_schema, as
inspected in the WSL sibling repo) — trimmed to only what the adapter reads.
"""
import json
import sqlite3
from decimal import Decimal

import pytest

from libracommerce.adapters.contalibra import (
    read_catalog_items,
    read_locations,
    read_parties_from_clients,
    read_parties_from_proveedores,
    read_sales,
    read_stock_movements,
)
from libracommerce.domain.catalog import CatalogItemType
from libracommerce.domain.entities import PartyType
from libracommerce.domain.inventory import StockMovementType
from libracommerce.domain.sales import SaleStatus


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
            estacion TEXT DEFAULT '',
            vendible INTEGER NOT NULL DEFAULT 1,
            tipo TEXT NOT NULL DEFAULT 'producto'
        );

        CREATE TABLE depositos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            activo INTEGER NOT NULL DEFAULT 1
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


def test_read_parties_from_clients_defaults_to_person(contalibra_conn):
    contalibra_conn.execute(
        "INSERT INTO clients (name, cuit_dni, email, activo) VALUES (?, ?, ?, ?)",
        ("Ana Gomez", "20304050607", "ana@example.com", 1),
    )
    parties = read_parties_from_clients(contalibra_conn)
    assert len(parties) == 1
    assert parties[0].party_type == PartyType.PERSON
    assert parties[0].display_name == "Ana Gomez"
    assert parties[0].tax_id == "20304050607"
    assert parties[0].active


def test_read_parties_from_clients_maps_inactive(contalibra_conn):
    contalibra_conn.execute("INSERT INTO clients (name, activo) VALUES ('Baja', 0)")
    parties = read_parties_from_clients(contalibra_conn)
    assert parties[0].active is False


def test_read_parties_from_proveedores_defaults_to_organization_and_active(contalibra_conn):
    contalibra_conn.execute(
        "INSERT INTO proveedores (nombre, cuit_dni) VALUES (?, ?)", ("Distribuidora SA", "30111222333")
    )
    parties = read_parties_from_proveedores(contalibra_conn)
    assert parties[0].party_type == PartyType.ORGANIZATION
    assert parties[0].active


def test_read_catalog_items_resolves_category_by_name(contalibra_conn):
    contalibra_conn.execute("INSERT INTO categorias_producto (nombre) VALUES ('Almacen')")
    contalibra_conn.execute(
        """INSERT INTO productos (nombre, precio_venta, precio_costo, unidad, categoria, estacion)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("Yerba", 1500, 900, "kg", "Almacen", ""),
    )
    items = read_catalog_items(contalibra_conn)
    assert items[0].item_type == CatalogItemType.PRODUCT
    assert items[0].category_id == 1
    assert items[0].default_sale_price == Decimal("1500")
    assert items[0].unit.code == "kg"
    assert items[0].unit.allows_fraction is False  # dato no disponible en Contalibra


def test_read_catalog_items_maps_tipo_servicio(contalibra_conn):
    contalibra_conn.execute(
        "INSERT INTO productos (nombre, unidad, tipo) VALUES ('Consulta', 'u', 'servicio')"
    )
    items = read_catalog_items(contalibra_conn)
    assert items[0].item_type == CatalogItemType.SERVICE


def test_read_catalog_items_defaults_to_product_when_tipo_column_missing(contalibra_conn):
    # Simula una instancia todavía en un libracore anterior a v0.17.0, sin la
    # columna `tipo` — el adaptador no debe asumir que existe, tiene que
    # verificarlo (PRAGMA table_info) y caer al default documentado.
    contalibra_conn.execute("ALTER TABLE productos RENAME TO productos_con_tipo")
    contalibra_conn.execute(
        """
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
            estacion TEXT DEFAULT '',
            vendible INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    contalibra_conn.execute("INSERT INTO productos (nombre, unidad) VALUES ('Yerba', 'kg')")
    items = read_catalog_items(contalibra_conn)
    assert items[0].item_type == CatalogItemType.PRODUCT


def test_read_catalog_items_category_none_when_unmatched(contalibra_conn):
    contalibra_conn.execute(
        "INSERT INTO productos (nombre, unidad, categoria) VALUES (?, ?, ?)",
        ("Yerba", "kg", "Categoria inexistente"),
    )
    items = read_catalog_items(contalibra_conn)
    assert items[0].category_id is None


def test_read_catalog_items_keeps_estacion_in_metadata(contalibra_conn):
    contalibra_conn.execute(
        "INSERT INTO productos (nombre, unidad, estacion) VALUES ('Milanesa', 'u', 'cocina')"
    )
    items = read_catalog_items(contalibra_conn)
    assert items[0].metadata == {"estacion": "cocina"}


def test_read_locations(contalibra_conn):
    contalibra_conn.execute("INSERT INTO depositos (nombre, activo) VALUES ('Deposito Central', 1)")
    locations = read_locations(contalibra_conn)
    assert locations[0].name == "Deposito Central"
    assert locations[0].location_type == "warehouse"


def test_read_stock_movements_maps_known_types_in_date_order(contalibra_conn):
    contalibra_conn.execute("INSERT INTO productos (nombre, unidad) VALUES ('Yerba', 'kg')")
    contalibra_conn.execute("INSERT INTO depositos (nombre) VALUES ('Deposito Central')")
    contalibra_conn.executemany(
        "INSERT INTO movimientos_stock (producto_id, tipo, cantidad, fecha, deposito_id, venta_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, "entrada", 10, "2026-07-01", 1, None),
            (1, "venta", -3, "2026-07-02", 1, 55),
            (1, "anulacion", 1, "2026-07-03", 1, 55),
        ],
    )
    movements = read_stock_movements(contalibra_conn)
    assert [m.movement_type for m in movements] == [
        StockMovementType.ADJUSTMENT,
        StockMovementType.SALE,
        StockMovementType.RETURN,
    ]
    assert movements[1].source_type == "venta"
    assert movements[1].source_id == 55
    assert movements[0].source_id is None


def test_read_stock_movements_maps_transferencia_between_depositos(contalibra_conn):
    # Generado por libracore/db/productos.py::transferir_stock — dos filas,
    # una por depósito, ambas con el mismo producto_id.
    contalibra_conn.execute("INSERT INTO productos (nombre, unidad) VALUES ('Yerba', 'kg')")
    contalibra_conn.executemany(
        "INSERT INTO depositos (nombre) VALUES (?)", [("Origen",), ("Destino",)]
    )
    contalibra_conn.executemany(
        "INSERT INTO movimientos_stock (producto_id, tipo, cantidad, fecha, deposito_id) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (1, "transferencia_salida", -5, "2026-07-01", 1),
            (1, "transferencia_entrada", 5, "2026-07-01", 2),
        ],
    )
    movements = read_stock_movements(contalibra_conn)
    assert [m.movement_type for m in movements] == [
        StockMovementType.TRANSFER_OUT,
        StockMovementType.TRANSFER_IN,
    ]


def test_read_stock_movements_skips_rows_without_deposito(contalibra_conn):
    contalibra_conn.execute("INSERT INTO productos (nombre, unidad) VALUES ('Yerba', 'kg')")
    contalibra_conn.execute(
        "INSERT INTO movimientos_stock (producto_id, tipo, cantidad, fecha, deposito_id) "
        "VALUES (1, 'ajuste', 5, '2026-07-01', NULL)"
    )
    assert read_stock_movements(contalibra_conn) == []


def test_read_stock_movements_rejects_unknown_type(contalibra_conn):
    contalibra_conn.execute("INSERT INTO productos (nombre, unidad) VALUES ('Yerba', 'kg')")
    contalibra_conn.execute("INSERT INTO depositos (nombre) VALUES ('Deposito Central')")
    contalibra_conn.execute(
        "INSERT INTO movimientos_stock (producto_id, tipo, cantidad, fecha, deposito_id) "
        "VALUES (1, 'produccion', 5, '2026-07-01', 1)"
    )
    with pytest.raises(ValueError):
        read_stock_movements(contalibra_conn)


def test_read_sales_maps_items_and_status(contalibra_conn):
    contalibra_conn.execute("INSERT INTO clients (name) VALUES ('Ana Gomez')")
    items = json.dumps([{"nombre": "Yerba", "qty": 2, "precio": 1500, "subtotal": 3000, "producto_id": 1}])
    contalibra_conn.execute(
        """INSERT INTO ventas (numero, fecha, cliente_id, items, subtotal, descuento, total, estado)
           VALUES ('V-0001', '2026-07-25', 1, ?, 3000, 0, 3000, 'cobrada')""",
        (items,),
    )
    sales = read_sales(contalibra_conn)
    assert len(sales) == 1
    sale = sales[0]
    assert sale.status == SaleStatus.CONFIRMED
    assert sale.customer_party_id == 1
    assert sale.items[0].kind == CatalogItemType.PRODUCT
    assert sale.items[0].item_id == 1
    assert sale.items[0].quantity == Decimal("2")
    assert sale.total == Decimal("3000")
    assert sale.confirmed_at is not None


def test_read_sales_maps_pendiente_and_anulada(contalibra_conn):
    empty_items = json.dumps([])
    contalibra_conn.execute(
        """INSERT INTO ventas (numero, fecha, items, subtotal, descuento, total, estado)
           VALUES ('V-0002', '2026-07-25', ?, 0, 0, 0, 'pendiente')""",
        (empty_items,),
    )
    contalibra_conn.execute(
        """INSERT INTO ventas (numero, fecha, items, subtotal, descuento, total, estado)
           VALUES ('V-0003', '2026-07-25', ?, 0, 0, 0, 'anulada')""",
        (empty_items,),
    )
    sales = {s.number: s for s in read_sales(contalibra_conn)}
    assert sales["V-0002"].status == SaleStatus.DRAFT
    assert sales["V-0002"].confirmed_at is None
    assert sales["V-0003"].status == SaleStatus.CANCELLED


def test_read_sales_maps_ad_hoc_item_without_producto_id_as_service(contalibra_conn):
    items = json.dumps(
        [{"nombre": "Servicio extra", "qty": 1, "precio": 500, "subtotal": 500, "producto_id": None}]
    )
    contalibra_conn.execute(
        """INSERT INTO ventas (numero, fecha, items, subtotal, descuento, total, estado)
           VALUES ('V-0004', '2026-07-25', ?, 500, 0, 500, 'cobrada')""",
        (items,),
    )
    sales = read_sales(contalibra_conn)
    line = sales[0].items[0]
    assert line.kind == CatalogItemType.SERVICE
    assert line.item_id is None
    assert line.description_snapshot == "Servicio extra"
