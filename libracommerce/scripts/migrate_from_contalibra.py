"""Migracion de datos, de una sola corrida, del catalogo/stock/ventas de
Contalibra hacia el esquema de LibraCommerce -- P7 del plan de
consolidacion de la familia Libra (ver
wiki/analyses/migracion-p7-contalibra-libracommerce.md).

Reusa `libracommerce.adapters.contalibra` para leer, pero a diferencia de
ese modulo (solo lectura) este SI escribe: SQL crudo directo contra las
tablas de LibraCommerce, reusando los mismos IDs de Contalibra (no una
tabla de mapeo separada) para que `producto_id`/`venta_id` historicos
sigan siendo validos despues de migrar. No pasa por
`SqliteCommerceRepository` a proposito -- ese repositorio siempre
autogenera IDs (`id is None`), el patron correcto para todo el codigo
nuevo que la Fase 2 va a escribir de ahora en mas; forzarlo a aceptar un
modo de PK explicita solo para este script de un solo uso seria una
abstraccion de mas en la API publica.

Se corre UNA vez por entorno (primero contra una copia de los datos
reales, nunca la base viva -- ver Fase 1 del plan; recien despues,
con confirmacion explicita, contra produccion real en la Fase 4). No es
un mecanismo de migracion continua.

Orden de escritura, por dependencias de foreign key:
units -> categories -> locations -> parties -> catalog_items ->
stock_movements -> sales -> sale_items.
"""
import json
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal

from libracommerce.adapters.contalibra import (
    read_catalog_items,
    read_locations,
    read_parties_from_clients,
    read_parties_from_proveedores,
    read_sales,
    read_stock_movements,
)
from libracommerce.db.schema import init_schema
from libracommerce.domain.catalog import CatalogItemType

# Offset fijo para los IDs de `proveedores` al insertarlos en `parties`
# junto con `clients` (misma tabla, un solo espacio de IDs). Nada en
# `movimientos_stock`/`ventas` referencia `proveedores.id`, asi que
# desplazarlo no rompe ninguna foreign key existente. Con el volumen real
# de Contalibra (decenas de filas por tabla) nunca colisiona con
# `clients.id`.
PROVEEDOR_ID_OFFSET = 100_000


@dataclass
class MigrationReport:
    """Conteos para el chequeo de fidelidad (Fase 1 del plan) -- no
    reemplaza la verificacion manual de sumas/totales contra la base
    real, pero confirma que no se perdio ninguna fila en el camino."""

    parties_from_clients: int = 0
    parties_from_proveedores: int = 0
    catalog_items: int = 0
    item_codes: int = 0
    locations: int = 0
    stock_movements: int = 0
    sales: int = 0
    sale_items: int = 0
    skipped_sale_items: list[str] = field(default_factory=list)


def _migrate_units(conn: sqlite3.Connection, target: sqlite3.Connection, items) -> None:
    seen = {item.unit.code: item.unit for item in items}
    for unit in seen.values():
        target.execute(
            """
            INSERT INTO units (code, name, allows_fraction, decimal_scale)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(code) DO NOTHING
            """,
            (unit.code, unit.name, int(unit.allows_fraction), unit.decimal_scale),
        )


def _migrate_categories(source: sqlite3.Connection, target: sqlite3.Connection) -> None:
    rows = source.execute("SELECT id, nombre FROM categorias_producto").fetchall()
    for category_id, name in rows:
        target.execute(
            "INSERT INTO categories (id, name, parent_id, active) VALUES (?, ?, NULL, 1)",
            (category_id, name),
        )


def _migrate_locations(source: sqlite3.Connection, target: sqlite3.Connection, locations) -> int:
    created_at = dict(source.execute("SELECT id, created_at FROM depositos").fetchall())
    for location in locations:
        target.execute(
            """
            INSERT INTO locations
                (id, name, branch_id, location_type, active, description, is_default, created_at)
            VALUES (?, ?, NULL, ?, ?, ?, ?, ?)
            """,
            (location.id, location.name, location.location_type, int(location.active),
             location.description, int(location.is_default), created_at.get(location.id)),
        )
    return len(locations)


def _migrate_parties(target: sqlite3.Connection, clients, proveedores) -> tuple[int, int]:
    for party in clients:
        target.execute(
            """
            INSERT INTO parties
                (id, party_type, display_name, legal_name, tax_id, tax_id_type, email, phone, active)
            VALUES (?, ?, ?, NULL, ?, NULL, ?, ?, ?)
            """,
            (party.id, party.party_type, party.display_name, party.tax_id,
             party.email, party.phone, int(party.active)),
        )
    for party in proveedores:
        target.execute(
            """
            INSERT INTO parties
                (id, party_type, display_name, legal_name, tax_id, tax_id_type, email, phone, active)
            VALUES (?, ?, ?, NULL, ?, NULL, ?, ?, ?)
            """,
            (party.id + PROVEEDOR_ID_OFFSET, party.party_type, party.display_name,
             party.tax_id, party.email, party.phone, int(party.active)),
        )
    return len(clients), len(proveedores)


def _migrate_catalog_items(source: sqlite3.Connection, target: sqlite3.Connection, items) -> int:
    # `created_at` no es parte del dominio `CatalogItem`, pero es un dato real
    # de Contalibra: sin esto, cada producto migrado quedaria fechado el dia de
    # la migracion (default CURRENT_TIMESTAMP) en vez de su alta original.
    created_at = dict(source.execute("SELECT id, created_at FROM productos").fetchall())
    for item in items:
        target.execute(
            """
            INSERT INTO catalog_items
                (id, item_type, name, description, category_id, unit_code, active,
                 sellable, purchasable, tax_profile, metadata_json,
                 default_sale_price, default_cost, min_stock, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.id, item.item_type, item.name, item.description, item.category_id,
                item.unit.code, int(item.active), int(item.sellable), int(item.purchasable),
                item.tax_profile, json.dumps(item.metadata, ensure_ascii=False),
                str(item.default_sale_price), str(item.default_cost), str(item.min_stock),
                created_at.get(item.id),
            ),
        )
    return len(items)


def _migrate_item_codes(source: sqlite3.Connection, target: sqlite3.Connection) -> int:
    """`productos.codigo` (UNIQUE, nullable) pasa a ser el codigo interno
    primario del item en `item_codes`. Contalibra tiene un solo codigo por
    producto; LibraCommerce admite varios por tipo, asi que este queda como
    `is_primary=1`."""
    rows = source.execute(
        "SELECT id, codigo FROM productos WHERE codigo IS NOT NULL AND codigo != ''"
    ).fetchall()
    for item_id, codigo in rows:
        target.execute(
            "INSERT INTO item_codes (item_id, code_type, code, is_primary) VALUES (?, 'internal', ?, 1)",
            (item_id, codigo),
        )
    return len(rows)


def _migrate_stock_movements(target: sqlite3.Connection, movements) -> int:
    for movement in movements:
        target.execute(
            """
            INSERT INTO stock_movements
                (id, item_id, variant_id, location_id, movement_type, quantity_delta,
                 occurred_at, source_type, source_id, unit_cost, lot_code, expires_at,
                 note, created_by, reason_code)
            VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?)
            """,
            (
                movement.id, movement.item_id, movement.location_id, movement.movement_type,
                str(movement.quantity_delta), movement.occurred_at.isoformat(),
                movement.source_type, movement.source_id,
                movement.note, movement.created_by, movement.reason_code,
            ),
        )
    return len(movements)


def _migrate_sales_and_items(target: sqlite3.Connection, sales, report: MigrationReport) -> None:
    for sale in sales:
        customer_party_id = sale.customer_party_id
        target.execute(
            """
            INSERT INTO sales
                (id, number, status, customer_party_id, branch_id, register_id, source_type,
                 source_id, subtotal, discount_total, tax_total, total, confirmed_at)
            VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL, ?, ?, ?, ?, ?)
            """,
            (
                sale.id, sale.number, sale.status, customer_party_id, sale.source_type,
                str(sale.subtotal), str(sale.discount_total), str(sale.tax_total),
                str(sale.total), sale.confirmed_at.isoformat() if sale.confirmed_at else None,
            ),
        )
        report.sales += 1
        for line in sale.items:
            if line.kind == CatalogItemType.PRODUCT and line.item_id is None:
                # No deberia poder pasar -- el adapter ya garantiza item_id
                # para lineas de producto -- pero si aparece, se excluye y
                # se deja constancia en vez de romper toda la migracion.
                report.skipped_sale_items.append(
                    f"venta {sale.number}: linea de producto sin item_id ({line.description_snapshot!r})"
                )
                continue
            target.execute(
                """
                INSERT INTO sale_items
                    (sale_id, kind, item_id, variant_id, description_snapshot, quantity,
                     unit_price, discount_amount, tax_rate, tax_amount, unit_cost_snapshot)
                VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    sale.id, line.kind, line.item_id, line.description_snapshot,
                    str(line.quantity), str(line.unit_price), str(line.discount_amount),
                    str(line.tax_rate), str(line.tax_amount),
                ),
            )
            report.sale_items += 1


def migrate(conn: sqlite3.Connection) -> MigrationReport:
    """Corre la migracion completa sobre `conn` -- lee las tablas de
    Contalibra y escribe las de LibraCommerce en la MISMA conexion/archivo
    (decision de diseno del plan, para preservar la atomicidad de
    `crear_venta_directa`/`anular_venta` una vez reescritas en la Fase 2).
    Idempotente solo en el sentido de que falla ruidosamente (IntegrityError
    por PK duplicada) si se corre dos veces sobre el mismo destino -- a
    proposito: no esta pensada para reintentarse en caliente, sino para
    correr una vez contra un archivo limpio (la copia de verificacion en la
    Fase 1, o la base real ya con backup en la Fase 4)."""
    init_schema(conn)

    report = MigrationReport()

    parties_clients = read_parties_from_clients(conn)
    parties_proveedores = read_parties_from_proveedores(conn)
    catalog_items = read_catalog_items(conn)
    locations = read_locations(conn)
    stock_movements = read_stock_movements(conn)
    sales = read_sales(conn)

    _migrate_units(conn, conn, catalog_items)
    _migrate_categories(conn, conn)
    report.locations = _migrate_locations(conn, conn, locations)
    report.parties_from_clients, report.parties_from_proveedores = _migrate_parties(
        conn, parties_clients, parties_proveedores
    )
    report.catalog_items = _migrate_catalog_items(conn, conn, catalog_items)
    report.item_codes = _migrate_item_codes(conn, conn)
    report.stock_movements = _migrate_stock_movements(conn, stock_movements)
    _migrate_sales_and_items(conn, sales, report)

    conn.commit()
    return report
