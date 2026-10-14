"""Las mermas valorizadas (ADR-059, 2026-10-14), contra los DOS motores.

Cada merma guarda el costo vigente del producto en `stock_movements.unit_cost` (una foto, como `sale_items.unit_cost_snapshot` de las ventas,
ADR-016) y se lee de tres lados: el historial de movimientos, la respuesta de la baja de un lote y el reporte de mermas por período.

🔴 Lo que se protege:

- **Es OPT-IN**, como `guardar_costo` de las ventas: sin pedirlo, la fila de una merma es la de siempre (`unit_cost` NULL). Es lo que fija
  `tests/test_ledger_sin_marcar.py` para Contalibra y Restolibra, y acá se repite en cada vía.
- **Todas las vías que escriben merma** estampan el costo cuando se pide: el ajuste en modo «Merma» (producto sin marcar y marcado, con FEFO),
  la baja de un lote, la devolución de un perecedero y la llamada directa a `add_movimiento_stock`.
- **Una foto**: si el costo cambia después, la merma conserva el que tenía. Sin costo cargado queda NULL y se cuenta aparte; nunca un 0.
- **Sin migración**: la columna `stock_movements.unit_cost` existe desde la baseline.
"""

from __future__ import annotations

import csv
import datetime
import io
import uuid
from decimal import Decimal

import pytest
from conftest import USUARIO, _schema_de_producto, _usuario, url_postgres
from fastapi import FastAPI
from fastapi.testclient import TestClient
from libracore.db import core

from libracommerce import migrar
from libracommerce.erp import catalogo, mermas, stock, vencimientos, ventas
from libracommerce.web.catalogo_router import OpcionesStock, build_productos_router, build_stock_router
from libracommerce.web.mermas_router import build_mermas_router
from libracommerce.web.vencimientos_router import build_vencimientos_escritura_router
from libracommerce.web.ventas_router import OpcionesVentas, build_ventas_router

HOY = datetime.date(2026, 9, 30)
PREVIA = "2026-09-01"


def _liberar():
    core._db_path = None
    core._database_url = None


@pytest.fixture(params=["sqlite", "postgres"])
def abrir_merma(request, tmp_path):
    """Un `conexion()` como el de un producto (LibraCore + LibraCommerce, depósito principal y la cabeza de la cadena de revisiones)."""
    destino = str(tmp_path / "merma.db") if request.param == "sqlite" else url_postgres()
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
    monkeypatch.setattr(core, "_ar_now", lambda: "2026-09-20 12:00:00")
    monkeypatch.setattr(vencimientos, "hoy_argentina", lambda ahora=None: HOY)


# ── Helpers ──────────────────────────────────────────────────────────────


def _producto(abrir, nombre="Yogur", *, costo=60.0, marcado=False, lotes_=(), stock_=20, unidad="u"):
    """Un producto con `stock_` unidades sin lote (o con los `lotes_` `(código, vence, cantidad)` que se pidan) y su costo."""
    with abrir() as conn:
        pid = catalogo.create_producto(conn, nombre, precio_venta=100.0, precio_costo=costo, unidad=unidad)
        if marcado:
            vencimientos.marcar_vence(conn, pid, True)
        for lote, vence, cantidad in lotes_:
            stock.add_movimiento_stock(conn, pid, "entrada", cantidad, "carga", fecha=PREVIA, lot_code=lote, expires_at=vence)
        if stock_:
            stock.add_movimiento_stock(conn, pid, "entrada", stock_, "carga", fecha=PREVIA)
        conn.commit()
    return pid


def _costo_ahora(abrir, pid, costo):
    with abrir() as conn:
        conn.execute("UPDATE catalog_items SET default_cost = ? WHERE id = ?", (costo, pid))
        conn.commit()


def _mermas(abrir, pid=None):
    """Las filas `waste` del ledger: `(cantidad, lote, costo)` con el costo como `Decimal` (`None` si NULL), en orden de escritura."""
    with abrir() as conn:
        sql = "SELECT quantity_delta, lot_code, unit_cost FROM stock_movements WHERE movement_type = 'waste'"
        filas = conn.execute(sql + (" AND item_id = ?" if pid else "") + " ORDER BY id", (pid,) if pid else ()).fetchall()
    return [(float(f[0]), f[1], None if f[2] is None else Decimal(str(f[2]))) for f in filas]


def _k() -> str:
    return uuid.uuid4().hex


# ═════════════════════════════════════════════════════ Sin migración

def test_no_hace_falta_una_revision_la_columna_ya_esta_en_la_baseline(abrir_merma):
    with abrir_merma() as conn:
        assert "unit_cost" in {f[1] for f in conn.execute("PRAGMA table_info(stock_movements)").fetchall()}
        assert [f[0] for f in conn.execute("SELECT version_num FROM alembic_version_libracommerce").fetchall()] == [
            "0007_devoluciones_de_servicio"]


# ═════════════════════════════════════════════════════ add_movimiento_stock: el candado y el snapshot


def test_sin_pedirlo_la_merma_es_la_fila_de_siempre(abrir_merma):
    pid = _producto(abrir_merma)
    with abrir_merma() as conn:
        stock.add_movimiento_stock(conn, pid, "merma", -2, "Merma: rotura", fecha=PREVIA)
        stock.salida_manual(conn, pid, "merma", 1, "Merma: rotura", fecha=PREVIA)
        conn.commit()
    assert _mermas(abrir_merma, pid) == [(-2.0, None, None), (-1.0, None, None)]


def test_con_guardar_costo_la_merma_guarda_el_costo_vigente(abrir_merma):
    pid = _producto(abrir_merma, costo=60.0)
    with abrir_merma() as conn:
        stock.add_movimiento_stock(conn, pid, "merma", -2, "Merma: rotura", fecha=PREVIA, guardar_costo=True)
        conn.commit()
    assert _mermas(abrir_merma, pid) == [(-2.0, None, Decimal("60"))]


def test_guardar_costo_solo_vale_para_una_merma(abrir_merma):
    """Una salida, un ajuste o una venta no guardan costo aunque se pida: no son pérdida."""
    pid = _producto(abrir_merma)
    with abrir_merma() as conn:
        for tipo in ("salida", "ajuste", "venta"):
            stock.add_movimiento_stock(conn, pid, tipo, -1, "x", fecha=PREVIA, guardar_costo=True)
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM stock_movements WHERE unit_cost IS NOT NULL").fetchone()[0] == 0


def test_el_costo_cambia_despues_y_la_merma_conserva_el_viejo(abrir_merma):
    pid = _producto(abrir_merma, costo=60.0)
    with abrir_merma() as conn:
        stock.add_movimiento_stock(conn, pid, "merma", -1, "Merma: rotura", fecha=PREVIA, guardar_costo=True)
        conn.commit()
    _costo_ahora(abrir_merma, pid, 90.0)
    with abrir_merma() as conn:
        stock.add_movimiento_stock(conn, pid, "merma", -1, "Merma: rotura", fecha=PREVIA, guardar_costo=True)
        conn.commit()
    assert _mermas(abrir_merma, pid) == [(-1.0, None, Decimal("60")), (-1.0, None, Decimal("90"))]


@pytest.mark.parametrize("costo", [0, 0.0])
def test_un_producto_sin_costo_queda_null_y_nunca_un_cero(abrir_merma, costo):
    pid = _producto(abrir_merma, costo=costo)
    with abrir_merma() as conn:
        stock.add_movimiento_stock(conn, pid, "merma", -1, "Merma: rotura", fecha=PREVIA, guardar_costo=True)
        conn.commit()
    assert _mermas(abrir_merma, pid) == [(-1.0, None, None)]


def test_una_variante_toma_el_costo_del_producto(abrir_merma):
    pid = _producto(abrir_merma, costo=60.0, stock_=0)
    with abrir_merma() as conn:
        vid = catalogo.create_variante(conn, pid, "Y-1", "Chico")["id"]
    with abrir_merma() as conn:
        stock.add_movimiento_stock(conn, pid, "entrada", 5, "carga", fecha=PREVIA, variant_id=vid)
        stock.add_movimiento_stock(conn, pid, "merma", -1, "Merma", fecha=PREVIA, variant_id=vid, guardar_costo=True)
        conn.commit()
    assert _mermas(abrir_merma, pid) == [(-1.0, None, Decimal("60"))]


# ═════════════════════════════════════════════════════ Las vías


def test_via_ajuste_merma_de_un_producto_sin_marcar(abrir_merma):
    pid = _producto(abrir_merma, costo=60.0)
    with abrir_merma() as conn:
        stock.salida_manual(conn, pid, "merma", 3, "Merma: rotura", fecha=PREVIA, guardar_costo=True)
        conn.commit()
    assert _mermas(abrir_merma, pid) == [(-3.0, None, Decimal("60"))]


def test_via_ajuste_merma_de_un_marcado_sale_por_fefo_y_cada_fila_lleva_el_costo(abrir_merma):
    pid = _producto(abrir_merma, costo=60.0, marcado=True, lotes_=(("L1", "2026-10-05", 4), ("L2", "2026-11-01", 4)), stock_=0)
    with abrir_merma() as conn:
        stock.salida_manual(conn, pid, "merma", 6, "Merma: rotura", fecha=PREVIA, guardar_costo=True)
        conn.commit()
    assert _mermas(abrir_merma, pid) == [(-4.0, "L1", Decimal("60")), (-2.0, "L2", Decimal("60"))]


def test_via_ajuste_merma_por_http_prendida_estampa_y_muestra(abrir_merma):
    pid = _producto(abrir_merma, costo=60.0)
    app = FastAPI()
    app.include_router(build_stock_router(conexion=abrir_merma, usuario_actual=_usuario, opciones=OpcionesStock(
        motivos_merma=("Rotura",), con_costo_de_merma=True)))
    c = TestClient(app)
    r = c.post(f"/api/stock/{pid}/ajuste", json={"modo": "merma", "cantidad": 3, "motivo": "Rotura", "fecha": PREVIA})
    assert r.status_code == 200, r.text
    c.post(f"/api/stock/{pid}/ajuste", json={"modo": "salida", "cantidad": 1, "fecha": PREVIA})
    assert _mermas(abrir_merma, pid) == [(-3.0, None, Decimal("60"))]
    movimientos = {m["tipo"]: m for m in c.get("/api/stock/movimientos", params={"producto_id": pid}).json()}
    assert (movimientos["merma"]["costo_unitario"], movimientos["merma"]["costo_total"], movimientos["merma"]["sin_costo"]) == (60.0, 180.0, False)
    assert (movimientos["salida"]["costo_unitario"], movimientos["salida"]["costo_total"], movimientos["salida"]["sin_costo"]) == (None, None, None)


def test_via_ajuste_merma_por_http_apagada_es_la_de_siempre(abrir_merma):
    """Contalibra y Restolibra: ni se guarda el costo ni las filas traen los campos nuevos."""
    pid = _producto(abrir_merma, costo=60.0)
    app = FastAPI()
    app.include_router(build_stock_router(conexion=abrir_merma, usuario_actual=_usuario,
                                          opciones=OpcionesStock(motivos_merma=("Rotura",))))
    c = TestClient(app)
    assert c.post(f"/api/stock/{pid}/ajuste", json={"modo": "merma", "cantidad": 3, "motivo": "Rotura", "fecha": PREVIA}).status_code == 200
    assert _mermas(abrir_merma, pid) == [(-3.0, None, None)]
    (fila,) = [m for m in c.get("/api/stock/movimientos", params={"producto_id": pid}).json() if m["tipo"] == "merma"]
    assert not {"costo_unitario", "costo_total", "sin_costo"} & set(fila)


def test_via_baja_de_un_lote_siempre_guarda_y_devuelve_el_costo(abrir_merma):
    pid = _producto(abrir_merma, costo=60.0, marcado=True, lotes_=(("L1", "2026-10-05", 10),), stock_=0)
    with abrir_merma() as conn:
        r = vencimientos.dar_de_baja_lote(conn, pid, 1, "L1", "2026-10-05", 4, clave_operacion=_k(), fecha=PREVIA)
        conn.commit()
    assert (r["costo_unitario"], r["costo_total"], r["sin_costo"]) == (60.0, 240.0, False)
    assert _mermas(abrir_merma, pid) == [(-4.0, "L1", Decimal("60"))]


def test_la_baja_de_un_lote_de_un_producto_sin_costo_dice_sin_costo(abrir_merma):
    pid = _producto(abrir_merma, costo=0, marcado=True, lotes_=(("L1", "2026-10-05", 10),), stock_=0)
    with abrir_merma() as conn:
        r = vencimientos.dar_de_baja_lote(conn, pid, 1, "L1", "2026-10-05", 4, clave_operacion=_k(), fecha=PREVIA)
        conn.commit()
    assert (r["costo_unitario"], r["costo_total"], r["sin_costo"]) == (None, None, True)
    assert _mermas(abrir_merma, pid) == [(-4.0, "L1", None)]


def test_el_reintento_de_la_baja_devuelve_el_costo_de_la_primera_vez_no_el_de_hoy(abrir_merma):
    pid = _producto(abrir_merma, costo=60.0, marcado=True, lotes_=(("L1", "2026-10-05", 10),), stock_=0)
    clave = _k()
    with abrir_merma() as conn:
        primera = vencimientos.dar_de_baja_lote(conn, pid, 1, "L1", "2026-10-05", 4, clave_operacion=clave, fecha=PREVIA)
        conn.commit()
    _costo_ahora(abrir_merma, pid, 100.0)
    with abrir_merma() as conn:
        repetida = vencimientos.dar_de_baja_lote(conn, pid, 1, "L1", "2026-10-05", 4, clave_operacion=clave, fecha=PREVIA)
        conn.commit()
    assert repetida["repetida"] is True
    assert {k: repetida[k] for k in ("costo_unitario", "costo_total", "sin_costo")} == {"costo_unitario": 60.0, "costo_total": 240.0, "sin_costo": False}
    assert primera["costo_total"] == repetida["costo_total"]
    assert len(_mermas(abrir_merma, pid)) == 1


def test_la_baja_de_un_lote_por_http_devuelve_los_campos_de_costo(abrir_merma):
    pid = _producto(abrir_merma, costo=12.5, marcado=True, lotes_=(("L1", "2026-10-05", 10),), stock_=0)
    app = FastAPI()
    app.include_router(build_vencimientos_escritura_router(
        conexion=abrir_merma, usuario_actual=_usuario, dependencias_marcar=[_libre()], dependencias_movimientos=[_libre()]))
    r = TestClient(app).post("/api/vencimientos/merma", json={
        "producto_id": pid, "deposito_id": 1, "lote": "L1", "vence": "2026-10-05", "cantidad": 3, "clave_operacion": _k()})
    assert r.status_code == 200, r.text
    assert (r.json()["costo_unitario"], r.json()["costo_total"], r.json()["sin_costo"]) == (12.5, 37.5, False)


def _libre():
    from fastapi import Depends

    return Depends(lambda: None)


def _vender_y_devolver(abrir, pid, *, guardar_costo):
    """Vende 3 de un perecedero (FEFO: del lote L1) y devuelve 1: el par `devolucion` + `merma` en el lote de origen."""
    total = 300.0
    vid = ventas.crear_venta_directa(
        abrir, fecha="2026-09-10", items=[{"nombre": "Yogur", "qty": 3, "precio": 100.0, "subtotal": total, "producto_id": pid}],
        subtotal=total, descuento=0.0, total=total, cliente_id=None, cliente_nombre="", usuario_id=USUARIO["id"],
        observaciones="", estado="cobrada", pagos=[{"medio": "efectivo", "monto": total, "estado": "aprobado"}],
        stock_habilitado=True)
    with abrir() as conn:
        linea = conn.execute("SELECT id FROM sale_items WHERE sale_id = ?", (vid,)).fetchone()["id"]
        deposito = catalogo.get_default_deposito_id(conn)
        ventas.devolver_items(conn, vid, {linea: 1.0}, deposito, usuario_id=USUARIO["id"], guardar_costo=guardar_costo)
        conn.commit()
    return vid


def test_via_devolucion_de_un_perecedero_la_merma_del_par_lleva_el_costo_si_se_pide(abrir_merma):
    pid = _producto(abrir_merma, costo=60.0, marcado=True, lotes_=(("L1", "2026-10-05", 6),), stock_=0)
    _vender_y_devolver(abrir_merma, pid, guardar_costo=True)
    assert _mermas(abrir_merma, pid) == [(-1.0, "L1", Decimal("60"))]


def test_via_devolucion_de_un_perecedero_sin_pedirlo_es_la_de_siempre(abrir_merma):
    pid = _producto(abrir_merma, costo=60.0, marcado=True, lotes_=(("L1", "2026-10-05", 6),), stock_=0)
    _vender_y_devolver(abrir_merma, pid, guardar_costo=False)
    assert _mermas(abrir_merma, pid) == [(-1.0, "L1", None)]


def test_via_devolucion_por_http_usa_el_guardar_costo_de_las_opciones_de_ventas(abrir_merma):
    for guardar, esperado in ((True, Decimal("60")), (False, None)):
        pid = _producto(abrir_merma, f"Yogur {guardar}", costo=60.0, marcado=True, lotes_=(("L1", "2026-10-05", 6),), stock_=0)
        app = FastAPI()
        app.include_router(build_productos_router(conexion=abrir_merma, usuario_actual=_usuario))
        app.include_router(build_ventas_router(conexion=abrir_merma, usuario_actual=_usuario, opciones=OpcionesVentas(guardar_costo=guardar)))
        c = TestClient(app)
        venta = c.post("/api/ventas", json={"fecha": "2026-09-10", "items": [{"nombre": "Yogur", "qty": 3, "precio": 100.0, "producto_id": pid}],
                                            "pagos": [{"medio": "efectivo", "monto": 300.0}]}).json()
        with abrir_merma() as conn:
            linea = conn.execute("SELECT id FROM sale_items WHERE sale_id = ?", (venta["id"],)).fetchone()["id"]
            deposito = catalogo.get_default_deposito_id(conn)
        r = c.post(f"/api/ventas/{venta['id']}/devolver", json={"lineas": [{"sale_item_id": linea, "cantidad": 1}], "deposito_id": deposito})
        assert r.status_code == 200, r.text
        assert _mermas(abrir_merma, pid) == [(-1.0, "L1", esperado)]


# ═════════════════════════════════════════════════════ El historial de movimientos


def test_el_historial_con_costo_valoriza_las_mermas_y_marca_las_que_no_tienen(abrir_merma):
    con_costo = _producto(abrir_merma, "Con costo", costo=60.0)
    sin_costo = _producto(abrir_merma, "Sin costo", costo=0)
    with abrir_merma() as conn:
        stock.add_movimiento_stock(conn, con_costo, "merma", -2.5, "Merma: rotura", fecha=PREVIA, guardar_costo=True)
        stock.add_movimiento_stock(conn, sin_costo, "merma", -1, "Merma: rotura", fecha=PREVIA, guardar_costo=True)
        # Una merma anterior a ADR-059: sin foto del costo, aunque el producto hoy lo tenga.
        stock.add_movimiento_stock(conn, con_costo, "merma", -1, "Merma: vieja", fecha=PREVIA)
        conn.commit()
        filas = stock.get_movimientos_stock(conn, con_costo=True)
        sin_campos = stock.get_movimientos_stock(conn)
    mermas_ = {(f["producto_id"], f["referencia"]): f for f in filas if f["tipo"] == "merma"}
    nueva = mermas_[(con_costo, "Merma: rotura")]
    assert (nueva["costo_unitario"], nueva["costo_total"], nueva["sin_costo"]) == (60.0, 150.0, False)
    assert (mermas_[(sin_costo, "Merma: rotura")]["costo_unitario"], mermas_[(sin_costo, "Merma: rotura")]["sin_costo"]) == (None, True)
    vieja = mermas_[(con_costo, "Merma: vieja")]
    assert (vieja["costo_unitario"], vieja["costo_total"], vieja["sin_costo"]) == (None, None, True)
    # Una entrada no es una merma: los campos existen y están vacíos (no se filtra el costo de compra).
    entrada = next(f for f in filas if f["tipo"] == "entrada")
    assert (entrada["costo_unitario"], entrada["costo_total"], entrada["sin_costo"]) == (None, None, None)
    # Sin pedirlo, las filas son las de siempre.
    assert not any({"costo_unitario", "costo_total", "sin_costo"} & set(f) for f in sin_campos)


def test_los_campos_de_importe_se_llaman_con_costo_para_que_el_filtro_de_los_productos_los_saque_solo():
    import re

    patron = re.compile(r"(^|_)(costos?|cost)($|_)")    # el de `SinCostos` de VentaLibra
    for campo in ("costo_unitario", "costo_total", "sin_costo", "movimientos_sin_costo", "cantidad_sin_costo"):
        assert patron.search(campo), campo


# ═════════════════════════════════════════════════════ El reporte de mermas por período


def _mermar(abrir, pid, cantidad, fecha, *, deposito=None, costo=True, tipo="merma"):
    with abrir() as conn:
        stock.add_movimiento_stock(conn, pid, tipo, -cantidad, "Merma: rotura", fecha=fecha, deposito_id=deposito, guardar_costo=costo)
        conn.commit()


def test_el_reporte_suma_por_producto_el_importe_y_cuenta_las_sin_costo(abrir_merma):
    yogur = _producto(abrir_merma, "Yogur", costo=60.0)
    leche = _producto(abrir_merma, "Leche", costo=10.0)
    _mermar(abrir_merma, yogur, 2, "2026-09-05")
    _mermar(abrir_merma, yogur, 1.5, "2026-09-10")
    _mermar(abrir_merma, leche, 4, "2026-09-12")
    _mermar(abrir_merma, yogur, 3, "2026-09-15", costo=False)          # una sin costo (anterior a ADR-059, o producto sin costo)
    with abrir_merma() as conn:
        r = mermas.reporte_mermas(conn, "2026-09-01", "2026-09-30")
    productos = {p["nombre"]: p for p in r["productos"]}
    assert (productos["Yogur"]["cantidad"], productos["Yogur"]["costo_total"], productos["Yogur"]["movimientos"]) == (6.5, 210.0, 3)
    assert (productos["Yogur"]["movimientos_sin_costo"], productos["Yogur"]["cantidad_sin_costo"], productos["Yogur"]["sin_costo"]) == (1, 3, True)
    assert (productos["Leche"]["cantidad"], productos["Leche"]["costo_total"], productos["Leche"]["sin_costo"]) == (4, 40.0, False)
    assert [p["nombre"] for p in r["productos"]] == ["Yogur", "Leche"]          # por importe descendente
    assert r["resumen"] == {"cantidad": 10.5, "costo_total": 250.0, "movimientos": 4, "movimientos_sin_costo": 1, "cantidad_sin_costo": 3,
                            "sin_costo": True, "productos": 2, "productos_sin_costo": 1}


def test_el_reporte_incluye_los_dos_extremos_y_deja_afuera_lo_demas(abrir_merma):
    pid = _producto(abrir_merma)
    for fecha in ("2026-08-31", "2026-09-01", "2026-09-30", "2026-10-01"):
        _mermar(abrir_merma, pid, 1, fecha)
    with abrir_merma() as conn:
        r = mermas.reporte_mermas(conn, "2026-09-01", "2026-09-30")
        un_dia = mermas.reporte_mermas(conn, "2026-09-30", "2026-09-30")
    assert r["resumen"]["movimientos"] == 2
    assert un_dia["resumen"]["movimientos"] == 1


def test_el_reporte_por_deposito_y_por_producto(abrir_merma):
    yogur = _producto(abrir_merma, "Yogur", costo=60.0, stock_=0)
    leche = _producto(abrir_merma, "Leche", costo=10.0, stock_=0)
    with abrir_merma() as conn:
        norte = catalogo.create_deposito(conn, "Norte")
        for pid in (yogur, leche):
            stock.add_movimiento_stock(conn, pid, "entrada", 50, "carga", fecha=PREVIA)
            stock.add_movimiento_stock(conn, pid, "entrada", 50, "carga", fecha=PREVIA, deposito_id=norte)
        conn.commit()
    _mermar(abrir_merma, yogur, 2, "2026-09-05")
    _mermar(abrir_merma, yogur, 5, "2026-09-05", deposito=norte)
    _mermar(abrir_merma, leche, 1, "2026-09-05", deposito=norte)
    with abrir_merma() as conn:
        solo_norte = mermas.reporte_mermas(conn, "2026-09-01", "2026-09-30", deposito_id=norte)
        solo_yogur = mermas.reporte_mermas(conn, "2026-09-01", "2026-09-30", producto_id=yogur)
        todo = mermas.reporte_mermas(conn, "2026-09-01", "2026-09-30")
    assert {p["nombre"]: p["costo_total"] for p in solo_norte["productos"]} == {"Yogur": 300.0, "Leche": 10.0}
    assert [p["nombre"] for p in solo_yogur["productos"]] == ["Yogur"] and solo_yogur["resumen"]["costo_total"] == 420.0
    assert todo["resumen"]["costo_total"] == 430.0


def test_el_reporte_solo_cuenta_mermas_que_restan_y_no_otras_salidas(abrir_merma):
    pid = _producto(abrir_merma)
    with abrir_merma() as conn:
        stock.add_movimiento_stock(conn, pid, "salida", -3, "uso", fecha="2026-09-05")
        stock.add_movimiento_stock(conn, pid, "ajuste", -3, "conteo", fecha="2026-09-05")
        stock.add_movimiento_stock(conn, pid, "merma", 2, "Merma anulada a mano", fecha="2026-09-05", guardar_costo=True)   # suma: no es pérdida
        conn.commit()
        r = mermas.reporte_mermas(conn, "2026-09-01", "2026-09-30")
    assert r["productos"] == [] and r["resumen"]["movimientos"] == 0 and r["resumen"]["costo_total"] == 0


def test_el_reporte_incluye_la_merma_de_la_devolucion_de_un_perecedero_y_la_baja_de_un_lote(abrir_merma):
    pid = _producto(abrir_merma, costo=60.0, marcado=True, lotes_=(("L1", "2026-10-05", 10),), stock_=0)
    _vender_y_devolver(abrir_merma, pid, guardar_costo=True)                                  # 1 u de merma a $60
    with abrir_merma() as conn:
        vencimientos.dar_de_baja_lote(conn, pid, 1, "L1", "2026-10-05", 2, clave_operacion=_k(), fecha="2026-09-20")    # 2 u a $60
        conn.commit()
        r = mermas.reporte_mermas(conn, "2026-09-01", "2026-09-30")
    (p,) = r["productos"]
    assert (p["cantidad"], p["costo_total"], p["movimientos"], p["sin_costo"]) == (3, 180.0, 2, False)


def test_el_importe_se_suma_exacto_y_se_redondea_al_final(abrir_merma):
    pid = _producto(abrir_merma, costo=0.333, stock_=100)
    for _ in range(3):
        _mermar(abrir_merma, pid, 1, "2026-09-05")           # cada una vale 0.333; sumadas, 0.999 -> 1.0 (redondear una por una daría 0.99)
    with abrir_merma() as conn:
        r = mermas.reporte_mermas(conn, "2026-09-01", "2026-09-30")
    assert r["resumen"]["costo_total"] == 1.0


@pytest.mark.parametrize("desde,hasta,deposito", [("ayer", "2026-09-30", None), ("2026-09-01", "mañana", None),
                                                  ("2026-09-30", "2026-09-01", None), ("2026-09-01", "2026-09-30", 999)])
def test_el_reporte_rechaza_fechas_ilegibles_rango_al_reves_y_deposito_inexistente(abrir_merma, desde, hasta, deposito):
    with abrir_merma() as conn, pytest.raises(ValueError):
        mermas.reporte_mermas(conn, desde, hasta, deposito_id=deposito)


# ── El router ────────────────────────────────────────────────────────────


def _client(abrir) -> TestClient:
    app = FastAPI()
    app.include_router(build_mermas_router(conexion=abrir))
    return TestClient(app)


def test_get_reportes_mermas(abrir_merma):
    yogur = _producto(abrir_merma, "Yogur", costo=60.0)
    _mermar(abrir_merma, yogur, 2, "2026-09-05")
    _mermar(abrir_merma, yogur, 1, "2026-09-06", costo=False)
    r = _client(abrir_merma).get("/api/reportes/mermas", params={"desde": "2026-09-01", "hasta": "2026-09-30"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["desde"], body["hasta"], body["deposito_id"]) == ("2026-09-01", "2026-09-30", None)
    assert body["resumen"]["costo_total"] == 120.0 and body["resumen"]["movimientos_sin_costo"] == 1
    assert body["productos"][0]["producto_id"] == yogur


def test_get_reportes_mermas_sin_fechas_es_el_mes_en_curso(abrir_merma):
    yogur = _producto(abrir_merma, "Yogur", costo=60.0)
    _mermar(abrir_merma, yogur, 2, "2026-09-05")
    _mermar(abrir_merma, yogur, 1, "2026-08-31")
    body = _client(abrir_merma).get("/api/reportes/mermas").json()          # hoy = 2026-09-30
    assert (body["desde"], body["hasta"], body["resumen"]["movimientos"]) == ("2026-09-01", "2026-09-30", 1)


def test_get_reportes_mermas_parametros_invalidos_son_422(abrir_merma):
    c = _client(abrir_merma)
    for params in ({"desde": "x"}, {"hasta": "x"}, {"desde": "2026-09-30", "hasta": "2026-09-01"}, {"deposito_id": 999}, {"deposito_id": "x"}):
        assert c.get("/api/reportes/mermas", params=params).status_code == 422, params
        assert c.get("/api/reportes/mermas/export", params=params).status_code == 422, params


def test_export_csv_de_mermas(abrir_merma):
    yogur = _producto(abrir_merma, "=Yogur", costo=60.0)
    _mermar(abrir_merma, yogur, 2, "2026-09-05")
    r = _client(abrir_merma).get("/api/reportes/mermas/export", params={"desde": "2026-09-01", "hasta": "2026-09-30"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'filename="mermas_2026-09-01_2026-09-30.csv"' in r.headers["content-disposition"]
    (fila,) = list(csv.DictReader(io.StringIO(r.text)))
    assert fila["nombre"] == "'=Yogur" and fila["cantidad"] == "2" and fila["costo_total"] == "120.0"      # neutraliza fórmulas
    assert list(fila) == ["producto_id", "codigo", "nombre", "unidad", "cantidad", "costo_total", "movimientos", "movimientos_sin_costo", "cantidad_sin_costo"]
