"""El DDL de lo que cruza los dos motores (P9-M5).

Hasta M4, `venta_links` —la tabla que ata una venta de LibraCommerce con la
factura, el remito, el turno de caja (LibraCore) y la orden de MercadoPago—
estaba declarada **tres veces**: en el `schema_propio.py` de Contalibra, en el de
Restolibra, y como constante en el `conftest.py` de este motor, que hacía de
consumidor. Tres copias del mismo `CREATE TABLE`, que es la forma más barata que
tiene un schema de divergir.

## Por qué acá y no en `db/schema.py`

`db.schema.init_schema()` tiene que poder correr **sobre una base vacía**, sin
LibraCore: así lo usan la fixture `abrir` de este repo y cualquier consumidor que
quiera sólo el comercio. `venta_links` tiene claves foráneas a `facturas`,
`remitos` y `turnos_caja`, que son de LibraCore — meterla ahí haría que
`init_schema()` reviente contra PostgreSQL con *relation "facturas" does not
exist* en cuanto alguien lo corra solo. (Contra SQLite no reventaría, que es
justo lo que hace peligrosa la mudanza: el motor de desarrollo no muestra el
problema.)

`erp/` es, por definición de la capa, **lo que cruza los dos motores**, y ya
depende de LibraCore por el extra `[erp]`. Ahí sí puede vivir una tabla con FKs a
los dos lados.

## Quién la crea, y en qué orden

La sigue creando el **producto**, desde su `init_schema_propio()`, después de
`init_core_schema()` y de `init_schema()`: es el único momento en que las cuatro
tablas referenciadas existen. Lo que cambia con M5 es que el DDL tiene **una sola
fuente**; el orden y el dueño son los mismos que antes, y por eso el schema que
queda en la base es idéntico —lo sostiene el `test_schema_propio_congelado.py` de
cada producto, que compara columnas contra una fixture—.
"""

from __future__ import annotations

#: El `CREATE TABLE` de `venta_links`, tal cual lo venían declarando los dos
#: productos. Se expone como constante además de la función porque una baseline
#: de Alembic puede querer el texto y no la llamada.
VENTA_LINKS_DDL = """
    CREATE TABLE IF NOT EXISTS venta_links (
        venta_id      INTEGER PRIMARY KEY REFERENCES sales(id) ON DELETE CASCADE,
        factura_id    INTEGER REFERENCES facturas(id) ON DELETE SET NULL,
        remito_id     INTEGER REFERENCES remitos(id) ON DELETE SET NULL,
        turno_id      INTEGER REFERENCES turnos_caja(id) ON DELETE SET NULL,
        mp_order_id   TEXT DEFAULT '',
        mp_payment_id TEXT DEFAULT ''
    )
"""


def crear_venta_links(conn) -> None:
    """Crea `venta_links` si no está. Idempotente.

    La llama el `init_schema_propio()` de cada producto, **después** de los dos
    motores: las FKs apuntan a `sales` (LibraCommerce) y a `facturas`, `remitos`
    y `turnos_caja` (LibraCore).
    """
    conn.execute(VENTA_LINKS_DDL)


#: El `CREATE TABLE` de `cliente_lista_precio`, extraído de Contalibra
#: (`app/db_mayorista.py`, add-on mayorista, P9-M2 lo había dejado atrás por
#: error: sólo se llevó `price_lists`/`item_prices`, no el enganche con el
#: cliente). Una fila por cliente —PK sobre `cliente_id`—, con las dos FK
#: `ON DELETE CASCADE` para que la asociación se vaya sola si se borra el
#: cliente o la lista, en vez de quedar colgada.
CLIENTE_LISTA_PRECIO_DDL = """
    CREATE TABLE IF NOT EXISTS cliente_lista_precio (
        cliente_id INTEGER PRIMARY KEY REFERENCES clients(id) ON DELETE CASCADE,
        lista_id   INTEGER NOT NULL REFERENCES price_lists(id) ON DELETE CASCADE
    )
"""


def crear_cliente_lista_precio(conn) -> None:
    """Crea `cliente_lista_precio` si no está. Idempotente.

    La llama el `init_schema_propio()` de cada producto, **después** de los dos
    motores: las FKs apuntan a `clients` (LibraCore) y a `price_lists`
    (LibraCommerce). Mismo criterio que `crear_venta_links`.
    """
    conn.execute(CLIENTE_LISTA_PRECIO_DDL)


#: Las promociones (2026-09-28, roadmap de producto de VentaLibra: "combos y 2x1"):
#: la regla (`promotions` + `promotion_items`) y el registro de qué promoción se
#: aplicó en cada venta (`sale_promotions`). Es construcción nueva, no extracción:
#: ningún producto de la familia las tenía. `sale_promotions` guarda el nombre y
#: el ahorro de ese momento, no sólo la FK: borrar o editar la promoción después
#: no reescribe lo que ya se vendió (`promotion_id` queda en NULL si se borra).
PROMOCIONES_DDL = (
    """
    CREATE TABLE IF NOT EXISTS promotions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('nxm', 'combo')),
        pay_quantity NUMERIC,
        combo_price NUMERIC,
        valid_from TEXT,
        valid_until TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS promotion_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        promotion_id INTEGER NOT NULL REFERENCES promotions(id) ON DELETE CASCADE,
        item_id INTEGER NOT NULL REFERENCES catalog_items(id),
        quantity NUMERIC NOT NULL CHECK (quantity > 0)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_promotion_items_promotion ON promotion_items(promotion_id)",
    """
    CREATE TABLE IF NOT EXISTS sale_promotions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
        promotion_id INTEGER REFERENCES promotions(id) ON DELETE SET NULL,
        name TEXT NOT NULL,
        times INTEGER NOT NULL,
        amount NUMERIC NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sale_promotions_sale ON sale_promotions(sale_id)",
)


def crear_promociones(conn) -> None:
    """Crea las tablas de promociones si no están. Idempotente.

    La llama el `init_schema_propio()` de cada producto, **después** de los dos
    motores: las FK apuntan a `catalog_items` y `sales` (LibraCommerce). Mismo
    criterio que `crear_cliente_lista_precio`.
    """
    for ddl in PROMOCIONES_DDL:
        conn.execute(ddl)
