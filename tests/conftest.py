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

# ── Una base de PostgreSQL por worker, restaurada desde una plantilla ───────
#
# Cada test contra PostgreSQL arranca de una base **nueva**, y rearmarla con
# `DROP SCHEMA public CASCADE` + `init_schema()` costaba ~1,5 s por test (medido;
# la suite tiene ~1.000). Ahora la base sale de `CREATE DATABASE ... TEMPLATE`
# (~0,1 s) desde una plantilla armada la primera vez que se pide, una por tipo de
# fixture (`repo`, `abrir`, `abrir_ventas`), porque cada una deja un estado distinto.
#
# Y una base **por worker** (`<base>_gw0`, ... y `<base>_main` sin xdist): la
# restauracion borra la base con `FORCE`, y con dos procesos sobre la misma se
# pisarian en pleno test. Se la pasa al resto de la suite pisando
# `LIBRACORE_POSTGRES_URL`, que es de donde la leen todos (`url_postgres()`,
# `test_schema_congelado`, `test_migraciones_alembic`). Esos dos ultimos hacen su
# propio `DROP SCHEMA` sobre esa URL y no usan plantillas.
#
# La URL original queda en `_LIBRACOMMERCE_PG_ORIGINAL`: el proceso que lanza a los
# workers tambien importa este modulo, y ellos heredan su entorno; sin guardarla
# derivarian el nombre de la base del controlador y no de la original.
#
# Sin la variable (fuera de CI se saltean los tests de PostgreSQL) no hace nada.
# Pide un rol con CREATEDB; el del servicio de CI es el superusuario del contenedor.
_PG_ORIGINAL = os.environ.setdefault("_LIBRACOMMERCE_PG_ORIGINAL", os.environ.get("LIBRACORE_POSTGRES_URL", ""))
_PG_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
_PG_BASE = ""
_PLANTILLAS = ("repo", "abrir", "ventas")


def _sql_admin(*sentencias: str) -> None:
    import psycopg

    admin = _PG_ORIGINAL.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(admin, autocommit=True) as conexion:
        for sentencia in sentencias:
            conexion.execute(sentencia)


def _existe_base(nombre: str) -> bool:
    import psycopg

    admin = _PG_ORIGINAL.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(admin, autocommit=True) as conexion:
        return conexion.execute("SELECT 1 FROM pg_database WHERE datname = %s", (nombre,)).fetchone() is not None


def _base_del_worker() -> None:
    import atexit
    from urllib.parse import urlsplit, urlunsplit

    global _PG_BASE
    if not _PG_ORIGINAL:
        return
    partes = urlsplit(_PG_ORIGINAL)
    _PG_BASE = f"{partes.path.lstrip('/')}_{_PG_WORKER}"

    def _soltar() -> None:
        # `FORCE` por si un test dejo una conexion viva y el DROP se colgaria.
        _sql_admin(*(f'DROP DATABASE IF EXISTS "{n}" WITH (FORCE)'
                     for n in (_PG_BASE, *(f"{_PG_BASE}_t_{t}" for t in _PLANTILLAS))))

    _soltar()  # restos de una corrida interrumpida
    _sql_admin(f'CREATE DATABASE "{_PG_BASE}"')
    atexit.register(_soltar)
    os.environ["LIBRACORE_POSTGRES_URL"] = urlunsplit(partes._replace(path=f"/{_PG_BASE}"))


_base_del_worker()


def _url_de(nombre: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    return urlunsplit(urlsplit(_PG_ORIGINAL)._replace(path=f"/{nombre}"))


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
    """Deja la base del worker como una nueva armada con `construir(conn)`, copiandola de la plantilla `plantilla`.

    La plantilla se arma la primera vez. `CREATE DATABASE ... TEMPLATE` falla si
    queda **alguien** conectado a ella, y se las termina por las dudas. Si armarla
    falla a medias se borra, para que la proxima vez no se tome una incompleta por buena.
    """
    nombre = f"{_PG_BASE}_t_{plantilla}"
    if not _existe_base(nombre):
        _sql_admin(f'CREATE DATABASE "{nombre}"')
        try:
            _en_base(_url_de(nombre), construir)
            _sql_admin(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = '{nombre}' AND pid <> pg_backend_pid()"
            )
        except BaseException:
            _sql_admin(f'DROP DATABASE IF EXISTS "{nombre}" WITH (FORCE)')
            raise
    _sql_admin(
        f'DROP DATABASE IF EXISTS "{_PG_BASE}" WITH (FORCE)',
        f'CREATE DATABASE "{_PG_BASE}" TEMPLATE "{nombre}"',
    )


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
