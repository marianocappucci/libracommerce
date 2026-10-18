"""El día de Argentina de un `occurred_at` / `received_at` guardado (ADR-066): `hora.dia_local` (Python) y `hora.dia_local_sql` (SQLite y
PostgreSQL) dan lo mismo para el mismo texto.

Las dos formas que guarda el motor: **sin zona** (ya es hora de Argentina: el día son los 10 primeros caracteres, como siempre) y **con
zona** (la recepción de mercadería, `...+00:00`: el día de ese instante en Argentina). El borde es la medianoche de Argentina, las 03:00Z."""
from __future__ import annotations

import datetime

import pytest
import test_vencimientos as _vto
from libracore.db import core
from test_web_compras import _deposito, _producto

from libracommerce import hora
from libracommerce.erp import mermas, stock

destino = _vto.destino          # una base por motor (SQLite y PostgreSQL)
abrir_vto = _vto.abrir_vto

#: (texto guardado, día de Argentina)
CASOS = [
    # Sin zona: el día no cambia, a cualquier hora.
    ("2026-10-17T00:00:00", "2026-10-17"),
    ("2026-10-17T23:59:59", "2026-10-17"),
    ("2026-10-17T23:30:00.123456", "2026-10-17"),
    ("2026-10-17 01:00:00", "2026-10-17"),
    ("2026-10-17", "2026-10-17"),
    # Con zona UTC: antes de las 03:00Z es el día anterior en Argentina.
    ("2026-10-18T02:30:00+00:00", "2026-10-17"),         # 23:30 AR del 17: el caso de la recepción
    ("2026-10-18T02:59:59.999999+00:00", "2026-10-17"),  # 23:59:59.999 AR
    ("2026-10-18T03:00:00+00:00", "2026-10-18"),         # medianoche AR justa
    ("2026-10-18T02:30:00Z", "2026-10-17"),
    ("2026-10-18T15:00:00+00:00", "2026-10-18"),
    # Con otra zona: se lleva al instante y de ahí a Argentina.
    ("2026-10-17T23:30:00-03:00", "2026-10-17"),
    ("2026-10-18T01:00:00+02:00", "2026-10-17"),         # 23:00Z del 17 = 20:00 AR
    # Cambio de mes, de año y 29 de febrero.
    ("2026-11-01T01:00:00+00:00", "2026-10-31"),
    ("2027-01-01T02:00:00+00:00", "2026-12-31"),
    ("2028-03-01T02:00:00+00:00", "2028-02-29"),
]


@pytest.mark.parametrize(("texto", "dia"), CASOS)
def test_dia_local_de_un_texto(texto, dia):
    assert hora.dia_local(texto) == dia


def test_dia_local_de_objetos_y_vacios():
    assert hora.dia_local(None) is None
    assert hora.dia_local(datetime.datetime(2026, 10, 18, 2, 30, tzinfo=datetime.UTC)) == "2026-10-17"
    assert hora.dia_local(datetime.datetime(2026, 10, 17, 23, 30)) == "2026-10-17"
    assert hora.dia_local(datetime.date(2026, 10, 17)) == "2026-10-17"
    assert hora.dia_local("cualquiera-cosa") == "cualquiera"


@pytest.fixture
def conn_de(destino):
    """Una conexión como la de un producto (`libracore.db.core`), con una tabla de prueba."""
    core.configure(destino)
    conn = core.get_connection()
    conn.execute("CREATE TABLE prueba_dias (id INTEGER PRIMARY KEY, valor TEXT)")
    conn.commit()
    yield conn
    conn.close()
    core._db_path = None
    core._database_url = None


def test_dia_local_sql_da_lo_mismo_que_python_en_los_dos_motores(conn_de):
    conn = conn_de
    for i, (texto, _) in enumerate(CASOS, start=1):
        conn.execute("INSERT INTO prueba_dias (id, valor) VALUES (?, ?)", (i, texto))
    conn.execute("INSERT INTO prueba_dias (id, valor) VALUES (?, NULL)", (len(CASOS) + 1,))
    conn.commit()
    filas = conn.execute(f"SELECT valor, {hora.dia_local_sql(conn, 'valor')} AS dia FROM prueba_dias ORDER BY id").fetchall()
    assert [(f["valor"], f["dia"]) for f in filas] == [*CASOS, (None, None)]


def test_el_filtro_por_dia_toma_los_bordes_de_la_medianoche_de_argentina(conn_de):
    conn = conn_de
    valores = [
        "2026-10-16T23:59:59",              # 16
        "2026-10-17T02:59:59+00:00",        # 16 (23:59:59 AR)
        "2026-10-17T03:00:00+00:00",        # 17 (00:00 AR)
        "2026-10-17T00:00:00",              # 17
        "2026-10-18T02:30:00+00:00",        # 17 (23:30 AR)
        "2026-10-18T03:00:00+00:00",        # 18
        "2026-10-18T00:00:00",              # 18
    ]
    for i, v in enumerate(valores, start=1):
        conn.execute("INSERT INTO prueba_dias (id, valor) VALUES (?, ?)", (i, v))
    conn.commit()

    def ids(desde=None, hasta=None):
        donde, params = hora.filtro_de_dias_sql(conn, "valor", desde, hasta)
        sql = "SELECT id FROM prueba_dias" + (" WHERE " + " AND ".join(donde) if donde else "") + " ORDER BY id"
        return [f["id"] for f in conn.execute(sql, params).fetchall()]

    assert ids("2026-10-17", "2026-10-17") == [3, 4, 5]
    assert ids("2026-10-16", "2026-10-16") == [1, 2]
    assert ids("2026-10-18", "2026-10-18") == [6, 7]
    assert ids(desde="2026-10-17") == [3, 4, 5, 6, 7]
    assert ids(hasta="2026-10-17") == [1, 2, 3, 4, 5]
    assert ids() == [1, 2, 3, 4, 5, 6, 7]


def test_el_reporte_de_mermas_cuenta_el_dia_de_argentina_sin_mover_las_filas_sin_zona(abrir_vto):
    producto, deposito = _producto(abrir_vto), _deposito(abrir_vto)
    with abrir_vto() as conn:
        stock.add_movimiento_stock(conn, producto, "entrada", 10, deposito_id=deposito, fecha="2026-10-01")
        stock.add_movimiento_stock(conn, producto, "merma", -1, deposito_id=deposito, fecha="2026-10-17")
        stock.add_movimiento_stock(conn, producto, "merma", -2, deposito_id=deposito, fecha="2026-10-18T02:30:00+00:00")
        stock.add_movimiento_stock(conn, producto, "merma", -4, deposito_id=deposito, fecha="2026-10-18T03:00:00+00:00")
        conn.commit()
        assert mermas.reporte_mermas(conn, "2026-10-17", "2026-10-17")["resumen"]["cantidad"] == 3
        assert mermas.reporte_mermas(conn, "2026-10-18", "2026-10-18")["resumen"]["cantidad"] == 4
        fechas_ = [m["fecha"] for m in stock.get_movimientos_stock(conn, producto)]
    assert fechas_ == ["2026-10-18", "2026-10-17", "2026-10-17", "2026-10-01"]
