"""Ningun DEFAULT del DDL de Libracommerce estampa una hora que no sea la de Argentina.

🔴 **El defecto que cubre no daba error y estuvo desde siempre.** El DEFAULT de
las columnas `created_at`/`updated_at` era `datetime('now')` o `DEFAULT CURRENT_TIMESTAMP`, que en
SQLite es UTC y que el adaptador de PostgreSQL traduce a UTC **a proposito**,
para que las dos bases guarden el mismo texto. O sea que las dos guardaban la
hora equivocada, y de la misma manera. Lo creado entre las 21:00 y la medianoche
quedaba fechado el dia siguiente.

Se midio en la instancia `compulibra` de Contalibra el 2026-08-29 y se barrieron
las 19 bases del VPS con schema de LibraCore: las 19 en UTC. Ver la revision
`0003` de [[libracore]] para el diagnostico completo.

🔑 **El barrido vive en el motor, no aca.** `defaults_fuera_de_hora_ar()` es la
misma funcion que corren LibraCore y los otros tres productos con DDL propio.
Copiar la regex en cada repo es la forma conocida de que empiecen a decir cosas
distintas: paso con las cinco definiciones de "hoy" del frontend, y solo tres
fijaban la zona.

🔑 **Y mira la PROPIEDAD final**, no el patron viejo: "ninguna columna con reloj
queda fuera de la hora de Argentina". Buscar `datetime('now')` dejaria pasar una
columna nueva escrita como `DEFAULT CURRENT_TIMESTAMP`, que tiene el mismo
problema con otra cara.
"""
from pathlib import Path

import pytest
from libracore.db.schema import defaults_con_reloj, defaults_fuera_de_hora_ar

from tests.conftest import url_postgres

RAIZ = Path(__file__).resolve().parents[1]

#: Se barren los directorios, no una lista de archivos escrita a mano: un DDL
#: nuevo en un modulo nuevo tiene que entrar solo. Las revisiones ya aplicadas
#: quedan afuera porque son historia y no se tocan.
_DIRECTORIOS = ('libracommerce',)
_EXCLUIR = ("__pycache__", "/migrations/versions/", "/tests/")


def _fuentes():
    for sub in _DIRECTORIOS:
        for archivo in sorted((RAIZ / sub).rglob("*.py")):
            if any(x in str(archivo) for x in _EXCLUIR):
                continue
            yield archivo


def test_el_barrido_encuentra_el_ddl():
    """Control: sin esto, una lista vacia pasaria por verde para siempre.

    Es el mismo control que lleva la guarda del motor. Un barrido que dejo de
    encontrar archivos —porque el DDL se movio de carpeta, por ejemplo— informa
    "limpio" sobre un repo que no miro.
    """
    encontradas = sum(
        len(defaults_con_reloj(f.read_text(encoding="utf-8"))) for f in _fuentes()
    )
    assert encontradas >= 20, f"el barrido encontro solo {encontradas} columnas con reloj"


@pytest.mark.parametrize("archivo", sorted(_fuentes()),
                         # Por la ruta y no por el nombre: desde P9-M5 hay dos `schema.py`
                         # (`db/` y `erp/`) y pytest los desambiguaba con un sufijo 0/1 que
                         # no dice cual de los dos fallo.
                         ids=lambda f: str(f.relative_to(RAIZ)))
def test_ninguna_columna_estampa_una_hora_que_no_sea_la_de_argentina(archivo):
    fuera = defaults_fuera_de_hora_ar(archivo.read_text(encoding="utf-8"))
    assert fuera == [], (
        f"{archivo.relative_to(RAIZ)} declara columnas con una hora que no es la "
        "de Argentina:\n" + "\n".join(fuera)
    )


# ── La base que YA existe: la migracion 0011 ────────────────────────────────

#: El DEFAULT que tenian estas columnas antes del arreglo, tal como PostgreSQL
#: lo guarda. Es lo que hay hoy en las bases de produccion.
_DEFAULT_VIEJO = "CURRENT_TIMESTAMP"


def test_la_migracion_arregla_una_base_que_ya_existia():
    """🔴 Sin esto el arreglo no le llega a ninguna base real.

    `CREATE TABLE IF NOT EXISTS` sobre una tabla que ya existe **no cambia
    ningun DEFAULT** — es un no-op silencioso, que es la razon por la que
    `db/migrations.py` existe. Lo que alcanza a las bases con datos es la
    migracion `0011`, y lo que se ejercita aca es eso: una base con el DEFAULT
    viejo, `init_schema()` de verdad, y la fila que sale despues.
    """
    from libracore.db import core

    from libracommerce.db.schema import init_schema

    core.configure(url_postgres())
    conn = core.get_connection()
    try:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
        init_schema(conn)
        conn.commit()

        # La base queda como una de produccion: DEFAULT viejo y la migracion
        # nueva marcada como NO aplicada.
        conn.execute(
            f"ALTER TABLE parties ALTER COLUMN created_at SET DEFAULT {_DEFAULT_VIEJO}"
        )
        conn.execute("DELETE FROM schema_migrations WHERE version = 11")
        conn.commit()

        # Control positivo: si el defecto no se reprodujo, lo de abajo no
        # probaria nada.
        assert _default_de(conn, "parties", "created_at") == _DEFAULT_VIEJO

        init_schema(conn)
        conn.commit()

        assert "interval" in _default_de(conn, "parties", "created_at")
    finally:
        conn.close()
        core._db_path = None
        core._database_url = None


def _default_de(conn, tabla: str, columna: str) -> str:
    fila = conn.execute(
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ? AND column_name = ?",
        (tabla, columna),
    ).fetchone()
    return fila[0]
