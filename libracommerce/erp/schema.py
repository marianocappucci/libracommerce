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
