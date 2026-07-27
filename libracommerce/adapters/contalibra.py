"""Read-only adapters that map Contalibra's real SQLite schema onto
LibraCommerce domain objects, per the migration strategy documented in
wiki/analyses/arquitectura-familia-libra-alcance.md ("Crear adaptadores
de lectura sobre las tablas actuales de Contalibra" — paso 2, antes de
normalizar o mover ninguna tabla real).

These functions only SELECT from a connection the caller already opened
against a Contalibra-shaped database. They never write, and they do not
import contalibra or libracore code — only raw SQL against the column
names documented in libracore/libracore/db/schema.py (`init_core_schema`),
so this package keeps no runtime dependency on either sibling repo.

Known, deliberate lossy mappings — Contalibra's schema does not capture
enough to do better, and these are defaults, not derived facts:
- Party.party_type: clients/proveedores don't distinguish person vs
  organization at all. clients defaults to PERSON, proveedores defaults
  to ORGANIZATION.
- CatalogItem.item_type: `productos.tipo` (libracore >= v0.17.0) maps
  'servicio' -> SERVICE and 'producto' -> PRODUCT. Instances still on an
  older libracore (no `tipo` column yet — checked via PRAGMA table_info,
  not assumed) default to PRODUCT, matching the column's own default.
- CatalogItem.unit: `productos.unidad` is free text ("u", "kg", "l"...)
  with no fraction/decimal-scale metadata, so allows_fraction/
  decimal_scale always default to False/0.
- CatalogItem.tax_profile: Contalibra resolves IVA at invoicing time
  (`facturas`), not per product — always None here.
- StockMovement.movement_type: `movimientos_stock.tipo` values `entrada`/
  `salida`/`ajuste` all collapse to ADJUSTMENT (Contalibra doesn't
  distinguish a reason beyond the sign of `cantidad`). `transferencia_salida`/
  `transferencia_entrada` (written by `productos.py::transferir_stock`) map
  to TRANSFER_OUT/TRANSFER_IN respectively.
- CatalogItem.purchasable: not tracked in `productos` — always True.
- CatalogItem.min_stock / Location.is_default / Location.description: these
  are NOT lossy — they map 1:1 onto `productos.stock_minimo`,
  `depositos.es_default` and `depositos.descripcion`, which LibraCommerce
  gained as first-class fields in migration 0002 precisely so the P7
  migration wouldn't have to drop them.
- StockMovement.unit_cost/lot_code/expires_at: not tracked in
  `movimientos_stock` — always None.
- Sale.tax_total: not tracked on `ventas` (only appears later on
  `facturas`) — always Decimal("0").
- Sale.confirmed_at: `ventas` has no separate confirmation timestamp;
  `created_at` is used only when estado is already "cobrada".

Ad-hoc sale lines (free-text `nombre`, no `producto_id` — Contalibra
allows these) map to a SERVICE-kind `SaleItem` with `item_id=None`, since
LibraCommerce now requires a catalog link only for PRODUCT lines (see
`libracommerce.domain.sales.SaleItem`). This is an assumption, not a
derived fact: Contalibra doesn't record whether an unlinked line was a
genuine one-off service or a product sold without going through the
catalog — see wiki/entities/libracommerce.md for the full list of gaps.
"""
import json
import sqlite3
from datetime import datetime
from decimal import Decimal

from libracommerce.domain.catalog import CatalogItem, CatalogItemType, Unit
from libracommerce.domain.entities import Party, PartyType
from libracommerce.domain.inventory import Location, StockMovement, StockMovementType
from libracommerce.domain.sales import Sale, SaleItem, SaleStatus


_STOCK_MOVEMENT_TYPE_MAP = {
    "venta": StockMovementType.SALE,
    "anulacion": StockMovementType.RETURN,
    "ajuste": StockMovementType.ADJUSTMENT,
    "entrada": StockMovementType.ADJUSTMENT,
    "salida": StockMovementType.ADJUSTMENT,
    "transferencia_salida": StockMovementType.TRANSFER_OUT,
    "transferencia_entrada": StockMovementType.TRANSFER_IN,
}

_SALE_STATUS_MAP = {
    "cobrada": SaleStatus.CONFIRMED,
    # 'parcial' = parcialmente COBRADA. Es estado de cobranza, no de la venta:
    # la venta esta confirmada igual. El valor original se preserva en
    # `Sale.status_detail`, mismo patron que `StockMovement.reason_code`.
    "parcial": SaleStatus.CONFIRMED,
    "pendiente": SaleStatus.DRAFT,
    "anulada": SaleStatus.CANCELLED,
}


def _to_decimal(value) -> Decimal:
    return Decimal(str(value))


def read_parties_from_clients(conn: sqlite3.Connection) -> list[Party]:
    rows = conn.execute(
        "SELECT id, name, cuit_dni, email, phone, activo FROM clients"
    ).fetchall()
    return [
        Party(
            id=row[0],
            party_type=PartyType.PERSON,
            display_name=row[1],
            tax_id=row[2] or None,
            email=row[3] or None,
            phone=row[4] or None,
            active=bool(row[5]),
        )
        for row in rows
    ]


def read_parties_from_proveedores(conn: sqlite3.Connection) -> list[Party]:
    rows = conn.execute(
        "SELECT id, nombre, cuit_dni, email, phone FROM proveedores"
    ).fetchall()
    return [
        Party(
            id=row[0],
            party_type=PartyType.ORGANIZATION,
            display_name=row[1],
            tax_id=row[2] or None,
            email=row[3] or None,
            phone=row[4] or None,
            active=True,  # proveedores no trackea activo/inactivo en Contalibra
        )
        for row in rows
    ]


def _resolve_category_id(conn: sqlite3.Connection, categoria_nombre: str | None) -> int | None:
    if not categoria_nombre:
        return None
    row = conn.execute(
        "SELECT id FROM categorias_producto WHERE nombre = ?", (categoria_nombre,)
    ).fetchone()
    return row[0] if row else None


def read_catalog_items(conn: sqlite3.Connection) -> list[CatalogItem]:
    has_tipo = any(row[1] == "tipo" for row in conn.execute("PRAGMA table_info(productos)"))
    columns = (
        "id, nombre, descripcion, categoria, unidad, activo, vendible, "
        "precio_venta, precio_costo, estacion, stock_minimo"
    )
    if has_tipo:
        columns += ", tipo"
    rows = conn.execute(f"SELECT {columns} FROM productos").fetchall()
    items = []
    for row in rows:
        unit_code = row[4] or "u"
        metadata = {"estacion": row[9]} if row[9] else {}
        tipo = row[11] if has_tipo else "producto"
        item_type = CatalogItemType.SERVICE if tipo == "servicio" else CatalogItemType.PRODUCT
        items.append(
            CatalogItem(
                id=row[0],
                item_type=item_type,
                name=row[1],
                unit=Unit(code=unit_code, name=unit_code, allows_fraction=False, decimal_scale=0),
                category_id=_resolve_category_id(conn, row[3]),
                description=row[2] or "",
                active=bool(row[5]),
                sellable=bool(row[6]),
                purchasable=True,
                tax_profile=None,
                metadata=metadata,
                default_sale_price=_to_decimal(row[7]),
                default_cost=_to_decimal(row[8]),
                min_stock=_to_decimal(row[10]),
            )
        )
    return items


def read_locations(conn: sqlite3.Connection) -> list[Location]:
    rows = conn.execute(
        "SELECT id, nombre, activo, descripcion, es_default FROM depositos"
    ).fetchall()
    return [
        Location(
            id=row[0], name=row[1], branch_id=None, location_type="warehouse",
            active=bool(row[2]), description=row[3] or "", is_default=bool(row[4]),
        )
        for row in rows
    ]


def read_stock_movements(conn: sqlite3.Connection) -> list[StockMovement]:
    rows = conn.execute(
        """
        SELECT id, producto_id, deposito_id, tipo, cantidad, fecha, venta_id,
               referencia, usuario_id
        FROM movimientos_stock
        WHERE deposito_id IS NOT NULL
        ORDER BY fecha, id
        """
    ).fetchall()
    movements = []
    for row in rows:
        (movement_id, item_id, location_id, tipo, cantidad, fecha, venta_id,
         referencia, usuario_id) = row
        if tipo not in _STOCK_MOVEMENT_TYPE_MAP:
            raise ValueError(f"movimientos_stock.id={movement_id}: tipo {tipo!r} sin mapeo conocido")
        movements.append(
            StockMovement(
                id=movement_id,
                item_id=item_id,
                location_id=location_id,
                movement_type=_STOCK_MOVEMENT_TYPE_MAP[tipo],
                quantity_delta=_to_decimal(cantidad),
                occurred_at=datetime.fromisoformat(fecha),
                source_type="venta" if venta_id else None,
                source_id=venta_id,
                note=referencia or "",
                created_by=usuario_id,
                reason_code=tipo,
            )
        )
    return movements


def read_sales(conn: sqlite3.Connection) -> list[Sale]:
    rows = conn.execute(
        """
        SELECT id, numero, items, subtotal, descuento, total, cliente_id, estado, created_at,
               fecha, cliente_nombre, usuario_id, observaciones
        FROM ventas
        """
    ).fetchall()
    sales = []
    for row in rows:
        (sale_id, numero, items_json, subtotal, descuento, total, cliente_id, estado, created_at,
         fecha, cliente_nombre, usuario_id, observaciones) = row
        if estado not in _SALE_STATUS_MAP:
            raise ValueError(f"venta {numero!r}: estado {estado!r} sin mapeo conocido")
        status = _SALE_STATUS_MAP[estado]

        sale_items = []
        for raw_item in json.loads(items_json):
            producto_id = raw_item.get("producto_id")
            # Contalibra doesn't tag a sale line as product-vs-service — a line
            # without producto_id is optimistically mapped as an ad-hoc SERVICE,
            # since that's the only kind our domain allows without item_id. This
            # is an assumption, not a fact: Contalibra can't tell us whether an
            # unlinked line was really a one-off service or a product sold
            # without going through the catalog (a data-quality gap in the
            # source system this adapter cannot resolve).
            kind = CatalogItemType.PRODUCT if producto_id is not None else CatalogItemType.SERVICE
            sale_items.append(
                SaleItem(
                    kind=kind,
                    item_id=producto_id,
                    description_snapshot=raw_item.get("nombre", ""),
                    quantity=_to_decimal(raw_item.get("qty", 0)),
                    unit_price=_to_decimal(raw_item.get("precio", 0)),
                )
            )

        sales.append(
            Sale(
                id=sale_id,
                number=numero,
                items=tuple(sale_items),
                status=status,
                customer_party_id=cliente_id,
                source_type="pos",
                subtotal=_to_decimal(subtotal),
                discount_total=_to_decimal(descuento),
                tax_total=Decimal("0"),
                total=_to_decimal(total),
                confirmed_at=datetime.fromisoformat(created_at) if status == SaleStatus.CONFIRMED and created_at else None,
                occurred_on=fecha,
                customer_name_snapshot=cliente_nombre or "",
                created_by=usuario_id,
                notes=observaciones or "",
                status_detail=estado,
            )
        )
    return sales
