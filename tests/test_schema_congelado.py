"""El gate del schema de LibraCommerce, contra los DOS motores.

Este motor ya tiene lo que a [[libracore]] le faltaba: una cadena numerada y
trackeada (`db/migrations.py` + la tabla `schema_migrations`). Lo que **no**
tenía es algo que impida que el schema y esa cadena se separen — que es
exactamente el defecto que la cadena vino a arreglar, un nivel más arriba.

Lo que se congela es el **resultado**: el schema que produce `init_schema()`
sobre una base vacía, volcado a texto ordenado y comparado contra una fixture
por motor. Congelar el texto del DDL por hash no serviría: se pondría rojo por
un comentario y verde por un `DEFAULT` cambiado.

Y hay una segunda cosa que sólo se ve ejecutando: **crear las tablas no es
correr la cadena**. Se asertan las dos por separado.

> Contra PostgreSQL, `init_schema()` recibe la conexión de LibraCore — la misma
> que le pasan los productos, porque las tablas de los dos motores conviven en
> la misma base. Hasta LibraCore v1.19.x eso moría en la primera línea con
> *syntax error at or near "PRAGMA"* y creaba **cero** tablas.
"""
import difflib
import os
import sqlite3
from pathlib import Path

import pytest

from libracommerce.db.migrations import _MIGRATIONS
from libracommerce.db.schema import init_schema

FIXTURES = Path(__file__).parent / "fixtures"

_COMO_REGENERAR = (
    "Si el cambio es deliberado, regenerá LAS DOS fixtures con "
    "`python -m tests.generar_fixtures_schema` y preguntate si no tendría que "
    "ser una migración nueva en db/migrations.py en vez de un cambio al DDL."
)


def _comparar(actual: str, nombre: str):
    esperado = (FIXTURES / nombre).read_text(encoding="utf-8")
    if actual == esperado:
        return
    diff = list(
        difflib.unified_diff(
            esperado.splitlines(), actual.splitlines(),
            fromfile=f"fixtures/{nombre} (congelado)", tofile="init_schema() (ahora)",
            lineterm="",
        )
    )
    recorte = "\n".join(diff[:40])
    if len(diff) > 40:
        recorte += f"\n... y {len(diff) - 40} líneas más"
    pytest.fail(f"El schema cambió respecto de la fixture.\n\n{recorte}\n\n{_COMO_REGENERAR}")


def _url_postgres() -> str:
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if url:
        return url
    if os.environ.get("CI"):
        pytest.fail(
            "LIBRACORE_POSTGRES_URL no está definida en CI — el gate de "
            "PostgreSQL no se saltea acá"
        )
    pytest.skip("LIBRACORE_POSTGRES_URL no configurada (fuera de CI se saltea)")


def test_schema_sqlite_congelado(tmp_path):
    from libracore.db.schema_dump import volcar_schema

    conn = sqlite3.connect(str(tmp_path / "gate.db"))
    try:
        init_schema(conn)
        conn.commit()
        _comparar(volcar_schema(conn), "schema_sqlite.txt")
    finally:
        conn.close()


def test_schema_postgres_congelado():
    from libracore.db import core
    from libracore.db.schema_dump import volcar_schema

    url = _url_postgres()
    core.configure(url)
    conn = core.get_connection()
    try:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
        init_schema(conn)
        conn.commit()
        _comparar(volcar_schema(conn), "schema_postgres.txt")
    finally:
        conn.close()
        core._db_path = None
        core._database_url = None


@pytest.mark.parametrize("motor", ["sqlite", "postgres"])
def test_la_cadena_queda_registrada_no_solo_las_tablas(motor, tmp_path):
    """Crear el schema no es correr la cadena.

    Una base fresca tiene las columnas nuevas porque el `CREATE TABLE` ya las
    incluye, así que cada migración es un no-op — pero igual tiene que quedar
    **registrada**, o la próxima que se agregue correría contra una base que
    dice no haber aplicado ninguna.
    """
    if motor == "sqlite":
        conn = sqlite3.connect(str(tmp_path / "cadena.db"))
        cerrar = conn.close
    else:
        from libracore.db import core

        url = _url_postgres()
        core.configure(url)
        conn = core.get_connection()
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()

        def cerrar():
            conn.close()
            core._db_path = None
            core._database_url = None

    try:
        init_schema(conn)
        conn.commit()
        aplicadas = {r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
        assert aplicadas == {v for v, _n, _f in _MIGRATIONS}
    finally:
        cerrar()
