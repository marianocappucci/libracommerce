"""Fixtures compartidas, y el repositorio contra los DOS motores.

PostgreSQL es el motor de produccion de la familia; SQLite queda para el nodo
offline y para correr rapido. Un test que solo se ejercita contra SQLite no
prueba nada sobre lo que corre en produccion, y hay diferencias que **solo**
se ven en el otro lado: el `transaction()` de este repositorio termina en un
`sqlite3.Connection.rollback()` o en el `ConnectionWrapper.rollback()` de
psycopg segun donde corra, y son dos implementaciones distintas.

Por eso `repo` esta parametrizada: cada test que la use corre dos veces.
"""

import os
import sqlite3

import pytest

from libracommerce.db.repository import SqliteCommerceRepository
from libracommerce.db.schema import init_schema


def url_postgres() -> str:
    """La URL de PostgreSQL, o saltea el test fuera de CI.

    Mismo criterio que `test_schema_congelado`: **en CI no se saltea**. Si la
    variable falta ahi, es que el servicio no se levanto, y dejar pasar los
    tests en verde seria peor que no tenerlos.
    """
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if url:
        return url
    if os.environ.get("CI"):
        pytest.fail(
            "LIBRACORE_POSTGRES_URL no está definida en CI — los tests contra "
            "PostgreSQL no se saltean acá"
        )
    pytest.skip("LIBRACORE_POSTGRES_URL no configurada (fuera de CI se saltea)")


@pytest.fixture(params=["sqlite", "postgres"])
def repo(request) -> SqliteCommerceRepository:
    """Un repositorio con el schema creado, contra cada motor.

    El id del test dice cual corrio (`[sqlite]` / `[postgres]`), asi que un
    rojo nombra el backend sin tener que abrir nada.
    """
    if request.param == "sqlite":
        conn = sqlite3.connect(":memory:")
        init_schema(conn)
        yield SqliteCommerceRepository(conn)
        conn.close()
        return

    from libracore.db import core

    url = url_postgres()
    core.configure(url)
    conn = core.get_connection()
    try:
        # Cada test arranca con la base vacia: los ids son seriales y varios
        # tests afirman sobre relaciones entre filas, no sobre valores fijos,
        # pero el estado de uno anterior igual falsearia los conteos.
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
        init_schema(conn)
        conn.commit()
        yield SqliteCommerceRepository(conn)
    finally:
        conn.close()
        core._db_path = None
        core._database_url = None


# ── Para las factories de router (P9): un `conexion()` como el que pasa un producto ──

from libracommerce.db.repository import SqliteCommerceRepository  # noqa: E402
from libracommerce.domain.inventory import Location  # noqa: E402

USUARIO = {"id": 7, "username": "cajero"}


def _usuario():
    return USUARIO


def _deposito_principal(conn):
    """Los productos nacen con un depósito default (lo siembra su `init_db`);
    sin él, un movimiento sin depósito explícito no tiene dónde caer."""
    SqliteCommerceRepository(conn).save_location(Location(id=None, name="Depósito principal", is_default=True))


@pytest.fixture(params=["sqlite", "postgres"])
def abrir(request, tmp_path):
    """Un `conexion()` como el que pasa un producto: un context manager que
    commitea al salir, contra una base con el schema del motor."""
    if request.param == "sqlite":
        ruta = str(tmp_path / "erp.db")
        conn = sqlite3.connect(ruta)
        init_schema(conn)
        _deposito_principal(conn)
        conn.commit()
        conn.close()

        def _abrir():
            c = sqlite3.connect(ruta)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys = ON")
            return c

        yield _abrir
        return

    from libracore.db import core

    url = url_postgres()
    core.configure(url)
    conn = core.get_connection()
    try:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
        init_schema(conn)
        _deposito_principal(conn)
        conn.commit()
    finally:
        conn.close()
    yield core.get_connection
    core._db_path = None
    core._database_url = None




# ── Para ventas (P9-M3): los DOS schemas, como los tiene un producto ─────────

#: El DDL de `venta_links` tal cual lo declaran Contalibra y Restolibra en su
#: `schema_propio`. El motor lo lee y escribe sin declararlo (hasta M5); la
#: fixture hace de consumidor.
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


def _schema_de_producto(conn):
    """Lo que hace el `init_db` de un producto, en el mismo orden: el core de
    LibraCore, el comercio, la tabla propia de vínculos, la FK de `ventas_pagos`
    repuntada a `sales`, y las semillas mínimas (usuario, caja, depósito)."""
    from libracore.db.schema import init_core_schema

    from libracommerce.erp.ventas import repuntar_fk_ventas_pagos

    init_core_schema(conn)
    init_schema(conn)
    conn.execute(VENTA_LINKS_DDL)
    conn.commit()
    assert repuntar_fk_ventas_pagos(conn) is True
    assert repuntar_fk_ventas_pagos(conn) is False  # idempotente
    conn.execute(
        "INSERT INTO usuarios (id, username, nombre, password_hash, role) VALUES (?,?,?,?,?)",
        (USUARIO["id"], USUARIO["username"], "Cajero", "x", "admin"),
    )
    conn.execute(
        "INSERT INTO cajas (nombre, descripcion, medios_pago, es_default) VALUES (?,?,?,1)",
        ("Caja principal", "", "[]"),
    )
    _deposito_principal(conn)
    conn.commit()


@pytest.fixture(params=["sqlite", "postgres"])
def abrir_ventas(request, tmp_path):
    """Como `abrir`, pero con los dos schemas: es lo que necesita una venta, que
    escribe en LibraCommerce y en LibraCore dentro de la misma transacción.

    Contra SQLite también pasa por `libracore.db.core`: los casos de uso llaman
    a `create_caja_movimiento`, que abre su propia conexión para resolver la
    caja default, y esa conexión sale de ahí."""
    from libracore.db import core

    if request.param == "sqlite":
        core.configure(str(tmp_path / "producto.db"))
    else:
        core.configure(url_postgres())
    conn = core.get_connection()
    try:
        if request.param == "postgres":
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
            conn.commit()
        _schema_de_producto(conn)
    finally:
        conn.close()
    yield core.get_connection
    core._db_path = None
    core._database_url = None
