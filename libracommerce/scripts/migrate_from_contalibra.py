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

# `lista_precio_items` de Contalibra es un modelo "flat" (un precio por
# producto por lista, sin vigencia). `item_prices.valid_from` es NOT NULL,
# asi que cada fila migrada necesita un valor -- este sentinel documenta
# "sin restriccion de fecha de inicio", consistente con que el modelo de
# origen nunca tuvo nocion de vigencia.
PRICE_SIN_VIGENCIA = "2000-01-01T00:00:00"


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
    venta_links: int = 0
    price_lists: int = 0
    item_prices: int = 0
    skipped_sale_items: list[str] = field(default_factory=list)
    price_list_default_conflicts: list[str] = field(default_factory=list)


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


def _migrate_price_lists(source: sqlite3.Connection, target: sqlite3.Connection,
                         report: MigrationReport) -> int:
    """`listas_precio` -> `price_lists`. A diferencia de LibraCommerce,
    Contalibra nunca enforceo "como mucho una lista default" a nivel de
    esquema (`es_default` nunca se gestiono desde ninguna API real -- no
    hay endpoint "set-default" para listas de precio, a diferencia de
    depositos). Si la base real tuviera mas de una fila con `es_default=1`
    (nunca visto en los datos ya revisados, pero no hay garantia de
    esquema), la segunda violaria el indice unico parcial de
    `price_lists`. Se resuelve quedandose con la primera por id y
    dejando constancia en el reporte en vez de que la migracion explote.
    """
    rows = source.execute(
        "SELECT id, nombre, descripcion, es_default, activa, created_at FROM listas_precio ORDER BY id"
    ).fetchall()
    ya_hay_default = False
    for row_id, nombre, descripcion, es_default, activa, created_at in rows:
        es_default_final = es_default
        if es_default and ya_hay_default:
            report.price_list_default_conflicts.append(
                f"listas_precio.id={row_id} ({nombre!r}) tambien tenia es_default=1; "
                "se migro con is_default=0 para no violar el indice unico de price_lists"
            )
            es_default_final = 0
        elif es_default:
            ya_hay_default = True
        target.execute(
            "INSERT INTO price_lists (id, name, description, active, is_default, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (row_id, nombre, descripcion, activa, es_default_final, created_at),
        )
    return len(rows)


def _migrate_item_prices(source: sqlite3.Connection, target: sqlite3.Connection) -> int:
    """`lista_precio_items` -> `item_prices`, con vigencia abierta (ver
    `PRICE_SIN_VIGENCIA`) y sin quiebre de cantidad ni sucursal -- el
    modelo de Contalibra nunca tuvo ninguno de los dos."""
    rows = source.execute(
        "SELECT lista_id, producto_id, precio FROM lista_precio_items"
    ).fetchall()
    for lista_id, producto_id, precio in rows:
        target.execute(
            "INSERT INTO item_prices (item_id, price_list_id, amount, valid_from) VALUES (?,?,?,?)",
            (producto_id, lista_id, precio, PRICE_SIN_VIGENCIA),
        )
    return len(rows)


def _migrate_stock_movements(source: sqlite3.Connection, target: sqlite3.Connection, movements) -> int:
    # `created_at` (cuando se REGISTRO el movimiento) es distinto de
    # `occurred_at` (cuando PASO). En un ledger append-only de un sistema
    # financiero los dos importan; el primero no se puede reconstruir despues.
    created_at = dict(source.execute("SELECT id, created_at FROM movimientos_stock").fetchall())
    for movement in movements:
        target.execute(
            """
            INSERT INTO stock_movements
                (id, item_id, variant_id, location_id, movement_type, quantity_delta,
                 occurred_at, source_type, source_id, unit_cost, lot_code, expires_at,
                 note, created_by, reason_code, created_at)
            VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?)
            """,
            (
                movement.id, movement.item_id, movement.location_id, movement.movement_type,
                str(movement.quantity_delta), movement.occurred_at.isoformat(),
                movement.source_type, movement.source_id,
                movement.note, movement.created_by, movement.reason_code,
                created_at.get(movement.id),
            ),
        )
    return len(movements)


def _migrate_sales_and_items(source: sqlite3.Connection, target: sqlite3.Connection,
                             sales, report: MigrationReport) -> None:
    created_at = dict(source.execute("SELECT id, created_at FROM ventas").fetchall())
    for sale in sales:
        customer_party_id = sale.customer_party_id
        target.execute(
            """
            INSERT INTO sales
                (id, number, status, customer_party_id, branch_id, register_id, source_type,
                 source_id, subtotal, discount_total, tax_total, total, confirmed_at,
                 created_at, occurred_on, customer_name_snapshot, created_by, notes, status_detail)
            VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sale.id, sale.number, sale.status, customer_party_id, sale.source_type,
                str(sale.subtotal), str(sale.discount_total), str(sale.tax_total),
                str(sale.total), sale.confirmed_at.isoformat() if sale.confirmed_at else None,
                created_at.get(sale.id), sale.occurred_on, sale.customer_name_snapshot,
                sale.created_by, sale.notes, sale.status_detail,
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


def _migrate_venta_links(conn: sqlite3.Connection) -> int:
    """`ventas` tiene 5 referencias a contextos que NO son de LibraCommerce:
    `factura_id`/`remito_id` (facturacion, LibraCore), `turno_id` (caja,
    LibraCore) y `mp_order_id`/`mp_payment_id` (MercadoPago). Meterlas en la
    tabla `sales` generica seria filtrar el dominio de otro producto dentro
    del motor; se mueven a una tabla propia de Contalibra que referencia
    `sales(id)` -- el pegamento entre contextos vive del lado del producto,
    no del motor.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS venta_links (
            venta_id      INTEGER PRIMARY KEY REFERENCES sales(id) ON DELETE CASCADE,
            factura_id    INTEGER REFERENCES facturas(id) ON DELETE SET NULL,
            remito_id     INTEGER REFERENCES remitos(id) ON DELETE SET NULL,
            turno_id      INTEGER REFERENCES turnos_caja(id) ON DELETE SET NULL,
            mp_order_id   TEXT DEFAULT '',
            mp_payment_id TEXT DEFAULT ''
        )
        """
    )
    rows = conn.execute(
        """
        SELECT id, factura_id, remito_id, turno_id, mp_order_id, mp_payment_id
        FROM ventas
        WHERE factura_id IS NOT NULL OR remito_id IS NOT NULL OR turno_id IS NOT NULL
           OR (mp_order_id IS NOT NULL AND mp_order_id != '')
           OR (mp_payment_id IS NOT NULL AND mp_payment_id != '')
        """
    ).fetchall()
    for row in rows:
        conn.execute(
            """INSERT INTO venta_links
               (venta_id, factura_id, remito_id, turno_id, mp_order_id, mp_payment_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            row,
        )
    return len(rows)


def _repoint_ventas_pagos_fk(conn: sqlite3.Connection) -> None:
    """`ventas_pagos.venta_id` referencia `ventas(id)`. Una vez que las ventas
    viven en `sales`, cada pago nuevo violaria esa FK. La tabla sigue siendo
    de dominio LibraCore (pagos/caja) y conserva su nombre y sus datos; solo
    se reapunta la FK a la nueva ubicacion de las ventas.

    Requiere el rebuild de 12 pasos que recomienda la documentacion de SQLite
    (no se puede alterar una FK con ALTER TABLE), con `foreign_keys = OFF`
    alrededor -- mismo procedimiento que la migracion 0001 de este repo.
    """
    fks = conn.execute("PRAGMA foreign_key_list(ventas_pagos)").fetchall()
    if not any(row[2] == "ventas" for row in fks):
        return  # ya reapuntada, o base nueva sin la FK vieja

    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("ALTER TABLE ventas_pagos RENAME TO ventas_pagos_old")
        conn.execute(
            """
            CREATE TABLE ventas_pagos (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                venta_id   INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
                medio      TEXT NOT NULL,
                monto      REAL NOT NULL,
                referencia TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            "INSERT INTO ventas_pagos (id, venta_id, medio, monto, referencia, created_at) "
            "SELECT id, venta_id, medio, monto, referencia, created_at FROM ventas_pagos_old"
        )
        conn.execute("DROP TABLE ventas_pagos_old")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _repoint_lista_precio_items_fk(conn: sqlite3.Connection) -> None:
    """`lista_precio_items.producto_id` referencia `productos(id)`. Las listas
    de precio siguen siendo de LibraCore (no se migran a `price_lists`/
    `item_prices`: ese es otro modelo, con vigencias y quiebres de cantidad,
    y mapear el modelo simple de Contalibra ahi es un proyecto propio), pero
    su FK tiene que apuntar a donde viven ahora los productos.

    Los IDs se preservaron en la migracion, asi que las filas existentes
    siguen siendo validas sin tocar sus datos.
    """
    fks = conn.execute("PRAGMA foreign_key_list(lista_precio_items)").fetchall()
    if not any(row[2] == "productos" for row in fks):
        return

    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("ALTER TABLE lista_precio_items RENAME TO lista_precio_items_old")
        conn.execute(
            """
            CREATE TABLE lista_precio_items (
                lista_id    INTEGER NOT NULL REFERENCES listas_precio(id) ON DELETE CASCADE,
                producto_id INTEGER NOT NULL REFERENCES catalog_items(id) ON DELETE CASCADE,
                precio      REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (lista_id, producto_id)
            )
            """
        )
        conn.execute(
            "INSERT INTO lista_precio_items (lista_id, producto_id, precio) "
            "SELECT lista_id, producto_id, precio FROM lista_precio_items_old"
        )
        conn.execute("DROP TABLE lista_precio_items_old")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


# Tabla nueva -> tabla vieja de la que hereda su secuencia de IDs.
_SEQUENCE_HEREDADA = {
    "sales": "ventas",
    "catalog_items": "productos",
    "stock_movements": "movimientos_stock",
    "locations": "depositos",
    "categories": "categorias_producto",
    "price_lists": "listas_precio",
}


def _preservar_sqlite_sequence(conn: sqlite3.Connection) -> None:
    """Evita que las tablas nuevas reusen IDs que las viejas ya consumieron.

    Bug real encontrado probando la app entera contra una copia de dev: la
    migracion inserta IDs explicitos, asi que el AUTOINCREMENT de la tabla
    nueva arranca en el maximo migrado -- pero la tabla vieja podia tener un
    `sqlite_sequence` mas alto por filas ya borradas. En dev, `ventas` estaba
    en 13 y `sales` quedo en 5: la venta siguiente tomo el id 6, que una
    venta vieja ya habia usado.

    Eso no es cosmetico. `caja_movimientos` guarda referencias con el id de
    la venta (`anulacion:venta:6:pago:5`), y `create_caja_movimiento` dedupe
    por referencia a proposito. Al reusar el id 6, la anulacion de la venta
    nueva choco con la referencia de la vieja y **el egreso de caja no se
    genero**: la venta quedaba anulada pero el dinero no se revertia.

    Se lleva cada secuencia al maximo entre la propia y la de la tabla vieja.
    """
    for tabla_nueva, tabla_vieja in _SEQUENCE_HEREDADA.items():
        vieja = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = ?", (tabla_vieja,)
        ).fetchone()
        if vieja is None:
            continue
        nueva = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = ?", (tabla_nueva,)
        ).fetchone()
        objetivo = max(vieja[0], nueva[0] if nueva else 0)
        if nueva is None:
            conn.execute(
                "INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (tabla_nueva, objetivo)
            )
        elif objetivo > nueva[0]:
            conn.execute(
                "UPDATE sqlite_sequence SET seq = ? WHERE name = ?", (objetivo, tabla_nueva)
            )


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
    report.stock_movements = _migrate_stock_movements(conn, conn, stock_movements)
    _migrate_sales_and_items(conn, conn, sales, report)
    report.venta_links = _migrate_venta_links(conn)
    report.price_lists = _migrate_price_lists(conn, conn, report)
    report.item_prices = _migrate_item_prices(conn, conn)
    _repoint_ventas_pagos_fk(conn)
    _repoint_lista_precio_items_fk(conn)
    _preservar_sqlite_sequence(conn)

    conn.commit()
    return report
