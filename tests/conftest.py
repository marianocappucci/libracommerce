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

# ── Una base de PostgreSQL por worker, restaurada desde una plantilla ───────
#
# Cada test contra PostgreSQL arranca de una base **nueva**, y rearmarla con
# `DROP SCHEMA public CASCADE` + `init_schema()` costaba ~1,5 s por test (medido;
# la suite tiene ~1.000). Ahora la base sale de `CREATE DATABASE ... TEMPLATE`
# (~0,1 s) desde una plantilla armada la primera vez que se pide, una por tipo de
# fixture (`repo`, `abrir`, `ventas`), porque cada una deja un estado distinto.
# El mecanismo (una base por worker de xdist, plantillas, `FORCE`) vive en
# `libracore.testing.pg_por_worker`; aca queda lo propio de este repo: que hay en
# cada plantilla.
#
# La URL del worker se pasa al resto de la suite pisando `LIBRACORE_POSTGRES_URL`,
# que es de donde la leen todos (`url_postgres()`, `test_schema_congelado`,
# `test_migraciones_alembic`). Esos dos ultimos hacen su propio `DROP SCHEMA` sobre
# esa URL y no usan plantillas. Sin la variable (fuera de CI se saltean los tests
# de PostgreSQL) no hace nada.
from libracore.testing.pg_por_worker import base_por_worker  # noqa: E402

from libracommerce.db.repository import SqliteCommerceRepository
from libracommerce.db.schema import init_schema

_PG = base_por_worker("libracommerce", os.environ.get("LIBRACORE_POSTGRES_URL", ""))
if _PG:
    os.environ["LIBRACORE_POSTGRES_URL"] = _PG.url


def _en_base(url: str, construir) -> None:
    """Corre `construir(conn)` sobre `url` con la capa de conexion de LibraCore y deja todo cerrado."""
    from libracore.db import core

    core.configure(url)
    conn = core.get_connection()
    try:
        construir(conn)
        conn.commit()
    finally:
        conn.close()
        core._db_path = None
        core._database_url = None


def _restaurar(plantilla: str, construir) -> None:
    """Deja la base del worker como una nueva armada con `construir(conn)`, copiada de la plantilla `plantilla`."""
    _PG.restaurar(plantilla, lambda url: _en_base(url, construir))


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
    # Cada test arranca con la base nueva: los ids son seriales y varios
    # tests afirman sobre relaciones entre filas, no sobre valores fijos,
    # pero el estado de uno anterior igual falsearia los conteos.
    _restaurar("repo", init_schema)
    core.configure(url)
    conn = core.get_connection()
    try:
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

    def _armar(conn):
        init_schema(conn)
        _deposito_principal(conn)

    _restaurar("abrir", _armar)
    core.configure(url)
    yield core.get_connection
    core._db_path = None
    core._database_url = None




# ── Para ventas (P9-M3): los DOS schemas, como los tiene un producto ─────────

def _schema_de_producto(conn):
    """Lo que hace el `init_db` de un producto, en el mismo orden: el core de
    LibraCore, el comercio, `venta_links` (que desde M5 declara el motor, en
    `erp.schema`, y el producto llama), la FK de `ventas_pagos` repuntada a
    `sales`, y las semillas mínimas (usuario, caja, depósito)."""
    from libracore.db.schema import init_core_schema

    from libracommerce.erp.schema import (
        crear_cliente_lista_precio,
        crear_promociones,
        crear_venta_links,
    )
    from libracommerce.erp.ventas import repuntar_fk_ventas_pagos

    init_core_schema(conn)
    init_schema(conn)
    crear_venta_links(conn)
    crear_cliente_lista_precio(conn)
    crear_promociones(conn)
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
        conn = core.get_connection()
        try:
            _schema_de_producto(conn)
        finally:
            conn.close()
    else:
        url = url_postgres()
        _restaurar("ventas", _schema_de_producto)
        core.configure(url)
    yield core.get_connection
    core._db_path = None
    core._database_url = None
