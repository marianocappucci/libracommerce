"""Vencimientos y lotes, la parte informativa (A-1, ADR-018), contra los dos motores.

Lo que se mide acá es lo que decide si se puede confiar en el aviso y, sobre todo, si los tres productos que
comparten el motor pueden tomarlo sin sorpresas:

- la revisión `0002` corre limpia sobre una base con datos y no toca una sola fila;
- **un producto sin marcar no cambia en nada**: el `INSERT` de `add_movimiento_stock` sin lote es el de siempre, y el
  ledger de un producto sin marcar es el mismo antes y después de la revisión;
- la ventana de días es cerrada (hoy y hoy+N entran), «hoy» es el de Argentina, y lo vencido se distingue de lo que
  vence hoy;
- asignar un vencimiento es un par ADITIVO (no se reescribe ninguna fila) y la merma de un lote no lo deja negativo;
- el lote y el vencimiento de una recepción de compra llegan al ledger.
"""

from __future__ import annotations

import csv
import datetime
import io
import sqlite3
import threading
import time
import uuid
from decimal import Decimal

import pytest
from conftest import USUARIO, _deposito_principal, _schema_de_producto, url_postgres
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from libracore.db import core

from libracommerce import migrar
from libracommerce.db.repository import repositorio_de
from libracommerce.db.schema import init_schema
from libracommerce.domain.entities import Party, PartyType
from libracommerce.erp import catalogo, compras, stock, vencimientos, ventas
from libracommerce.web.vencimientos_router import build_vencimientos_escritura_router, build_vencimientos_router

#: «Hoy» de las pruebas: la ventana de 15 días llega hasta el 15 de octubre.
HOY = datetime.date(2026, 9, 30)
PREVIA = "2026-09-01"


# ── Fixtures: una base por motor, con el schema del motor y la revisión 0002 aplicada ──


@pytest.fixture(params=["sqlite", "postgres"])
def destino(request, tmp_path):
    """El destino de una base VACÍA (sin schema), como lo recibe `libracommerce-migrar`."""
    if request.param == "sqlite":
        yield str(tmp_path / "vencimientos.db")
        return
    url = url_postgres()
    _limpiar_postgres(url)
    yield url


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


def _limpiar_postgres(url: str):
    def limpiar(conn):
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()

    _con_conexion(url, limpiar)


def _crear_base_al_dia_de_0001(destino: str):
    """La base de una instancia hoy: el schema de `init_schema()` (la baseline) y su depósito, sin la revisión 0002."""

    def crear(conn):
        init_schema(conn)
        _deposito_principal(conn)
        conn.commit()

    _con_conexion(destino, crear)


@pytest.fixture
def abrir_vto(destino):
    """Un `conexion()` como el de un producto, con la revisión 0002 ya aplicada (`upgrade head`)."""
    _crear_base_al_dia_de_0001(destino)
    migrar.upgrade(destino)
    core.configure(destino)
    yield core.get_connection
    _liberar()


@pytest.fixture
def abrir_vto_ventas(destino):
    """Como `abrir_vto`, pero con los dos schemas de un producto (LibraCore y LibraCommerce): es lo que necesita una
    venta de mostrador real (`erp.ventas`), que escribe en los dos dentro de la misma transacción."""
    core.configure(destino)
    conn = core.get_connection()
    try:
        _schema_de_producto(conn)
    finally:
        conn.close()
    _liberar()
    migrar.upgrade(destino)
    core.configure(destino)
    yield core.get_connection
    _liberar()


def _columnas_de(conn, tabla: str) -> set[str]:
    return {f[1] for f in conn.execute(f"PRAGMA table_info({tabla})").fetchall()}


def _indices_de(conn, tabla: str) -> set[str]:
    if core.is_postgres():
        filas = conn.execute("SELECT indexname FROM pg_indexes WHERE tablename = ?", (tabla,)).fetchall()
    else:
        filas = conn.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ?", (tabla,)).fetchall()
    return {f[0] for f in filas}


_COLUMNAS_LEDGER = ("item_id, variant_id, location_id, movement_type, quantity_delta, occurred_at, source_type, "
                    "source_id, unit_cost, lot_code, expires_at, note, created_by, reason_code")


def _venta_anterior_a_a4(conn, venta_id, items, fecha="", usuario_id=None, deposito_id=None):
    """Una salida por venta como la escribía el motor ANTES de A-4 PR-2: una fila por línea en el bucket «sin lote», aunque
    el producto esté marcado. Desde el PR-2 `descontar_stock_venta` de un marcado elige lote (FEFO), así que estas pruebas
    del parche de la merma (salidas sin conciliar: ventas viejas, y hasta el PR-3 devoluciones, transferencias y ajustes)
    arman ese ledger a mano."""
    for item in items:
        stock.add_movimiento_stock(conn, item["producto_id"], "venta", -abs(float(item["qty"])), f"Venta ID {venta_id}",
                                   fecha=fecha, venta_id=venta_id, usuario_id=usuario_id, deposito_id=deposito_id,
                                   variant_id=item.get("variante_id"))


def _ledger(conn) -> list[tuple]:
    """El ledger entero sin `id` ni `created_at` (que dependen del momento), en orden de escritura."""
    n = len(_COLUMNAS_LEDGER.split(","))
    return [tuple(f[i] for i in range(n))
            for f in conn.execute(f"SELECT {_COLUMNAS_LEDGER} FROM stock_movements ORDER BY id").fetchall()]


# ── Helpers de datos ─────────────────────────────────────────────────────


def _k() -> str:
    """Una `clave_operacion` nueva (un UUID, como la genera un cliente por cada intento del usuario)."""
    return uuid.uuid4().hex


def _asignar(conn, *args, **kw):
    kw.setdefault("clave_operacion", _k())
    return vencimientos.asignar_vencimiento_a_saldo(conn, *args, **kw)


def _baja(conn, *args, **kw):
    kw.setdefault("clave_operacion", _k())
    return vencimientos.dar_de_baja_lote(conn, *args, **kw)



def _producto(conn, nombre="Yogur", *, vence=True, categoria="", unidad="u", activo=True):
    pid = catalogo.create_producto(conn, nombre, precio_venta=100.0, precio_costo=60.0, categoria=categoria,
                                   unidad=unidad)
    if vence:
        vencimientos.marcar_vence(conn, pid, True)
    if not activo:
        conn.execute("UPDATE catalog_items SET active = 0 WHERE id = ?", (pid,))
    return pid


def _entrada(conn, pid, cantidad, *, lote=None, vence=None, deposito=None, variante=None, fecha=PREVIA):
    stock.add_movimiento_stock(conn, pid, "entrada", cantidad, "carga", fecha=fecha, deposito_id=deposito,
                               variant_id=variante, lot_code=lote, expires_at=vence)


def _dias(n: int) -> str:
    """El vencimiento que queda a `n` días de `HOY`."""
    return (HOY + datetime.timedelta(days=n)).isoformat()


def _proximos(abrir, **kw):
    kw.setdefault("hoy", HOY)
    with abrir() as conn:
        return vencimientos.proximos_a_vencer(conn, **kw)


def _lotes(abrir, pid, **kw):
    kw.setdefault("hoy", HOY)
    with abrir() as conn:
        return vencimientos.lotes_de(conn, pid, **kw)


def _nombres(reporte) -> list[str]:
    return [f"{r['nombre']}|{r['lote']}" for r in reporte["lotes"]]


# ═════════════════════════════════════════════════════════ La revisión 0002

#: La cabeza de la cadena: sube con cada revisión nueva (la 0002 queda verificada igual: la columna y el índice).
_CABEZA = "0005_min_stock_por_sucursal"


def test_la_revision_0002_corre_sobre_datos_previos_sin_tocar_una_fila(destino):
    """Una base «de hoy» —con productos, stock y una venta— sube a `head` sin perder ni cambiar nada, en los dos
    motores. Lo que queda: la columna en 0 para todos, el índice, la versión registrada, y el ledger idéntico."""
    _crear_base_al_dia_de_0001(destino)
    migrar.upgrade(destino, "0001_baseline_commerce")

    def poblar(conn):
        yerba = catalogo.create_producto(conn, "Yerba", precio_venta=100.0, precio_costo=60.0)
        leche = catalogo.create_producto(conn, "Leche", precio_venta=80.0, precio_costo=50.0)
        stock.ajustar_stock(conn, yerba, 40.0, "inicial", fecha=PREVIA)
        stock.add_movimiento_stock(conn, leche, "entrada", 12, "carga", fecha=PREVIA)
        stock.descontar_stock_venta(conn, 1, [{"producto_id": yerba, "qty": 3}], fecha=PREVIA)
        conn.commit()
        assert "tracks_expiry" not in _columnas_de(conn, "catalog_items")
        assert "idx_stock_item_location_lot" not in _indices_de(conn, "stock_movements")
        return _ledger(conn), stock.get_stock_por_deposito(conn)

    antes, stock_antes = _con_conexion(destino, poblar)

    migrar.upgrade(destino)

    def verificar(conn):
        assert "tracks_expiry" in _columnas_de(conn, "catalog_items")
        assert "idx_stock_item_location_lot" in _indices_de(conn, "stock_movements")
        marcas = conn.execute("SELECT tracks_expiry FROM catalog_items").fetchall()
        assert len(marcas) == 2 and {m[0] for m in marcas} == {0}
        assert _ledger(conn) == antes, "la revisión modificó el ledger"
        assert stock.get_stock_por_deposito(conn) == stock_antes
        return [f[0] for f in conn.execute("SELECT version_num FROM alembic_version_libracommerce").fetchall()]

    assert _con_conexion(destino, verificar) == [_CABEZA]

    # Idempotente: una segunda corrida no cambia nada ni falla.
    migrar.upgrade(destino)
    assert _con_conexion(destino, verificar) == [_CABEZA]


def test_la_revision_0002_baja_sin_tocar_el_ledger(destino):
    from alembic import command

    _crear_base_al_dia_de_0001(destino)
    migrar.upgrade(destino)

    def con_lotes(conn):
        pid = _producto(conn)
        _entrada(conn, pid, 10, lote="L1", vence=_dias(5))
        conn.commit()
        return _ledger(conn)

    antes = _con_conexion(destino, con_lotes)
    command.downgrade(migrar.configuracion(destino), "0001_baseline_commerce")

    def verificar(conn):
        assert "tracks_expiry" not in _columnas_de(conn, "catalog_items")
        assert "idx_stock_item_location_lot" not in _indices_de(conn, "stock_movements")
        assert _ledger(conn) == antes, "bajar la revisión modificó el ledger"

    _con_conexion(destino, verificar)
    migrar.upgrade(destino)  # y vuelve a subir


def test_init_schema_sigue_congelado_y_la_columna_es_solo_de_la_revision(destino):
    """El gate del schema (`test_schema_congelado`) congela `init_schema()`: la columna nueva NO está ahí sino en
    la revisión. Es la mitad que dice «la migrada tiene más»."""
    _crear_base_al_dia_de_0001(destino)

    def antes(conn):
        return _columnas_de(conn, "catalog_items")

    sin = _con_conexion(destino, antes)
    migrar.upgrade(destino)
    con = _con_conexion(destino, antes)
    assert "tracks_expiry" not in sin
    assert con - sin == {"tracks_expiry", "lead_time_days", "max_stock", "supplier_party_id"}   # las de la 0002, la 0003 y la 0004


def test_sin_la_revision_el_reporte_falla_diciendo_que_hace_falta_y_el_ledger_sigue_andando(destino):
    """Una base que no corrió `libracommerce-migrar upgrade` no tiene la columna: el módulo lo dice; y escribir un
    lote en el ledger (que sólo usa columnas de la baseline) funciona igual."""
    _crear_base_al_dia_de_0001(destino)
    core.configure(destino)
    try:
        with core.get_connection() as conn:
            pid = catalogo.create_producto(conn, "Yogur", precio_venta=1.0, precio_costo=1.0)
            stock.add_movimiento_stock(conn, pid, "entrada", 5, "carga", fecha=PREVIA, lot_code="L1",
                                       expires_at="2026-10-05")
            assert [(f["lote"], f["vence"], f["saldo"]) for f in vencimientos.lotes_de(conn, pid, hoy=HOY)] == [
                ("L1", "2026-10-05", 5)]
            with pytest.raises(vencimientos.SinRevision, match="libracommerce-migrar upgrade"):
                vencimientos.proximos_a_vencer(conn, hoy=HOY)
            with pytest.raises(vencimientos.SinRevision):
                vencimientos.marcar_vence(conn, pid, True)
    finally:
        _liberar()


# ═════════════════════════════════════════ Un producto sin marcar no cambia en nada


_INSERT_DE_SIEMPRE = """INSERT INTO stock_movements
           (item_id, variant_id, location_id, movement_type, quantity_delta, occurred_at,
            source_type, source_id, note, created_by, reason_code)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)"""


class _Grabadora:
    """Una conexión que anota lo que se le ejecuta, para comparar el `INSERT` carácter por carácter."""

    def __init__(self, conn):
        self._conn = conn
        self.ejecutado: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.ejecutado.append((sql, tuple(params)))
        return self._conn.execute(sql, params)


def test_sin_lote_ni_vencimiento_el_insert_es_el_de_siempre(abrir_vto):
    with abrir_vto() as conn:
        pid = catalogo.create_producto(conn, "Yerba", precio_venta=1.0, precio_costo=1.0)
        deposito = catalogo.get_default_deposito_id(conn)
        g = _Grabadora(conn)
        stock.add_movimiento_stock(g, pid, "venta", -2, "Venta ID 9", fecha="2026-09-10", venta_id=9,
                                   usuario_id=7, deposito_id=deposito)
        stock.add_movimiento_stock(g, pid, "entrada", 5, "carga", fecha="2026-09-11", deposito_id=deposito,
                                   lot_code=None, expires_at=None)
    assert [s for s, _ in g.ejecutado] == [_INSERT_DE_SIEMPRE, _INSERT_DE_SIEMPRE]
    assert g.ejecutado[0][1] == (pid, None, deposito, "sale", -2, "2026-09-10T00:00:00", "venta", 9, "Venta ID 9",
                                 7, "venta")
    assert g.ejecutado[1][1] == (pid, None, deposito, "adjustment", 5, "2026-09-11T00:00:00", None, None, "carga",
                                 None, "entrada")


def _operaciones_de_un_producto_sin_marcar(conn, pid, deposito_b):
    """Todo lo que hace un producto sin lotes: entrada, ajuste, venta, merma y una transferencia entre depósitos."""
    stock.add_movimiento_stock(conn, pid, "entrada", 20, "carga", fecha=PREVIA)
    stock.ajustar_stock(conn, pid, 18.0, "conteo", fecha="2026-09-02")
    stock.descontar_stock_venta(conn, 5, [{"producto_id": pid, "qty": 3}], fecha="2026-09-03", usuario_id=7)
    stock.add_movimiento_stock(conn, pid, "merma", -1, "Merma: Rotura", fecha="2026-09-04")
    stock.add_movimiento_stock(conn, pid, "transferencia_salida", -4, "a norte", fecha="2026-09-05")
    stock.add_movimiento_stock(conn, pid, "transferencia_entrada", 4, "de principal", fecha="2026-09-05",
                               deposito_id=deposito_b)


def test_el_ledger_de_un_producto_sin_marcar_es_el_mismo_antes_y_despues_de_la_revision(destino):
    """El mismo guion sobre dos productos sin marcar —uno con la base en 0001, otro con la base en 0002— escribe
    filas idénticas (salvo el producto): la revisión no cambia lo que un producto que no la usa escribe."""
    _crear_base_al_dia_de_0001(destino)
    migrar.upgrade(destino, "0001_baseline_commerce")

    def antes(conn):
        norte = catalogo.create_deposito(conn, "Norte")
        pid = catalogo.create_producto(conn, "Yerba", precio_venta=1.0, precio_costo=1.0)
        _operaciones_de_un_producto_sin_marcar(conn, pid, norte)
        conn.commit()
        return norte, pid

    norte, viejo = _con_conexion(destino, antes)
    migrar.upgrade(destino)

    def despues(conn):
        nuevo = catalogo.create_producto(conn, "Yerba 2", precio_venta=1.0, precio_costo=1.0)
        _operaciones_de_un_producto_sin_marcar(conn, nuevo, norte)
        conn.commit()
        filas = conn.execute(f"SELECT item_id, {_COLUMNAS_LEDGER} FROM stock_movements ORDER BY id").fetchall()
        n = len(_COLUMNAS_LEDGER.split(","))
        por_producto = {}
        for f in filas:
            por_producto.setdefault(f[0], []).append(tuple(f[i + 1] for i in range(n)))
        return por_producto, nuevo

    por_producto, nuevo = _con_conexion(destino, despues)

    def sin_item(filas):  # la primera columna del ledger es el `item_id`: lo único que tiene que diferir
        return [(None, *f[1:]) for f in filas]

    assert len(por_producto[viejo]) == 6
    assert sin_item(por_producto[viejo]) == sin_item(por_producto[nuevo])
    assert all(f[9] is None and f[10] is None for f in por_producto[nuevo]), "un producto sin marcar escribió lote"


def test_un_producto_sin_marcar_no_aparece_en_ningun_reporte_de_vencimientos(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn, vence=False)
        _entrada(conn, pid, 10, lote="L1", vence=_dias(3))  # incluso con lote: sin la marca, no se avisa
    reporte = _proximos(abrir_vto)
    assert reporte["lotes"] == [] and reporte["sin_lote"] == []
    assert reporte["resumen"]["lotes_por_vencer"] == 0


def test_el_stock_total_no_cambia_por_tener_lote(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn)
        norte = catalogo.create_deposito(conn, "Norte")
        principal = catalogo.get_default_deposito_id(conn)
        _entrada(conn, pid, 10, lote="L1", vence=_dias(5))
        _entrada(conn, pid, 6, lote="L2", vence=_dias(40))
        _entrada(conn, pid, 4)
        _entrada(conn, pid, 7, lote="L1", vence=_dias(5), deposito=norte)
        _venta_anterior_a_a4(conn, 1, [{"producto_id": pid, "qty": 2}], fecha="2026-09-05")
        assert stock.get_stock_actual(conn, pid) == 25
        assert stock.get_stock_actual(conn, pid, principal) == 18
        assert stock.get_stock_actual(conn, pid, norte) == 7
        assert stock.get_stock_por_deposito(conn)[pid] == {principal: 18.0, norte: 7.0}
        assert sum(f["saldo"] for f in vencimientos.lotes_de(conn, pid, hoy=HOY)) == 25


# ═══════════════════════════════════════════ Normalización de lote y vencimiento


@pytest.mark.parametrize("entrada", [
    "2026-10-05", "2026-10-05T00:00:00", "2026-10-05T23:59:59", "2026-10-05 10:30", "2026-10-05T10:30:00Z",
    "2026-10-05T01:00:00+00:00", " 2026-10-05 ", datetime.date(2026, 10, 5), datetime.datetime(2026, 10, 5, 22, 30),
])
def test_el_vencimiento_se_normaliza_a_fecha(entrada):
    assert stock.normalizar_vencimiento(entrada) == "2026-10-05"


@pytest.mark.parametrize("basura", ["", "   ", "mañana", "05/10/2026", "2026-13-01", "2026-02-30", 20261005, None,
                                    1.5, ["2026-10-05"]])
def test_un_vencimiento_ilegible_se_rechaza_con_el_valor_a_la_vista(basura):
    with pytest.raises(ValueError, match="expires_at no es una fecha válida"):
        stock.normalizar_vencimiento(basura)


def test_el_lote_se_recorta_y_no_puede_quedar_vacio():
    assert stock.normalizar_lote("  L-2026/09  ") == "L-2026/09"
    for malo in ("", "   ", 5, None):
        with pytest.raises(ValueError, match="lot_code"):
            stock.normalizar_lote(malo)
    with pytest.raises(ValueError, match="64"):
        stock.normalizar_lote("L" * 65)
    assert stock.normalizar_lote("L" * 64) == "L" * 64


def test_add_movimiento_guarda_lote_recortado_y_fecha_normalizada_y_valida_antes_de_escribir(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn)
        stock.add_movimiento_stock(conn, pid, "entrada", 3, "carga", fecha=PREVIA, lot_code="  L1 ",
                                   expires_at=datetime.datetime(2026, 10, 5, 10, 30))
        stock.add_movimiento_stock(conn, pid, "entrada", 2, "sólo fecha", fecha=PREVIA, expires_at="2026-11-01")
        with pytest.raises(ValueError):
            stock.add_movimiento_stock(conn, pid, "entrada", 9, "malo", fecha=PREVIA, expires_at="mañana")
        with pytest.raises(ValueError):
            stock.add_movimiento_stock(conn, pid, "entrada", 9, "malo", fecha=PREVIA, lot_code="  ")
        filas = conn.execute("SELECT lot_code, expires_at, quantity_delta FROM stock_movements ORDER BY id").fetchall()
    assert [(f[0], f[1], float(f[2])) for f in filas] == [("L1", "2026-10-05", 3.0), (None, "2026-11-01", 2.0)]


# ═══════════════════════════════════════════════════════ Existencias por lote


def test_lotes_de_agrupa_por_deposito_lote_y_vencimiento_e_incluye_el_bucket_sin_lote(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn)
        norte = catalogo.create_deposito(conn, "Norte")
        principal = catalogo.get_default_deposito_id(conn)
        _entrada(conn, pid, 10, lote="L1", vence=_dias(5))
        _entrada(conn, pid, 5, lote="L1", vence=_dias(5))          # mismo lote: suma
        _entrada(conn, pid, 6, lote="L2", vence=_dias(40))
        _entrada(conn, pid, 4)                                    # sin lote
        _entrada(conn, pid, 7, lote="L1", vence=_dias(5), deposito=norte)
        _venta_anterior_a_a4(conn, 1, [{"producto_id": pid, "qty": 2}], fecha="2026-09-05")  # sin lote: -2
    filas = _lotes(abrir_vto, pid)
    resumen = [(f["deposito_id"], f["lote"], f["vence"], f["saldo"], f["sin_lote"], f["estado"]) for f in filas]
    assert resumen == [
        (principal, "L1", _dias(5), 15, False, "vigente"),
        (principal, "L2", _dias(40), 6, False, "vigente"),
        (principal, None, None, 2, True, "sin_fecha"),
        (norte, "L1", _dias(5), 7, False, "vigente"),
    ]
    assert filas[0]["dias_para_vencer"] == 5 and filas[2]["dias_para_vencer"] is None
    assert {f["deposito"] for f in filas} == {"Depósito principal", "Norte"}


def test_lotes_de_omite_los_saldos_en_cero_y_muestra_el_negativo(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn)
        _entrada(conn, pid, 10, lote="L1", vence=_dias(5))
        stock.add_movimiento_stock(conn, pid, "merma", -10, "todo", fecha=PREVIA, lot_code="L1", expires_at=_dias(5))
        stock.add_movimiento_stock(conn, pid, "venta", -3, "sin lote", fecha=PREVIA)
    assert [(f["lote"], f["saldo"]) for f in _lotes(abrir_vto, pid)] == [(None, -3)]


def test_el_mismo_lote_escrito_de_dos_formas_es_un_solo_bucket(abrir_vto):
    """La recepción de compras guarda `datetime.isoformat()`; asignar guarda `AAAA-MM-DD`: son el mismo lote."""
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        for vence, lote in (("2026-10-05T00:00:00", "L1"), ("2026-10-05", " L1")):
            conn.execute(
                "INSERT INTO stock_movements (item_id, location_id, movement_type, quantity_delta, occurred_at, "
                "lot_code, expires_at) VALUES (?,?,?,?,?,?,?)",
                (pid, deposito, "purchase", 4, "2026-09-01T00:00:00", lote, vence),
            )
    assert [(f["lote"], f["vence"], f["saldo"]) for f in _lotes(abrir_vto, pid)] == [("L1", "2026-10-05", 8)]


def test_lotes_de_por_deposito_y_sucursal_y_errores(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn)
        a = catalogo.create_sucursal(conn, "Centro")
        b = catalogo.create_sucursal(conn, "Norte")
        dep_a, dep_b = catalogo.get_deposito_de_venta(conn, a), catalogo.get_deposito_de_venta(conn, b)
        _entrada(conn, pid, 3, lote="A", vence=_dias(1), deposito=dep_a)
        _entrada(conn, pid, 5, lote="B", vence=_dias(2), deposito=dep_b)
    assert [f["lote"] for f in _lotes(abrir_vto, pid)] == ["A", "B"]
    assert [f["lote"] for f in _lotes(abrir_vto, pid, sucursal_id=a)] == ["A"]
    assert [f["sucursal"] for f in _lotes(abrir_vto, pid, deposito_id=dep_b)] == ["Norte"]
    assert _lotes(abrir_vto, pid, sucursal_id=a, deposito_id=dep_b) == []
    with abrir_vto() as conn:
        with pytest.raises(vencimientos.ProductoNoEncontrado):
            vencimientos.lotes_de(conn, 9999)
        with pytest.raises(ValueError, match="sucursal"):
            vencimientos.lotes_de(conn, pid, sucursal_id=9999)
        with pytest.raises(ValueError, match="depósito"):
            vencimientos.lotes_de(conn, pid, deposito_id=9999)


# ══════════════════════════════════════════════════ Próximos a vencer


def _yogures(conn):
    """Un producto marcado con un lote en cada borde de la ventana de 15 días (`HOY` = 2026-09-30)."""
    pid = _producto(conn, "Yogur")
    for lote, n in (("VIEJO", -10), ("AYER", -1), ("HOY", 0), ("MAS15", 15), ("MAS16", 16)):
        _entrada(conn, pid, 1, lote=lote, vence=_dias(n))
    return pid


def test_la_ventana_es_cerrada_hoy_y_hoy_mas_dias_entran_y_lo_siguiente_no(abrir_vto):
    with abrir_vto() as conn:
        _yogures(conn)
    r = _proximos(abrir_vto)
    assert [(x["lote"], x["dias_para_vencer"], x["estado"]) for x in r["lotes"]] == [
        ("VIEJO", -10, "vencido"), ("AYER", -1, "vencido"), ("HOY", 0, "por_vencer"), ("MAS15", 15, "por_vencer"),
    ]
    assert (r["hoy"], r["dias"], r["hasta"]) == ("2026-09-30", 15, "2026-10-15")
    # Con 16 días entra el que vence hoy+16; con 1, sólo hoy y mañana.
    assert [x["lote"] for x in _proximos(abrir_vto, dias=16)["lotes"]][-1] == "MAS16"
    assert [x["lote"] for x in _proximos(abrir_vto, dias=1)["lotes"]] == ["VIEJO", "AYER", "HOY"]


def test_sin_incluir_vencidos_solo_lo_que_todavia_no_vencio(abrir_vto):
    with abrir_vto() as conn:
        _yogures(conn)
    r = _proximos(abrir_vto, incluir_vencidos=False)
    assert [x["lote"] for x in r["lotes"]] == ["HOY", "MAS15"]
    assert r["resumen"]["lotes_vencidos"] == 0 and r["resumen"]["lotes_por_vencer"] == 2


def test_el_resumen_cuenta_lotes_y_unidades(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn, "Yogur")
        _entrada(conn, pid, 4, lote="A", vence=_dias(-3))
        _entrada(conn, pid, 6, lote="B", vence=_dias(-2))
        _entrada(conn, pid, 2.5, lote="C", vence=_dias(7))
        _entrada(conn, pid, 8, lote="D", vence=_dias(90))   # fuera de la ventana
        _entrada(conn, pid, 3)                              # sin lote: se avisa aparte
    assert _proximos(abrir_vto)["resumen"] == {
        "lotes_por_vencer": 1, "lotes_vencidos": 2, "unidades_por_vencer": 2.5, "unidades_vencidas": 10,
        "productos": 1, "productos_sin_lote": 1, "productos_con_salidas_sin_lote": 0,
        "saldos_sin_fecha": 1, "saldos_con_salidas_sin_lote": 0,
    }


def test_un_producto_sin_marcar_o_inactivo_no_aparece_y_un_lote_sin_saldo_tampoco(abrir_vto):
    with abrir_vto() as conn:
        _entrada(conn, _producto(conn, "Marcado"), 5, lote="OK", vence=_dias(3))
        _entrada(conn, _producto(conn, "SinMarca", vence=False), 5, lote="X", vence=_dias(3))
        _entrada(conn, _producto(conn, "Inactivo", activo=False), 5, lote="Y", vence=_dias(3))
        vaciado = _producto(conn, "Vaciado")
        _entrada(conn, vaciado, 5, lote="Z", vence=_dias(3))
        stock.add_movimiento_stock(conn, vaciado, "merma", -5, "todo", fecha=PREVIA, lot_code="Z", expires_at=_dias(3))
    assert _nombres(_proximos(abrir_vto)) == ["Marcado|OK"]


def test_lo_que_no_tiene_fecha_no_se_avisa_pero_se_lista_como_sin_lote(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn, "Leche")
        _entrada(conn, pid, 9)                     # sin lote ni fecha: no hay qué avisar, pero hay que fecharlo
        _entrada(conn, pid, 2, lote="SOLO-LOTE")   # con código y sin fecha: tampoco se avisa
    r = _proximos(abrir_vto)
    assert r["lotes"] == []
    assert [(x["nombre"], x["saldo"], x["situacion"]) for x in r["sin_lote"]] == [("Leche", 9, "sin_fecha")]


def test_las_salidas_sin_lote_se_marcan_porque_hasta_A4_dejan_los_lotes_sobreestimados(abrir_vto):
    """Se recibe un lote y se vende SIN lote (la venta todavía no elige): el lote sigue diciendo 10 y el bucket sin
    lote queda en −3. El reporte no lo esconde: lo lista aparte."""
    with abrir_vto() as conn:
        pid = _producto(conn, "Yogur")
        _entrada(conn, pid, 10, lote="L1", vence=_dias(5))
        _venta_anterior_a_a4(conn, 1, [{"producto_id": pid, "qty": 3}], fecha="2026-09-10")
    r = _proximos(abrir_vto)
    assert [(x["lote"], x["saldo"]) for x in r["lotes"]] == [("L1", 10)]
    assert [(x["nombre"], x["saldo"], x["situacion"]) for x in r["sin_lote"]] == [("Yogur", -3, "salidas_sin_lote")]
    assert r["resumen"]["productos_con_salidas_sin_lote"] == 1 and r["resumen"]["productos_sin_lote"] == 0


def test_los_saldos_sin_lote_se_clasifican_por_deposito_y_no_se_cancelan_entre_depositos(abrir_vto):
    """−5 sin lote en un depósito y +5 en otro NO suman 0: el negativo (salidas que todavía no bajaron ningún lote de
    ese depósito) se conserva en la respuesta y en los contadores, y el positivo se sigue avisando como sin fecha."""
    with abrir_vto() as conn:
        yogur = _producto(conn, "Yogur")
        norte = catalogo.create_deposito(conn, "Norte")
        principal = catalogo.get_default_deposito_id(conn)
        _entrada(conn, yogur, 10, lote="L1", vence=_dias(5), deposito=principal)
        _venta_anterior_a_a4(conn, 1, [{"producto_id": yogur, "qty": 5}], fecha="2026-09-10",
                                    deposito_id=principal)                      # −5 sin lote en principal
        _entrada(conn, yogur, 5, deposito=norte)                                # +5 sin lote en norte
    r = _proximos(abrir_vto)
    assert [(x["deposito_id"], x["saldo"], x["situacion"]) for x in r["sin_lote"]] == [
        (principal, -5, "salidas_sin_lote"), (norte, 5, "sin_fecha")]
    assert r["resumen"]["productos_con_salidas_sin_lote"] == 1 and r["resumen"]["productos_sin_lote"] == 1
    assert r["resumen"]["saldos_con_salidas_sin_lote"] == 1 and r["resumen"]["saldos_sin_fecha"] == 1
    # Filtrando por depósito se ve sólo lo de ese depósito.
    assert [x["situacion"] for x in _proximos(abrir_vto, deposito_id=principal)["sin_lote"]] == ["salidas_sin_lote"]
    assert [x["situacion"] for x in _proximos(abrir_vto, deposito_id=norte)["sin_lote"]] == ["sin_fecha"]


def test_los_saldos_sin_lote_se_clasifican_tambien_por_variante(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn, "Yogur")
        rojo = catalogo.create_variante(conn, pid, "SKU-R", "Frutilla")["id"]
        azul = catalogo.create_variante(conn, pid, "SKU-A", "Durazno")["id"]
        _entrada(conn, pid, 4, variante=rojo)
        _entrada(conn, pid, 4, variante=azul)
        _venta_anterior_a_a4(conn, 1, [{"producto_id": pid, "qty": 6, "variante_id": azul}],
                                    fecha="2026-09-10")            # el neto del producto es +2: no debe esconder −2
    r = _proximos(abrir_vto)
    assert [(x["variante"], x["saldo"], x["situacion"]) for x in r["sin_lote"]] == [
        ("Durazno", -2, "salidas_sin_lote"), ("Frutilla", 4, "sin_fecha")]


def test_varios_depositos_y_sucursales_se_filtran_y_se_ordenan_por_vencimiento(abrir_vto):
    with abrir_vto() as conn:
        a = catalogo.create_sucursal(conn, "Centro")
        b = catalogo.create_sucursal(conn, "Norte")
        dep_a, dep_b = catalogo.get_deposito_de_venta(conn, a), catalogo.get_deposito_de_venta(conn, b)
        principal = catalogo.get_default_deposito_id(conn)  # sin sucursal
        yogur = _producto(conn, "Yogur", categoria="Lácteos")
        queso = _producto(conn, "Queso", categoria="Fiambres")
        _entrada(conn, yogur, 5, lote="Y1", vence=_dias(9), deposito=dep_a)
        _entrada(conn, yogur, 6, lote="Y1", vence=_dias(9), deposito=dep_b)
        _entrada(conn, queso, 7, lote="Q1", vence=_dias(2), deposito=dep_a)
        _entrada(conn, queso, 8, lote="Q2", vence=_dias(4), deposito=principal)
    todo = _proximos(abrir_vto)
    assert _nombres(todo) == ["Queso|Q1", "Queso|Q2", "Yogur|Y1", "Yogur|Y1"]
    assert [x["sucursal"] for x in todo["lotes"]] == ["Centro", None, "Centro", "Norte"]
    assert _nombres(_proximos(abrir_vto, sucursal_id=a)) == ["Queso|Q1", "Yogur|Y1"]
    assert [x["saldo"] for x in _proximos(abrir_vto, sucursal_id=b)["lotes"]] == [6]
    assert _nombres(_proximos(abrir_vto, deposito_id=principal)) == ["Queso|Q2"]
    assert _nombres(_proximos(abrir_vto, categoria="lácteos")) == ["Yogur|Y1", "Yogur|Y1"]
    assert _nombres(_proximos(abrir_vto, producto_id=queso)) == ["Queso|Q1", "Queso|Q2"]
    assert _proximos(abrir_vto, producto_id=99999)["lotes"] == []


@pytest.mark.parametrize("dias", [0, -1, 366, 2.5, "15", True, None])
def test_dias_fuera_de_rango_es_error(abrir_vto, dias):
    with abrir_vto() as conn, pytest.raises(ValueError, match="dias"):
        vencimientos.proximos_a_vencer(conn, dias=dias, hoy=HOY)


def test_el_tope_de_dias_es_valido_y_una_sucursal_o_deposito_inexistente_es_error(abrir_vto):
    assert _proximos(abrir_vto, dias=vencimientos.MAX_DIAS_AVISO)["hasta"] == "2027-09-30"
    with abrir_vto() as conn:
        with pytest.raises(ValueError, match="sucursal 999"):
            vencimientos.proximos_a_vencer(conn, sucursal_id=999, hoy=HOY)
        with pytest.raises(ValueError, match="depósito 999"):
            vencimientos.proximos_a_vencer(conn, deposito_id=999, hoy=HOY)


def test_hoy_es_el_de_argentina_y_no_el_del_servidor():
    utc = datetime.UTC
    # 01:30 UTC del 1 de octubre son las 22:30 del 30 de septiembre en Buenos Aires.
    assert vencimientos.hoy_argentina(datetime.datetime(2026, 10, 1, 1, 30, tzinfo=utc)) == datetime.date(2026, 9, 30)
    assert vencimientos.hoy_argentina(datetime.datetime(2026, 10, 1, 3, 0, tzinfo=utc)) == datetime.date(2026, 10, 1)
    assert vencimientos.hoy_argentina(datetime.datetime(2026, 10, 1, 2, 59, tzinfo=utc)) == datetime.date(2026, 9, 30)
    assert abs((vencimientos.hoy_argentina() - datetime.datetime.now(utc).date()).days) <= 1


def test_sin_hoy_explicito_el_reporte_usa_la_fecha_de_argentina(abrir_vto, monkeypatch):
    with abrir_vto() as conn:
        _entrada(conn, _producto(conn), 1, lote="L", vence="2026-10-15")
    monkeypatch.setattr(vencimientos, "hoy_argentina", lambda ahora=None: HOY)
    with abrir_vto() as conn:
        r = vencimientos.proximos_a_vencer(conn)
    assert r["hoy"] == "2026-09-30" and [x["lote"] for x in r["lotes"]] == ["L"]


# ═══════════════════════════════════════════════════ Marcar y asignar vencimiento


def test_marcar_vence_marca_desmarca_y_no_toca_el_ledger(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn, vence=False)
        _entrada(conn, pid, 5)
        antes = _ledger(conn)
        assert vencimientos.ficha(conn, pid)["vence"] is False
        assert vencimientos.marcar_vence(conn, pid, True) == {"producto_id": pid, "vence": True}
        assert vencimientos.ficha(conn, pid)["vence"] is True
        vencimientos.marcar_vence(conn, pid, False)
        assert vencimientos.ficha(conn, pid)["vence"] is False
        assert _ledger(conn) == antes


def test_marcar_vence_valida(abrir_vto):
    with abrir_vto() as conn:
        servicio = catalogo.create_producto(conn, "Flete", precio_venta=1.0, tipo="servicio")
        pid = _producto(conn, vence=False)
        with pytest.raises(vencimientos.ProductoNoEncontrado):
            vencimientos.marcar_vence(conn, 9999, True)
        with pytest.raises(vencimientos.ReglaDeNegocio, match="servicio"):
            vencimientos.marcar_vence(conn, servicio, True)
        for malo in (1, "si", None):
            with pytest.raises(ValueError, match="vence"):
                vencimientos.marcar_vence(conn, pid, malo)


def test_la_marca_sobrevive_a_editar_el_producto_y_a_recibir_una_compra(abrir_vto):
    """`save_catalog_item` hace un `UPDATE` con las columnas que conoce: no puede pisar `tracks_expiry`."""
    with abrir_vto() as conn:
        pid = _producto(conn, "Yogur")
        catalogo.update_producto(conn, pid, "Yogur entero", "", "", 120.0, 70.0, "u", "", 1)
        assert vencimientos.ficha(conn, pid)["vence"] is True
        proveedor = repositorio_de(conn).save_party(Party(None, PartyType.ORGANIZATION, "Distribuidora SA")).id
        rid = compras.crear_recepcion(conn, supplier_party_id=proveedor)["id"]
        compras.agregar_linea_recepcion(conn, rid, item_id=pid, quantity=Decimal("5"), unit_cost=Decimal("65"))
        compras.confirmar_recepcion(conn, rid, location_id=catalogo.get_default_deposito_id(conn),
                                    occurred_at=datetime.datetime(2026, 9, 20, 9))
        assert vencimientos.ficha(conn, pid)["vence"] is True


def _todo_el_ledger(conn):
    return conn.execute("SELECT * FROM stock_movements ORDER BY id").fetchall()


def test_asignar_es_un_par_aditivo_y_no_toca_las_filas_viejas_ni_el_total(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        _entrada(conn, pid, 10)                       # sin lote
        _entrada(conn, pid, 3, lote="L0", vence=_dias(60))
        antes = [tuple(f) for f in _todo_el_ledger(conn)]
        total = stock.get_stock_actual(conn, pid)
        r = _asignar(
            conn, pid, deposito, "  L1 ", datetime.date(2026, 10, 5), 4, usuario_id=7, nota="Conteo de góndola",
        )
        despues = [tuple(f) for f in _todo_el_ledger(conn)]
        assert stock.get_stock_actual(conn, pid) == total == 13
        assert stock.get_stock_actual(conn, pid, deposito) == 13
    assert despues[:len(antes)] == antes, "se reescribió una fila ya escrita"
    assert len(despues) == len(antes) + 2
    with abrir_vto() as conn:
        filas = conn.execute(
            "SELECT quantity_delta, lot_code, expires_at, note, occurred_at, reason_code, movement_type, "
            "created_by, location_id FROM stock_movements ORDER BY id DESC LIMIT 2"
        ).fetchall()
    positiva, negativa = filas  # de la más nueva a la más vieja
    assert (float(negativa[0]), negativa[1], negativa[2]) == (-4.0, None, None)
    assert (float(positiva[0]), positiva[1], positiva[2]) == (4.0, "L1", "2026-10-05")
    # La misma referencia (con un identificador que las une), la misma fecha, el mismo depósito y usuario.
    marca = r["referencia"]
    assert marca.startswith("Conteo de góndola [asignación ") and marca in negativa[3] and marca in positiva[3]
    assert negativa[4] == positiva[4] and negativa[8] == positiva[8] == deposito and negativa[7] == positiva[7] == 7
    assert (negativa[5], negativa[6]) == (positiva[5], positiva[6]) == ("ajuste", "adjustment")
    assert r["cantidad"] == 4 and r["saldo_sin_lote"] == 6
    resumen = [(f["lote"], f["vence"], f["saldo"]) for f in _lotes(abrir_vto, pid)]
    assert resumen == [("L1", "2026-10-05", 4), ("L0", _dias(60), 3), (None, None, 6)]


def test_asignar_falla_sin_saldo_sin_lote_y_no_escribe_nada(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        norte = catalogo.create_deposito(conn, "Norte")
        _entrada(conn, pid, 5)
        _entrada(conn, pid, 9, deposito=norte)          # el saldo de otro depósito no cuenta
        _entrada(conn, pid, 8, lote="L0", vence=_dias(9))   # un lote con saldo tampoco es «sin lote»
        antes = _todo_el_ledger(conn)
        with pytest.raises(vencimientos.SaldoInsuficiente, match="saldo sin lote es 5"):
            _asignar(conn, pid, deposito, "L1", "2026-10-05", 6)
        assert [tuple(f) for f in _todo_el_ledger(conn)] == [tuple(f) for f in antes]
        # Justo lo que hay sí se puede.
        _asignar(conn, pid, deposito, "L1", "2026-10-05", 5)
        with pytest.raises(vencimientos.SaldoInsuficiente):
            _asignar(conn, pid, deposito, "L2", "2026-10-06", 1)


def test_asignar_respeta_la_variante(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        variante = catalogo.create_variante(conn, pid, "SKU-1", "Frutilla")["id"]
        _entrada(conn, pid, 5, variante=variante)
        with pytest.raises(vencimientos.SaldoInsuficiente):  # el saldo es de la variante, no del producto a secas
            _asignar(conn, pid, deposito, "L1", "2026-10-05", 1)
        _asignar(conn, pid, deposito, "L1", "2026-10-05", 2, variante_id=variante)
    filas = _lotes(abrir_vto, pid)
    assert [(f["variante"], f["lote"], f["saldo"]) for f in filas] == [
        ("Frutilla", "L1", 2), ("Frutilla", None, 3)]


def test_asignar_valida_entradas_producto_y_deposito(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn)
        sin_marca = _producto(conn, "Sin marca", vence=False)
        deposito = catalogo.get_default_deposito_id(conn)
        _entrada(conn, pid, 5)
        _entrada(conn, sin_marca, 5)
        antes = [tuple(f) for f in _todo_el_ledger(conn)]
        malos = [
            dict(lot_code="  ", expires_at="2026-10-05", cantidad=1),
            dict(lot_code="L1", expires_at="mañana", cantidad=1),
            dict(lot_code="L1", expires_at="2026-10-05", cantidad=0),
            dict(lot_code="L1", expires_at="2026-10-05", cantidad=-2),
            dict(lot_code="L1", expires_at="2026-10-05", cantidad="mucho"),
            dict(lot_code="L1", expires_at="2026-10-05", cantidad=True),
            dict(lot_code="L1", expires_at="2026-10-05", cantidad=float("nan")),
            dict(lot_code=None, expires_at="2026-10-05", cantidad=1),
            dict(lot_code="L1", expires_at=None, cantidad=1),
        ]
        for mal in malos:
            with pytest.raises(ValueError):
                _asignar(conn, pid, deposito, **mal)
        with pytest.raises(ValueError, match="depósito 999"):
            _asignar(conn, pid, 999, "L1", "2026-10-05", 1)
        with pytest.raises(vencimientos.ProductoNoEncontrado):
            _asignar(conn, 9999, deposito, "L1", "2026-10-05", 1)
        with pytest.raises(vencimientos.ReglaDeNegocio, match="no está marcado"):
            _asignar(conn, sin_marca, deposito, "L1", "2026-10-05", 1)
        assert [tuple(f) for f in _todo_el_ledger(conn)] == antes


def test_asignar_acepta_cantidades_fraccionarias(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn, "Queso", unidad="kg")
        deposito = catalogo.get_default_deposito_id(conn)
        _entrada(conn, pid, 2.5)
        _asignar(conn, pid, deposito, "Q1", "2026-10-05", Decimal("0.75"))
    assert [(f["lote"], f["saldo"]) for f in _lotes(abrir_vto, pid)] == [("Q1", 0.75), (None, 1.75)]


# ═════════════════════════════════════════════════════════════ Dar de baja un lote


def test_la_merma_de_un_lote_baja_su_saldo_con_tipo_merma_y_no_deja_negativo(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        _entrada(conn, pid, 10, lote="L1", vence=_dias(-2))
        _entrada(conn, pid, 5, lote="L2", vence=_dias(20))
        r = _baja(conn, pid, deposito, "L1", _dias(-2), 4, usuario_id=7, nota="Heladera 2")
        assert r["saldo_restante"] == 6 and r["cantidad"] == 4
        fila = conn.execute(
            "SELECT quantity_delta, movement_type, reason_code, lot_code, expires_at, note, created_by "
            "FROM stock_movements ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert (float(fila[0]), fila[1], fila[2], fila[3], fila[4], fila[6]) == (-4.0, "waste", "merma", "L1",
                                                                                 _dias(-2), 7)
        assert fila[5].startswith("Merma: Vencimiento") and "L1" in fila[5] and "Heladera 2" in fila[5]
        # Justo lo que queda sí; una unidad más, no.
        with pytest.raises(vencimientos.SaldoInsuficiente, match="tiene 6"):
            _baja(conn, pid, deposito, "L1", _dias(-2), 7)
        antes = [tuple(f) for f in _todo_el_ledger(conn)]
        with pytest.raises(vencimientos.SaldoInsuficiente):
            _baja(conn, pid, deposito, "L1", _dias(-2), 6.0001)
        assert [tuple(f) for f in _todo_el_ledger(conn)] == antes
        _baja(conn, pid, deposito, "L1", _dias(-2), 6, motivo="Rotura")
    assert [(f["lote"], f["saldo"]) for f in _lotes(abrir_vto, pid)] == [("L2", 5)]
    reporte = _proximos(abrir_vto)
    assert reporte["lotes"] == [] or all(x["lote"] != "L1" for x in reporte["lotes"])
    with abrir_vto() as conn:
        assert stock.get_stock_actual(conn, pid) == 5


def test_la_merma_no_puede_sacar_de_un_lote_que_no_existe_ni_de_otro_deposito_ni_de_otra_fecha(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        norte = catalogo.create_deposito(conn, "Norte")
        _entrada(conn, pid, 10, lote="L1", vence=_dias(3))
        _entrada(conn, pid, 10)                                   # sin lote: la merma de un lote no lo toca
        antes = [tuple(f) for f in _todo_el_ledger(conn)]
        for args in ((deposito, "NO-EXISTE", _dias(3)), (norte, "L1", _dias(3)), (deposito, "L1", _dias(4)),
                     (deposito, "L1", None)):
            with pytest.raises(vencimientos.SaldoInsuficiente):
                _baja(conn, pid, *args, 1)
        assert [tuple(f) for f in _todo_el_ledger(conn)] == antes


def test_la_merma_valida_entradas(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        _entrada(conn, pid, 10, lote="L1", vence=_dias(3))
        with pytest.raises(ValueError, match="lote o el vencimiento"):
            _baja(conn, pid, deposito, None, None, 1)
        for cantidad in (0, -1, "x", None):
            with pytest.raises(ValueError):
                _baja(conn, pid, deposito, "L1", _dias(3), cantidad)
        with pytest.raises(ValueError):
            _baja(conn, pid, deposito, "L1", "mañana", 1)
        with pytest.raises(ValueError, match="depósito 999"):
            _baja(conn, pid, 999, "L1", _dias(3), 1)
        with pytest.raises(vencimientos.ProductoNoEncontrado):
            _baja(conn, 9999, deposito, "L1", _dias(3), 1)


def test_se_puede_dar_de_baja_un_lote_que_solo_tiene_fecha(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        _entrada(conn, pid, 3, vence="2026-10-05")
        _baja(conn, pid, deposito, None, "2026-10-05", 3)
    assert _lotes(abrir_vto, pid) == []


def test_dos_bajas_o_asignaciones_seguidas_no_gastan_dos_veces_el_mismo_saldo(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        _entrada(conn, pid, 5, lote="L1", vence=_dias(3))
        _baja(conn, pid, deposito, "L1", _dias(3), 3)
        with pytest.raises(vencimientos.SaldoInsuficiente):
            _baja(conn, pid, deposito, "L1", _dias(3), 3)


def test_en_postgres_dos_escrituras_del_mismo_producto_se_serializan(abrir_vto):
    """El `UPDATE` de sí mismo que toma el producto hace que la segunda transacción espere a la primera: sin eso las
    dos leerían el mismo saldo y las dos pasarían la validación. (SQLite serializa todas las escrituras de la base.)"""
    if not core.is_postgres():
        pytest.skip("sólo PostgreSQL toma el bloqueo por fila")
    with abrir_vto() as conn:
        pid = _producto(conn)
        _entrada(conn, pid, 5, lote="L1", vence=_dias(3))
    deposito = None
    resultados: list = []

    primera = core.get_connection()
    try:
        deposito = catalogo.get_default_deposito_id(primera)
        _baja(primera, pid, deposito, "L1", _dias(3), 4)  # sin commit: tiene el producto

        def segunda():
            c = core.get_connection()
            try:
                _baja(c, pid, deposito, "L1", _dias(3), 4)
                c.commit()
                resultados.append("pasó")
            except vencimientos.SaldoInsuficiente:
                c.rollback()
                resultados.append("saldo insuficiente")
            finally:
                c.close()

        hilo = threading.Thread(target=segunda)
        hilo.start()
        time.sleep(1.0)
        assert hilo.is_alive() and not resultados, "la segunda baja no esperó a la primera"
        primera.commit()
        hilo.join(10)
    finally:
        primera.close()
    assert resultados == ["saldo insuficiente"]
    assert [(f["lote"], f["saldo"]) for f in _lotes(abrir_vto, pid)] == [("L1", 1)]


# ═══════════════════════════════════════════ Idempotencia: `clave_operacion`


def _sin_id(filas):
    return [tuple(f) for f in filas]


def test_un_reintento_de_asignar_con_la_misma_clave_no_duplica_y_devuelve_lo_de_la_primera_vez(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        _entrada(conn, pid, 10)
        clave = _k()
        primera = _asignar(conn, pid, deposito, "L1", "2026-10-05", 4, clave_operacion=clave, nota="Conteo")
        assert primera["repetida"] is False
        despues_de_la_primera = _sin_id(_todo_el_ledger(conn))
        _venta_anterior_a_a4(conn, 1, [{"producto_id": pid, "qty": 2}], fecha="2026-09-20")  # pasa el tiempo
        con_la_venta = _sin_id(_todo_el_ledger(conn))
        segunda = _asignar(conn, pid, deposito, "L1", "2026-10-05", 4, clave_operacion=clave, nota="otra nota")
        assert _sin_id(_todo_el_ledger(conn)) == con_la_venta, "el reintento escribió"
        assert len(con_la_venta) == len(despues_de_la_primera) + 1
    assert segunda == {**primera, "repetida": True}, "el resultado repetido no es el de la primera vez"
    assert segunda["saldo_sin_lote"] == 6            # el de entonces (10 − 4), no el de ahora (6 − 2)
    assert [(f["lote"], f["saldo"]) for f in _lotes(abrir_vto, pid)] == [("L1", 4), (None, 4)]


def test_la_clave_viaja_al_final_de_la_nota_de_los_dos_movimientos_en_la_misma_transaccion(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        _entrada(conn, pid, 10)
        clave = _k()
        _asignar(conn, pid, deposito, "L1", "2026-10-05", 4, clave_operacion=clave)
        _baja(conn, pid, deposito, "L1", "2026-10-05", 1, clave_operacion="baja-" + clave)
        notas = [f[0] for f in conn.execute("SELECT note FROM stock_movements ORDER BY id").fetchall()]
    assert [n.endswith(f"[op:{clave}]") for n in notas] == [False, True, True, False]
    assert notas[-1].endswith(f"[op:baja-{clave}]")
    # Una transacción que se deshace se lleva la marca con los movimientos: la clave no queda «gastada».
    with pytest.raises(RuntimeError):
        with abrir_vto() as conn:
            _asignar(conn, pid, deposito, "L2", "2026-10-06", 1, clave_operacion="se-deshace")
            raise RuntimeError("falló algo después")
    with abrir_vto() as conn:
        assert _asignar(conn, pid, deposito, "L2", "2026-10-06", 1, clave_operacion="se-deshace")["repetida"] is False


def test_la_misma_clave_con_otros_parametros_es_un_error_y_no_escribe(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        norte = catalogo.create_deposito(conn, "Norte")
        variante = catalogo.create_variante(conn, pid, "SKU-1", "Frutilla")["id"]
        _entrada(conn, pid, 20)
        clave = _k()
        _asignar(conn, pid, deposito, "L1", "2026-10-05", 4, clave_operacion=clave)
        antes = _sin_id(_todo_el_ledger(conn))
        distintos = [
            (pid, deposito, "L1", "2026-10-05", 5, {}), (pid, deposito, "L2", "2026-10-05", 4, {}),
            (pid, deposito, "L1", "2026-10-06", 4, {}), (pid, norte, "L1", "2026-10-05", 4, {}),
            (pid, deposito, "L1", "2026-10-05", 4, {"variante_id": variante}),
        ]
        for item, dep, lote, vence, cantidad, kw in distintos:
            with pytest.raises(vencimientos.ClaveDeOperacionReusada, match="otros parámetros"):
                _asignar(conn, item, dep, lote, vence, cantidad, clave_operacion=clave, **kw)
        # Ni una baja con la clave de una asignación, ni al revés.
        with pytest.raises(vencimientos.ClaveDeOperacionReusada):
            _baja(conn, pid, deposito, "L1", "2026-10-05", 4, clave_operacion=clave)
        clave_baja = _k()
        _baja(conn, pid, deposito, "L1", "2026-10-05", 1, clave_operacion=clave_baja)
        con_la_baja = _sin_id(_todo_el_ledger(conn))
        with pytest.raises(vencimientos.ClaveDeOperacionReusada):
            _asignar(conn, pid, deposito, "L9", "2026-10-05", 1, clave_operacion=clave_baja)
        with pytest.raises(vencimientos.ClaveDeOperacionReusada):
            _baja(conn, pid, deposito, "L1", "2026-10-05", 2, clave_operacion=clave_baja)
        assert _sin_id(_todo_el_ledger(conn)) == con_la_baja and con_la_baja[:len(antes)] == antes


def test_un_reintento_de_la_baja_no_descuenta_dos_veces(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        _entrada(conn, pid, 10, lote="L1", vence=_dias(-2))
        clave = _k()
        primera = _baja(conn, pid, deposito, "L1", _dias(-2), 10, clave_operacion=clave)   # vacía el lote
        assert primera["saldo_restante"] == 0 and primera["repetida"] is False
        n = len(_todo_el_ledger(conn))
        segunda = _baja(conn, pid, deposito, "L1", _dias(-2), 10, clave_operacion=clave)   # el lote ya no tiene saldo
        assert len(_todo_el_ledger(conn)) == n
        assert segunda == {**primera, "repetida": True}
        assert stock.get_stock_actual(conn, pid) == 0
        # La clave de una operación que FALLÓ no se gasta: con saldo, la misma clave sirve.
        clave2 = _k()
        with pytest.raises(vencimientos.SaldoInsuficiente):
            _baja(conn, pid, deposito, "L1", _dias(-2), 3, clave_operacion=clave2)
        _entrada(conn, pid, 3, lote="L1", vence=_dias(-2))
        assert _baja(conn, pid, deposito, "L1", _dias(-2), 3, clave_operacion=clave2)["repetida"] is False


def test_un_reintento_tras_desmarcar_el_producto_devuelve_igual_lo_anterior(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        _entrada(conn, pid, 5)
        clave = _k()
        primera = _asignar(conn, pid, deposito, "L1", "2026-10-05", 2, clave_operacion=clave)
        vencimientos.marcar_vence(conn, pid, False)
        assert _asignar(conn, pid, deposito, "L1", "2026-10-05", 2, clave_operacion=clave) == {**primera,
                                                                                            "repetida": True}


@pytest.mark.parametrize("mala", ["", "   ", "x" * 65, "a[b", "a]b", "a\nb", 5, None, b"k"])
def test_la_clave_de_operacion_se_valida_antes_de_escribir(abrir_vto, mala):
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        _entrada(conn, pid, 5, lote="L1", vence=_dias(3))
        _entrada(conn, pid, 5)
        antes = _sin_id(_todo_el_ledger(conn))
        with pytest.raises(ValueError, match="clave_operacion"):
            _asignar(conn, pid, deposito, "L2", "2026-10-05", 1, clave_operacion=mala)
        with pytest.raises(ValueError, match="clave_operacion"):
            _baja(conn, pid, deposito, "L1", _dias(3), 1, clave_operacion=mala)
        assert _sin_id(_todo_el_ledger(conn)) == antes


def test_la_clave_es_obligatoria_en_las_funciones(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        with pytest.raises(TypeError, match="clave_operacion"):
            vencimientos.asignar_vencimiento_a_saldo(conn, pid, deposito, "L1", "2026-10-05", 1)
        with pytest.raises(TypeError, match="clave_operacion"):
            vencimientos.dar_de_baja_lote(conn, pid, deposito, "L1", "2026-10-05", 1)


def test_la_clave_se_recorta_y_acepta_64_caracteres(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        _entrada(conn, pid, 5)
        clave = "k" * 64
        assert _asignar(conn, pid, deposito, "L1", "2026-10-05", 1, clave_operacion=f"  {clave} ")["repetida"] is False
        assert _asignar(conn, pid, deposito, "L1", "2026-10-05", 1, clave_operacion=clave)["repetida"] is True


def test_claves_parecidas_no_se_confunden(abrir_vto):
    """Los comodines de `LIKE` (`%`, `_`), las mayúsculas (SQLite no las distingue) y los prefijos no hacen que una
    clave nueva se lea como un reintento de otra."""
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        _entrada(conn, pid, 100)
        for clave in ("abc", "ABC", "ab_", "abX", "a%c", "abcd", "xabc", "k_1", "kX1", "op:abc"):
            r = _asignar(conn, pid, deposito, f"L-{clave}", "2026-10-05", 1, clave_operacion=clave)
            assert r["repetida"] is False, clave
    assert len(_lotes(abrir_vto, pid)) == 11


def test_un_texto_libre_no_puede_imitar_la_marca_de_una_operacion(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        _entrada(conn, pid, 10, lote="L1", vence=_dias(3))
        _entrada(conn, pid, 10)
        _asignar(conn, pid, deposito, "L2", "2026-10-05", 1, nota="[op:ajena]", clave_operacion=_k())
        _baja(conn, pid, deposito, "L1", _dias(3), 1, nota="[op:ajena]", motivo="[op:ajena]", clave_operacion=_k())
        assert _asignar(conn, pid, deposito, "L3", "2026-10-05", 1, clave_operacion="ajena")["repetida"] is False


def _dos_reintentos_a_la_vez(abrir_vto, operacion):
    """Corre `operacion(conn, clave)` en dos conexiones a la vez, con la misma clave. Devuelve los dos resultados."""
    resultados: list = []
    errores: list = []
    barrera = threading.Barrier(2)

    def intento():
        c = core.get_connection()
        try:
            barrera.wait(10)
            resultados.append(operacion(c))
            c.commit()
        except Exception as e:  # noqa: BLE001 - se informa en el assert
            c.rollback()
            errores.append(e)
        finally:
            c.close()

    hilos = [threading.Thread(target=intento) for _ in range(2)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(30)
    assert not any(h.is_alive() for h in hilos), "un reintento quedó colgado"
    assert not errores, errores
    return resultados


def test_en_postgres_dos_reintentos_simultaneos_de_asignar_escriben_una_sola_vez(abrir_vto):
    if not core.is_postgres():
        pytest.skip("sólo PostgreSQL toma el bloqueo por fila")
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        _entrada(conn, pid, 10)
        antes = len(_todo_el_ledger(conn))
    clave = _k()
    resultados = _dos_reintentos_a_la_vez(
        abrir_vto, lambda c: _asignar(c, pid, deposito, "L1", "2026-10-05", 4, clave_operacion=clave))
    assert sorted(r["repetida"] for r in resultados) == [False, True]
    assert {r["referencia"] for r in resultados} == {resultados[0]["referencia"]}
    with abrir_vto() as conn:
        assert len(_todo_el_ledger(conn)) == antes + 2
    assert [(f["lote"], f["saldo"]) for f in _lotes(abrir_vto, pid)] == [("L1", 4), (None, 6)]


def test_en_postgres_dos_reintentos_simultaneos_de_la_baja_descuentan_una_sola_vez(abrir_vto):
    if not core.is_postgres():
        pytest.skip("sólo PostgreSQL toma el bloqueo por fila")
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        _entrada(conn, pid, 10, lote="L1", vence=_dias(3))
        antes = len(_todo_el_ledger(conn))
    clave = _k()
    resultados = _dos_reintentos_a_la_vez(
        abrir_vto, lambda c: _baja(c, pid, deposito, "L1", _dias(3), 4, clave_operacion=clave))
    assert sorted(r["repetida"] for r in resultados) == [False, True]
    assert {r["saldo_restante"] for r in resultados} == {6}
    with abrir_vto() as conn:
        assert len(_todo_el_ledger(conn)) == antes + 1
    assert [(f["lote"], f["saldo"]) for f in _lotes(abrir_vto, pid)] == [("L1", 6)]


def test_en_postgres_el_reintento_espera_a_la_primera_que_no_termino_de_confirmar(abrir_vto):
    """El caso deterministico de la carrera: la primera transaccion escribio y todavia no hizo commit; el reintento
    tiene que esperar (no ve la marca hasta el commit) y, cuando llega, devolver lo escrito sin repetirlo."""
    if not core.is_postgres():
        pytest.skip("sólo PostgreSQL toma el bloqueo por fila")
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        _entrada(conn, pid, 10)
    clave = _k()
    resultado: list = []
    primera = core.get_connection()
    try:
        _asignar(primera, pid, deposito, "L1", "2026-10-05", 4, clave_operacion=clave)  # sin commit

        def reintento():
            c = core.get_connection()
            try:
                resultado.append(_asignar(c, pid, deposito, "L1", "2026-10-05", 4, clave_operacion=clave))
                c.commit()
            finally:
                c.close()

        hilo = threading.Thread(target=reintento)
        hilo.start()
        time.sleep(1.0)
        assert hilo.is_alive() and not resultado, "el reintento no esperó a la primera"
        primera.commit()
        hilo.join(10)
    finally:
        primera.close()
    assert [r["repetida"] for r in resultado] == [True]
    assert [(f["lote"], f["saldo"]) for f in _lotes(abrir_vto, pid)] == [("L1", 4), (None, 6)]


def test_la_clave_de_operacion_es_por_producto_la_misma_clave_en_otro_producto_es_otra_operacion(abrir_vto):
    with abrir_vto() as conn:
        a = _producto(conn, "Yogur")
        b = _producto(conn, "Leche")
        deposito = catalogo.get_default_deposito_id(conn)
        _entrada(conn, a, 10)
        _entrada(conn, b, 10)
        _entrada(conn, b, 10, lote="LB", vence=_dias(3))
        clave = _k()
        ra = _asignar(conn, a, deposito, "L1", "2026-10-05", 4, clave_operacion=clave)
        rb = _asignar(conn, b, deposito, "L1", "2026-10-05", 4, clave_operacion=clave)   # otra operación: no 409
        assert (ra["repetida"], rb["repetida"]) == (False, False)
        assert len(_todo_el_ledger(conn)) == 2 + 1 + 4
        # El reintento de cada uno encuentra lo suyo y no lo del otro, sin escribir.
        n = len(_todo_el_ledger(conn))
        assert _asignar(conn, a, deposito, "L1", "2026-10-05", 4, clave_operacion=clave) == {**ra, "repetida": True}
        assert _asignar(conn, b, deposito, "L1", "2026-10-05", 4, clave_operacion=clave) == {**rb, "repetida": True}
        # Con otros datos sobre el mismo producto sigue siendo 409; sobre el otro producto, ídem con lo suyo.
        with pytest.raises(vencimientos.ClaveDeOperacionReusada):
            _asignar(conn, a, deposito, "L1", "2026-10-05", 5, clave_operacion=clave)
        # Y la baja con la misma clave en un tercer producto también es independiente.
        mb = _baja(conn, b, deposito, "LB", _dias(3), 1, clave_operacion=clave + "-x")
        assert mb["repetida"] is False
        assert len(_todo_el_ledger(conn)) == n + 1


def _dos_productos_a_la_vez(operaciones):
    """Corre cada `operacion(conn)` en su propia conexión y a la vez (barrera). Devuelve los resultados en orden."""
    resultados: dict = {}
    errores: list = []
    barrera = threading.Barrier(len(operaciones))

    def intento(i, operacion):
        c = core.get_connection()
        try:
            barrera.wait(10)
            resultados[i] = operacion(c)
            c.commit()
        except Exception as e:  # noqa: BLE001 - se informa en el assert
            c.rollback()
            errores.append(e)
        finally:
            c.close()

    hilos = [threading.Thread(target=intento, args=(i, op)) for i, op in enumerate(operaciones)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(30)
    assert not any(h.is_alive() for h in hilos), "una operación quedó colgada"
    assert not errores, errores
    return [resultados[i] for i in range(len(operaciones))]


def test_en_postgres_la_misma_clave_en_dos_productos_a_la_vez_escribe_una_vez_en_cada_uno(abrir_vto):
    """Los dos productos toman bloqueos distintos y no se ven: no tienen por qué, la clave es de cada producto. Los dos
    escriben, cada uno una vez, y el reintento de cada uno devuelve lo suyo con `repetida: true`."""
    if not core.is_postgres():
        pytest.skip("sólo PostgreSQL toma el bloqueo por fila")
    with abrir_vto() as conn:
        a, b = _producto(conn, "Yogur"), _producto(conn, "Leche")
        deposito = catalogo.get_default_deposito_id(conn)
        for pid in (a, b):
            _entrada(conn, pid, 10)
            _entrada(conn, pid, 10, lote="LB", vence=_dias(3))
        antes = len(_todo_el_ledger(conn))
    clave = _k()
    ra, rb = _dos_productos_a_la_vez([
        lambda c: _asignar(c, a, deposito, "L1", "2026-10-05", 4, clave_operacion=clave),
        lambda c: _asignar(c, b, deposito, "L1", "2026-10-05", 4, clave_operacion=clave),
    ])
    assert (ra["repetida"], rb["repetida"]) == (False, False) and ra["producto_id"] != rb["producto_id"]
    clave_baja = _k()   # otra operación (una baja) sobre los mismos productos: clave propia; la misma en los dos
    ba, bb = _dos_productos_a_la_vez([
        lambda c: _baja(c, a, deposito, "LB", _dias(3), 2, clave_operacion=clave_baja),
        lambda c: _baja(c, b, deposito, "LB", _dias(3), 2, clave_operacion=clave_baja),
    ])
    assert (ba["repetida"], bb["repetida"]) == (False, False)
    with abrir_vto() as conn:
        assert len(_todo_el_ledger(conn)) == antes + 2 * 2 + 2 * 1
    # El reintento de cada uno (a la vez también) devuelve lo suyo, sin escribir.
    r2a, r2b = _dos_productos_a_la_vez([
        lambda c: _asignar(c, a, deposito, "L1", "2026-10-05", 4, clave_operacion=clave),
        lambda c: _asignar(c, b, deposito, "L1", "2026-10-05", 4, clave_operacion=clave),
    ])
    assert r2a == {**ra, "repetida": True} and r2b == {**rb, "repetida": True}
    b2a, b2b = _dos_productos_a_la_vez([
        lambda c: _baja(c, a, deposito, "LB", _dias(3), 2, clave_operacion=clave_baja),
        lambda c: _baja(c, b, deposito, "LB", _dias(3), 2, clave_operacion=clave_baja),
    ])
    assert b2a == {**ba, "repetida": True} and b2b == {**bb, "repetida": True}
    with abrir_vto() as conn:
        assert len(_todo_el_ledger(conn)) == antes + 2 * 2 + 2 * 1
    for pid in (a, b):
        assert sorted((f["lote"] or "", f["saldo"]) for f in _lotes(abrir_vto, pid)) == [("", 6), ("L1", 4), ("LB", 8)]


# ═════════════════════════════════ La variante tiene que ser del producto


def _dos_productos_con_variantes(conn):
    a, b = _producto(conn, "Yogur"), _producto(conn, "Zapatilla")
    va = catalogo.create_variante(conn, a, "Y-1", "Frutilla")["id"]
    vb = catalogo.create_variante(conn, b, "Z-1", "Talle 40")["id"]
    deposito = catalogo.get_default_deposito_id(conn)
    _entrada(conn, a, 10, variante=va)
    _entrada(conn, b, 10, variante=vb)                        # el saldo de la variante ajena existe: no se puede tocar
    _entrada(conn, b, 5, lote="LB", vence=_dias(3), variante=vb)
    return a, b, va, vb, deposito


def test_una_variante_de_otro_producto_se_rechaza_antes_de_calcular_saldos_o_escribir(abrir_vto):
    with abrir_vto() as conn:
        a, b, va, vb, deposito = _dos_productos_con_variantes(conn)
        antes = _sin_id(_todo_el_ledger(conn))
        with pytest.raises(ValueError, match=f"variante {vb} no existe o no es del producto {a}"):
            _asignar(conn, a, deposito, "L1", "2026-10-05", 4, variante_id=vb)
        with pytest.raises(ValueError, match="no es del producto"):
            _baja(conn, a, deposito, "LB", _dias(3), 1, variante_id=vb)
        with pytest.raises(ValueError, match="no es del producto"):
            vencimientos.lotes_de(conn, a, variante_id=vb)
        for inexistente in (99999, True):
            with pytest.raises(ValueError, match="variante"):
                _asignar(conn, a, deposito, "L1", "2026-10-05", 1, variante_id=inexistente)
        with pytest.raises(vencimientos.ProductoNoEncontrado):
            _asignar(conn, 9999, deposito, "L1", "2026-10-05", 1, variante_id=va)
        assert _sin_id(_todo_el_ledger(conn)) == antes
        # La propia sí; y la clave de una operación rechazada no se gasta.
        clave = _k()
        with pytest.raises(ValueError):
            _asignar(conn, a, deposito, "L1", "2026-10-05", 4, variante_id=vb, clave_operacion=clave)
        assert _asignar(conn, a, deposito, "L1", "2026-10-05", 4, variante_id=va,
                        clave_operacion=clave)["repetida"] is False
    assert [(f["variante_id"], f["lote"], f["saldo"]) for f in _lotes(abrir_vto, b)] == [
        (vb, "LB", 5), (vb, None, 10)]   # el otro producto no se movió


def test_lotes_de_por_variante_filtra_y_una_variante_inactiva_se_puede_consultar_y_mermar(abrir_vto):
    with abrir_vto() as conn:
        a, b, va, vb, deposito = _dos_productos_con_variantes(conn)
        va2 = catalogo.create_variante(conn, a, "Y-2", "Durazno")["id"]
        _entrada(conn, a, 3, lote="LX", vence=_dias(2), variante=va2)
        conn.execute("UPDATE item_variants SET active = 0 WHERE id = ?", (va2,))
    assert [(f["variante_id"], f["saldo"]) for f in _lotes(abrir_vto, a, variante_id=va)] == [(va, 10)]
    assert [(f["variante_id"], f["lote"], f["saldo"]) for f in _lotes(abrir_vto, a, variante_id=va2)] == [
        (va2, "LX", 3)]
    assert len(_lotes(abrir_vto, a)) == 2
    with abrir_vto() as conn:
        assert _baja(conn, a, deposito, "LX", _dias(2), 3, variante_id=va2)["saldo_restante"] == 0


def test_la_variante_ajena_es_422_por_http_en_asignar_merma_y_lotes(abrir_vto):
    with abrir_vto() as conn:
        a, b, va, vb, deposito = _dos_productos_con_variantes(conn)
        antes = _sin_id(_todo_el_ledger(conn))
    c = _app(abrir_vto)
    r = c.post("/api/vencimientos/asignar", json={"producto_id": a, "deposito_id": deposito, "lote": "L1",
                                                  "vence": "2026-10-05", "cantidad": 1, "variante_id": vb,
                                                  "clave_operacion": _k()})
    assert r.status_code == 422 and "no es del producto" in r.json()["detail"]
    r = c.post("/api/vencimientos/merma", json={"producto_id": a, "deposito_id": deposito, "lote": "LB",
                                                "vence": _dias(3), "cantidad": 1, "variante_id": vb,
                                                "clave_operacion": _k()})
    assert r.status_code == 422
    assert c.get(f"/api/vencimientos/productos/{a}/lotes", params={"variante_id": vb}).status_code == 422
    assert c.get(f"/api/vencimientos/productos/{a}/lotes", params={"variante_id": va}).status_code == 200
    with abrir_vto() as conn:
        assert _sin_id(_todo_el_ledger(conn)) == antes


# ═══════ Parche hasta A-4: la merma mira también el stock total y las salidas sin conciliar (ADR-018, 2026-10-01)


def _vender(abrir, pid, cantidad, *, deposito=None, fecha="2026-09-10"):
    """Una venta de mostrador por el mismo camino que `POST /api/ventas` (`erp.ventas.crear_venta_directa`) pero con el
    stock armado a mano como lo escribía el motor antes de A-4 PR-2 (`_venta_anterior_a_a4`: sin lote, aunque el producto
    esté marcado): desde el PR-2 la venta real de un marcado elige lote."""
    linea = {"nombre": "Yogur", "qty": cantidad, "precio": 100.0, "subtotal": round(cantidad * 100.0, 2),
             "producto_id": pid}
    total = linea["subtotal"]
    pagos = [{"medio": "efectivo", "monto": total, "estado": "aprobado"}]
    vid = ventas.crear_venta_directa(
        abrir, fecha=fecha, items=[linea], subtotal=total, descuento=0.0, total=total, cliente_id=None,
        cliente_nombre="", usuario_id=USUARIO["id"], observaciones="",
        estado=ventas.estado_segun_pagos(total, pagos), pagos=pagos, stock_habilitado=False, deposito_id=deposito,
    )
    with abrir() as conn:
        _venta_anterior_a_a4(conn, vid, [linea], fecha=fecha, deposito_id=deposito)
        conn.commit()
    return vid


def test_asignar_vender_todo_y_dar_de_baja_el_lote_es_409_y_no_escribe(abrir_vto_ventas):
    """El caso de Codex: se asignan 10 a un lote, se venden 10 (quedan −10 sin lote y el lote conserva 10) y mermar el
    lote descontaría dos veces lo vendido (stock total −10)."""
    abrir = abrir_vto_ventas
    with abrir() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        _entrada(conn, pid, 10)
        _asignar(conn, pid, deposito, "L1", _dias(3), 10)
    _vender(abrir, pid, 10, deposito=deposito)
    with abrir() as conn:
        assert stock.get_stock_actual(conn, pid) == 0
        assert [(f["lote"], f["saldo"]) for f in vencimientos.lotes_de(conn, pid, hoy=HOY)] == [("L1", 10),
                                                                                                 (None, -10)]
        antes = _sin_id(_todo_el_ledger(conn))
        with pytest.raises(vencimientos.ReglaDeNegocio, match="salidas sin lote sin conciliar en este depósito") as e:
            _baja(conn, pid, deposito, "L1", _dias(3), 10)
        assert "conciliá con el conteo físico antes de dar de baja" in str(e.value)
        assert not isinstance(e.value, vencimientos.SaldoInsuficiente)
        with pytest.raises(vencimientos.ReglaDeNegocio):
            _baja(conn, pid, deposito, "L1", _dias(3), 1)             # ni una unidad
        assert _sin_id(_todo_el_ledger(conn)) == antes
        assert stock.get_stock_actual(conn, pid) == 0


def test_la_salida_sin_lote_de_otro_deposito_o_variante_no_bloquea_la_merma(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    with abrir() as conn:
        pid = _producto(conn)
        principal = catalogo.get_default_deposito_id(conn)
        norte = catalogo.create_deposito(conn, "Norte")
        variante = catalogo.create_variante(conn, pid, "Y-1", "Frutilla")["id"]
        _entrada(conn, pid, 10, lote="L1", vence=_dias(3), deposito=principal)
        _entrada(conn, pid, 5, deposito=norte)
        _entrada(conn, pid, 5, variante=variante)
        stock.add_movimiento_stock(conn, pid, "venta", -8, "otra variante", fecha="2026-09-10",
                                   deposito_id=principal, variant_id=variante)   # −3 sin lote de OTRA variante
        stock.add_movimiento_stock(conn, pid, "venta", -7, "otro depósito", fecha="2026-09-10", deposito_id=norte)
        assert _baja(conn, pid, principal, "L1", _dias(3), 4)["saldo_restante"] == 6
        with pytest.raises(vencimientos.ReglaDeNegocio):                 # la variante con −3 sin lote sí se frena
            _entrada(conn, pid, 6, lote="LV", vence=_dias(3), variante=variante)
            _baja(conn, pid, principal, "LV", _dias(3), 1, variante_id=variante)


def test_sin_salidas_sin_lote_y_con_stock_suficiente_la_merma_sigue_funcionando(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    with abrir() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        _entrada(conn, pid, 10)
        _asignar(conn, pid, deposito, "L1", _dias(3), 6)              # L1: 6 y sin lote: 4
    _vender(abrir, pid, 3, deposito=deposito)                          # sin lote: 4 − 3 = 1 (no negativo)
    with abrir() as conn:
        r = _baja(conn, pid, deposito, "L1", _dias(3), 6)              # vence el lote entero
        assert (r["saldo_restante"], r["repetida"]) == (0, False)
        assert stock.get_stock_actual(conn, pid, deposito) == 1


def test_el_stock_total_insuficiente_es_409_aunque_el_lote_alcance(abrir_vto):
    """Otro lote en negativo (lo que dejaría una salida que sí eligió lote) deja el total corto: 10 en L1, −5 en L2."""
    with abrir_vto() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        _entrada(conn, pid, 10, lote="L1", vence=_dias(3))
        stock.add_movimiento_stock(conn, pid, "venta", -5, "salió de L2", fecha=PREVIA, lot_code="L2",
                                   expires_at=_dias(9))
        antes = _sin_id(_todo_el_ledger(conn))
        with pytest.raises(vencimientos.SaldoInsuficiente, match="stock total del producto en el depósito .* es 5"):
            _baja(conn, pid, deposito, "L1", _dias(3), 8)
        assert _sin_id(_todo_el_ledger(conn)) == antes
        assert _baja(conn, pid, deposito, "L1", _dias(3), 5)["saldo_restante"] == 5      # justo lo que hay en total
        with pytest.raises(vencimientos.SaldoInsuficiente):
            _baja(conn, pid, deposito, "L1", _dias(3), 1)


def test_la_idempotencia_va_primero_un_reintento_de_una_merma_hecha_devuelve_lo_anterior_aunque_ahora_no_pase(
        abrir_vto_ventas):
    abrir = abrir_vto_ventas
    with abrir() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        _entrada(conn, pid, 10)
        _asignar(conn, pid, deposito, "L1", _dias(3), 10)
        clave = _k()
        primera = _baja(conn, pid, deposito, "L1", _dias(3), 4, clave_operacion=clave)
        assert primera["repetida"] is False
    _vender(abrir, pid, 1, deposito=deposito)    # después: sin lote −1 (salidas sin conciliar) y el lote con 6
    with abrir() as conn:
        with pytest.raises(vencimientos.ReglaDeNegocio):   # una baja NUEVA ya no pasa...
            _baja(conn, pid, deposito, "L1", _dias(3), 1)
        n = len(_todo_el_ledger(conn))
        # ...pero el reintento de la ya hecha sí devuelve lo de la primera vez, sin escribir.
        assert _baja(conn, pid, deposito, "L1", _dias(3), 4, clave_operacion=clave) == {**primera, "repetida": True}
        assert len(_todo_el_ledger(conn)) == n


def test_la_merma_con_salidas_sin_conciliar_es_409_por_http(abrir_vto_ventas, monkeypatch):
    _hoy_fijo(monkeypatch)
    abrir = abrir_vto_ventas
    with abrir() as conn:
        pid = _producto(conn)
        deposito = catalogo.get_default_deposito_id(conn)
        _entrada(conn, pid, 10)
        _asignar(conn, pid, deposito, "L1", _dias(3), 10)
    _vender(abrir, pid, 10, deposito=deposito)
    c = _app(abrir)
    r = c.post("/api/vencimientos/merma", json={"producto_id": pid, "deposito_id": deposito, "lote": "L1",
                                                "vence": _dias(3), "cantidad": 1, "clave_operacion": _k()})
    assert r.status_code == 409 and "conciliá con el conteo físico" in r.json()["detail"]


# ════════════════════════════════════════════ La recepción de compra llega al ledger


def _recibir_con_lote(abrir, pid, cantidad, lote, vence, *, deposito=None):
    with abrir() as conn:
        proveedor = repositorio_de(conn).save_party(Party(None, PartyType.ORGANIZATION, "Distribuidora SA")).id
        rid = compras.crear_recepcion(conn, supplier_party_id=proveedor)["id"]
        compras.agregar_linea_recepcion(conn, rid, item_id=pid, quantity=Decimal(str(cantidad)),
                                        unit_cost=Decimal("65"), lot_code=lote, expires_at=vence)
        compras.confirmar_recepcion(
            conn, rid, location_id=deposito or catalogo.get_default_deposito_id(conn),
            occurred_at=datetime.datetime(2026, 9, 20, 9),
        )
        conn.commit()
    return rid


def test_la_recepcion_de_compra_con_lote_y_vencimiento_llega_al_ledger_y_al_reporte(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn, "Yogur")
        deposito = catalogo.get_default_deposito_id(conn)
    rid = _recibir_con_lote(abrir_vto, pid, 12, "R-77", datetime.datetime(2026, 10, 8), deposito=deposito)
    _recibir_con_lote(abrir_vto, pid, 3, None, None)
    with abrir_vto() as conn:
        fila = conn.execute(
            "SELECT movement_type, quantity_delta, lot_code, expires_at, source_type, source_id FROM stock_movements "
            "WHERE lot_code IS NOT NULL"
        ).fetchall()
    assert [(f[0], float(f[1]), f[2], f[4], f[5]) for f in fila] == [("purchase", 12.0, "R-77", "purchase_receipt", rid)]
    assert fila[0][3].startswith("2026-10-08")   # `datetime.isoformat()`: la fecha, con la hora si el dominio la trae
    assert [(f["lote"], f["vence"], f["saldo"]) for f in _lotes(abrir_vto, pid)] == [
        ("R-77", "2026-10-08", 12), (None, None, 3)]
    r = _proximos(abrir_vto)
    assert [(x["lote"], x["dias_para_vencer"], x["saldo"]) for x in r["lotes"]] == [("R-77", 8, 12)]
    assert [(x["nombre"], x["saldo"]) for x in r["sin_lote"]] == [("Yogur", 3)]
    with abrir_vto() as conn:
        assert stock.get_stock_actual(conn, pid) == 15


# ═══════════════════════════════════════════════════════════════ El router


def _libre():
    """Un gate que deja pasar: las pruebas que no miran la autorización igual tienen que declarar una."""


def _app(abrir, **kw_escritura):
    kw_escritura.setdefault("dependencias_marcar", [Depends(_libre)])
    kw_escritura.setdefault("dependencias_movimientos", [Depends(_libre)])
    app = FastAPI()
    app.include_router(build_vencimientos_router(conexion=abrir))
    app.include_router(build_vencimientos_escritura_router(
        conexion=abrir, usuario_actual=lambda: USUARIO, **kw_escritura))
    return TestClient(app)


def _sembrar_para_el_router(abrir):
    with abrir() as conn:
        yogur = _producto(conn, "Yogur", categoria="Lácteos")
        _entrada(conn, yogur, 10, lote="L1", vence=_dias(5))
        _entrada(conn, yogur, 4, lote="L0", vence=_dias(-2))
        _entrada(conn, yogur, 6)
        return yogur, catalogo.get_default_deposito_id(conn)


def _hoy_fijo(monkeypatch):
    monkeypatch.setattr(vencimientos, "hoy_argentina", lambda ahora=None: HOY)


def test_get_devuelve_parametros_resumen_lotes_y_sin_lote(abrir_vto, monkeypatch):
    _hoy_fijo(monkeypatch)
    _sembrar_para_el_router(abrir_vto)
    body = _app(abrir_vto).get("/api/vencimientos").json()
    assert (body["dias"], body["hoy"], body["hasta"], body["incluir_vencidos"]) == (15, "2026-09-30", "2026-10-15", True)
    assert [(x["lote"], x["estado"]) for x in body["lotes"]] == [("L0", "vencido"), ("L1", "por_vencer")]
    assert body["resumen"]["lotes_vencidos"] == 1 and body["resumen"]["unidades_por_vencer"] == 10
    assert [x["nombre"] for x in body["sin_lote"]] == ["Yogur"]
    chico = _app(abrir_vto).get("/api/vencimientos", params={"dias": 2, "incluir_vencidos": "false"}).json()
    assert chico["lotes"] == []


def test_parametros_invalidos_son_422(abrir_vto):
    c = _app(abrir_vto)
    invalidos = [{"dias": 0}, {"dias": -3}, {"dias": vencimientos.MAX_DIAS_AVISO + 1}, {"dias": "muchos"},
                 {"dias": 2.5}, {"incluir_vencidos": "quizás"}, {"sucursal_id": 999}, {"deposito_id": 999},
                 {"sucursal_id": "x"}]
    for params in invalidos:
        for ruta in ("/api/vencimientos", "/api/vencimientos/export"):
            assert c.get(ruta, params=params).status_code == 422, (ruta, params)
    assert c.get("/api/vencimientos", params={"dias": vencimientos.MAX_DIAS_AVISO}).status_code == 200


def test_export_csv(abrir_vto, monkeypatch):
    _hoy_fijo(monkeypatch)
    _sembrar_para_el_router(abrir_vto)
    r = _app(abrir_vto).get("/api/vencimientos/export")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'filename="vencimientos_2026-09-30.csv"' in r.headers["content-disposition"]
    filas = list(csv.DictReader(io.StringIO(r.text)))
    assert [(f["nombre"], f["lote"], f["vence"], f["dias_para_vencer"], f["saldo"], f["estado"]) for f in filas] == [
        ("Yogur", "L0", _dias(-2), "-2", "4", "vencido"), ("Yogur", "L1", _dias(5), "5", "10", "por_vencer")]
    assert list(filas[0]) == ["producto_id", "codigo", "nombre", "categoria", "unidad", "sucursal", "deposito",
                              "variante", "lote", "vence", "dias_para_vencer", "saldo", "estado"]


def test_el_export_de_vencimientos_neutraliza_formulas_y_no_toca_los_numeros_negativos(abrir_vto, monkeypatch):
    _hoy_fijo(monkeypatch)
    with abrir_vto() as conn:
        malo = _producto(conn, "=HYPERLINK(\"http://x\",\"a\")", categoria="-Lácteos")
        normal = _producto(conn, "Yogur - 1 kg")
        dep = catalogo.create_deposito(conn, "@Norte")
        _entrada(conn, malo, 4, lote="+LOTE-1", vence=_dias(-3), deposito=dep)     # vencido: días_para_vencer = −3
        _entrada(conn, malo, 2, lote="@L2", vence=_dias(5))
        _entrada(conn, malo, 1, lote="-L3", vence=_dias(6))
        _entrada(conn, normal, 7, lote="L-9", vence=_dias(2))
    r = _app(abrir_vto).get("/api/vencimientos/export")
    assert r.status_code == 200
    filas = list(csv.DictReader(io.StringIO(r.text)))
    del_malo = [f for f in filas if f["producto_id"] == str(malo)]
    assert {f["nombre"] for f in del_malo} == {"'=HYPERLINK(\"http://x\",\"a\")"}
    assert {f["categoria"] for f in del_malo} == {"'-Lácteos"}
    assert {f["lote"] for f in del_malo} == {"'+LOTE-1", "'@L2", "'-L3"}
    assert {f["deposito"] for f in del_malo} >= {"'@Norte"}
    vencido = next(f for f in del_malo if f["lote"] == "'+LOTE-1")
    assert vencido["dias_para_vencer"] == "-3" and vencido["saldo"] == "4", "los números (negativos también) no se tocan"
    bueno = next(f for f in filas if f["producto_id"] == str(normal))
    assert (bueno["nombre"], bueno["lote"], bueno["dias_para_vencer"]) == ("Yogur - 1 kg", "L-9", "2")


def test_get_lotes_de_un_producto(abrir_vto, monkeypatch):
    _hoy_fijo(monkeypatch)
    yogur, _ = _sembrar_para_el_router(abrir_vto)
    c = _app(abrir_vto)
    body = c.get(f"/api/vencimientos/productos/{yogur}/lotes").json()
    assert body["producto"]["producto_id"] == yogur and body["producto"]["vence"] is True
    assert body["hoy"] == "2026-09-30"
    assert [(x["lote"], x["saldo"], x["estado"]) for x in body["lotes"]] == [
        ("L0", 4, "vencido"), ("L1", 10, "vigente"), (None, 6, "sin_fecha")]
    assert c.get("/api/vencimientos/productos/9999/lotes").status_code == 404
    assert c.get(f"/api/vencimientos/productos/{yogur}/lotes", params={"sucursal_id": 999}).status_code == 422


def test_marcar_por_http(abrir_vto):
    with abrir_vto() as conn:
        pid = _producto(conn, vence=False)
        servicio = catalogo.create_producto(conn, "Flete", precio_venta=1.0, tipo="servicio")
    c = _app(abrir_vto)
    assert c.put(f"/api/vencimientos/productos/{pid}", json={"vence": True}).json() == {"producto_id": pid, "vence": True}
    with abrir_vto() as conn:
        assert vencimientos.ficha(conn, pid)["vence"] is True
    assert c.put(f"/api/vencimientos/productos/{pid}", json={"vence": False}).json()["vence"] is False
    assert c.put("/api/vencimientos/productos/9999", json={"vence": True}).status_code == 404
    assert c.put(f"/api/vencimientos/productos/{servicio}", json={"vence": True}).status_code == 409
    for cuerpo in ({}, {"vence": "si"}, {"vence": 1}, {"vence": None}, {"otro": True}):
        assert c.put(f"/api/vencimientos/productos/{pid}", json=cuerpo).status_code == 422, cuerpo


def test_asignar_por_http(abrir_vto, monkeypatch):
    _hoy_fijo(monkeypatch)
    yogur, deposito = _sembrar_para_el_router(abrir_vto)
    c = _app(abrir_vto)
    cuerpo = {"producto_id": yogur, "deposito_id": deposito, "lote": "L9", "vence": "2026-11-03", "cantidad": 2.5,
              "nota": "Conteo", "clave_operacion": _k()}
    r = c.post("/api/vencimientos/asignar", json=cuerpo)
    assert r.status_code == 200, r.text
    assert r.json()["saldo_sin_lote"] == 3.5 and r.json()["lote"] == "L9" and r.json()["cantidad"] == 2.5
    with abrir_vto() as conn:
        creadores = {f[0] for f in conn.execute("SELECT created_by FROM stock_movements WHERE note LIKE 'Conteo%'")}
    assert creadores == {USUARIO["id"]}
    # Sin saldo sin lote suficiente: 409 y el mensaje dice cuánto hay.
    r = c.post("/api/vencimientos/asignar", json={**cuerpo, "clave_operacion": _k(), "cantidad": 99})
    assert r.status_code == 409 and "saldo sin lote es 3.5" in r.json()["detail"]
    invalidos = [{"lote": "  "}, {"vence": "mañana"}, {"cantidad": 0}, {"cantidad": -1}, {"cantidad": "x"},
                 {"deposito_id": 999}, {"lote": None}, {"vence": None}]
    for cambio in invalidos:
        assert c.post("/api/vencimientos/asignar", json={**cuerpo, "clave_operacion": _k(), **cambio}).status_code == 422, cambio
    assert c.post("/api/vencimientos/asignar", json={**cuerpo, "clave_operacion": _k(), "producto_id": 9999}).status_code == 404
    with abrir_vto() as conn:
        sin_marca = _producto(conn, "Sin marca", vence=False)
        _entrada(conn, sin_marca, 5)
    assert c.post("/api/vencimientos/asignar", json={**cuerpo, "clave_operacion": _k(), "producto_id": sin_marca}).status_code == 409


def test_merma_por_http(abrir_vto, monkeypatch):
    _hoy_fijo(monkeypatch)
    yogur, deposito = _sembrar_para_el_router(abrir_vto)
    c = _app(abrir_vto)
    cuerpo = {"producto_id": yogur, "deposito_id": deposito, "lote": "L0", "vence": _dias(-2), "cantidad": 3,
              "clave_operacion": _k()}
    r = c.post("/api/vencimientos/merma", json=cuerpo)
    assert r.status_code == 200, r.text
    assert r.json()["saldo_restante"] == 1
    with abrir_vto() as conn:
        fila = conn.execute("SELECT movement_type, created_by, note FROM stock_movements ORDER BY id DESC LIMIT 1"
                            ).fetchone()
    assert (fila[0], fila[1]) == ("waste", USUARIO["id"]) and fila[2].startswith("Merma: Vencimiento")
    r = c.post("/api/vencimientos/merma", json={**cuerpo, "clave_operacion": _k(), "cantidad": 2})
    assert r.status_code == 409 and "tiene 1" in r.json()["detail"]
    r = c.post("/api/vencimientos/merma", json={**cuerpo, "clave_operacion": _k(), "motivo": "Rotura", "cantidad": 1})
    assert r.status_code == 200
    for cambio in ({"cantidad": 0}, {"lote": None, "vence": None}, {"vence": "ayer"}, {"deposito_id": 999}):
        assert c.post("/api/vencimientos/merma", json={**cuerpo, "clave_operacion": _k(), **cambio}).status_code == 422, cambio
    assert c.post("/api/vencimientos/merma", json={**cuerpo, "clave_operacion": _k(), "producto_id": 9999}).status_code == 404


def test_un_error_de_negocio_no_deja_nada_escrito_por_http(abrir_vto):
    yogur, deposito = _sembrar_para_el_router(abrir_vto)
    with abrir_vto() as conn:
        antes = [tuple(f) for f in _todo_el_ledger(conn)]
    c = _app(abrir_vto)
    c.post("/api/vencimientos/asignar", json={"producto_id": yogur, "deposito_id": deposito, "lote": "X",
                                              "vence": "2026-11-03", "cantidad": 999, "clave_operacion": _k()})
    c.post("/api/vencimientos/merma", json={"producto_id": yogur, "deposito_id": deposito, "lote": "L1",
                                            "vence": _dias(5), "cantidad": 999, "clave_operacion": _k()})
    with abrir_vto() as conn:
        assert [tuple(f) for f in _todo_el_ledger(conn)] == antes


def test_un_reintento_por_http_devuelve_lo_anterior_con_repetida_y_no_escribe(abrir_vto, monkeypatch):
    _hoy_fijo(monkeypatch)
    yogur, deposito = _sembrar_para_el_router(abrir_vto)
    c = _app(abrir_vto)
    clave = _k()
    asignar = {"producto_id": yogur, "deposito_id": deposito, "lote": "L9", "vence": "2026-11-03", "cantidad": 2,
               "clave_operacion": clave}
    primera = c.post("/api/vencimientos/asignar", json=asignar)
    with abrir_vto() as conn:
        n = len(_todo_el_ledger(conn))
    segunda = c.post("/api/vencimientos/asignar", json=asignar)
    assert primera.status_code == segunda.status_code == 200
    assert primera.json()["repetida"] is False and segunda.json() == {**primera.json(), "repetida": True}
    # Los mismos datos con otra clave sí son otra operación; con la misma clave y otros datos, 409.
    r = c.post("/api/vencimientos/asignar", json={**asignar, "cantidad": 3})
    assert r.status_code == 409 and "otros parámetros" in r.json()["detail"]
    with abrir_vto() as conn:
        assert len(_todo_el_ledger(conn)) == n
    assert c.post("/api/vencimientos/asignar", json={**asignar, "clave_operacion": _k()}).json()["repetida"] is False

    merma = {"producto_id": yogur, "deposito_id": deposito, "lote": "L0", "vence": _dias(-2), "cantidad": 3,
             "clave_operacion": _k()}
    m1, m2 = c.post("/api/vencimientos/merma", json=merma), c.post("/api/vencimientos/merma", json=merma)
    assert (m1.status_code, m2.status_code) == (200, 200) and m2.json() == {**m1.json(), "repetida": True}
    assert m1.json()["saldo_restante"] == 1
    assert c.post("/api/vencimientos/merma", json={**merma, "cantidad": 1}).status_code == 409
    assert c.post("/api/vencimientos/asignar", json={**asignar, "clave_operacion": merma["clave_operacion"]}
                  ).status_code == 409
    with abrir_vto() as conn:
        assert stock.get_stock_actual(conn, yogur, deposito) == 20 - 3


def test_la_clave_de_operacion_es_obligatoria_en_los_cuerpos(abrir_vto):
    yogur, deposito = _sembrar_para_el_router(abrir_vto)
    c = _app(abrir_vto)
    base_a = {"producto_id": yogur, "deposito_id": deposito, "lote": "L9", "vence": "2026-11-03", "cantidad": 1}
    base_m = {"producto_id": yogur, "deposito_id": deposito, "lote": "L1", "vence": _dias(5), "cantidad": 1}
    with abrir_vto() as conn:
        antes = _sin_id(_todo_el_ledger(conn))
    for ruta, base in (("asignar", base_a), ("merma", base_m)):
        for clave in ({}, {"clave_operacion": None}, {"clave_operacion": ""}, {"clave_operacion": "   "},
                      {"clave_operacion": "x" * 65}, {"clave_operacion": 7}, {"clave_operacion": "a[b"}):
            r = c.post(f"/api/vencimientos/{ruta}", json={**base, **clave})
            assert r.status_code == 422, (ruta, clave, r.text)
    with abrir_vto() as conn:
        assert _sin_id(_todo_el_ledger(conn)) == antes


def test_la_factory_de_escritura_falla_al_construirse_sin_usuario_o_sin_autorizacion(abrir_vto):
    libre = [Depends(_libre)]
    ok = dict(conexion=abrir_vto, usuario_actual=lambda: USUARIO, dependencias_marcar=libre,
              dependencias_movimientos=libre)
    build_vencimientos_escritura_router(**ok)  # completa: se construye
    build_vencimientos_escritura_router(**{**ok, "dependencias_marcar": tuple(libre)})

    with pytest.raises(ValueError, match="usuario_actual"):
        build_vencimientos_escritura_router(**{**ok, "usuario_actual": None})
    sin_usuario = {k: v for k, v in ok.items() if k != "usuario_actual"}
    with pytest.raises(ValueError, match="usuario_actual"):
        build_vencimientos_escritura_router(**sin_usuario)
    for nombre in ("dependencias_marcar", "dependencias_movimientos"):
        for malo in (None, [], (), Depends(_libre), "gate"):
            with pytest.raises(ValueError, match=nombre):
                build_vencimientos_escritura_router(**{**ok, nombre: malo})
        ausente = {k: v for k, v in ok.items() if k != nombre}
        with pytest.raises(ValueError, match=nombre):
            build_vencimientos_escritura_router(**ausente)
    with pytest.raises(ValueError):
        build_vencimientos_escritura_router(conexion=abrir_vto)  # como se montaba antes: ya no se puede


def test_el_router_de_lectura_solo_lee_y_el_de_escritura_solo_escribe(abrir_vto):
    lectura = build_vencimientos_router(conexion=abrir_vto)
    escritura = build_vencimientos_escritura_router(
        conexion=abrir_vto, usuario_actual=lambda: USUARIO, dependencias_marcar=[Depends(_libre)],
        dependencias_movimientos=[Depends(_libre)])
    assert {m for r in lectura.routes for m in r.methods} == {"GET"}
    assert {m for r in escritura.routes for m in r.methods} == {"PUT", "POST"}
    rutas = {(m, r.path) for router in (lectura, escritura) for r in router.routes for m in r.methods}
    assert len(rutas) == 7  # y no chocan entre sí (la entrada con lote es la séptima)


def test_las_escrituras_se_pueden_guardar_por_dependencia_distinta_a_las_lecturas(abrir_vto):
    """El producto pone una capacidad a leer y otras a escribir: acá la marca la pide el encargado y asignar o dar
    de baja también el depósito, y la lectura otra distinta."""
    yogur, deposito = _sembrar_para_el_router(abrir_vto)

    def rol(*permitidos):
        def gate(x_rol: str = Header("")):
            if x_rol not in permitidos:
                raise HTTPException(403, "sin permiso")
        return Depends(gate)

    app = FastAPI()
    app.include_router(build_vencimientos_router(conexion=abrir_vto), dependencies=[rol("encargado", "deposito")])
    app.include_router(
        build_vencimientos_escritura_router(
            conexion=abrir_vto, usuario_actual=lambda: USUARIO,
            dependencias_marcar=[rol("encargado")], dependencias_movimientos=[rol("encargado", "deposito")],
        ),
        dependencies=[rol("encargado", "deposito", "cajero")],
    )
    c = TestClient(app)
    asignar = {"producto_id": yogur, "deposito_id": deposito, "lote": "L9", "vence": "2026-11-03", "cantidad": 1,
               "clave_operacion": _k()}

    def ir(rol_, metodo, ruta, **kw):
        return c.request(metodo, ruta, headers={"x-rol": rol_}, **kw).status_code

    assert ir("deposito", "GET", "/api/vencimientos") == 200
    assert ir("cajero", "GET", "/api/vencimientos") == 403                       # el cajero no lee...
    assert ir("cajero", "POST", "/api/vencimientos/asignar", json=asignar) == 403  # ...ni mueve el ledger
    assert ir("cajero", "PUT", f"/api/vencimientos/productos/{yogur}", json={"vence": True}) == 403
    assert ir("deposito", "POST", "/api/vencimientos/asignar", json=asignar) == 200
    assert ir("deposito", "POST", "/api/vencimientos/merma",
              json={**asignar, "clave_operacion": _k()}) == 200
    assert ir("deposito", "PUT", f"/api/vencimientos/productos/{yogur}", json={"vence": True}) == 403  # marcar: encargado
    assert ir("encargado", "PUT", f"/api/vencimientos/productos/{yogur}", json={"vence": True}) == 200
    assert ir("", "GET", "/api/vencimientos") == 403


def test_sin_la_revision_el_router_responde_503_con_el_comando(destino):
    _crear_base_al_dia_de_0001(destino)
    core.configure(destino)
    try:
        c = _app(core.get_connection)
        r = c.get("/api/vencimientos")
        assert r.status_code == 503 and "libracommerce-migrar upgrade" in r.json()["detail"]
        assert c.put("/api/vencimientos/productos/1", json={"vence": True}).status_code == 503
    finally:
        _liberar()


def test_el_sqlite_usado_tiene_la_funcion_de_borrar_columnas():
    """El `downgrade` usa `ALTER TABLE ... DROP COLUMN` (SQLite 3.35+): si el intérprete fuera más viejo, el test de
    bajada fallaría por eso y no por la revisión."""
    assert sqlite3.sqlite_version_info >= (3, 35, 0)


def test_las_tres_escrituras_exigen_la_identidad_de_usuario_actual(abrir_vto):
    """Hallazgo de Codex: `marcar` no ejecutaba `usuario_actual`, así que un gate que no autentica dejaba cambiar la
    marca sin usuario identificado. Las tres rutas tienen que rechazar a quien `usuario_actual` rechaza."""
    from fastapi import HTTPException

    def _sin_sesion():
        raise HTTPException(401, "sin sesión")

    app = FastAPI()
    app.include_router(build_vencimientos_escritura_router(
        conexion=abrir_vto, usuario_actual=_sin_sesion,
        dependencias_marcar=[Depends(_libre)], dependencias_movimientos=[Depends(_libre)]))
    cliente = TestClient(app)
    cuerpo = {"producto_id": 1, "deposito_id": 1, "lote": "L", "vence": "2026-12-01", "cantidad": 1,
              "clave_operacion": "k"}
    assert cliente.put("/api/vencimientos/productos/1", json={"vence": True}).status_code == 401
    assert cliente.post("/api/vencimientos/asignar", json=cuerpo).status_code == 401
    assert cliente.post("/api/vencimientos/merma", json=cuerpo).status_code == 401
