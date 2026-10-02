"""Estacionalidad de la reposición (ADR-023): el ajuste de la proyección por lo que pasó hace un año. Las fixtures y los helpers son los de `tests/test_reposicion.py`."""
from __future__ import annotations

import datetime

import pytest
import test_reposicion as _rep
from test_reposicion import HOY, _fijar, _producto, _reporte, _venta

from libracommerce.erp import catalogo, reposicion, stock

abrir_vto_ventas = _rep.abrir_vto_ventas
destino = _rep.destino

AÑO_PASADO = "2025-09-{:02d}"          # HOY es el 30 de septiembre de 2026: la ventana de referencia es septiembre de 2025
SIGUIENTE = "2025-10-{:02d}"           # y la proyectada, los 18 días que siguen (3 de cobertura + 15 de plazo... = 18 de horizonte)


def _helado(abrir, *, referencia=(5, 10, 15, 20, 25), por_dia_referencia=6, proyectadas=(3, 10, 17), por_dia_proyectada=18, nombre="Helado"):
    """Un producto que HOY rota 1 por día (queda con 10 y su sugerido normal es 8) y que hace un año vendió `por_dia_referencia` unidades en cada día de
    `referencia` (septiembre) y `por_dia_proyectada` en cada día de `proyectadas` (octubre)."""
    with abrir() as conn:
        pid = catalogo.create_producto(conn, nombre, precio_venta=100.0, precio_costo=60.0)
        stock.ajustar_stock(conn, pid, 1000.0, "inicial", fecha="2025-08-01")
        conn.commit()
    for d in referencia:
        _venta(abrir, [(pid, nombre, por_dia_referencia, 100.0)], AÑO_PASADO.format(d))
    for d in proyectadas:
        _venta(abrir, [(pid, nombre, por_dia_proyectada, 100.0)], SIGUIENTE.format(d))
    with abrir() as conn:
        stock.ajustar_stock(conn, pid, 40.0, "inicial", fecha=_rep.PREVIA)
        conn.commit()
    _venta(abrir, [(pid, nombre, 10, 100.0)], "2026-09-10")
    _venta(abrir, [(pid, nombre, 20, 100.0)], "2026-09-20")
    return pid


def _fila(abrir, nombre="Helado", **kw):
    return {f["nombre"]: f for f in _reporte(abrir, solo_a_pedir=False, **kw)}[nombre]


def test_un_producto_que_el_año_pasado_se_vendio_el_triple_despues_se_pide_el_triple(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    _helado(abrir)
    sin = _fila(abrir)
    assert sin["factor_estacional"] is None and sin["sugerido"] == 8                      # 18 − 10, la cuenta de siempre
    con = _fila(abrir, estacionalidad=True)
    # Referencia: 30 unidades en 30 días (1 por día). Proyectada: 54 en 18 días (3 por día). Factor 3: necesidad 18 × 3 = 54, menos los 10 que hay.
    assert con["factor_estacional"] == 3.0 and con["sugerido"] == 44
    assert con["stock"] == sin["stock"] and con["unidades_vendidas"] == sin["unidades_vendidas"]          # sólo cambia la proyección


def test_sin_estacionalidad_el_resultado_es_el_de_siempre_aunque_haya_historia(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    _helado(abrir)
    a = _fila(abrir)
    b = _fila(abrir, estacionalidad=False)
    assert a == b


def test_sin_historia_de_hace_un_año_no_hay_factor_ni_ajuste(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    _rep._yerba_de_referencia(abrir)                                                      # sólo ventas de este año
    fila = _fila(abrir, "Yerba", estacionalidad=True)
    assert fila["factor_estacional"] is None and fila["sugerido"] == 8


def test_con_menos_de_tres_dias_con_venta_en_la_referencia_no_se_ajusta(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    _helado(abrir, referencia=(5, 10))                                                    # sólo dos días: es ruido, no una temporada
    fila = _fila(abrir, estacionalidad=True)
    assert fila["factor_estacional"] is None and fila["sugerido"] == 8


def test_el_factor_se_acota_a_un_cuarto_y_a_cuatro(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    _helado(abrir, proyectadas=(3, 10, 17), por_dia_proyectada=500)                       # 1500 en 18 días contra 1 por día: factor altísimo
    assert _fila(abrir, estacionalidad=True)["factor_estacional"] == 4.0
    _helado(abrir, nombre="Pan dulce", proyectadas=())                                    # no se vendió nada después: factor 0
    pan = _fila(abrir, "Pan dulce", estacionalidad=True)
    assert pan["factor_estacional"] == 0.25 and pan["sugerido"] == 0                      # 18 × 0,25 = 4,5 → 5 − 10 = nada que pedir


def test_el_plazo_propio_del_producto_alarga_su_ventana_proyectada(abrir_vto_ventas):
    """Con un plazo de 40 días el horizonte es 55 y la ventana proyectada del año pasado también: ve las ventas de octubre y noviembre."""
    abrir = abrir_vto_ventas
    pid = _helado(abrir, proyectadas=(3, 10, 17), por_dia_proyectada=18)
    for d in (5, 20):
        _venta(abrir, [(pid, "Helado", 100, 100.0)], f"2025-11-{d:02d}")          # noviembre del año pasado: sólo entra con un horizonte largo
    con_general = _fila(abrir, estacionalidad=True)["factor_estacional"]
    _fijar(abrir, pid, plazo=40)
    con_propio = _fila(abrir, estacionalidad=True)["factor_estacional"]
    assert con_general == 3.0 and con_propio > con_general


def test_un_producto_sin_rotacion_reciente_sigue_sin_pedirse(abrir_vto_ventas):
    """No inventa temporada: con necesidad cero, el factor no la multiplica."""
    abrir = abrir_vto_ventas
    with abrir() as conn:
        pid = catalogo.create_producto(conn, "Turrón", precio_venta=100.0, precio_costo=60.0)
        stock.ajustar_stock(conn, pid, 1000.0, "inicial", fecha="2025-08-01")
        conn.commit()
    for d in (5, 10, 15):
        _venta(abrir, [(pid, "Turrón", 6, 100.0)], AÑO_PASADO.format(d))
    for d in (3, 10, 17):
        _venta(abrir, [(pid, "Turrón", 50, 100.0)], SIGUIENTE.format(d))
    fila = _fila(abrir, "Turrón", estacionalidad=True)
    assert fila["unidades_vendidas"] == 0 and fila["sugerido"] == 0 and fila["factor_estacional"] is not None


def test_el_29_de_febrero_cae_en_el_28_del_año_anterior():
    assert reposicion._un_anio_atras(datetime.date(2028, 2, 29)) == datetime.date(2027, 2, 28)
    assert reposicion._un_anio_atras(datetime.date(2026, 9, 30)) == datetime.date(2025, 9, 30)
    assert reposicion._un_anio_atras(datetime.date(2025, 3, 1)) == datetime.date(2024, 3, 1)


def test_el_router_acepta_estacionalidad_la_devuelve_y_el_csv_trae_el_factor(abrir_vto_ventas):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from libracommerce.web.reposicion_router import build_reposicion_router

    abrir = abrir_vto_ventas
    _helado(abrir)
    app = FastAPI()
    app.include_router(build_reposicion_router(conexion=abrir))
    c = TestClient(app)
    r = c.get("/api/reportes/reposicion", params={"estacionalidad": "true", "solo_a_pedir": "false"})
    assert r.status_code == 200 and r.json()["estacionalidad"] is True
    assert c.get("/api/reportes/reposicion").json()["estacionalidad"] is False
    csv = c.get("/api/reportes/reposicion/export", params={"solo_a_pedir": "false"}).text.splitlines()
    assert csv[0].endswith(",factor_estacional")


def test_las_ordenes_en_borrador_usan_el_mismo_ajuste_si_se_pide(abrir_vto_ventas):
    from test_proveedor_por_producto import _con_proveedor, _nuevo_tercero

    from libracommerce.erp import reposicion_ordenes

    abrir = abrir_vto_ventas
    pid = _helado(abrir)
    _con_proveedor(abrir, pid, _nuevo_tercero(abrir, "Heladería Norte"))

    def generar(clave, **kw):
        with abrir() as conn:
            r = reposicion_ordenes.generar_ordenes_borrador(conn, clave_operacion=clave, hoy=HOY, **kw)
            conn.commit()
        return r

    # La misma clave con y sin estacionalidad son pedidos distintos (la huella incluye el parámetro).
    r = generar("est-1", estacionalidad=True)
    assert [float(li["cantidad"]) for o in r["ordenes"] for li in o["lineas"]] == [44.0]
    with pytest.raises(reposicion_ordenes.ClaveReusada):
        generar("est-1")
