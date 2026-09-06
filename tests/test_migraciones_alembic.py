"""La cadena de Alembic de LibraCommerce, EJECUTADA contra los dos motores.

Lo que se cuida es que **las dos rutas al schema converjan**: una instalación
nueva (base vacía → `upgrade head`) y una instancia viva (schema puesto por
`init_schema()` en el arranque → `upgrade head`) terminan en el MISMO schema,
y las dos quedan con la versión registrada en `alembic_version_libracommerce`.

Y lo que ningún test de LibraCore cubre, porque es de este motor: **las dos
cadenas conviven en la misma base**. En Contalibra y Restolibra `upgrade` de
LibraCore y `upgrade` de LibraCommerce corren una detrás de la otra sobre la
misma base, cada una con su tabla de versión. Si compartieran `alembic_version`,
la segunda encontraría una revisión que no conoce.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from libracore import migrar as migrar_core
from libracore.db import core
from libracore.db.schema_dump import volcar_schema

from libracommerce import migrar
from libracommerce.db.schema import init_schema

RAIZ = Path(__file__).resolve().parents[1]


def _liberar():
    core._db_path = None
    core._database_url = None


def _con_conexion(destino: str, funcion):
    core.configure(destino)
    conn = core.get_connection()
    try:
        return funcion(conn)
    finally:
        conn.close()
        _liberar()


def _volcar(destino: str) -> str:
    return _con_conexion(destino, volcar_schema)


def _sin_version(volcado: str) -> list[str]:
    """El volcado sin las tablas de versión ni las cabeceras con conteos: son
    de la herramienta, no del schema del motor."""
    return [
        linea
        for linea in volcado.splitlines()
        if "alembic_version" not in linea and not linea.startswith("## ")
    ]


def _version_registrada(destino: str, tabla: str) -> list[str]:
    def leer(conn):
        return [fila[0] for fila in conn.execute(f"SELECT version_num FROM {tabla}").fetchall()]

    return _con_conexion(destino, leer)


def _tablas(destino: str) -> set[str]:
    def leer(conn):
        if core.is_postgres():
            filas = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            ).fetchall()
        else:
            filas = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        return {fila[0] for fila in filas}

    return _con_conexion(destino, leer)


def _revision_head() -> str:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(migrar.configuracion("sqlite:///x")).get_current_head()


def _crear_base_viva(destino: str):
    """Como la de una instancia: schema puesto por el arranque, sin versión."""

    def crear(conn):
        init_schema(conn)
        conn.commit()

    _con_conexion(destino, crear)


def _alembic_como_proceso(destino: str, *args: str):
    """Por el `alembic.ini` del repo y como PROCESO, no por API: es la forma
    en que se corre parado en el repo, y si el ini o el env estuvieran mal,
    la API desde el test lo taparía."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *args],
        cwd=RAIZ,
        env={**os.environ, "DATABASE_URL": destino},
        capture_output=True,
        text=True,
    )


def _url_postgres() -> str:
    url = os.environ.get("LIBRACORE_POSTGRES_URL")
    if url:
        return url
    if os.environ.get("CI"):
        pytest.fail("LIBRACORE_POSTGRES_URL no está definida en CI")
    pytest.skip("LIBRACORE_POSTGRES_URL no configurada (fuera de CI se saltea)")


def _limpiar_postgres(url: str):
    def limpiar(conn):
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()

    _con_conexion(url, limpiar)


# ─────────────────────────────────────────────────────────────── el invariante


def _las_dos_rutas_convergen(destino_vacio: str, destino_vivo: str):
    head = _revision_head()

    migrar.upgrade(destino_vacio)
    assert _version_registrada(destino_vacio, migrar.TABLA_DE_VERSION) == [head]

    _crear_base_viva(destino_vivo)
    assert migrar.TABLA_DE_VERSION not in _tablas(destino_vivo), "la base viva no debería tener versión todavía"
    migrar.upgrade(destino_vivo)
    assert _version_registrada(destino_vivo, migrar.TABLA_DE_VERSION) == [head]

    assert _sin_version(_volcar(destino_vacio)) == _sin_version(_volcar(destino_vivo))
    # Y la ruta de Alembic produce lo mismo que produce el arranque de la app:
    # con la baseline como única revisión, un `upgrade` sobre base vacía ES
    # `init_schema()`. Cuando aparezca la primera revisión real, este assert
    # pasa a ser "la migrada tiene MÁS", como en LibraCore.
    assert "catalog_items" in _tablas(destino_vacio)
    assert "schema_migrations" in _tablas(destino_vacio), "la cadena numerada vieja tiene que seguir registrada"


def _no_pisa_la_tabla_de_libracore(destino: str):
    """🔴 La tabla de versión es propia: `alembic_version` queda para LibraCore."""
    migrar.upgrade(destino)
    tablas = _tablas(destino)
    assert migrar.TABLA_DE_VERSION in tablas
    assert "alembic_version" not in tablas, "la cadena de LibraCommerce escribió en la tabla de LibraCore"


def _las_dos_cadenas_conviven(destino: str):
    """Como en Contalibra y Restolibra: LibraCore y LibraCommerce en la misma base,
    migradas una detrás de la otra, cada una con su versión."""
    migrar_core.upgrade(destino)
    migrar.upgrade(destino)
    tablas = _tablas(destino)
    assert {"alembic_version", migrar.TABLA_DE_VERSION} <= tablas
    assert "clients" in tablas and "catalog_items" in tablas
    assert _version_registrada(destino, migrar.TABLA_DE_VERSION) == [_revision_head()]
    assert len(_version_registrada(destino, "alembic_version")) == 1
    # Y en el orden inverso también, que es el que un deploy podría usar.
    migrar.upgrade(destino)
    migrar_core.upgrade(destino)
    assert _version_registrada(destino, migrar.TABLA_DE_VERSION) == [_revision_head()]


# ───────────────────────────────────────────────────────────────────── SQLite


def test_las_dos_rutas_al_schema_convergen_sqlite(tmp_path):
    _las_dos_rutas_convergen(str(tmp_path / "vacia.db"), str(tmp_path / "viva.db"))


def test_no_pisa_la_tabla_de_version_de_libracore_sqlite(tmp_path):
    _no_pisa_la_tabla_de_libracore(str(tmp_path / "sola.db"))


def test_las_dos_cadenas_conviven_en_la_misma_base_sqlite(tmp_path):
    _las_dos_cadenas_conviven(str(tmp_path / "las_dos.db"))


def test_upgrade_es_idempotente_sqlite(tmp_path):
    destino = str(tmp_path / "dos_veces.db")
    migrar.upgrade(destino)
    antes = _volcar(destino)
    migrar.upgrade(destino)
    assert _volcar(destino) == antes
    assert _version_registrada(destino, migrar.TABLA_DE_VERSION) == [_revision_head()]


def test_alembic_ini_del_repo_corre_la_misma_cadena(tmp_path):
    """El `alembic.ini` y el `env.py` como los usa alguien parado en el repo."""
    destino = str(tmp_path / "por_ini.db")
    proceso = _alembic_como_proceso(destino, "upgrade", "head")
    assert proceso.returncode == 0, proceso.stderr[-2000:]
    assert _version_registrada(destino, migrar.TABLA_DE_VERSION) == [_revision_head()]
    actual = _alembic_como_proceso(destino, "current")
    assert _revision_head() in actual.stdout + actual.stderr


def test_el_cli_migra_contra_el_destino_explicito(tmp_path, monkeypatch):
    """`libracommerce-migrar upgrade` con la salida de emergencia, de punta a punta."""
    destino = str(tmp_path / "cli.db")
    monkeypatch.setenv("LIBRACOMMERCE_MIGRAR_URL", destino)
    monkeypatch.setenv("DATABASE_URL", str(tmp_path / "la_equivocada.db"))
    assert migrar.main(["upgrade"]) == 0
    assert _version_registrada(destino, migrar.TABLA_DE_VERSION) == [_revision_head()]
    assert not (tmp_path / "la_equivocada.db").exists(), "migró DATABASE_URL en vez del destino explícito"


def test_el_modo_offline_falla_en_vez_de_mentir(tmp_path):
    proceso = _alembic_como_proceso(str(tmp_path / "off.db"), "upgrade", "head", "--sql")
    assert proceso.returncode != 0
    assert "offline" in (proceso.stdout + proceso.stderr).lower()


def test_la_baseline_no_se_baja(tmp_path):
    destino = str(tmp_path / "baja.db")
    migrar.upgrade(destino)
    with pytest.raises(Exception, match="no se baja"):
        from alembic import command

        command.downgrade(migrar.configuracion(destino), "base")


# ───────────────────────────────────────────────────────────────── PostgreSQL


def test_las_dos_rutas_al_schema_convergen_postgres(tmp_path):
    """Contra PostgreSQL no hay dos bases: se corre la ruta vacía, se vuelca, se
    limpia, y se corre la viva. Lo que se compara es el volcado."""
    url = _url_postgres()
    head = _revision_head()

    _limpiar_postgres(url)
    migrar.upgrade(url)
    assert _version_registrada(url, migrar.TABLA_DE_VERSION) == [head]
    desde_vacia = _sin_version(_volcar(url))

    _limpiar_postgres(url)
    _crear_base_viva(url)
    migrar.upgrade(url)
    assert _version_registrada(url, migrar.TABLA_DE_VERSION) == [head]
    desde_viva = _sin_version(_volcar(url))

    assert desde_vacia == desde_viva
    assert "schema_migrations" in _tablas(url)


def test_no_pisa_la_tabla_de_version_de_libracore_postgres():
    url = _url_postgres()
    _limpiar_postgres(url)
    _no_pisa_la_tabla_de_libracore(url)


def test_las_dos_cadenas_conviven_en_la_misma_base_postgres():
    url = _url_postgres()
    _limpiar_postgres(url)
    _las_dos_cadenas_conviven(url)
