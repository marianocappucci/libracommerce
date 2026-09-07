"""El DDL de `venta_links` vive en el motor (P9-M5) y es el mismo que los
productos venían declarando por su cuenta."""

from __future__ import annotations

import sqlite3

import pytest

from libracommerce.erp.schema import VENTA_LINKS_DDL, crear_venta_links


def _columnas(conn) -> dict[str, str]:
    filas = conn.execute("PRAGMA table_info(venta_links)").fetchall()
    return {f[1]: f[2].upper() for f in filas}


def test_crea_la_tabla_con_sus_seis_columnas_y_es_idempotente(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "x.db"))
    # Las cuatro tablas referenciadas, mínimas: acá sólo se prueba el DDL.
    for t in ("sales", "facturas", "remitos", "turnos_caja"):
        conn.execute(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY)")

    crear_venta_links(conn)
    assert _columnas(conn) == {
        "venta_id": "INTEGER", "factura_id": "INTEGER", "remito_id": "INTEGER",
        "turno_id": "INTEGER", "mp_order_id": "TEXT", "mp_payment_id": "TEXT",
    }
    # Idempotente: dos veces no falla ni duplica.
    crear_venta_links(conn)
    assert len(_columnas(conn)) == 6

    # Las cuatro FKs, con el borrado que corresponde a cada una: la venta se
    # lleva su fila, los comprobantes y el turno sólo dejan el campo en null.
    fks = {f[2]: (f[3], f[6]) for f in conn.execute("PRAGMA foreign_key_list(venta_links)").fetchall()}
    assert fks["sales"] == ("venta_id", "CASCADE")
    assert fks["facturas"][1] == "SET NULL" and fks["remitos"][1] == "SET NULL"
    assert fks["turnos_caja"][1] == "SET NULL"
    conn.close()


def test_el_ddl_apunta_a_los_dos_motores():
    """El control de por qué esto NO puede vivir en `db/schema.py`: la tabla
    referencia tres tablas de LibraCore, que ahí no existirían."""
    assert "REFERENCES sales(" in VENTA_LINKS_DDL
    for ajena in ("facturas", "remitos", "turnos_caja"):
        assert f"REFERENCES {ajena}(" in VENTA_LINKS_DDL


def test_sin_las_tablas_referenciadas_postgres_lo_rechaza(abrir):
    """Por qué el producto la crea DESPUÉS de los dos motores.

    Contra PostgreSQL, crear `venta_links` sobre un schema que sólo tiene el
    comercio falla: `facturas` no existe. Contra SQLite pasa sin chistar — que es
    exactamente lo que haría invisible el error si se moviera a `init_schema()`.
    """
    with abrir() as conn:
        if isinstance(conn, sqlite3.Connection):
            crear_venta_links(conn)  # SQLite no valida la FK al crear
            assert _columnas(conn)
            return
        with pytest.raises(Exception) as e:
            crear_venta_links(conn)
        assert "facturas" in str(e.value)


def test_la_fixture_de_ventas_usa_la_del_motor(abrir_ventas):
    """`conftest` tenía su propia copia del DDL hasta M5; ahora hace de
    consumidor de la de verdad."""
    with abrir_ventas() as conn:
        assert conn.execute("SELECT COUNT(*) FROM venta_links").fetchone()[0] == 0
