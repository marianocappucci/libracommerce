"""Devolución, transferencia y ajuste por lote, y los avisos por HTTP (A-4 PR-3, ADR-018, 2026-09-30), contra los dos motores.

El PR-2 hizo que la venta y la anulación de un producto **marcado** (`tracks_expiry = 1`) sigan el lote (FEFO). El PR-3
cierra A-4: la devolución, la transferencia y el ajuste de un marcado también. Un producto sin marcar y un marcado sin
lotes escriben el ledger de siempre: eso lo fija `tests/test_ledger_sin_marcar.py` (que no se toca salvo los
`test_a4_pr3_*`, invertidos a propósito) y acá sólo se suma la parte nueva.

- **devolución** (`erp.ventas.devolver_items`): un PAR por tramo devuelto, en el lote de origen: `devolucion +q` y `merma −q`
  (la devolución de un perecedero va a merma); el tope sigue siendo por (ítem, variante) total; el reparto entre lotes
  sigue el orden en que salió la venta y resta lo ya devuelto; lo que ningún lote recibe va «sin lote», sin par. La
  receta sigue sin ser soportada (límite preexistente, fijado acá).
- **transferencia** (`catalogo.transferir_stock` → `usecases.transfer_stock(tramos=...)`): un par salida/entrada por tramo
  FEFO; la entrada copia lote y vencimiento; 1:1 con `source_id`.
- **ajuste** (`stock.ajustar_stock`, `POST /api/stock/{pid}/ajuste`): el conteo de un lote, FEFO si baja, «sin lote» si sube.
- **avisos** (`OpcionesVentas.con_avisos_de_vencimiento`): `avisos` en `POST`/`GET /api/ventas` y `POST /api/ventas/plan-salida`;
  apagada, todo igual que hoy.
- la **concurrencia** en PostgreSQL: el bloqueo por producto en la devolución, la transferencia y el ajuste.
"""

from __future__ import annotations

import datetime
import threading
import time
from decimal import Decimal

import pytest
from conftest import USUARIO, _schema_de_producto, _usuario, url_postgres
from fastapi import FastAPI
from fastapi.testclient import TestClient
from libracore.db import core

# Los helpers de `tests/test_fefo_venta.py` (las fixtures se repiten acá: importarlas dispara F811 en cada test).
from test_fefo_venta import (
    EN_15,
    EN_16,
    FECHA_HOY,
    FECHA_VENTA,
    HOY,
    LEJOS,
    PREVIA,
    VENCE_HOY,
    VENCIDO,
    _anular,
    _deposito_nuevo,
    _ensanchar_la_ventana,
    _ledger,
    _linea,
    _principal,
    _producto,
    _receta,
    _saldos,
    _solo_postgres,
    _vender,
)

from libracommerce import migrar
from libracommerce.erp import catalogo, lotes, margen, reposicion, stock, vencimientos, ventas
from libracommerce.usecases.inventory import TramoDeTransferencia, transfer_stock
from libracommerce.web.catalogo_router import (
    OpcionesStock,
    build_depositos_router,
    build_productos_router,
    build_stock_router,
)
from libracommerce.web.ventas_router import OpcionesVentas, build_ventas_router

# ── Fixtures (las mismas de `tests/test_fefo_venta.py`: una base por motor con la revisión 0002 y «hoy» fijo) ──


def _liberar():
    core._db_path = None
    core._database_url = None


@pytest.fixture(params=["sqlite", "postgres"])
def abrir_fefo(request, tmp_path):
    """Un `conexion()` como el de un producto (LibraCore + LibraCommerce, depósito principal y revisión `0002`)."""
    destino = str(tmp_path / "fefo.db") if request.param == "sqlite" else url_postgres()
    core.configure(destino)
    conn = core.get_connection()
    try:
        if request.param == "postgres":
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
            conn.commit()
        _schema_de_producto(conn)
    finally:
        conn.close()
    _liberar()
    migrar.upgrade(destino)
    core.configure(destino)
    yield core.get_connection
    _liberar()


@pytest.fixture(autouse=True)
def hoy_fijo(monkeypatch):
    monkeypatch.setattr(core, "_ar_now", lambda: f"{FECHA_HOY} 12:00:00")
    monkeypatch.setattr(vencimientos, "hoy_argentina", lambda ahora=None: HOY)


# ── Helpers ──────────────────────────────────────────────────────────────


def _lineas(abrir, vid) -> list[int]:
    with abrir() as conn:
        return [f[0] for f in conn.execute("SELECT id FROM sale_items WHERE sale_id = ? ORDER BY id", (vid,))]


def _devolver(abrir, vid, pedido, deposito, **kw):
    with abrir() as conn:
        resultado = ventas.devolver_items(conn, vid, pedido, deposito, usuario_id=USUARIO["id"], **kw)
        conn.commit()
    return resultado


def _pares(abrir, vid):
    """Lo que escribieron las devoluciones de una venta, en orden: `(reason_code, cantidad, lote, vence, depósito)`."""
    with abrir() as conn:
        return [
            (f["reason_code"], float(f["quantity_delta"]), f["lot_code"], f["expires_at"], f["location_id"])
            for f in conn.execute(
                "SELECT reason_code, quantity_delta, lot_code, expires_at, location_id FROM stock_movements "
                "WHERE source_id = ? AND reason_code IN ('devolucion', 'merma') ORDER BY id", (vid,))
        ]


def _transferir(abrir, pid, origen, destino, cantidad, **kw):
    with abrir() as conn:
        catalogo.transferir_stock(conn, pid, origen, destino, cantidad, usuario_id=USUARIO["id"],
                                  fecha="2026-09-12", **kw)
        conn.commit()


def _movs_de_transferencia(abrir):
    """`(tipo, cantidad, depósito, lote, vence, source_id, id)` de cada fila de transferencia, en orden."""
    with abrir() as conn:
        return [
            (f["movement_type"], float(f["quantity_delta"]), f["location_id"], f["lot_code"], f["expires_at"],
             f["source_id"], f["id"])
            for f in conn.execute(
                "SELECT id, movement_type, quantity_delta, location_id, lot_code, expires_at, source_id "
                "FROM stock_movements WHERE source_type = 'transfer' ORDER BY id")
        ]


def _ajustar(abrir, pid, nuevo, **kw):
    with abrir() as conn:
        stock.ajustar_stock(conn, pid, nuevo, "conteo", usuario_id=USUARIO["id"], fecha="2026-09-12", **kw)
        conn.commit()


def _ajustes(abrir, desde=0):
    """`(cantidad, lote, vence, depósito)` de las filas de ajuste escritas después de `desde`."""
    with abrir() as conn:
        return [(float(f["quantity_delta"]), f["lot_code"], f["expires_at"], f["location_id"]) for f in conn.execute(
            "SELECT quantity_delta, lot_code, expires_at, location_id FROM stock_movements "
            "WHERE reason_code = 'ajuste' AND id > ? ORDER BY id", (desde,))]


def _ultimo_id(abrir) -> int:
    with abrir() as conn:
        return conn.execute("SELECT COALESCE(MAX(id), 0) FROM stock_movements").fetchone()[0]


def _stock_total(abrir, pid, deposito=None) -> float:
    with abrir() as conn:
        return stock.get_stock_actual(conn, pid, deposito)


# ═════════════════════════════════════════════════════ Devolución ════════


def test_la_devolucion_parcial_escribe_un_par_en_el_lote_de_origen(abrir_fefo):
    pid = _producto(abrir_fefo)                          # L1 6 (2026-10-05) y L2 4 (2026-11-01), marcado
    dep = _principal(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(pid, 8)])          # L1 −6, L2 −2
    (linea,) = _lineas(abrir_fefo, vid)
    _devolver(abrir_fefo, vid, {linea: 1.0}, dep)
    assert _pares(abrir_fefo, vid) == [("devolucion", 1.0, "L1", "2026-10-05", dep),
                                       ("merma", -1.0, "L1", "2026-10-05", dep)]
    # El neto es 0: lo devuelto no vuelve al estante (ni al lote ni al «sin lote»).
    assert _saldos(abrir_fefo, pid) == {"L2": 2}
    assert _stock_total(abrir_fefo, pid) == 2.0


def test_el_par_lleva_los_datos_de_siempre_y_la_nota_de_la_merma(abrir_fefo):
    pid = _producto(abrir_fefo)
    dep = _principal(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(pid, 3)])
    (linea,) = _lineas(abrir_fefo, vid)
    antes = _ultimo_id(abrir_fefo)
    _devolver(abrir_fefo, vid, {linea: 2.0}, dep)
    with abrir_fefo() as conn:
        filas = [tuple(f) for f in conn.execute(
            "SELECT movement_type, reason_code, source_type, source_id, note, created_by, occurred_at, variant_id "
            "FROM stock_movements WHERE id > ? ORDER BY id", (antes,))]
    assert filas == [
        ("return", "devolucion", "venta", vid, f"Devolución venta ID {vid}", 7, f"{FECHA_HOY}T00:00:00", None),
        ("waste", "merma", "venta", vid, f"Merma: devolución venta ID {vid} — lote L1", 7, f"{FECHA_HOY}T00:00:00",
         None),
    ]


def test_una_devolucion_que_cruza_lotes_escribe_un_par_por_lote_en_el_orden_en_que_salieron(abrir_fefo):
    pid = _producto(abrir_fefo)
    dep = _principal(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(pid, 8)])          # L1 −6, L2 −2
    (linea,) = _lineas(abrir_fefo, vid)
    resultado = _devolver(abrir_fefo, vid, {linea: 8.0}, dep)
    assert _pares(abrir_fefo, vid) == [
        ("devolucion", 6.0, "L1", "2026-10-05", dep), ("merma", -6.0, "L1", "2026-10-05", dep),
        ("devolucion", 2.0, "L2", "2026-11-01", dep), ("merma", -2.0, "L2", "2026-11-01", dep),
    ]
    assert resultado["venta"]["estado"] == "devuelta" and resultado["importe"] == 800.0
    assert _saldos(abrir_fefo, pid) == {"L2": 2}          # exacto a lo que dejó la venta
    assert _stock_total(abrir_fefo, pid) == 2.0


def test_devoluciones_acumuladas_restan_lo_ya_devuelto_por_lote_y_respetan_el_tope(abrir_fefo):
    pid = _producto(abrir_fefo)
    dep = _principal(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(pid, 8)])          # L1 −6, L2 −2
    (linea,) = _lineas(abrir_fefo, vid)
    _devolver(abrir_fefo, vid, {linea: 5.0}, dep)         # L1 5
    _devolver(abrir_fefo, vid, {linea: 2.0}, dep)         # L1 1 (lo que le quedaba) y L2 1
    assert _pares(abrir_fefo, vid)[2:] == [
        ("devolucion", 1.0, "L1", "2026-10-05", dep), ("merma", -1.0, "L1", "2026-10-05", dep),
        ("devolucion", 1.0, "L2", "2026-11-01", dep), ("merma", -1.0, "L2", "2026-11-01", dep),
    ]
    with abrir_fefo() as conn:
        assert ventas.obtener_venta(conn, vid)["estado"] == "devuelta_parcial"
    _devolver(abrir_fefo, vid, {linea: 1.0}, dep)         # L2 1: ya no queda nada
    with abrir_fefo() as conn:
        assert ventas.obtener_venta(conn, vid)["estado"] == "devuelta"
    with pytest.raises(ValueError, match="estado 'devuelta'"):      # ya no admite más devoluciones
        _devolver(abrir_fefo, vid, {linea: 1.0}, dep)
    assert len(_pares(abrir_fefo, vid)) == 8              # 4 pares (5 | 1 y 1 | 1): la última no escribió nada
    assert _stock_total(abrir_fefo, pid) == 2.0           # nunca volvió nada al estante


def test_dos_lineas_del_mismo_producto_en_una_devolucion_se_reparten_en_secuencia(abrir_fefo):
    pid = _producto(abrir_fefo)
    dep = _principal(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(pid, 5), _linea(pid, 3)])     # L1 −5 | L1 −1, L2 −2
    primera, segunda = _lineas(abrir_fefo, vid)
    _devolver(abrir_fefo, vid, {primera: 4.0, segunda: 4.0}, dep)
    assert _pares(abrir_fefo, vid) == [
        ("devolucion", 4.0, "L1", "2026-10-05", dep), ("merma", -4.0, "L1", "2026-10-05", dep),
        ("devolucion", 2.0, "L1", "2026-10-05", dep), ("merma", -2.0, "L1", "2026-10-05", dep),
        ("devolucion", 2.0, "L2", "2026-11-01", dep), ("merma", -2.0, "L2", "2026-11-01", dep),
    ]


def test_el_par_va_al_deposito_de_la_devolucion_y_con_la_variante(abrir_fefo):
    pid = _producto(abrir_fefo, lotes_=())
    norte = _deposito_nuevo(abrir_fefo)
    dep = _principal(abrir_fefo)
    with abrir_fefo() as conn:
        v = catalogo.create_variante(conn, pid, "Y-1", "Frutilla")["id"]
        stock.add_movimiento_stock(conn, pid, "entrada", 5, "carga", fecha=PREVIA, lot_code="V1", expires_at=LEJOS,
                                   variant_id=v)
        conn.commit()
    vid = _vender(abrir_fefo, [_linea(pid, 3, variante_id=v)])
    (linea,) = _lineas(abrir_fefo, vid)
    _devolver(abrir_fefo, vid, {linea: 2.0}, norte)      # la sucursal que recibe no es la de la venta
    with abrir_fefo() as conn:
        filas = [(f["reason_code"], float(f["quantity_delta"]), f["location_id"], f["variant_id"], f["lot_code"])
                 for f in conn.execute("SELECT * FROM stock_movements WHERE source_id = ? AND reason_code IN "
                                       "('devolucion', 'merma') ORDER BY id", (vid,))]
    assert filas == [("devolucion", 2.0, norte, v, "V1"), ("merma", -2.0, norte, v, "V1")]
    assert _saldos(abrir_fefo, pid, dep) == {"V1": 2} and _saldos(abrir_fefo, pid, norte) == {}


def test_un_rechazo_no_deja_filas_y_la_anulacion_posterior_sigue_exacta(abrir_fefo):
    pid = _producto(abrir_fefo)
    dep = _principal(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(pid, 8)])
    (linea,) = _lineas(abrir_fefo, vid)
    antes = _ledger(abrir_fefo)
    with pytest.raises(ValueError, match="quedan 8"):
        _devolver(abrir_fefo, vid, {linea: 9.0}, dep)
    assert _ledger(abrir_fefo) == antes and _pares(abrir_fefo, vid) == []
    assert _anular(abrir_fefo, vid) is True
    assert _saldos(abrir_fefo, pid) == {"L1": 6, "L2": 4}     # exacto a antes de vender, lote por lote


def test_si_falla_a_mitad_el_caller_hace_rollback_y_no_queda_ningun_par(abrir_fefo):
    """La atomicidad de siempre: `devolver_items` no commitea; una línea inválida después de una válida levanta y el
    rollback del caller se lleva las filas ya escritas."""
    pid = _producto(abrir_fefo)
    dep = _principal(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(pid, 2), _linea(pid, 3)])
    primera, segunda = _lineas(abrir_fefo, vid)
    antes = _ledger(abrir_fefo)
    with abrir_fefo() as conn:
        with pytest.raises(ValueError, match="quedan 3"):
            ventas.devolver_items(conn, vid, {primera: 2.0, segunda: 4.0}, dep, usuario_id=7)
        conn.rollback()
    assert _ledger(abrir_fefo) == antes


def test_una_venta_con_devolucion_no_se_anula_y_no_escribe_nada(abrir_fefo):
    pid = _producto(abrir_fefo)
    dep = _principal(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(pid, 4)])
    (linea,) = _lineas(abrir_fefo, vid)
    _devolver(abrir_fefo, vid, {linea: 1.0}, dep)
    antes = _ledger(abrir_fefo)
    with pytest.raises(ventas.VentaConDevoluciones):
        _anular(abrir_fefo, vid)
    assert _ledger(abrir_fefo) == antes


def test_un_producto_sin_marcar_devuelve_una_fila_suelta_como_siempre(abrir_fefo):
    pid = _producto(abrir_fefo, "Pan", lotes_=(), sin_lote=10, marcado=False)
    dep = _principal(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(pid, 3)])
    (linea,) = _lineas(abrir_fefo, vid)
    _devolver(abrir_fefo, vid, {linea: 2.0}, dep)
    assert _pares(abrir_fefo, vid) == [("devolucion", 2.0, None, None, dep)]      # sin merma, sin lote
    assert _stock_total(abrir_fefo, pid) == 9.0                                   # y el stock SÍ vuelve


def test_un_marcado_cuya_venta_no_salio_de_ningun_lote_devuelve_la_fila_suelta(abrir_fefo):
    """Una venta de antes de A-4 (todo «sin lote»): no hay lote al que volver, así que la devolución es la de siempre."""
    pid = _producto(abrir_fefo, "Yogur", lotes_=(), sin_lote=10, marcado=False)
    dep = _principal(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(pid, 3)])          # sale sin lote (todavía no está marcado)
    with abrir_fefo() as conn:
        vencimientos.marcar_vence(conn, pid, True)
        stock.add_movimiento_stock(conn, pid, "entrada", 5, "carga", fecha=PREVIA, lot_code="N1", expires_at=LEJOS)
        conn.commit()
    (linea,) = _lineas(abrir_fefo, vid)
    _devolver(abrir_fefo, vid, {linea: 2.0}, dep)
    assert _pares(abrir_fefo, vid) == [("devolucion", 2.0, None, None, dep)]


def test_lo_que_ningun_lote_puede_recibir_va_sin_lote_y_sin_par(abrir_fefo):
    pid = _producto(abrir_fefo, lotes_=(("L1", "2026-10-05", 6),))
    dep = _principal(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(pid, 8)])          # L1 −6 y 2 de faltante «sin lote»
    (linea,) = _lineas(abrir_fefo, vid)
    _devolver(abrir_fefo, vid, {linea: 7.0}, dep)
    assert _pares(abrir_fefo, vid) == [
        ("devolucion", 6.0, "L1", "2026-10-05", dep), ("merma", -6.0, "L1", "2026-10-05", dep),
        ("devolucion", 1.0, None, None, dep),             # el resto, suelto y sin merma
    ]


def test_el_tope_sigue_siendo_por_item_y_variante_sin_lote(abrir_fefo):
    """Devoluciones anteriores sin lote (las de antes del PR-3) cuentan para el tope aunque no sean de ningún lote."""
    pid = _producto(abrir_fefo)
    dep = _principal(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(pid, 8)])
    (linea,) = _lineas(abrir_fefo, vid)
    with abrir_fefo() as conn:           # la devolución de antes del PR-3: una fila suelta, sin lote
        stock.add_movimiento_stock(conn, pid, "devolucion", 7, f"Devolución venta ID {vid}", venta_id=vid,
                                   deposito_id=dep, fecha=PREVIA)
        conn.commit()
    with pytest.raises(ValueError, match="quedan 1"):
        _devolver(abrir_fefo, vid, {linea: 2.0}, dep)
    _devolver(abrir_fefo, vid, {linea: 1.0}, dep)       # queda 1, y los lotes (L1 6 − 0, L2 2) lo reciben en orden
    assert _pares(abrir_fefo, vid)[1:] == [("devolucion", 1.0, "L1", "2026-10-05", dep),
                                           ("merma", -1.0, "L1", "2026-10-05", dep)]


def test_limite_la_devolucion_no_es_receta_aware_y_no_toca_los_lotes_de_los_insumos(abrir_fefo):
    """🔴 Límite EXISTENTE, fijado acá y NO resuelto en el PR-3: la venta de un plato con receta descontó INSUMOS (por lote,
    si están marcados) y `devolver_items` devuelve el PLATO, que nunca tuvo stock. Los lotes de los insumos no vuelven (ni
    van a merma). Es un pendiente: cuando `devolver_items` sea receta-aware este test se invierte."""
    insumo = _producto(abrir_fefo, "Pan")                                   # L1 6, L2 4, marcado
    plato = _producto(abrir_fefo, "Tostado", lotes_=(), marcado=False)
    dep = _principal(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(plato, 2)], hooks=_receta(plato, [(insumo, "3")]))   # L1 −6
    (linea,) = _lineas(abrir_fefo, vid)
    assert _saldos(abrir_fefo, insumo) == {"L2": 4}
    _devolver(abrir_fefo, vid, {linea: 1.0}, dep)
    assert _pares(abrir_fefo, vid) == [("devolucion", 1.0, None, None, dep)]        # el plato, suelto
    assert _saldos(abrir_fefo, insumo) == {"L2": 4}                                # el insumo, intacto
    with abrir_fefo() as conn:
        assert stock.get_stock_actual(conn, plato) == 1.0                            # un plato con stock fantasma


def test_margen_y_reposicion_leen_lo_mismo_con_la_devolucion_en_par_que_con_un_producto_sin_marcar(abrir_fefo):
    """El margen lee las filas `devolucion` y la reposición la rotación (ventas netas): la merma del par no cuenta en
    ninguna. Lo único que difiere, a propósito, es el STOCK: lo devuelto de un perecedero no vuelve al estante."""
    perecedero = _producto(abrir_fefo, "Yogur")                                           # 6 + 4, marcado
    comun = _producto(abrir_fefo, "Yogur común", lotes_=(), sin_lote=10, marcado=False)
    dep = _principal(abrir_fefo)
    for qty, fecha in ((4, "2026-09-10"), (3, "2026-09-20")):
        vid = _vender(abrir_fefo, [_linea(perecedero, qty), _linea(comun, qty)], fecha=fecha)
        a, b = _lineas(abrir_fefo, vid)
        _devolver(abrir_fefo, vid, {a: 1.0, b: 1.0}, dep)
    with abrir_fefo() as conn:
        rep = margen.reporte_margen(conn, "2026-09-01", "2026-09-30")
        por = {p["nombre"]: p for p in rep["productos"]}
        x, y = por["Yogur"], por["Yogur común"]
        assert {k: x[k] for k in x if k not in ("producto_id", "nombre")} == {
            k: y[k] for k in y if k not in ("producto_id", "nombre")}
        assert x["unidades"] == 5                        # 7 vendidas − 2 devueltas
        sug = {f["nombre"]: f for f in reposicion.sugerencia_reposicion(conn, hoy=HOY, solo_a_pedir=False)}
        p, c = sug["Yogur"], sug["Yogur común"]
        assert p["unidades_vendidas"] == c["unidades_vendidas"] == 5
        assert p["stock"] == 3 and c["stock"] == 5       # la diferencia a propósito: el perecedero devuelto se descartó


def test_la_devolucion_aparece_en_movimientos_con_una_merma_de_mas(abrir_fefo):
    pid = _producto(abrir_fefo)
    dep = _principal(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(pid, 3)])
    (linea,) = _lineas(abrir_fefo, vid)
    _devolver(abrir_fefo, vid, {linea: 1.0}, dep)
    with abrir_fefo() as conn:
        movs = stock.get_movimientos_stock(conn, pid)
    assert [(m["tipo"], m["cantidad"], m["venta_id"]) for m in movs if m["tipo"] in ("devolucion", "merma")] == [
        ("merma", -1.0, vid), ("devolucion", 1.0, vid)]          # el más nuevo primero


# ── El tratamiento por lote lo decide la VENTA, no la marca de hoy ──────


def _marcar(abrir, pid, valor):
    with abrir() as conn:
        vencimientos.marcar_vence(conn, pid, valor)
        conn.commit()


def test_vender_por_lote_desmarcar_y_devolver_sigue_siendo_un_par_en_el_lote_de_origen_con_neto_cero(abrir_fefo):
    pid = _producto(abrir_fefo)
    dep = _principal(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(pid, 8)])          # L1 −6, L2 −2
    (linea,) = _lineas(abrir_fefo, vid)
    _marcar(abrir_fefo, pid, False)
    _devolver(abrir_fefo, vid, {linea: 1.0}, dep)         # parcial
    assert _pares(abrir_fefo, vid) == [("devolucion", 1.0, "L1", "2026-10-05", dep),
                                       ("merma", -1.0, "L1", "2026-10-05", dep)]
    assert _stock_total(abrir_fefo, pid) == 2.0           # neto 0: el stock no subió
    _devolver(abrir_fefo, vid, {linea: 6.0}, dep)         # acumulado: L1 5 y L2 1
    assert _pares(abrir_fefo, vid)[2:] == [
        ("devolucion", 5.0, "L1", "2026-10-05", dep), ("merma", -5.0, "L1", "2026-10-05", dep),
        ("devolucion", 1.0, "L2", "2026-11-01", dep), ("merma", -1.0, "L2", "2026-11-01", dep)]
    with pytest.raises(ValueError, match="quedan 1"):
        _devolver(abrir_fefo, vid, {linea: 2.0}, dep)     # el tope: queda 1 (de L2)
    assert _stock_total(abrir_fefo, pid) == 2.0


def test_desmarcado_la_devolucion_total_y_el_tope_siguen_por_item_y_variante(abrir_fefo):
    pid = _producto(abrir_fefo)
    dep = _principal(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(pid, 4)])
    (linea,) = _lineas(abrir_fefo, vid)
    _marcar(abrir_fefo, pid, False)
    antes = _ledger(abrir_fefo)
    with pytest.raises(ValueError, match="quedan 4"):
        _devolver(abrir_fefo, vid, {linea: 5.0}, dep)
    assert _ledger(abrir_fefo) == antes
    r = _devolver(abrir_fefo, vid, {linea: 4.0}, dep)
    assert r["venta"]["estado"] == "devuelta"
    assert _pares(abrir_fefo, vid) == [("devolucion", 4.0, "L1", "2026-10-05", dep),
                                       ("merma", -4.0, "L1", "2026-10-05", dep)]


def test_desmarcar_y_volver_a_marcar_entre_devoluciones_no_cambia_nada(abrir_fefo):
    pid = _producto(abrir_fefo)
    dep = _principal(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(pid, 8)])
    (linea,) = _lineas(abrir_fefo, vid)
    _marcar(abrir_fefo, pid, False)
    _devolver(abrir_fefo, vid, {linea: 2.0}, dep)
    _marcar(abrir_fefo, pid, True)
    _devolver(abrir_fefo, vid, {linea: 2.0}, dep)
    assert _pares(abrir_fefo, vid) == [("devolucion", 2.0, "L1", "2026-10-05", dep), ("merma", -2.0, "L1", "2026-10-05", dep),
                                       ("devolucion", 2.0, "L1", "2026-10-05", dep), ("merma", -2.0, "L1", "2026-10-05", dep)]
    assert _stock_total(abrir_fefo, pid) == 2.0


def test_desmarcar_antes_de_devolver_una_venta_anterior_a_a4_se_comporta_como_hoy(abrir_fefo):
    pid = _producto(abrir_fefo, "Yogur", lotes_=(), sin_lote=10, marcado=False)
    dep = _principal(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(pid, 3)])          # sale sin lote (todavía sin marcar)
    with abrir_fefo() as conn:
        vencimientos.marcar_vence(conn, pid, True)
        stock.add_movimiento_stock(conn, pid, "entrada", 5, "carga", fecha=PREVIA, lot_code="N1", expires_at=LEJOS)
        conn.commit()
    _marcar(abrir_fefo, pid, False)
    (linea,) = _lineas(abrir_fefo, vid)
    _devolver(abrir_fefo, vid, {linea: 2.0}, dep)
    assert _pares(abrir_fefo, vid) == [("devolucion", 2.0, None, None, dep)]      # suelta, sin merma
    assert _stock_total(abrir_fefo, pid) == 14.0          # 10 − 3 + 5 (lote N1) + 2: la devolución SÍ vuelve al stock


def test_la_anulacion_de_una_venta_por_lote_es_exacta_aunque_se_desmarque_despues(abrir_fefo):
    pid = _producto(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(pid, 8)])
    _marcar(abrir_fefo, pid, False)
    assert _anular(abrir_fefo, vid) is True
    assert _saldos(abrir_fefo, pid) == {"L1": 6, "L2": 4}


def test_la_transferencia_y_el_ajuste_de_un_desmarcado_con_lotes_en_el_ledger_siguen_la_marca_actual(abrir_fefo):
    """Son operaciones sobre stock PRESENTE, no sobre una venta pasada: con la marca apagada se comportan como un producto
    sin marcar (sin lote), aunque el ledger tenga lotes; al volver a marcar, por FEFO otra vez."""
    pid = _producto(abrir_fefo)
    dep = _principal(abrir_fefo)
    norte = _deposito_nuevo(abrir_fefo)
    _marcar(abrir_fefo, pid, False)
    marca = _ultimo_id(abrir_fefo)
    _transferir(abrir_fefo, pid, dep, norte, 4)
    _ajustar(abrir_fefo, pid, 3)                          # 10 → 3: −7, sin lote
    assert [(m[0], m[1], m[3]) for m in _movs_de_transferencia(abrir_fefo)] == [
        ("transfer_out", -4.0, None), ("transfer_in", 4.0, None)]
    assert _ajustes(abrir_fefo, marca) == [(-7.0, None, None, dep)]
    _marcar(abrir_fefo, pid, True)
    marca = _ultimo_id(abrir_fefo)
    _ajustar(abrir_fefo, pid, 1)                          # 3 → 1: −2, ahora por FEFO (L1 sigue en 6)
    assert _ajustes(abrir_fefo, marca) == [(-2.0, "L1", "2026-10-05", dep)]


def test_en_postgres_dos_devoluciones_simultaneas_de_un_desmarcado_con_venta_por_lote_no_devuelven_de_mas(
        abrir_fefo, monkeypatch):
    _solo_postgres()
    pid = _producto(abrir_fefo, lotes_=(("L1", "2026-10-05", 10),))
    dep = _principal(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(pid, 4)])
    (linea,) = _lineas(abrir_fefo, vid)
    _marcar(abrir_fefo, pid, False)          # el bloqueo sale de las filas de la venta, no de la marca
    original = ventas._lotes_a_devolver

    def lento(*a, **k):
        r = original(*a, **k)
        time.sleep(0.4)
        return r

    monkeypatch.setattr(ventas, "_lotes_a_devolver", lento)
    resultados, errores = _en_hilos([_devolver_en_conexion_propia(vid, {linea: 3.0}, dep)] * 2)
    assert len(resultados) == 1 and len(errores) == 1 and isinstance(errores[0], ValueError), (resultados, errores)


# ── Concurrencia de la devolución (PostgreSQL) ──────────────────────────


def _en_hilos(trabajos):
    """Corre cada `trabajo()` en su propio hilo, todos a la vez. Devuelve `(resultados, errores)`."""
    barrera = threading.Barrier(len(trabajos))
    resultados: list = []
    errores: list = []

    def correr(trabajo):
        try:
            barrera.wait(10)
            resultados.append(trabajo())
        except Exception as e:  # noqa: BLE001 - se informa en el assert
            errores.append(e)

    hilos = [threading.Thread(target=correr, args=(t,)) for t in trabajos]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(60)
    assert not any(h.is_alive() for h in hilos), "una operación quedó colgada"
    return resultados, errores


def _devolver_en_conexion_propia(vid, pedido, deposito):
    def trabajo():
        c = core.get_connection()
        try:
            ventas.devolver_items(c, vid, pedido, deposito, usuario_id=USUARIO["id"])
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()
        return "ok"
    return trabajo


def test_en_postgres_dos_devoluciones_simultaneas_del_mismo_marcado_no_devuelven_de_mas(abrir_fefo, monkeypatch):
    _solo_postgres()
    pid = _producto(abrir_fefo, lotes_=(("L1", "2026-10-05", 10),))
    dep = _principal(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(pid, 4)])
    (linea,) = _lineas(abrir_fefo, vid)
    # Se ensancha la ventana entre leer lo ya devuelto y escribir: sin el bloqueo por producto las dos pasan el tope.
    original = ventas._lotes_a_devolver

    def lento(*a, **k):
        r = original(*a, **k)
        time.sleep(0.4)
        return r

    monkeypatch.setattr(ventas, "_lotes_a_devolver", lento)
    resultados, errores = _en_hilos([_devolver_en_conexion_propia(vid, {linea: 3.0}, dep)] * 2)
    assert len(resultados) == 1 and len(errores) == 1 and isinstance(errores[0], ValueError), (resultados, errores)
    assert [p for p in _pares(abrir_fefo, vid) if p[0] == "devolucion"] == [("devolucion", 3.0, "L1", "2026-10-05", dep)]


def test_en_postgres_devoluciones_de_ventas_con_dos_productos_en_orden_cruzado_no_hacen_deadlock(abrir_fefo,
                                                                                              monkeypatch):
    _solo_postgres()
    _ensanchar_la_ventana(monkeypatch, despues_de_tomar=0.3)
    a = _producto(abrir_fefo, "A", lotes_=(("A1", "2026-10-05", 50),))
    b = _producto(abrir_fefo, "B", lotes_=(("B1", "2026-10-05", 50),))
    dep = _principal(abrir_fefo)
    v1 = _vender(abrir_fefo, [_linea(a, 2), _linea(b, 2)])
    v2 = _vender(abrir_fefo, [_linea(a, 2), _linea(b, 2)])
    (a1, b1), (a2, b2) = _lineas(abrir_fefo, v1), _lineas(abrir_fefo, v2)
    # Dos devoluciones con las líneas en orden opuesto: el bloqueo va en orden ascendente de id, no de línea.
    resultados, errores = _en_hilos([
        _devolver_en_conexion_propia(v1, {a1: 1.0, b1: 1.0}, dep),
        _devolver_en_conexion_propia(v2, {b2: 1.0, a2: 1.0}, dep),
    ])
    assert not errores, errores
    assert _saldos(abrir_fefo, a) == {"A1": 46} and _saldos(abrir_fefo, b) == {"B1": 46}


def test_los_productos_marcados_que_se_devuelven_se_toman_en_orden_ascendente_de_id(abrir_fefo):
    a = _producto(abrir_fefo, "A")
    b = _producto(abrir_fefo, "B")
    dep = _principal(abrir_fefo)
    vid = _vender(abrir_fefo, [_linea(b, 2), _linea(a, 2)])
    lb, la = _lineas(abrir_fefo, vid)
    tomados: list = []
    with abrir_fefo() as conn:
        original = conn.execute

        class Espia:
            def __getattr__(self, nombre):
                return getattr(conn, nombre)

            def execute(self, sql, params=()):
                if sql.startswith("UPDATE catalog_items SET tracks_expiry = tracks_expiry"):
                    tomados.append(params[0])
                return original(sql, params)

        ventas.devolver_items(Espia(), vid, {lb: 1.0, la: 1.0}, dep, usuario_id=7)
        conn.commit()
    assert tomados == sorted([a, b])


# ═════════════════════════════════════════════════ Transferencia ═════════


def test_la_transferencia_escribe_un_par_por_lote_fefo_con_el_vencido_primero_y_el_sin_lote_ultimo(abrir_fefo):
    pid = _producto(abrir_fefo, lotes_=(("NUEVO", LEJOS, 5), ("VIEJO", VENCIDO, 2)), sin_lote=4)
    dep = _principal(abrir_fefo)
    norte = _deposito_nuevo(abrir_fefo)
    _transferir(abrir_fefo, pid, dep, norte, 9)
    movs = _movs_de_transferencia(abrir_fefo)
    assert [m[:5] for m in movs] == [
        ("transfer_out", -2.0, dep, "VIEJO", VENCIDO), ("transfer_in", 2.0, norte, "VIEJO", VENCIDO),
        ("transfer_out", -5.0, dep, "NUEVO", LEJOS), ("transfer_in", 5.0, norte, "NUEVO", LEJOS),
        ("transfer_out", -2.0, dep, None, None), ("transfer_in", 2.0, norte, None, None),
    ]
    # Cada entrada apunta a SU salida (1:1) y la salida no apunta a nada.
    for i in (0, 2, 4):
        assert movs[i][5] is None and movs[i + 1][5] == movs[i][6]
    assert _saldos(abrir_fefo, pid, dep) == {None: 2}
    assert _saldos(abrir_fefo, pid, norte) == {"VIEJO": 2, "NUEVO": 5, None: 2}
    assert _stock_total(abrir_fefo, pid) == 11.0


def test_get_transferencias_muestra_una_fila_por_tramo_y_cada_una_es_1_a_1(abrir_fefo):
    pid = _producto(abrir_fefo)                          # L1 6, L2 4
    dep = _principal(abrir_fefo)
    norte = _deposito_nuevo(abrir_fefo)
    _transferir(abrir_fefo, pid, dep, norte, 8)          # L1 6 y L2 2
    with abrir_fefo() as conn:
        filas = catalogo.get_transferencias(conn)
        assert sorted(f["cantidad"] for f in filas) == [2.0, 6.0]      # dos filas, cada una con su cantidad
        assert {(f["origen_id"], f["destino_id"], f["producto_id"]) for f in filas} == {(dep, norte, pid)}
        assert len({f["id"] for f in filas}) == 2
        assert len(catalogo.get_transferencias(conn, norte)) == 2 and len(catalogo.get_transferencias(conn, dep)) == 2
        assert set(filas[0]) == {"id", "producto_id", "producto", "variant_id", "cantidad", "origen_id", "origen",
                                 "destino_id", "destino", "fecha", "observaciones", "usuario_id"}   # mismas claves


def test_una_transferencia_de_un_solo_lote_es_un_solo_par(abrir_fefo):
    pid = _producto(abrir_fefo)
    dep = _principal(abrir_fefo)
    norte = _deposito_nuevo(abrir_fefo)
    _transferir(abrir_fefo, pid, dep, norte, 6)
    assert [m[:5] for m in _movs_de_transferencia(abrir_fefo)] == [
        ("transfer_out", -6.0, dep, "L1", "2026-10-05"), ("transfer_in", 6.0, norte, "L1", "2026-10-05")]
    with abrir_fefo() as conn:
        assert len(catalogo.get_transferencias(conn)) == 1


def test_un_producto_sin_marcar_y_un_marcado_sin_lotes_transfieren_el_par_de_siempre(abrir_fefo):
    comun = _producto(abrir_fefo, "Pan", lotes_=(), sin_lote=10, marcado=False)
    vacio = _producto(abrir_fefo, "Sal", lotes_=(), sin_lote=10)        # marcado, sin ningún lote
    dep = _principal(abrir_fefo)
    norte = _deposito_nuevo(abrir_fefo)
    _transferir(abrir_fefo, comun, dep, norte, 4)
    _transferir(abrir_fefo, vacio, dep, norte, 4)
    movs = _movs_de_transferencia(abrir_fefo)
    assert [m[:5] for m in movs] == [
        ("transfer_out", -4.0, dep, None, None), ("transfer_in", 4.0, norte, None, None)] * 2


def test_una_transferencia_mixta_lleva_lo_que_hay_en_lotes_y_el_resto_sin_lote(abrir_fefo):
    pid = _producto(abrir_fefo, lotes_=(("L1", "2026-10-05", 2),), sin_lote=5)
    dep = _principal(abrir_fefo)
    norte = _deposito_nuevo(abrir_fefo)
    _transferir(abrir_fefo, pid, dep, norte, 4)
    assert [m[:5] for m in _movs_de_transferencia(abrir_fefo)] == [
        ("transfer_out", -2.0, dep, "L1", "2026-10-05"), ("transfer_in", 2.0, norte, "L1", "2026-10-05"),
        ("transfer_out", -2.0, dep, None, None), ("transfer_in", 2.0, norte, None, None)]


def test_la_transferencia_mira_los_buckets_del_origen_y_de_la_variante(abrir_fefo):
    pid = _producto(abrir_fefo, lotes_=(("L1", "2026-10-05", 5),))
    norte = _deposito_nuevo(abrir_fefo)
    dep = _principal(abrir_fefo)
    with abrir_fefo() as conn:
        v = catalogo.create_variante(conn, pid, "Y-1", "Frutilla")["id"]
        stock.add_movimiento_stock(conn, pid, "entrada", 3, "carga", fecha=PREVIA, lot_code="V1", expires_at=VENCIDO,
                                   variant_id=v)
        stock.add_movimiento_stock(conn, pid, "entrada", 9, "carga", fecha=PREVIA, lot_code="N1", expires_at=VENCIDO,
                                   deposito_id=norte)
        conn.commit()
    _transferir(abrir_fefo, pid, dep, norte, 2, variant_id=v)
    with abrir_fefo() as conn:
        filas = [(f["movement_type"], float(f["quantity_delta"]), f["location_id"], f["variant_id"], f["lot_code"])
                 for f in conn.execute("SELECT * FROM stock_movements WHERE source_type = 'transfer' ORDER BY id")]
    assert filas == [("transfer_out", -2.0, dep, v, "V1"), ("transfer_in", 2.0, norte, v, "V1")]   # ni L1 ni N1


def test_la_guarda_de_disponibilidad_total_no_cambia(abrir_fefo):
    pid = _producto(abrir_fefo)                          # 10 en total
    dep = _principal(abrir_fefo)
    norte = _deposito_nuevo(abrir_fefo)
    antes = _ledger(abrir_fefo)
    with abrir_fefo() as conn:
        with pytest.raises(ValueError, match=r"Stock insuficiente en depósito origen \(disponible: 10.0\)"):
            catalogo.transferir_stock(conn, pid, dep, norte, 11)
        conn.rollback()
    assert _ledger(abrir_fefo) == antes


def test_con_un_sin_lote_negativo_heredado_la_guarda_mira_el_total_y_los_lotes_se_transfieren(abrir_fefo):
    pid = _producto(abrir_fefo)                          # L1 6 y L2 4
    dep = _principal(abrir_fefo)
    norte = _deposito_nuevo(abrir_fefo)
    with abrir_fefo() as conn:
        stock.add_movimiento_stock(conn, pid, "venta", -4, "Venta ID 99", fecha=PREVIA)   # una salida de antes de A-4
        conn.commit()
    with abrir_fefo() as conn:
        with pytest.raises(ValueError, match="Stock insuficiente"):
            catalogo.transferir_stock(conn, pid, dep, norte, 7)             # total 6: no alcanza aunque los lotes sumen 10
        conn.rollback()
    _transferir(abrir_fefo, pid, dep, norte, 6)
    assert _saldos(abrir_fefo, pid, norte) == {"L1": 6}


def test_cantidad_no_positiva_y_mismo_deposito_se_rechazan_como_siempre_sin_tomar_el_producto(abrir_fefo):
    pid = _producto(abrir_fefo)
    dep = _principal(abrir_fefo)
    norte = _deposito_nuevo(abrir_fefo)
    registro: list[str] = []

    class Espia:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, nombre):
            return getattr(self._conn, nombre)

        def execute(self, sql, params=()):
            registro.append(sql)
            return self._conn.execute(sql, params)

        def cursor(self):
            return Espia(self._conn.cursor())

    with abrir_fefo() as conn:
        with pytest.raises(ValueError, match="positiva"):
            catalogo.transferir_stock(Espia(conn), pid, dep, norte, 0)
        with pytest.raises(ValueError, match="mismo deposito"):
            catalogo.transferir_stock(Espia(conn), pid, dep, dep, 1)
    assert not [s for s in registro if s.startswith("UPDATE")], "no se toma un producto para una operación inválida"


def test_transfer_stock_sin_tramos_es_el_camino_de_siempre_y_los_tramos_se_validan(abrir_fefo):
    from libracommerce.db.repository import repositorio_de

    pid = _producto(abrir_fefo)
    dep = _principal(abrir_fefo)
    norte = _deposito_nuevo(abrir_fefo)
    momento = datetime.datetime(2026, 9, 12)
    with abrir_fefo() as conn:
        repo = repositorio_de(conn)
        salida, entrada = transfer_stock(repo, item_id=pid, from_location_id=dep, to_location_id=norte,
                                         quantity=Decimal("3"), occurred_at=momento)
        assert (salida.lot_code, salida.expires_at, entrada.lot_code, entrada.expires_at) == (None,) * 4
        antes = _ledger(abrir_fefo)
        for tramos, mensaje in (([], "vacios"),
                                ([TramoDeTransferencia(Decimal("1"))], "no suman"),
                                ([TramoDeTransferencia(Decimal("4")), TramoDeTransferencia(Decimal("-1"))],
                                 "positivos")):
            with pytest.raises(ValueError, match=mensaje):
                transfer_stock(repo, item_id=pid, from_location_id=dep, to_location_id=norte, quantity=Decimal("3"),
                               occurred_at=momento, tramos=tramos)
    assert _ledger(abrir_fefo) == antes


def test_transfer_stock_con_tramos_escribe_el_par_de_cada_uno_en_una_transaccion(abrir_fefo):
    from libracommerce.db.repository import repositorio_de

    pid = _producto(abrir_fefo)
    dep = _principal(abrir_fefo)
    norte = _deposito_nuevo(abrir_fefo)
    momento = datetime.datetime(2026, 9, 12)
    tramos = [TramoDeTransferencia(Decimal("2"), "L1", datetime.date(2026, 10, 5)),
              TramoDeTransferencia(Decimal("1"), None, None)]
    with abrir_fefo() as conn:
        salida, entrada = transfer_stock(repositorio_de(conn), item_id=pid, from_location_id=dep,
                                         to_location_id=norte, quantity=Decimal("3"), occurred_at=momento,
                                         tramos=tramos)
    assert (salida.quantity_delta, salida.lot_code, entrada.source_id) == (Decimal("-2"), "L1", salida.id)   # el primero
    assert [m[:5] for m in _movs_de_transferencia(abrir_fefo)] == [
        ("transfer_out", -2.0, dep, "L1", "2026-10-05"), ("transfer_in", 2.0, norte, "L1", "2026-10-05"),
        ("transfer_out", -1.0, dep, None, None), ("transfer_in", 1.0, norte, None, None)]


def test_en_postgres_transferencias_simultaneas_del_mismo_marcado_no_sobreconsumen_un_lote(abrir_fefo, monkeypatch):
    _solo_postgres()
    _ensanchar_la_ventana(monkeypatch, despues_de_leer_saldos=0.3)
    pid = _producto(abrir_fefo, lotes_=(("L1", "2026-10-05", 5), ("L2", "2026-11-01", 5)))
    dep = _principal(abrir_fefo)
    norte = _deposito_nuevo(abrir_fefo)

    def transferir():
        c = core.get_connection()
        try:
            catalogo.transferir_stock(c, pid, dep, norte, 4, usuario_id=7, fecha="2026-09-12")
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()
        return "ok"

    resultados, errores = _en_hilos([transferir, transferir])
    assert not errores, errores
    assert _saldos(abrir_fefo, pid, dep) == {"L2": 2}                  # 10 − 8: ni un lote negativo
    assert _saldos(abrir_fefo, pid, norte) == {"L1": 5, "L2": 3}


def test_en_postgres_una_transferencia_espera_a_una_venta_del_mismo_producto(abrir_fefo):
    _solo_postgres()
    pid = _producto(abrir_fefo, lotes_=(("L1", "2026-10-05", 5),))
    dep = _principal(abrir_fefo)
    norte = _deposito_nuevo(abrir_fefo)
    resultado: list = []
    venta = core.get_connection()
    try:
        stock.descontar_stock_venta(venta, 1, [{"producto_id": pid, "qty": 4}], fecha=FECHA_VENTA)   # sin commit

        def transferir():
            c = core.get_connection()
            try:
                catalogo.transferir_stock(c, pid, dep, norte, 1, usuario_id=7, fecha="2026-09-12")
                resultado.append("pasó")
            except ValueError as e:
                resultado.append(str(e))
            finally:
                c.close()

        hilo = threading.Thread(target=transferir)
        hilo.start()
        time.sleep(1.0)
        assert hilo.is_alive() and not resultado, "la transferencia no esperó a la venta"
        venta.commit()
        hilo.join(10)
    finally:
        venta.close()
    assert resultado == ["pasó"]                                           # quedó 1 en L1 después de la venta


# ═════════════════════════════════════════════════════ Ajuste ════════════


def test_el_ajuste_con_lote_cuenta_ese_bucket_y_no_el_total(abrir_fefo):
    pid = _producto(abrir_fefo)                          # L1 6, L2 4
    dep = _principal(abrir_fefo)
    marca = _ultimo_id(abrir_fefo)
    _ajustar(abrir_fefo, pid, 4, lot_code="L1", expires_at="2026-10-05")       # L1 6 → 4: −2
    assert _ajustes(abrir_fefo, marca) == [(-2.0, "L1", "2026-10-05", dep)]
    assert _saldos(abrir_fefo, pid) == {"L1": 4, "L2": 4}
    assert _stock_total(abrir_fefo, pid) == 8.0
    _ajustar(abrir_fefo, pid, 4, lot_code="L1", expires_at="2026-10-05")       # ya está en 4: nada
    assert len(_ajustes(abrir_fefo, marca)) == 1


def test_el_ajuste_con_lote_de_un_lote_nuevo_lo_crea_y_el_mismo_codigo_con_otra_fecha_es_otro_bucket(abrir_fefo):
    pid = _producto(abrir_fefo)
    dep = _principal(abrir_fefo)
    marca = _ultimo_id(abrir_fefo)
    _ajustar(abrir_fefo, pid, 5, lot_code="L3", expires_at=date_iso("2026-12-01"))
    _ajustar(abrir_fefo, pid, 1, lot_code="L1", expires_at="2026-10-06")       # L1 con OTRA fecha: otro bucket
    _ajustar(abrir_fefo, pid, 2, lot_code="SINFECHA")                          # un lote sin fecha
    assert _ajustes(abrir_fefo, marca) == [(5.0, "L3", "2026-12-01", dep), (1.0, "L1", "2026-10-06", dep),
                                           (2.0, "SINFECHA", None, dep)]


def date_iso(texto):
    return datetime.date.fromisoformat(texto)       # un `date` también vale como vencimiento


def test_el_ajuste_con_lote_va_al_deposito_y_a_la_variante_pedidos(abrir_fefo):
    pid = _producto(abrir_fefo)
    norte = _deposito_nuevo(abrir_fefo)
    marca = _ultimo_id(abrir_fefo)
    _ajustar(abrir_fefo, pid, 3, lot_code="L1", expires_at="2026-10-05", deposito_id=norte)   # en norte L1 no tiene nada
    assert _ajustes(abrir_fefo, marca) == [(3.0, "L1", "2026-10-05", norte)]


def test_el_ajuste_sin_lote_que_baja_sale_por_fefo_con_una_fila_por_lote(abrir_fefo):
    pid = _producto(abrir_fefo, lotes_=(("L1", "2026-10-05", 6), ("L2", "2026-11-01", 4)), sin_lote=3)   # 13 en total
    dep = _principal(abrir_fefo)
    marca = _ultimo_id(abrir_fefo)
    _ajustar(abrir_fefo, pid, 4)                          # −9: L1 6, L2 3
    assert _ajustes(abrir_fefo, marca) == [(-6.0, "L1", "2026-10-05", dep), (-3.0, "L2", "2026-11-01", dep)]
    assert _saldos(abrir_fefo, pid) == {"L2": 1, None: 3}
    assert _stock_total(abrir_fefo, pid) == 4.0


def test_el_ajuste_sin_lote_sale_primero_el_vencido_y_el_sin_lote_ultimo(abrir_fefo):
    pid = _producto(abrir_fefo, lotes_=(("NUEVO", LEJOS, 5), ("VIEJO", VENCIDO, 2)), sin_lote=4)
    dep = _principal(abrir_fefo)
    marca = _ultimo_id(abrir_fefo)
    _ajustar(abrir_fefo, pid, 1)                          # −10
    assert _ajustes(abrir_fefo, marca) == [(-2.0, "VIEJO", VENCIDO, dep), (-5.0, "NUEVO", LEJOS, dep),
                                           (-3.0, None, None, dep)]


def test_el_ajuste_sin_lote_que_sube_entra_en_el_sin_lote(abrir_fefo):
    pid = _producto(abrir_fefo)
    dep = _principal(abrir_fefo)
    marca = _ultimo_id(abrir_fefo)
    _ajustar(abrir_fefo, pid, 13)
    assert _ajustes(abrir_fefo, marca) == [(3.0, None, None, dep)]
    assert _saldos(abrir_fefo, pid) == {"L1": 6, "L2": 4, None: 3}


def test_un_ajuste_sin_diferencia_no_escribe_nada(abrir_fefo):
    pid = _producto(abrir_fefo)
    marca = _ultimo_id(abrir_fefo)
    _ajustar(abrir_fefo, pid, 10)
    assert _ajustes(abrir_fefo, marca) == []


def test_un_producto_sin_marcar_ajusta_como_siempre_y_no_acepta_lote(abrir_fefo):
    pid = _producto(abrir_fefo, "Pan", lotes_=(), sin_lote=10, marcado=False)
    dep = _principal(abrir_fefo)
    marca = _ultimo_id(abrir_fefo)
    _ajustar(abrir_fefo, pid, 7)
    _ajustar(abrir_fefo, pid, 9)
    assert _ajustes(abrir_fefo, marca) == [(-3.0, None, None, dep), (2.0, None, None, dep)]
    antes = _ledger(abrir_fefo)
    with abrir_fefo() as conn:
        with pytest.raises(ValueError, match="sólo se pueden usar con un producto que vence"):
            stock.ajustar_stock(conn, pid, 5, "conteo", lot_code="L1")
        with pytest.raises(ValueError, match="sólo se pueden usar con un producto que vence"):
            stock.ajustar_stock(conn, pid, 5, "conteo", expires_at="2026-10-05")
        conn.rollback()
    assert _ledger(abrir_fefo) == antes


def test_un_lote_invalido_en_el_ajuste_es_value_error_y_no_escribe(abrir_fefo):
    pid = _producto(abrir_fefo)
    antes = _ledger(abrir_fefo)
    with abrir_fefo() as conn:
        for kw in ({"lot_code": "   "}, {"lot_code": "L1", "expires_at": "no-es-fecha"}):
            with pytest.raises(ValueError):
                stock.ajustar_stock(conn, pid, 5, "conteo", **kw)
        conn.rollback()
    assert _ledger(abrir_fefo) == antes


def test_limite_sin_deposito_id_el_total_es_de_todos_los_depositos_y_el_fefo_corre_donde_se_escribe(abrir_fefo):
    """🔴 Límite PREEXISTENTE, conservado: sin `deposito_id` el valor se compara contra el total de todos los depósitos pero
    la fila se escribe en el depósito por defecto. Para un marcado con el stock repartido el FEFO corre ahí, no donde está
    el resto: el «sin lote» del principal queda negativo aunque otro depósito tenga lotes."""
    pid = _producto(abrir_fefo, lotes_=(("L1", "2026-10-05", 2),))
    dep = _principal(abrir_fefo)
    norte = _deposito_nuevo(abrir_fefo)
    with abrir_fefo() as conn:
        stock.add_movimiento_stock(conn, pid, "entrada", 8, "carga", fecha=PREVIA, lot_code="L2", expires_at="2026-11-01",
                                   deposito_id=norte)
        conn.commit()
    marca = _ultimo_id(abrir_fefo)
    _ajustar(abrir_fefo, pid, 6)                          # total 10 → 6: −4, en el depósito por defecto
    assert _ajustes(abrir_fefo, marca) == [(-2.0, "L1", "2026-10-05", dep), (-2.0, None, None, dep)]
    assert _saldos(abrir_fefo, pid, dep) == {None: -2} and _saldos(abrir_fefo, pid, norte) == {"L2": 8}
    # Con `deposito_id` el ajuste es de ese depósito y el FEFO corre sobre él.
    marca = _ultimo_id(abrir_fefo)
    _ajustar(abrir_fefo, pid, 5, deposito_id=norte)       # norte 8 → 5: −3 de L2
    assert _ajustes(abrir_fefo, marca) == [(-3.0, "L2", "2026-11-01", norte)]


def test_el_ajuste_con_variante_mira_solo_los_buckets_de_esa_variante(abrir_fefo):
    pid = _producto(abrir_fefo, lotes_=(("L1", "2026-10-05", 5),))
    dep = _principal(abrir_fefo)
    with abrir_fefo() as conn:
        v = catalogo.create_variante(conn, pid, "Y-1", "Frutilla")["id"]
        stock.add_movimiento_stock(conn, pid, "entrada", 4, "carga", fecha=PREVIA, lot_code="V1", expires_at=LEJOS,
                                   variant_id=v)
        conn.commit()
    marca = _ultimo_id(abrir_fefo)
    _ajustar(abrir_fefo, pid, 1, variant_id=v)           # −3 de la variante: L1 (del producto sin variante) no se toca
    assert _ajustes(abrir_fefo, marca) == [(-3.0, "V1", LEJOS, dep)]
    assert _saldos(abrir_fefo, pid, variante=None) == {"L1": 5}


def test_el_ajuste_de_un_marcado_toma_el_producto_y_el_de_un_sin_marcar_no(abrir_fefo):
    marcado = _producto(abrir_fefo, "Yogur")
    comun = _producto(abrir_fefo, "Pan", lotes_=(), sin_lote=10, marcado=False)
    tomados: list = []
    with abrir_fefo() as conn:
        original = conn.execute

        class Espia:
            def __getattr__(self, nombre):
                return getattr(conn, nombre)

            def execute(self, sql, params=()):
                if sql.startswith("UPDATE catalog_items SET tracks_expiry = tracks_expiry"):
                    tomados.append(params[0])
                return original(sql, params)

        stock.ajustar_stock(Espia(), comun, 5, "conteo")
        assert tomados == []
        stock.ajustar_stock(Espia(), marcado, 5, "conteo")
        conn.commit()
    assert tomados == [marcado]


def test_en_postgres_dos_ajustes_a_la_baja_del_mismo_marcado_no_consumen_dos_veces_el_lote(abrir_fefo, monkeypatch):
    _solo_postgres()
    _ensanchar_la_ventana(monkeypatch, despues_de_leer_saldos=0.3)
    pid = _producto(abrir_fefo, lotes_=(("L1", "2026-10-05", 5), ("L2", "2026-11-01", 5)))

    def ajustar_en_conexion_propia(nuevo):
        def trabajo():
            c = core.get_connection()
            try:
                stock.ajustar_stock(c, pid, nuevo, "conteo", fecha="2026-09-12")
                c.commit()
            except Exception:
                c.rollback()
                raise
            finally:
                c.close()
        return trabajo

    # Cada conteo lleva el TOTAL a 6 y a 2: se serializan (el segundo ve lo que dejó el primero), sin lote negativo.
    _, errores = _en_hilos([ajustar_en_conexion_propia(6), ajustar_en_conexion_propia(6)])
    assert not errores, errores
    assert _stock_total(abrir_fefo, pid) == 6.0                       # el segundo ya encontró 6: no ajustó nada de más
    assert all(s >= 0 for s in _saldos(abrir_fefo, pid).values())


# ═══════════════════════════════════════════ HTTP: ajuste con lote ═══════


def _app(abrir, *, avisos=False, con_lotes=False, merma=False, **kw) -> TestClient:
    app = FastAPI()
    app.include_router(build_productos_router(conexion=abrir, usuario_actual=_usuario))
    app.include_router(build_stock_router(conexion=abrir, usuario_actual=_usuario,
                                          opciones=OpcionesStock(
                                              por_deposito=True, con_lotes=con_lotes,
                                              motivos_merma=("Rotura", "Vencimiento") if merma else ())))
    app.include_router(build_depositos_router(conexion=abrir, usuario_actual=_usuario))
    app.include_router(build_ventas_router(conexion=abrir, usuario_actual=_usuario,
                                           opciones=OpcionesVentas(con_avisos_de_vencimiento=avisos, **kw)))
    return TestClient(app)


def test_http_el_ajuste_con_lote_cuenta_ese_lote_y_la_respuesta_es_la_de_siempre(abrir_fefo):
    pid = _producto(abrir_fefo)
    dep = _principal(abrir_fefo)
    client = _app(abrir_fefo, con_lotes=True)
    marca = _ultimo_id(abrir_fefo)
    r = client.post(f"/api/stock/{pid}/ajuste", json={"modo": "absoluto", "cantidad": 4, "lot_code": "L1",
                                                      "expires_at": "2026-10-05", "deposito_id": dep, "fecha": "2026-09-12"})
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"producto", "stock_actual", "stock_deposito"} and r.json()["stock_actual"] == 8.0
    assert _ajustes(abrir_fefo, marca) == [(-2.0, "L1", "2026-10-05", dep)]


def test_http_ajuste_con_lote_en_un_producto_sin_marcar_en_otro_modo_o_invalido_es_422(abrir_fefo):
    marcado = _producto(abrir_fefo)
    comun = _producto(abrir_fefo, "Pan", lotes_=(), sin_lote=10, marcado=False)
    client = _app(abrir_fefo, con_lotes=True)
    antes = _ledger(abrir_fefo)
    casos = [
        (comun, {"modo": "absoluto", "cantidad": 5, "lot_code": "L1"}),                      # no está marcado
        (comun, {"modo": "entrada", "cantidad": 5, "lot_code": "L1", "expires_at": "2026-10-05"}),   # no está marcado
        (marcado, {"modo": "salida", "cantidad": 1, "expires_at": "2026-10-05"}),                   # otro modo
        (marcado, {"modo": "entrada", "cantidad": 5, "lot_code": "  "}),
        (marcado, {"modo": "absoluto", "cantidad": 5, "lot_code": "  "}),                    # lote vacío
        (marcado, {"modo": "absoluto", "cantidad": 5, "lot_code": "L1", "expires_at": "ayer"}),
    ]
    for pid, cuerpo in casos:
        r = client.post(f"/api/stock/{pid}/ajuste", json=cuerpo)
        assert r.status_code == 422, (cuerpo, r.text)
    assert _ledger(abrir_fefo) == antes


def test_http_sin_la_opcion_el_esquema_y_las_respuestas_del_ajuste_son_los_de_siempre(abrir_fefo):
    marcado = _producto(abrir_fefo)
    apagado, prendido = _app(abrir_fefo), _app(abrir_fefo, con_lotes=True)
    esquema = apagado.get("/openapi.json").json()["components"]["schemas"]["AjustePayload"]
    assert "lot_code" not in esquema["properties"] and "expires_at" not in esquema["properties"]
    assert "lot_code" in prendido.get("/openapi.json").json()["components"]["schemas"]["AjusteConLotePayload"]["properties"]
    # Apagada, el lote del cuerpo se ignora (como cualquier campo desconocido): el ajuste es el del total, por FEFO.
    marca = _ultimo_id(abrir_fefo)
    r = apagado.post(f"/api/stock/{marcado}/ajuste", json={"modo": "absoluto", "cantidad": 9, "lot_code": "L2",
                                                           "expires_at": "2026-11-01", "fecha": "2026-09-12"})
    assert r.status_code == 200
    assert [a[:3] for a in _ajustes(abrir_fefo, marca)] == [(-1.0, "L1", "2026-10-05")]
    # Y sin campos de lote, las dos aplicaciones responden igual.
    r1 = apagado.post(f"/api/stock/{marcado}/ajuste", json={"modo": "absoluto", "cantidad": 7, "fecha": "2026-09-12"})
    r2 = prendido.post(f"/api/stock/{marcado}/ajuste", json={"modo": "absoluto", "cantidad": 7, "fecha": "2026-09-12"})
    assert r1.status_code == r2.status_code == 200 and r1.content == r2.content


def test_http_la_transferencia_de_un_marcado_responde_lo_de_siempre(abrir_fefo):
    pid = _producto(abrir_fefo)
    dep = _principal(abrir_fefo)
    norte = _deposito_nuevo(abrir_fefo)
    r = _app(abrir_fefo).post("/api/depositos/transferir", json={
        "producto_id": pid, "origen_id": dep, "destino_id": norte, "cantidad": 8, "fecha": "2026-09-12"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "cantidad": 8, "origen": {"id": dep, "nombre": r.json()["origen"]["nombre"],
                                                              "stock": 2.0},
                        "destino": {"id": norte, "nombre": "Norte", "stock": 8.0}}
    assert _saldos(abrir_fefo, pid, norte) == {"L1": 6, "L2": 2}


# ═════════════════════════════════════════════ HTTP: avisos de vencimiento ══


def _post_venta(client, pid, qty, **extra):
    cuerpo = {"fecha": FECHA_VENTA, "items": [{"nombre": "Yogur", "qty": qty, "precio": 100.0, "producto_id": pid}],
              "pagos": [{"medio": "efectivo", "monto": qty * 100.0}], **extra}
    r = client.post("/api/ventas", json=cuerpo)
    assert r.status_code == 200, r.text
    return r


def test_avisos_apagado_las_respuestas_y_el_openapi_son_los_de_hoy(abrir_fefo):
    pid = _producto(abrir_fefo, lotes_=(("VIEJO", VENCIDO, 5),))
    apagado, prendido = _app(abrir_fefo), _app(abrir_fefo, avisos=True)
    r = _post_venta(apagado, pid, 7)                     # un vencido y un faltante: con la opción habría avisos
    assert "avisos" not in r.json()
    vid = r.json()["id"]
    assert "avisos" not in apagado.get(f"/api/ventas/{vid}").json()
    assert apagado.get(f"/api/ventas/{vid}").content == r.content       # POST y GET son los mismos bytes de siempre
    assert prendido.get(f"/api/ventas/{vid}").json()["avisos"]          # con la opción SÍ hay avisos
    # La ruta no existe: lo único que queda es `GET /{vid}`, que captura el path (405 para un POST, como hoy).
    assert apagado.post("/api/ventas/plan-salida", json={"items": []}).status_code == 405
    apagado_openapi = apagado.get("/openapi.json").content
    assert b"plan-salida" not in apagado_openapi and b"PlanSalida" not in apagado_openapi
    assert b"avisos" not in apagado_openapi
    paths_on = prendido.get("/openapi.json").json()["paths"]
    paths_off = apagado.get("/openapi.json").json()["paths"]
    assert set(paths_on) - set(paths_off) == {"/api/ventas/plan-salida"} and set(paths_off) - set(paths_on) == set()
    # Ni la respuesta ni el resto del esquema cambian con la opción prendida (sólo se suma la ruta y sus dos cuerpos).
    off, on = apagado.get("/openapi.json").json(), prendido.get("/openapi.json").json()
    assert {k: v for k, v in on["paths"].items() if k != "/api/ventas/plan-salida"} == off["paths"]
    assert {k: v for k, v in on["components"]["schemas"].items()
            if k not in ("PlanSalidaPayload", "PlanSalidaLinea")} == off["components"]["schemas"]


def test_avisos_prendido_sin_avisos_la_clave_no_esta_y_los_bytes_son_los_de_siempre(abrir_fefo):
    perecedero = _producto(abrir_fefo, lotes_=(("LARGO", LEJOS, 50),))
    comun = _producto(abrir_fefo, "Pan", lotes_=(), sin_lote=50, marcado=False)
    apagado, prendido = _app(abrir_fefo), _app(abrir_fefo, avisos=True)
    for pid in (perecedero, comun):
        r = _post_venta(prendido, pid, 2)
        assert "avisos" not in r.json()
        assert apagado.get(f"/api/ventas/{r.json()['id']}").content == r.content
        assert prendido.get(f"/api/ventas/{r.json()['id']}").content == r.content


def test_avisos_de_un_lote_vencido_por_vencer_y_faltante_sin_lote(abrir_fefo):
    pid = _producto(abrir_fefo, lotes_=(("VIEJO", VENCIDO, 2), ("PRONTO", "2026-10-05", 3), ("LARGO", LEJOS, 50)))
    cliente = _app(abrir_fefo, avisos=True)
    dep = _principal(abrir_fefo)
    r = _post_venta(cliente, pid, 6)                      # VIEJO 2 (vencido), PRONTO 3 (en 5 días), LARGO 1
    avisos = r.json()["avisos"]
    base = {"producto_id": pid, "nombre": "Yogur", "deposito_id": dep, "variante_id": None}
    assert avisos == [
        {**base, "tipo": "lote_vencido", "lote": "VIEJO", "vence": VENCIDO, "dias_para_vencer": -20, "cantidad": 2},
        {**base, "tipo": "por_vencer", "lote": "PRONTO", "vence": "2026-10-05", "dias_para_vencer": 5, "cantidad": 3},
    ]
    # El mismo aviso en el detalle de la venta.
    assert cliente.get(f"/api/ventas/{r.json()['id']}").json()["avisos"] == avisos
    # El faltante: lo que ningún lote ni el «sin lote» tenía.
    pid2 = _producto(abrir_fefo, "Leche", lotes_=(("L1", LEJOS, 2),))
    r2 = _post_venta(cliente, pid2, 5)
    assert [(a["tipo"], a["lote"], a["cantidad"]) for a in r2.json()["avisos"]] == [("faltante_sin_lote", None, 3)]


def test_avisos_umbral_de_15_dias_cerrado(abrir_fefo):
    pid = _producto(abrir_fefo, lotes_=(("HOY", VENCE_HOY, 1), ("EN15", EN_15, 1), ("EN16", EN_16, 1)))
    r = _post_venta(_app(abrir_fefo, avisos=True), pid, 3)
    assert [(a["tipo"], a["lote"], a["dias_para_vencer"]) for a in r.json()["avisos"]] == [
        ("por_vencer", "HOY", 0), ("por_vencer", "EN15", 15)]       # EN16 queda afuera


def test_plan_salida_dice_que_lote_saldria_y_no_escribe_ni_sobrecuenta(abrir_fefo):
    pid = _producto(abrir_fefo, lotes_=(("VIEJO", VENCIDO, 2), ("L1", "2026-10-05", 6), ("L2", "2026-11-01", 4)))
    dep = _principal(abrir_fefo)
    cliente = _app(abrir_fefo, avisos=True)
    cuerpo = {"items": [{"producto_id": pid, "qty": 9, "variante_id": None}]}
    antes = _ledger(abrir_fefo)
    r = cliente.post("/api/ventas/plan-salida", json=cuerpo)
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["hoy"] == "2026-09-30" and plan["dias"] == 15
    assert [(s["lote"], s["cantidad"], s["estado"], s["deposito_id"]) for s in plan["salidas"]] == [
        ("VIEJO", 2, "vencido", dep), ("L1", 6, "vigente", dep), ("L2", 1, "vigente", dep)]
    assert [(a["tipo"], a["lote"]) for a in plan["avisos"]] == [("lote_vencido", "VIEJO"), ("por_vencer", "L1")]
    # Lectura pura: ni una fila de más, y pedirlo dos veces (o diez) da lo mismo: no se sobrecuenta.
    assert _ledger(abrir_fefo) == antes
    assert all(cliente.post("/api/ventas/plan-salida", json=cuerpo).json() == plan for _ in range(3))
    assert _ledger(abrir_fefo) == antes
    # Y es lo que la venta real escribe.
    vid = _post_venta(cliente, pid, 9).json()["id"]
    with abrir_fefo() as conn:
        real = [(f["lot_code"], -float(f["quantity_delta"])) for f in conn.execute(
            "SELECT lot_code, quantity_delta FROM stock_movements WHERE source_id = ? AND reason_code = 'venta' "
            "ORDER BY id", (vid,))]
    assert real == [(s["lote"], s["cantidad"]) for s in plan["salidas"]]


def test_plan_salida_respeta_el_deposito_y_las_lineas_en_secuencia(abrir_fefo):
    pid = _producto(abrir_fefo)                           # L1 6 y L2 4 en el principal
    norte = _deposito_nuevo(abrir_fefo)
    with abrir_fefo() as conn:
        stock.add_movimiento_stock(conn, pid, "entrada", 3, "carga", fecha=PREVIA, lot_code="N1", expires_at=LEJOS,
                                   deposito_id=norte)
        conn.commit()
    cliente = _app(abrir_fefo, avisos=True)
    items = [{"producto_id": pid, "qty": 4}, {"producto_id": pid, "qty": 4}]
    en_norte = cliente.post("/api/ventas/plan-salida", json={"items": items, "deposito_id": norte}).json()
    assert [(s["linea"], s["lote"], s["cantidad"], s["faltante"]) for s in en_norte["salidas"]] == [
        (0, "N1", 3, 0), (0, None, 1, 1), (1, None, 4, 4)]
    por_defecto = cliente.post("/api/ventas/plan-salida", json={"items": items}).json()
    assert [(s["linea"], s["lote"], s["cantidad"]) for s in por_defecto["salidas"]] == [
        (0, "L1", 4), (1, "L1", 2), (1, "L2", 2)]      # la segunda línea ve lo que dejó la primera


def test_plan_salida_con_lineas_invalidas_es_422(abrir_fefo):
    pid = _producto(abrir_fefo)
    cliente = _app(abrir_fefo, avisos=True)
    antes = _ledger(abrir_fefo)
    casos = [
        {"items": []},
        {"items": [{"producto_id": pid, "qty": 0}]},
        {"items": [{"producto_id": pid, "qty": -1}]},
        {"items": [{"producto_id": pid, "qty": 1}, {"producto_id": pid, "qty": 0}]},
        {"items": [{"producto_id": 999999, "qty": 1}]},
        {"items": [{"producto_id": pid, "qty": 1}], "deposito_id": 999999},
        {"items": [{"qty": 1}]},
        {"items": [{"producto_id": pid, "qty": "mucho"}]},
    ]
    for cuerpo in casos:
        r = cliente.post("/api/ventas/plan-salida", json=cuerpo)
        assert r.status_code == 422, (cuerpo, r.status_code, r.text)
    assert _ledger(abrir_fefo) == antes


def test_en_postgres_el_plan_de_salida_no_espera_a_quien_tiene_tomado_el_producto(abrir_fefo):
    """Lectura pura: no bloquea. Con el producto tomado por otra transacción (una venta a medio hacer) el plan responde
    igual, sin esperar."""
    _solo_postgres()
    pid = _producto(abrir_fefo)
    cliente = _app(abrir_fefo, avisos=True)
    otra = core.get_connection()
    try:
        lotes.tomar_productos(otra, [pid])                # el bloqueo de una venta en curso
        resultado: list = []
        hilo = threading.Thread(target=lambda: resultado.append(
            cliente.post("/api/ventas/plan-salida", json={"items": [{"producto_id": pid, "qty": 3}]}).status_code))
        hilo.start()
        hilo.join(5)
        assert not hilo.is_alive() and resultado == [200], "el plan de salida esperó un bloqueo"
    finally:
        otra.rollback()
        otra.close()


# ═══════════════ Las salidas manuales del endpoint de ajuste (salida y merma) siguen el lote ═══════════════


def _filas_de_movimiento(abrir, desde):
    """`(movement_type, reason_code, cantidad, lote, vence, nota)` de lo escrito después de `desde`."""
    with abrir() as conn:
        return [(f["movement_type"], f["reason_code"], float(f["quantity_delta"]), f["lot_code"], f["expires_at"],
                 f["note"]) for f in conn.execute(
            "SELECT movement_type, reason_code, quantity_delta, lot_code, expires_at, note FROM stock_movements "
            "WHERE id > ? ORDER BY id", (desde,))]


def test_la_salida_manual_de_un_marcado_sale_por_fefo_y_conserva_tipo_y_referencia(abrir_fefo):
    pid = _producto(abrir_fefo, lotes_=(("NUEVO", LEJOS, 5), ("VIEJO", VENCIDO, 2)), sin_lote=4)
    marca = _ultimo_id(abrir_fefo)
    r = _app(abrir_fefo).post(f"/api/stock/{pid}/ajuste", json={"modo": "salida", "cantidad": 9, "referencia": "rotura",
                                                                 "fecha": "2026-09-12"})
    assert r.status_code == 200 and r.json()["stock_actual"] == 2.0
    assert _filas_de_movimiento(abrir_fefo, marca) == [
        ("adjustment", "salida", -2.0, "VIEJO", VENCIDO, "rotura"),      # el vencido primero
        ("adjustment", "salida", -5.0, "NUEVO", LEJOS, "rotura"),
        ("adjustment", "salida", -2.0, None, None, "rotura"),            # el «sin lote», último
    ]
    assert _saldos(abrir_fefo, pid) == {None: 2}


def test_la_merma_manual_de_un_marcado_sale_por_fefo_y_lo_que_no_tiene_respaldo_va_sin_lote(abrir_fefo):
    pid = _producto(abrir_fefo, lotes_=(("L1", "2026-10-05", 3),))
    marca = _ultimo_id(abrir_fefo)
    r = _app(abrir_fefo, merma=True).post(f"/api/stock/{pid}/ajuste", json={
        "modo": "merma", "cantidad": 5, "motivo": "Rotura", "fecha": "2026-09-12"})
    assert r.status_code == 200
    assert _filas_de_movimiento(abrir_fefo, marca) == [
        ("waste", "merma", -3.0, "L1", "2026-10-05", "Merma: Rotura"),
        ("waste", "merma", -2.0, None, None, "Merma: Rotura"),           # el faltante, a una fila «sin lote»
    ]
    assert _saldos(abrir_fefo, pid) == {None: -2}


def test_la_salida_y_la_merma_manuales_respetan_deposito_y_variante(abrir_fefo):
    pid = _producto(abrir_fefo, lotes_=(("L1", "2026-10-05", 5),))
    norte = _deposito_nuevo(abrir_fefo)
    with abrir_fefo() as conn:
        v = catalogo.create_variante(conn, pid, "Y-1", "Frutilla")["id"]
        stock.add_movimiento_stock(conn, pid, "entrada", 4, "carga", fecha=PREVIA, lot_code="V1", expires_at=VENCIDO,
                                   deposito_id=norte, variant_id=v)
        conn.commit()
    marca = _ultimo_id(abrir_fefo)
    r = _app(abrir_fefo).post(f"/api/stock/{pid}/ajuste", json={"modo": "salida", "cantidad": 3, "deposito_id": norte,
                                                                 "variant_id": v, "fecha": "2026-09-12"})
    assert r.status_code == 200
    assert _filas_de_movimiento(abrir_fefo, marca)[0][:5] == ("adjustment", "salida", -3.0, "V1", VENCIDO)
    assert _saldos(abrir_fefo, pid, norte, variante=v) == {"V1": 1} and _saldos(abrir_fefo, pid, variante=None) == {"L1": 5}


def test_un_sin_marcar_y_un_marcado_sin_lotes_escriben_la_misma_salida_y_merma_manual_de_siempre(abrir_fefo):
    comun = _producto(abrir_fefo, "Pan", lotes_=(), sin_lote=10, marcado=False)
    vacio = _producto(abrir_fefo, "Sal", lotes_=(), sin_lote=10)          # marcado, sin ningún lote
    cliente = _app(abrir_fefo, merma=True)
    resultados = []
    for pid in (comun, vacio):
        marca = _ultimo_id(abrir_fefo)
        for cuerpo in ({"modo": "salida", "cantidad": 3, "referencia": "uso"},
                       {"modo": "merma", "cantidad": 2.5, "motivo": "Rotura"}):
            assert cliente.post(f"/api/stock/{pid}/ajuste", json={**cuerpo, "fecha": "2026-09-12"}).status_code == 200
        resultados.append(_filas_de_movimiento(abrir_fefo, marca))
    assert resultados[0] == resultados[1] == [
        ("adjustment", "salida", -3.0, None, None, "uso"), ("waste", "merma", -2.5, None, None, "Merma: Rotura")]


def test_la_salida_manual_de_un_sin_marcar_es_la_llamada_de_siempre_y_no_toma_el_producto(abrir_fefo):
    comun = _producto(abrir_fefo, "Pan", lotes_=(), sin_lote=10, marcado=False)
    registro: list[str] = []

    class Espia:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, nombre):
            return getattr(self._conn, nombre)

        def execute(self, sql, params=()):
            registro.append(" ".join(sql.split()))
            return self._conn.execute(sql, params)

    with abrir_fefo() as conn:
        stock.salida_manual(Espia(conn), comun, "salida", 3, "uso", usuario_id=7, fecha="2026-09-12")
        conn.commit()
    assert not [s for s in registro if s.startswith("UPDATE")] and not [s for s in registro if "GROUP BY" in s]
    assert [s for s in registro if s.startswith("INSERT INTO stock_movements")] == [
        " ".join("""INSERT INTO stock_movements
           (item_id, variant_id, location_id, movement_type, quantity_delta, occurred_at,
            source_type, source_id, note, created_by, reason_code)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""".split())]


def test_la_entrada_manual_sin_lote_de_un_marcado_entra_sin_lote_y_con_lote_va_a_ese_bucket(abrir_fefo):
    pid = _producto(abrir_fefo)
    dep = _principal(abrir_fefo)
    cliente = _app(abrir_fefo, con_lotes=True)
    marca = _ultimo_id(abrir_fefo)
    assert cliente.post(f"/api/stock/{pid}/ajuste", json={"modo": "entrada", "cantidad": 3, "fecha": "2026-09-12"}
                        ).status_code == 200
    r = cliente.post(f"/api/stock/{pid}/ajuste", json={"modo": "entrada", "cantidad": 2, "factor": 6, "unidad_compra": "caja",
                                                       "lot_code": "L3", "expires_at": "2026-12-01", "deposito_id": dep,
                                                       "fecha": "2026-09-12"})
    assert r.status_code == 200, r.text
    assert [f[:5] for f in _filas_de_movimiento(abrir_fefo, marca)] == [
        ("adjustment", "entrada", 3.0, None, None), ("adjustment", "entrada", 12.0, "L3", "2026-12-01")]
    assert _saldos(abrir_fefo, pid) == {"L1": 6, "L2": 4, None: 3, "L3": 12}


# ═══════════ Sin variante, con stock por variante: el ajuste y la salida manual piden la variante ═══════════


def _con_variante(abrir, *, marcado=True, cantidad=10, deposito=None):
    """Un producto cuyo stock está SÓLO en la variante A (un lote `VA`), y A."""
    pid = _producto(abrir, "Remera", lotes_=(), marcado=marcado)
    with abrir() as conn:
        v = catalogo.create_variante(conn, pid, "R-A", "A")["id"]
        stock.add_movimiento_stock(conn, pid, "entrada", cantidad, "carga", fecha=PREVIA, lot_code="VA",
                                   expires_at=LEJOS, variant_id=v, deposito_id=deposito)
        conn.commit()
    return pid, v


def test_ajustar_un_marcado_sin_variante_con_stock_por_variante_es_un_error_y_no_escribe(abrir_fefo):
    pid, v = _con_variante(abrir_fefo)
    dep = _principal(abrir_fefo)
    antes = _ledger(abrir_fefo)
    with abrir_fefo() as conn:
        for nuevo in (5, 15):                               # baja y sube
            with pytest.raises(lotes.VarianteRequerida, match="indicá la variante"):
                stock.ajustar_stock(conn, pid, nuevo, "conteo", deposito_id=dep)
        with pytest.raises(lotes.VarianteRequerida):
            stock.ajustar_stock(conn, pid, 5, "conteo")      # sin depósito: el por defecto
        with pytest.raises(lotes.VarianteRequerida):
            stock.salida_manual(conn, pid, "salida", 3, "uso", deposito_id=dep)
        conn.rollback()
    assert _ledger(abrir_fefo) == antes and _saldos(abrir_fefo, pid, variante=v) == {"VA": 10}


def test_http_ajuste_salida_y_merma_sin_variante_con_stock_por_variante_son_422_y_no_escriben(abrir_fefo):
    pid, v = _con_variante(abrir_fefo)
    dep = _principal(abrir_fefo)
    cliente = _app(abrir_fefo, merma=True)
    antes = _ledger(abrir_fefo)
    for cuerpo in ({"modo": "absoluto", "cantidad": 5}, {"modo": "salida", "cantidad": 2},
                   {"modo": "merma", "cantidad": 2, "motivo": "Rotura"}):
        r = cliente.post(f"/api/stock/{pid}/ajuste", json={**cuerpo, "deposito_id": dep})
        assert r.status_code == 422 and "indicá la variante" in r.json()["detail"], (cuerpo, r.text)
    assert _ledger(abrir_fefo) == antes


def test_con_variante_explicita_el_ajuste_y_la_salida_manual_bajan_el_lote_de_esa_variante(abrir_fefo):
    pid, v = _con_variante(abrir_fefo)
    dep = _principal(abrir_fefo)
    cliente = _app(abrir_fefo, merma=True)
    marca = _ultimo_id(abrir_fefo)
    r = cliente.post(f"/api/stock/{pid}/ajuste", json={"modo": "absoluto", "cantidad": 5, "deposito_id": dep,
                                                      "variant_id": v, "fecha": "2026-09-12"})
    assert r.status_code == 200, r.text
    assert _saldos(abrir_fefo, pid, variante=v) == {"VA": 5}
    assert cliente.post(f"/api/stock/{pid}/ajuste", json={"modo": "salida", "cantidad": 2, "deposito_id": dep,
                                                         "variant_id": v, "fecha": "2026-09-12"}).status_code == 200
    assert _saldos(abrir_fefo, pid, variante=v) == {"VA": 3}
    assert [f[:5] for f in _filas_de_movimiento(abrir_fefo, marca)] == [
        ("adjustment", "ajuste", -5.0, "VA", LEJOS), ("adjustment", "salida", -2.0, "VA", LEJOS)]


def test_sin_stock_en_variantes_el_ajuste_y_la_salida_sin_variante_funcionan_como_siempre(abrir_fefo):
    pid, v = _con_variante(abrir_fefo)
    dep = _principal(abrir_fefo)
    norte = _deposito_nuevo(abrir_fefo)
    with abrir_fefo() as conn:                               # la variante se vació: saldo 0 en su lote
        stock.add_movimiento_stock(conn, pid, "salida", -10, "uso", fecha=PREVIA, lot_code="VA", expires_at=LEJOS,
                                   variant_id=v)
        stock.add_movimiento_stock(conn, pid, "entrada", 6, "carga", fecha=PREVIA, lot_code="N1", expires_at=LEJOS)
        stock.add_movimiento_stock(conn, pid, "entrada", 9, "carga", fecha=PREVIA, lot_code="VN", expires_at=LEJOS,
                                   variant_id=v, deposito_id=norte)     # y hay stock por variante… en OTRO depósito
        conn.commit()
    marca = _ultimo_id(abrir_fefo)
    _ajustar(abrir_fefo, pid, 4, deposito_id=dep, variant_id=None)
    with abrir_fefo() as conn:
        stock.salida_manual(conn, pid, "salida", 1, "uso", deposito_id=dep)
        conn.commit()
    assert [a[:3] for a in _ajustes(abrir_fefo, marca)][0] == (-2.0, "N1", LEJOS)
    assert _saldos(abrir_fefo, pid, dep, variante=None) == {"N1": 3}


def test_un_sin_marcar_con_stock_por_variante_ajusta_como_siempre(abrir_fefo):
    pid, v = _con_variante(abrir_fefo, marcado=False)
    dep = _principal(abrir_fefo)
    marca = _ultimo_id(abrir_fefo)
    _ajustar(abrir_fefo, pid, 4, deposito_id=dep)            # compara el total (10) y escribe −6 sin variante, sin lote
    with abrir_fefo() as conn:
        stock.salida_manual(conn, pid, "salida", 1, "uso", deposito_id=dep)
        conn.commit()
    assert _ajustes(abrir_fefo, marca) == [(-6.0, None, None, dep)]


def test_la_transferencia_es_por_variante_y_no_tiene_este_problema(abrir_fefo):
    """La guarda de `transfer_stock` mira `variant_id IS NULL` cuando no se pasa variante, igual que el FEFO: no hay
    diferencia entre lo que se compara y lo que se planifica. Sin stock en la variante NULL, la guarda rechaza."""
    pid, v = _con_variante(abrir_fefo)
    dep = _principal(abrir_fefo)
    norte = _deposito_nuevo(abrir_fefo)
    with abrir_fefo() as conn:
        with pytest.raises(ValueError, match="Stock insuficiente"):
            catalogo.transferir_stock(conn, pid, dep, norte, 3)
        conn.rollback()
    _transferir(abrir_fefo, pid, dep, norte, 3, variant_id=v)
    assert _saldos(abrir_fefo, pid, norte, variante=v) == {"VA": 3}


# ── Concurrencia de la salida manual (PostgreSQL) ───────────────────────


def test_en_postgres_dos_salidas_manuales_simultaneas_del_mismo_marcado_no_sobreconsumen_un_lote(abrir_fefo, monkeypatch):
    _solo_postgres()
    _ensanchar_la_ventana(monkeypatch, despues_de_leer_saldos=0.3)
    pid = _producto(abrir_fefo, lotes_=(("L1", "2026-10-05", 5), ("L2", "2026-11-01", 5)))

    def salida():
        c = core.get_connection()
        try:
            stock.salida_manual(c, pid, "salida", 4, "uso", fecha="2026-09-12")
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    _, errores = _en_hilos([salida, salida])
    assert not errores, errores
    assert _saldos(abrir_fefo, pid) == {"L2": 2}          # 10 − 8: ni un lote negativo


def test_en_postgres_una_merma_manual_espera_a_una_venta_del_mismo_producto(abrir_fefo):
    _solo_postgres()
    pid = _producto(abrir_fefo, lotes_=(("L1", "2026-10-05", 5),))
    resultado: list = []
    venta = core.get_connection()
    try:
        stock.descontar_stock_venta(venta, 1, [{"producto_id": pid, "qty": 4}], fecha=FECHA_VENTA)   # sin commit

        def merma():
            c = core.get_connection()
            try:
                stock.salida_manual(c, pid, "merma", 3, "Merma: Rotura", fecha="2026-09-12")
                c.commit()
                resultado.append("pasó")
            finally:
                c.close()

        hilo = threading.Thread(target=merma)
        hilo.start()
        time.sleep(1.0)
        assert hilo.is_alive() and not resultado, "la merma no esperó a la venta"
        venta.commit()
        hilo.join(10)
    finally:
        venta.close()
    assert resultado == ["pasó"]
    assert _saldos(abrir_fefo, pid) == {None: -2}         # vio L1 en 1: 1 del lote y 2 de faltante


def test_http_si_falla_el_calculo_de_avisos_la_venta_confirmada_se_devuelve_igual(abrir_fefo, monkeypatch):
    """Hallazgo de Codex (2026-10-01): los avisos se calculan DESPUÉS del commit de la venta; si ese cálculo lanzara y la respuesta
    fuera un error, el POS reintentaría el cobro (venta y cobro duplicados). Tiene que fallar ABIERTO: venta confirmada, sin `avisos`."""
    from libracommerce.erp import lotes

    pid = _producto(abrir_fefo)
    dep = _principal(abrir_fefo)
    client = _app(abrir_fefo, avisos=True, stock_habilitado=lambda: True)

    def _falla(*_a, **_k):
        raise RuntimeError("falla simulada del cálculo de avisos")

    monkeypatch.setattr(lotes, "avisos_de_venta", _falla)
    cuerpo = {"fecha": "2026-09-12", "items": [{"producto_id": pid, "nombre": "Yerba", "qty": 1, "precio": 100}],
              "subtotal": 100, "descuento": 0, "total": 100, "deposito_id": dep,
              "pagos": [{"medio": "efectivo", "monto": 100}]}
    antes = _ledger(abrir_fefo)
    r = client.post("/api/ventas", json=cuerpo)
    assert r.status_code in (200, 201), r.text
    venta = r.json()
    assert venta.get("id") and "avisos" not in venta
    assert len(_ledger(abrir_fefo)) > len(antes)                       # la venta y el descuento de stock quedaron registrados
    d = client.get(f"/api/ventas/{venta['id']}")
    assert d.status_code == 200 and "avisos" not in d.json()
