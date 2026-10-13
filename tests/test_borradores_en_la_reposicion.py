"""Los borradores en la reposición (ADR-056): `contar_borradores` y `en_borrador`, contra los dos motores.

Hasta v0.58 una orden de compra en `draft` restaba de lo sugerido como si ya estuviera pedida (el motor no tenía cómo enviarla, así que la que ya se le había pedido al
proveedor seguía en `draft`). Con `enviar_orden` (ADR-055) un borrador es un papel de trabajo todavía no mandado, y quien quiere ver qué falta pedir de verdad necesita que no
reste. Opción C del humano: la respuesta suma `en_borrador` (lo pendiente de las órdenes `draft`) aparte de `en_camino`, y `contar_borradores` elige si restan.

Lo que fijan: con `true` (el default) nada cambia, y los tests existentes de `test_reposicion.py` lo prueban sin tocarlos; con `false` un borrador de 10 no cambia lo sugerido y se
ve en `en_borrador`; `generar_ordenes_borrador` los sigue mirando para no duplicar; una cancelada no resta con ninguna de las dos; el CSV y el eco del parámetro.
"""

from __future__ import annotations

import csv
import io

import test_proveedor_por_producto as _prov
import test_reposicion as _rep
import test_vencimientos as _vto
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from test_proveedor_por_producto import _con_proveedor, _nuevo_tercero
from test_reposicion import HOY, _orden, _proveedor, _reporte, _yerba_de_referencia

from libracommerce.erp import compras, reposicion, reposicion_ordenes
from libracommerce.web.reposicion_router import build_reposicion_ordenes_router, build_reposicion_router

destino = _vto.destino
abrir_vto_ventas = _rep.abrir_vto_ventas
_ = _prov


def _fila(abrir, **kw):
    kw.setdefault("solo_a_pedir", False)
    [fila] = _reporte(abrir, **kw)
    return fila


def _numeros(fila) -> tuple:
    return fila["en_camino"], fila["en_borrador"], fila["sugerido"]


def _cliente(abrir) -> TestClient:
    app = FastAPI()
    app.include_router(build_reposicion_router(conexion=abrir, reloj=lambda: HOY))
    return TestClient(app)


# ── La cuenta ────────────────────────────────────────────────────────────


def test_con_true_el_borrador_resta_como_siempre_y_se_ve_aparte(abrir_vto_ventas):
    """Yerba: stock 10, rota 1 por día, hay que cubrir 18: sin nada, sugiere 8. Un borrador de 10 lo deja en 0 (como hasta ahora), y `en_borrador` lo muestra."""
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    assert _numeros(_fila(abrir)) == (0.0, 0.0, 8)
    _orden(abrir, _proveedor(abrir), [(yerba, 10)])

    explicito, por_defecto = _fila(abrir, contar_borradores=True), _fila(abrir)
    assert explicito == por_defecto                                  # `True` es el default: ni una clave de diferencia
    assert _numeros(por_defecto) == (10.0, 10.0, 0)                  # cuenta como pedido (18 − 10 − 10 < 0) Y se ve como borrador


def test_con_false_un_borrador_de_10_no_cambia_lo_sugerido_y_aparece_en_borrador(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    _orden(abrir, _proveedor(abrir), [(yerba, 10)])
    fila = _fila(abrir, contar_borradores=False)
    assert _numeros(fila) == (0.0, 10.0, 8)                          # igual que sin borrador: sugiere 8; el borrador está aparte
    assert fila["en_camino_sin_sucursal"] == 0.0                     # y sigue a `en_camino`: tampoco cuenta ahí
    [a_pedir] = _reporte(abrir, contar_borradores=False)             # con `solo_a_pedir` (el default) sigue apareciendo en la lista
    assert a_pedir["sugerido"] == 8


def test_enviada_y_parcial_cuentan_con_las_dos_opciones_y_no_son_borrador(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    proveedor = _proveedor(abrir)
    _orden(abrir, proveedor, [(yerba, 3)], estado="sent")
    _orden(abrir, proveedor, [(yerba, 2)], estado="partial")
    for contar in (True, False):
        assert _numeros(_fila(abrir, contar_borradores=contar)) == (5.0, 0.0, 3)       # 18 − 10 − 5


def test_mezcla_de_borrador_y_enviada_con_false_cuenta_solo_la_enviada(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    proveedor = _proveedor(abrir)
    _orden(abrir, proveedor, [(yerba, 4)], estado="sent")
    _orden(abrir, proveedor, [(yerba, 10)])                           # borrador
    assert _numeros(_fila(abrir, contar_borradores=False)) == (4.0, 10.0, 4)        # 18 − 10 − 4
    assert _numeros(_fila(abrir, contar_borradores=True)) == (14.0, 10.0, 0)


def test_una_cancelada_no_cuenta_ni_como_en_camino_ni_como_borrador(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    proveedor = _proveedor(abrir)
    for estado in ("draft", "sent"):
        oid = _orden(abrir, proveedor, [(yerba, 10)], estado=estado)
        with abrir() as conn:
            compras.cancelar_orden(conn, oid)
    for contar in (True, False):
        assert _numeros(_fila(abrir, contar_borradores=contar)) == (0.0, 0.0, 8)


def test_el_borrador_respeta_la_sucursal_como_en_camino(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    a, b, _dep_a, _dep_b = _rep._dos_sucursales(abrir)
    with abrir() as conn:
        yerba = _rep._producto(conn, "Yerba", inicial=1.0)
    proveedor = _proveedor(abrir)
    _orden(abrir, proveedor, [(yerba, 4)], branch_id=a)
    _orden(abrir, proveedor, [(yerba, 9)], branch_id=b)
    _orden(abrir, proveedor, [(yerba, 2)])                            # sin sucursal: cuenta en todas
    visto = {s: (f["en_camino"], f["en_borrador"], f["en_camino_sin_sucursal"])
             for s in (a, b, None) for f in _reporte(abrir, sucursal_id=s, solo_a_pedir=False, contar_borradores=False)}
    assert visto == {a: (0.0, 6.0, 0.0), b: (0.0, 11.0, 0.0), None: (0.0, 15.0, 0.0)}


# ── generar_ordenes_borrador los sigue mirando ───────────────────────────


def _escenario(abrir):
    yerba = _yerba_de_referencia(abrir)
    norte = _nuevo_tercero(abrir, "Distribuidora Norte")
    _con_proveedor(abrir, yerba, norte)
    with abrir() as conn:
        conn.execute("UPDATE catalog_items SET default_cost = 60 WHERE id = ?", (yerba,))
        conn.commit()
    return yerba


def _generar(abrir, clave, **kw):
    kw.setdefault("hoy", HOY)
    with abrir() as conn:
        r = reposicion_ordenes.generar_ordenes_borrador(conn, clave_operacion=clave, **kw)
        conn.commit()
    return r


def _ordenes(abrir) -> int:
    with abrir() as conn:
        return conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0]


def test_generar_dos_veces_con_false_no_duplica(abrir_vto_ventas):
    """Con la vista sin restar los borradores, la generación igual los ve: la segunda vez no encuentra nada que pedir."""
    abrir = abrir_vto_ventas
    _escenario(abrir)
    primera = _generar(abrir, "a", contar_borradores=False)
    segunda = _generar(abrir, "b", contar_borradores=False)
    assert len(primera["ordenes"]) == 1 and segunda["ordenes"] == []
    assert _ordenes(abrir) == 1


def test_generar_con_un_borrador_ya_hecho_a_mano_y_false_no_pide_lo_mismo_otra_vez(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _escenario(abrir)
    _orden(abrir, _proveedor(abrir), [(yerba, 10)])                   # un borrador cualquiera
    assert _fila(abrir, contar_borradores=False)["sugerido"] == 8     # la pantalla (con false) todavía sugiere pedir...
    r = _generar(abrir, "c", contar_borradores=False)
    assert r["ordenes"] == [] and _ordenes(abrir) == 1                # ...pero generar no crea otra orden encima del borrador


def test_generar_vuelve_a_pedir_cuando_el_borrador_se_cancela(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    _escenario(abrir)
    primera = _generar(abrir, "a", contar_borradores=False)
    with abrir() as conn:
        compras.cancelar_orden(conn, primera["ordenes"][0]["id"])
    segunda = _generar(abrir, "b", contar_borradores=False)
    assert len(segunda["ordenes"]) == 1                               # la cancelada no resta: hay que volver a pedir
    assert _ordenes(abrir) == 2


def test_la_huella_de_la_clave_no_depende_de_contar_borradores(abrir_vto_ventas):
    """No cambia lo que se crea, así que un reintento con el otro valor es el mismo pedido (no `ClaveReusada`)."""
    abrir = abrir_vto_ventas
    _escenario(abrir)
    primera = _generar(abrir, "misma", contar_borradores=False)
    otra_vez = _generar(abrir, "misma", contar_borradores=True)
    assert otra_vez["repetida"] is True and otra_vez["ordenes"][0]["id"] == primera["ordenes"][0]["id"]
    assert _ordenes(abrir) == 1


def test_el_router_de_generar_acepta_contar_borradores(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    _escenario(abrir)

    def permitir():
        return None

    app = FastAPI()
    app.include_router(build_reposicion_ordenes_router(
        conexion=abrir, usuario_actual=lambda: {"id": 7}, dependencias_escribir=[Depends(permitir)], reloj=lambda: HOY))
    c = TestClient(app)
    r = c.post("/api/reportes/reposicion/ordenes", json={"clave_operacion": "k1", "contar_borradores": False})
    assert r.status_code == 200, r.text
    assert len(r.json()["ordenes"]) == 1 and r.json()["repetida"] is False
    r = c.post("/api/reportes/reposicion/ordenes", json={"clave_operacion": "k2", "contar_borradores": False})
    assert r.status_code == 200 and r.json()["ordenes"] == []        # no duplica
    assert c.post("/api/reportes/reposicion/ordenes", json={"clave_operacion": "k3", "contar_borradores": "quizás"}).status_code == 422


# ── La API ───────────────────────────────────────────────────────────────


def test_get_repite_el_parametro_y_cada_fila_trae_en_borrador(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    _orden(abrir, _proveedor(abrir), [(yerba, 10)])
    c = _cliente(abrir)

    por_defecto = c.get("/api/reportes/reposicion", params={"solo_a_pedir": "false"}).json()
    assert por_defecto["contar_borradores"] is True
    assert (por_defecto["productos"][0]["en_camino"], por_defecto["productos"][0]["en_borrador"]) == (10.0, 10.0)

    sin_contar = c.get("/api/reportes/reposicion", params={"solo_a_pedir": "false", "contar_borradores": "false"}).json()
    assert sin_contar["contar_borradores"] is False
    [fila] = sin_contar["productos"]
    assert (fila["en_camino"], fila["en_borrador"], fila["sugerido"]) == (0.0, 10.0, 8)
    assert sin_contar["resumen"]["a_pedir"] == 1 and por_defecto["resumen"]["a_pedir"] == 0

    assert c.get("/api/reportes/reposicion", params={"contar_borradores": "tal vez"}).status_code == 422
    assert c.get("/api/reportes/reposicion/export", params={"contar_borradores": "tal vez"}).status_code == 422


def test_se_puede_ordenar_por_en_borrador(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    with abrir() as conn:
        sal = _rep._producto(conn, "Sal", inicial=2.0, minimo=10.0)
    proveedor = _proveedor(abrir)
    _orden(abrir, proveedor, [(yerba, 10)])
    _orden(abrir, proveedor, [(sal, 1)])
    r = _cliente(abrir).get("/api/reportes/reposicion", params={"contar_borradores": "false", "orden": "en_borrador", "sentido": "desc"})
    assert [p["nombre"] for p in r.json()["productos"]] == ["Yerba", "Sal"]
    assert "en_borrador" in reposicion.ORDENES


def _filas_del_csv(texto: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(texto)))


def test_el_csv_es_el_de_siempre_con_true_y_suma_en_borrador_con_false(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    _orden(abrir, _proveedor(abrir), [(yerba, 10)])
    c = _cliente(abrir)

    con = c.get("/api/reportes/reposicion/export", params={"solo_a_pedir": "false"})
    assert "en_borrador" not in con.text.splitlines()[0]             # el CSV de hoy, byte a byte
    assert con.text.splitlines()[0].endswith(",stock_minimo_propio,por_vencer")
    [fila] = _filas_del_csv(con.text)
    assert (fila["en_camino"], fila["sugerido"]) == ("10.0", "0")

    sin = c.get("/api/reportes/reposicion/export", params={"solo_a_pedir": "false", "contar_borradores": "false"})
    assert sin.text.splitlines()[0].endswith(",stock_minimo_propio,por_vencer,en_borrador")
    [fila] = _filas_del_csv(sin.text)
    assert (fila["en_camino"], fila["en_borrador"], fila["sugerido"]) == ("0.0", "10.0", "8")


def test_sin_borradores_la_columna_es_cero_y_el_resto_no_cambia(abrir_vto_ventas):
    abrir = abrir_vto_ventas
    _yerba_de_referencia(abrir)
    for contar in (True, False):
        fila = _fila(abrir, contar_borradores=contar)
        assert (fila["en_camino"], fila["en_borrador"], fila["sugerido"]) == (0.0, 0.0, 8)
