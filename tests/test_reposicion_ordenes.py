"""Órdenes de compra en borrador desde la reposición sugerida (ADR-022). Las fixtures y los helpers son los de `tests/test_reposicion.py`
y `tests/test_proveedor_por_producto.py` (una base por motor, con las revisiones al día)."""
from __future__ import annotations

from decimal import Decimal

import pytest
import test_proveedor_por_producto as _prov
import test_reposicion as _rep
import test_vencimientos as _vto
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_proveedor_por_producto import _con_proveedor, _nuevo_tercero
from test_reposicion import HOY, _dos_sucursales, _producto, _venta, _yerba_de_referencia

from libracommerce.erp import compras, reposicion, reposicion_ordenes
from libracommerce.web.reposicion_router import build_reposicion_ordenes_router

destino = _vto.destino
abrir_vto_ventas = _rep.abrir_vto_ventas
_ = _prov  # (los fixtures de proveedor viven allá)


def _generar(abrir, clave="op-1", **kw):
    kw.setdefault("hoy", HOY)
    with abrir() as conn:
        r = reposicion_ordenes.generar_ordenes_borrador(conn, clave_operacion=clave, **kw)
        conn.commit()
    return r


def _cantidad_de_ordenes(abrir) -> int:
    with abrir() as conn:
        return conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0]


@pytest.fixture
def escenario(abrir_vto_ventas):
    """Yerba (stock 10, rota 1 por día: sugiere 8) con el proveedor Norte; Sal (stock 2, mínimo 10: sugiere 8) con Sur; Azúcar (mínimo 6, nada
    en stock: sugiere 6) sin proveedor; y Fideos, que no hay que pedir."""
    abrir = abrir_vto_ventas
    yerba = _yerba_de_referencia(abrir)
    with abrir() as conn:
        sal = _producto(conn, "Sal", inicial=2.0, minimo=10.0)
        azucar = _producto(conn, "Azúcar", minimo=6.0)
        fideos = _producto(conn, "Fideos", inicial=100.0)
        conn.execute("UPDATE catalog_items SET default_cost = 60 WHERE id = ?", (yerba,))
        conn.execute("UPDATE catalog_items SET default_cost = 25.5 WHERE id = ?", (sal,))
        conn.execute("UPDATE catalog_items SET default_cost = 0 WHERE id = ?", (azucar,))
        conn.commit()
    norte, sur = _nuevo_tercero(abrir, "Distribuidora Norte"), _nuevo_tercero(abrir, "Mayorista Sur")
    _con_proveedor(abrir, yerba, norte)
    _con_proveedor(abrir, sal, sur)
    return {"abrir": abrir, "yerba": yerba, "sal": sal, "azucar": azucar, "fideos": fideos, "norte": norte, "sur": sur}


def test_una_orden_en_borrador_por_proveedor_con_el_sugerido_y_el_costo_vigente(escenario):
    e = escenario
    r = _generar(e["abrir"])
    assert r["repetida"] is False and r["omitidos"] == []
    por_proveedor = {o["supplier_party_id"]: o for o in r["ordenes"]}
    assert set(por_proveedor) == {e["norte"], e["sur"]}
    norte, sur = por_proveedor[e["norte"]], por_proveedor[e["sur"]]
    assert norte["status"] == "draft" and sur["status"] == "draft"
    assert norte["proveedor"] == "Distribuidora Norte" and norte["number"] != sur["number"]
    (linea,) = norte["lineas"]
    assert (linea["producto_id"], linea["nombre"], Decimal(linea["cantidad"]), Decimal(linea["costo_unitario"]), linea["costo_cero"]) == (
        e["yerba"], "Yerba", Decimal("8"), Decimal("60"), False)
    assert Decimal(linea["subtotal"]) == Decimal("480") and Decimal(norte["total"]) == Decimal("480")
    assert Decimal(sur["total"]) == Decimal("204.0")                                  # 8 × 25,5
    # Azúcar no tiene proveedor: no entra en ninguna orden, y se informa con su sugerido.
    assert r["sin_proveedor"] == [{"producto_id": e["azucar"], "nombre": "Azúcar", "sugerido": 6}]
    assert _cantidad_de_ordenes(e["abrir"]) == 2
    with e["abrir"]() as conn:                                                         # lo guardado es lo mismo, y en borrador
        for o in r["ordenes"]:
            guardada = compras.obtener_orden(conn, o["id"])
            assert guardada["status"] == "draft" and len(guardada["items"]) == 1


def test_el_costo_cero_se_marca_para_completarlo(escenario):
    e = escenario
    _con_proveedor(e["abrir"], e["azucar"], e["norte"])
    r = _generar(e["abrir"])
    norte = next(o for o in r["ordenes"] if o["supplier_party_id"] == e["norte"])
    assert {li["nombre"]: li["costo_cero"] for li in norte["lineas"]} == {"Yerba": False, "Azúcar": True}
    assert r["sin_proveedor"] == []


def test_la_misma_clave_devuelve_las_mismas_ordenes_y_no_crea_otras(escenario):
    e = escenario
    primera = _generar(e["abrir"], clave="intento-1")
    otra_vez = _generar(e["abrir"], clave="intento-1")
    assert otra_vez["repetida"] is True
    assert sorted(o["id"] for o in otra_vez["ordenes"]) == sorted(o["id"] for o in primera["ordenes"])
    assert _cantidad_de_ordenes(e["abrir"]) == 2


def test_generar_dos_veces_con_claves_distintas_no_duplica_lo_pedido(escenario):
    """Las órdenes en borrador cuentan como «en camino»: la segunda no encuentra nada que pedir."""
    e = escenario
    _generar(e["abrir"], clave="a")
    segunda = _generar(e["abrir"], clave="b")
    assert segunda["ordenes"] == [] and [x["nombre"] for x in segunda["sin_proveedor"]] == ["Azúcar"]
    assert _cantidad_de_ordenes(e["abrir"]) == 2


def test_producto_ids_acota_y_lo_que_no_hay_que_pedir_se_informa(escenario):
    e = escenario
    r = _generar(e["abrir"], producto_ids=[e["yerba"], e["fideos"]])
    assert [o["supplier_party_id"] for o in r["ordenes"]] == [e["norte"]]
    assert r["omitidos"] == [e["fideos"]] and r["sin_proveedor"] == []


def test_el_filtro_por_proveedor_y_la_sucursal_viajan_a_la_orden(escenario):
    e = escenario
    r = _generar(e["abrir"], proveedor_id=e["sur"])
    assert [o["supplier_party_id"] for o in r["ordenes"]] == [e["sur"]] and r["sin_proveedor"] == []
    a, _b, dep_a, _dep_b = _dos_sucursales(e["abrir"])
    with e["abrir"]() as conn:
        from libracommerce.erp import stock
        stock.ajustar_stock(conn, e["sal"], 1.0, "inicial", fecha=_rep.PREVIA, deposito_id=dep_a)
        conn.commit()
    r = _generar(e["abrir"], clave="suc", sucursal_id=a, proveedor_id=e["sur"])
    assert r["ordenes"] and all(o["branch_id"] == a for o in r["ordenes"])


def test_si_algo_falla_no_queda_ninguna_orden(escenario):
    e = escenario
    llamadas = []

    def numerador_roto(conn):
        llamadas.append(1)
        if len(llamadas) == 2:
            raise RuntimeError("se cayó la numeración")
        return compras.numero_por_defecto(conn)

    # El `pytest.raises` AFUERA: si estuviera en el mismo `with`, atraparía la excepción antes de que la conexión la vea y confirmaría lo escrito.
    with pytest.raises(RuntimeError), e["abrir"]() as conn:
        reposicion_ordenes.generar_ordenes_borrador(conn, clave_operacion="x", numerador=numerador_roto, hoy=HOY)
    assert _cantidad_de_ordenes(e["abrir"]) == 0


@pytest.mark.parametrize("clave", [None, "", "  ", "x" * 65, 5, "a]", "[op:a", "con espacio", "a/b"])
def test_la_clave_es_obligatoria_y_acotada(escenario, clave):
    with escenario["abrir"]() as conn, pytest.raises(ValueError, match="clave_operacion"):
        reposicion_ordenes.generar_ordenes_borrador(conn, clave_operacion=clave, hoy=HOY)


def test_parametros_invalidos_y_solo_a_pedir_no_se_aceptan(escenario):
    with escenario["abrir"]() as conn:
        with pytest.raises(ValueError, match="solo_a_pedir"):
            reposicion_ordenes.generar_ordenes_borrador(conn, clave_operacion="k", solo_a_pedir=False, hoy=HOY)
        with pytest.raises(ValueError):
            reposicion_ordenes.generar_ordenes_borrador(conn, clave_operacion="k", dias_rotacion=0, hoy=HOY)
        with pytest.raises(ValueError, match="producto_ids"):
            reposicion_ordenes.generar_ordenes_borrador(conn, clave_operacion="k", producto_ids=["1"], hoy=HOY)
    assert _cantidad_de_ordenes(escenario["abrir"]) == 0


def test_sin_la_revision_0004_pide_la_revision(abrir_ventas):
    with abrir_ventas() as conn, pytest.raises(reposicion.SinRevision, match="0004"):
        reposicion_ordenes.generar_ordenes_borrador(conn, clave_operacion="k", hoy=HOY)


# ── El router ────────────────────────────────────────────────────────────


def _cliente(abrir, **extra) -> TestClient:
    app = FastAPI()
    app.include_router(build_reposicion_ordenes_router(
        conexion=abrir, usuario_actual=lambda: {"id": 7}, dependencias_escribir=[Depends(lambda: None)], **extra))
    return TestClient(app, raise_server_exceptions=False)


def test_el_router_genera_deja_el_usuario_y_reintenta_con_la_misma_clave(escenario):
    e = escenario
    c = _cliente(e["abrir"])
    r = c.post("/api/reportes/reposicion/ordenes", json={"clave_operacion": "uuid-1"})
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert {o["proveedor_id"] for o in cuerpo["ordenes"]} == {e["norte"], e["sur"]} and all("supplier_party_id" not in o for o in cuerpo["ordenes"])
    with e["abrir"]() as conn:
        assert {f[0] for f in conn.execute("SELECT created_by FROM purchase_orders").fetchall()} == {7}
    again = c.post("/api/reportes/reposicion/ordenes", json={"clave_operacion": "uuid-1"}).json()
    assert again["repetida"] is True and _cantidad_de_ordenes(e["abrir"]) == 2


@pytest.mark.parametrize("cuerpo", [
    {}, {"clave_operacion": ""}, {"clave_operacion": "k", "dias_rotacion": 0}, {"clave_operacion": "k", "x": 1},
    {"clave_operacion": "k", "producto_ids": ["a"]}, {"clave_operacion": "k", "proveedor_id": 99999},
])
def test_el_router_rechaza_lo_invalido_con_422_y_no_escribe(escenario, cuerpo):
    c = _cliente(escenario["abrir"])
    assert c.post("/api/reportes/reposicion/ordenes", json=cuerpo).status_code == 422
    assert _cantidad_de_ordenes(escenario["abrir"]) == 0


def test_el_router_traduce_los_ids_con_los_ganchos_de_compras(escenario):
    e = escenario
    offset = 100_000

    def resolver(_conn, proveedor_id):
        if proveedor_id < offset:
            raise HTTPException(404, "proveedor inexistente")
        return proveedor_id - offset

    c = _cliente(e["abrir"], resolver_proveedor=resolver, proveedor_de=lambda _conn, party: party + offset)
    r = c.post("/api/reportes/reposicion/ordenes", json={"clave_operacion": "t", "proveedor_id": e["sur"] + offset}).json()
    assert [o["proveedor_id"] for o in r["ordenes"]] == [e["sur"] + offset]
    assert c.post("/api/reportes/reposicion/ordenes", json={"clave_operacion": "t2", "proveedor_id": 5}).status_code == 404


def test_si_la_traduccion_falla_no_queda_ninguna_orden(escenario):
    def rota(_conn, _party):
        raise RuntimeError("sin proveedor")

    c = _cliente(escenario["abrir"], proveedor_de=rota)
    assert c.post("/api/reportes/reposicion/ordenes", json={"clave_operacion": "r"}).status_code == 500
    assert _cantidad_de_ordenes(escenario["abrir"]) == 0


def test_el_router_no_se_monta_sin_autorizacion_ni_usuario(escenario):
    abrir = escenario["abrir"]
    for vacio in (None, [], ()):
        with pytest.raises(ValueError, match="dependencias_escribir"):
            build_reposicion_ordenes_router(conexion=abrir, usuario_actual=lambda: {"id": 1}, dependencias_escribir=vacio)
    with pytest.raises(ValueError, match="usuario_actual"):
        build_reposicion_ordenes_router(conexion=abrir, dependencias_escribir=[Depends(lambda: None)])


def test_el_router_sin_la_revision_responde_503(abrir_ventas):
    c = _cliente(abrir_ventas)
    assert c.post("/api/reportes/reposicion/ordenes", json={"clave_operacion": "k"}).status_code == 503


def test_los_topes_limitan_lo_que_se_pide_a_lo_que_la_persona_confirmo(escenario):
    """Entre la vista previa y el pedido el sugerido puede subir (bajó el stock): la orden no se pasa de lo que se vio. Si bajó, se pide menos."""
    e = escenario
    r = _generar(e["abrir"], topes={e["yerba"]: 5, e["sal"]: 100})                   # Yerba sugiere 8 (tope 5); Sal sugiere 8 (tope 100)
    cantidades = {li["nombre"]: Decimal(li["cantidad"]) for o in r["ordenes"] for li in o["lineas"]}
    assert cantidades == {"Yerba": Decimal("5"), "Sal": Decimal("8")}
    totales = {o["proveedor"]: Decimal(o["total"]) for o in r["ordenes"]}
    assert totales == {"Distribuidora Norte": Decimal("300"), "Mayorista Sur": Decimal("204.0")}


@pytest.mark.parametrize("topes", [{1: 0}, {1: -2}, {1: "x"}, {1: True}, {"a": 3}, {1: float("nan")}])
def test_los_topes_invalidos_se_rechazan_sin_escribir(escenario, topes):
    with escenario["abrir"]() as conn, pytest.raises(ValueError):
        reposicion_ordenes.generar_ordenes_borrador(conn, clave_operacion="k", topes=topes, hoy=HOY)
    assert _cantidad_de_ordenes(escenario["abrir"]) == 0


def test_el_router_acepta_topes_con_ids_como_texto_de_json(escenario):
    e = escenario
    c = _cliente(e["abrir"])
    r = c.post("/api/reportes/reposicion/ordenes", json={"clave_operacion": "t", "topes": {str(e["yerba"]): 3}}).json()
    assert Decimal({li["nombre"]: li["cantidad"] for o in r["ordenes"] for li in o["lineas"]}["Yerba"]) == Decimal("3")
    assert c.post("/api/reportes/reposicion/ordenes", json={"clave_operacion": "t2", "topes": {str(e["yerba"]): 0}}).status_code == 422


def test_las_claves_con_comodines_de_like_no_se_confunden(escenario):
    """`%` y `_` no son comodines y las mayúsculas cuentan: una clave no trae las órdenes de otra."""
    e = escenario
    _generar(e["abrir"], clave="Aa_b", producto_ids=[e["yerba"]])
    for otra in ("aa_b", "AaXb", "A"):
        r = _generar(e["abrir"], clave=otra, producto_ids=[e["sal"]])
        assert r["repetida"] is False and [li["nombre"] for o in r["ordenes"] for li in o["lineas"]] == ["Sal"]
        with e["abrir"]() as conn:                                                # deshacemos para la próxima vuelta de la prueba
            conn.execute("DELETE FROM purchase_order_items WHERE purchase_order_id = ?", (r["ordenes"][0]["id"],))
            conn.execute("DELETE FROM purchase_orders WHERE id = ?", (r["ordenes"][0]["id"],))
            conn.commit()
    assert _generar(e["abrir"], clave="Aa_b", producto_ids=[e["yerba"]])["repetida"] is True


def test_la_misma_clave_con_otros_datos_es_un_conflicto_y_no_devuelve_lo_anterior(escenario):
    e = escenario
    _generar(e["abrir"], clave="k", producto_ids=[e["yerba"]])
    for cambio in ({"producto_ids": [e["sal"]]}, {"producto_ids": [e["yerba"]], "dias_cobertura": 20},
                   {"producto_ids": [e["yerba"]], "topes": {e["yerba"]: 2}}, {"producto_ids": None}):
        with pytest.raises(reposicion_ordenes.ClaveReusada), e["abrir"]() as conn:
            reposicion_ordenes.generar_ordenes_borrador(conn, clave_operacion="k", hoy=HOY, **cambio)
    assert _generar(e["abrir"], clave="k", producto_ids=[e["yerba"]])["repetida"] is True      # los mismos datos siguen siendo el reintento
    assert _cantidad_de_ordenes(e["abrir"]) == 1


def test_el_router_contesta_409_a_una_clave_reusada_con_otros_datos(escenario):
    e = escenario
    c = _cliente(e["abrir"])
    assert c.post("/api/reportes/reposicion/ordenes", json={"clave_operacion": "k", "producto_ids": [e["yerba"]]}).status_code == 200
    assert c.post("/api/reportes/reposicion/ordenes", json={"clave_operacion": "k", "producto_ids": [e["sal"]]}).status_code == 409


def test_el_tope_se_redondea_hacia_abajo_a_la_unidad_del_producto(escenario):
    """Yerba se pide entera: un tope de 5,5 pide 5; un tope de 0,5 no alcanza ni una unidad y la línea se omite (no se crea una orden de 0,5)."""
    e = escenario
    r = _generar(e["abrir"], clave="a", topes={e["yerba"]: 5.5})
    assert Decimal({li["nombre"]: li["cantidad"] for o in r["ordenes"] for li in o["lineas"]}["Yerba"]) == Decimal("5")
    r = _generar(e["abrir"], clave="b", producto_ids=[e["sal"]], topes={e["sal"]: 0.5})
    assert r["ordenes"] == [] and r["omitidos"] == [e["sal"]]
    assert _cantidad_de_ordenes(e["abrir"]) == 2                                          # la de Norte (5) y la de Sur de la primera tanda



def test_un_reintento_exacto_devuelve_tambien_lo_que_no_se_pidio(escenario):
    e = escenario
    primera = _generar(e["abrir"], clave="mixta", producto_ids=[e["yerba"], e["azucar"], e["fideos"]])
    assert [x["nombre"] for x in primera["sin_proveedor"]] == ["Azúcar"] and primera["omitidos"] == [e["fideos"]]
    otra = _generar(e["abrir"], clave="mixta", producto_ids=[e["yerba"], e["azucar"], e["fideos"]])
    assert otra["repetida"] is True
    assert otra["sin_proveedor"] == primera["sin_proveedor"] and otra["omitidos"] == primera["omitidos"]


def test_una_peticion_que_no_crea_nada_no_deja_registro_y_se_puede_repetir(escenario):
    e = escenario
    vacia = _generar(e["abrir"], clave="vacia", producto_ids=[e["azucar"]])               # sólo un producto sin proveedor
    assert vacia["ordenes"] == [] and vacia["repetida"] is False
    _con_proveedor(e["abrir"], e["azucar"], e["norte"])
    despues = _generar(e["abrir"], clave="vacia", producto_ids=[e["azucar"]])             # ya tiene proveedor: ahora sí crea
    assert [o["supplier_party_id"] for o in despues["ordenes"]] == [e["norte"]]


def test_dos_pedidos_a_la_vez_no_duplican_las_ordenes(escenario):
    """Dos hilos piden lo mismo con claves distintas (o con la misma): el segundo espera al primero y encuentra sus órdenes ya contadas como «en camino».
    Sin el bloqueo previo, los dos leen lo mismo y cada uno crea las suyas."""
    import threading

    e = escenario
    resultados: list = []
    errores: list = []
    barrera = threading.Barrier(2)

    def pedir(clave):
        try:
            barrera.wait(timeout=10)
            resultados.append(_generar(e["abrir"], clave=clave))
        except Exception as exc:  # noqa: BLE001 - se informa abajo
            errores.append(exc)

    hilos = [threading.Thread(target=pedir, args=(c,)) for c in ("hilo-a", "hilo-b")]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=60)
    assert not errores, errores
    assert _cantidad_de_ordenes(e["abrir"]) == 2                                           # una por proveedor, no cuatro
    assert sum(len(r["ordenes"]) for r in resultados) == 2
