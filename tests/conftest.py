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
