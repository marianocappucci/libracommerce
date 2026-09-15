"""Las factories de catálogo, depósitos y stock, montadas como las monta un
producto y ejercitadas por HTTP contra los DOS motores (P9-M1).

El contrato que se fija es el de los productos: estos tests son, en su
mayoría, los de `tests/test_productos_stock.py` y
`tests/test_transferencias_deposito.py` de Contalibra y Restolibra portados
tal cual, más los comportamientos que existían en uno solo (código
autogenerado, merma con motivo, conversión de unidad de compra, fijar en
negativo → 422) y el gancho de recetas. Cuando los productos adopten las
factories, sus suites siguen corriendo sin tocar: ése es el gate real.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from conftest import USUARIO, _usuario  # noqa: F401  (y la fixture `abrir`, que pytest carga sola)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from libracommerce.domain.scale import ScaleFormat, ScaleValueKind
from libracommerce.erp import Hooks, Insumo
from libracommerce.erp import catalogo as erp_catalogo
from libracommerce.erp import stock as erp_stock
from libracommerce.web.catalogo_router import (
    MOTIVOS_MERMA_GASTRONOMICOS,
    OpcionesCatalogo,
    OpcionesStock,
    build_depositos_router,
    build_productos_router,
    build_stock_router,
)


def _app(abrir, *, catalogo=None, stock=None) -> TestClient:
    app = FastAPI()
    app.include_router(build_productos_router(conexion=abrir, usuario_actual=_usuario, opciones=catalogo))
    app.include_router(build_depositos_router(conexion=abrir, usuario_actual=_usuario))
    app.include_router(build_stock_router(conexion=abrir, usuario_actual=_usuario, opciones=stock))
    return TestClient(app)


@pytest.fixture
def client(abrir):
    return _app(abrir)


@pytest.fixture
def client_gastronomico(abrir):
    """Como lo monta Restolibra: código autogenerado y mermas con motivo."""
    return _app(
        abrir,
        catalogo=OpcionesCatalogo(generar_codigo_si_falta=True),
        stock=OpcionesStock(motivos_merma=MOTIVOS_MERMA_GASTRONOMICOS),
    )


def _crear_producto(client, nombre="Yerba 1kg", **extra):
    payload = {"nombre": nombre, "codigo": extra.pop("codigo", ""),
               "precio_venta": 1500.0, "precio_costo": 900.0}
    payload.update(extra)
    resp = client.post("/api/productos", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _stock_de(client, pid):
    resp = client.get(f"/api/stock/{pid}")
    assert resp.status_code == 200, resp.text
    return resp.json()["stock_actual"]


# ── Los tests de los productos, tal cual ─────────────────────────────────


def test_crear_y_listar_producto(client):
    _crear_producto(client, "Yerba 1kg", codigo="Y001")
    nombres = [p["nombre"] for p in client.get("/api/productos").json()]
    assert "Yerba 1kg" in nombres


def test_actualizar_producto(client):
    p = _crear_producto(client)
    resp = client.put(f"/api/productos/{p['id']}", json={
        "nombre": "Yerba 1kg suave", "precio_venta": 1800.0, "precio_costo": 900.0})
    assert resp.status_code == 200
    assert resp.json()["nombre"] == "Yerba 1kg suave"
    assert resp.json()["precio_venta"] == 1800.0


def test_borrar_producto(client):
    p = _crear_producto(client, "Efimero")
    assert client.delete(f"/api/productos/{p['id']}").status_code == 200
    assert not any(x["id"] == p["id"] for x in client.get("/api/productos").json())
    assert client.delete(f"/api/productos/{p['id']}").status_code == 404


def test_categorias_crud(client):
    resp = client.post("/api/productos/categorias", json={"nombre": "Almacen"})
    assert resp.status_code == 200
    cats = client.get("/api/productos/categorias").json()
    assert any(c["nombre"] == "Almacen" for c in cats)
    cid = next(c["id"] for c in cats if c["nombre"] == "Almacen")
    assert client.delete(f"/api/productos/categorias/{cid}").status_code == 200
    assert client.post("/api/productos/categorias", json={"nombre": "  "}).status_code == 422


def test_ajuste_absoluto_fija_el_stock(client):
    p = _crear_producto(client, "Con stock")
    resp = client.post(f"/api/stock/{p['id']}/ajuste", json={"modo": "absoluto", "cantidad": 50})
    assert resp.status_code == 200, resp.text
    assert _stock_de(client, p["id"]) == 50


def test_entrada_y_salida_mueven_el_stock(client):
    p = _crear_producto(client, "Movido")
    client.post(f"/api/stock/{p['id']}/ajuste", json={"modo": "absoluto", "cantidad": 10})
    client.post(f"/api/stock/{p['id']}/ajuste", json={"modo": "entrada", "cantidad": 5})
    assert _stock_de(client, p["id"]) == 15
    client.post(f"/api/stock/{p['id']}/ajuste", json={"modo": "salida", "cantidad": 3})
    assert _stock_de(client, p["id"]) == 12


def test_movimientos_de_stock_quedan_registrados(client):
    p = _crear_producto(client, "Auditado")
    client.post(f"/api/stock/{p['id']}/ajuste",
                json={"modo": "absoluto", "cantidad": 7, "referencia": "conteo inicial"})
    resp = client.get("/api/stock/movimientos")
    assert resp.status_code == 200
    assert "conteo inicial" in resp.text
    assert resp.json()[0]["tipo"] == "ajuste"
    assert resp.json()[0]["usuario_id"] == USUARIO["id"]


def test_servicio_no_aparece_en_stock(client):
    """Un tipo='servicio' no tiene inventario: el listado de stock no lo trae
    como alerta ni como fila con stock, y la venta nunca lo descuenta."""
    p = _crear_producto(client, "Consultoria", tipo="servicio")
    assert p["tipo"] == "servicio"
    data = client.get("/api/stock").json()
    assert not any(a["id"] == p["id"] for a in data["alertas"])


def test_alertas_bajo_minimo(client):
    p = _crear_producto(client, "Escaso", stock_minimo=5)
    client.post(f"/api/stock/{p['id']}/ajuste", json={"modo": "absoluto", "cantidad": 3})
    data = client.get("/api/stock").json()
    assert [a["id"] for a in data["alertas"]] == [p["id"]]


# ── Depósitos y transferencias (tests de los productos) ──────────────────


def test_transferencia_entre_depositos(client):
    p = _crear_producto(client, "Plug RJ45")
    origen = client.post("/api/depositos", json={"nombre": "Central"}).json()
    client.post(f"/api/depositos/{origen['id']}/set-default")
    destino = client.post("/api/depositos", json={"nombre": "Sucursal"}).json()
    client.post(f"/api/stock/{p['id']}/ajuste", json={"modo": "absoluto", "cantidad": 10})
    resp = client.post("/api/depositos/transferir", json={
        "producto_id": p["id"], "origen_id": origen["id"], "destino_id": destino["id"], "cantidad": 4})
    assert resp.status_code == 200, resp.text
    por_deposito = {d["id"]: d["stock_actual"] for d in client.get(f"/api/depositos/stock-producto/{p['id']}").json()}
    assert por_deposito[origen["id"]] == 6 and por_deposito[destino["id"]] == 4
    assert _stock_de(client, p["id"]) == 10  # el total no cambia


def test_transferencia_sin_stock_es_422_con_el_texto_para_humanos(client):
    p = _crear_producto(client, "Vacio")
    origen = client.post("/api/depositos", json={"nombre": "Central"}).json()
    destino = client.post("/api/depositos", json={"nombre": "Otro"}).json()
    resp = client.post("/api/depositos/transferir", json={
        "producto_id": p["id"], "origen_id": origen["id"], "destino_id": destino["id"], "cantidad": 1})
    assert resp.status_code == 422
    assert "Stock insuficiente en depósito origen" in resp.json()["detail"]


# Lo que sigue viene de `tests/test_transferencias_deposito.py` de Contalibra y
# Restolibra, que lo tenian escrito dos veces --byte a byte-- contra estas
# mismas factories. Son las tres cosas que la delegacion en el motor NO tenia
# que cambiar, y las tres se degradan en silencio: nada da error, la pantalla
# simplemente dice otra cosa.


def _transferencia_con_stock(client, cantidad=100):
    """Un producto con `cantidad` unidades en el deposito default y un segundo
    deposito vacio al que transferir."""
    p = _crear_producto(client, "Plug RJ45")
    origen = client.post("/api/depositos", json={"nombre": "Central"}).json()
    client.post(f"/api/depositos/{origen['id']}/set-default")
    destino = client.post("/api/depositos", json={"nombre": "Camioneta"}).json()
    client.post(f"/api/stock/{p['id']}/ajuste", json={"modo": "entrada", "cantidad": cantidad, "referencia": "Carga inicial"})
    return p, origen, destino


def test_el_mensaje_de_stock_insuficiente_dice_cuanto_hay(client):
    """El 422 lo lee una persona. El error del dominio nombra ids ('el deposito
    3 para el item 7'), que no le dicen nada a quien mira una pantalla con
    nombres: por eso se traduce, y la traduccion tiene que decir cuanto hay."""
    p, origen, destino = _transferencia_con_stock(client)
    resp = client.post("/api/depositos/transferir", json={
        "producto_id": p["id"], "origen_id": origen["id"], "destino_id": destino["id"], "cantidad": 101})
    assert resp.status_code == 422
    detalle = resp.json()["detail"]
    assert "Stock insuficiente en depósito origen" in detalle
    assert "100" in detalle, "el mensaje tiene que decir cuanto hay disponible"


def test_la_transferencia_conserva_el_vocabulario_de_los_productos(client, abrir):
    """La pantalla de actividad de los productos muestra
    `COALESCE(reason_code, movement_type)` **sin mapa**: si el `reason_code` no
    viajara, pasaria a decir 'transfer_out' en produccion.

    🔴 **Por eso se mira la fila y no solo el listado.** `/api/stock/movimientos`
    traduce `transfer_out` de vuelta con `_MOVEMENT_TYPE_A_TIPO` cuando falta el
    `reason_code`, asi que el listado dice lo mismo con y sin el: se midio, la
    mutacion que saca el `reason_code` de las dos patas lo dejaba en verde. La
    pantalla de actividad lee la tabla, no este listado.
    """
    p, origen, destino = _transferencia_con_stock(client)
    client.post("/api/depositos/transferir", json={
        "producto_id": p["id"], "origen_id": origen["id"], "destino_id": destino["id"], "cantidad": 10})
    tipos = {m["tipo"] for m in client.get(f"/api/stock/movimientos?producto_id={p['id']}").json()}
    assert {"transferencia_salida", "transferencia_entrada"} <= tipos
    assert "transfer_out" not in tipos and "transfer_in" not in tipos

    conn = abrir()
    try:
        filas = conn.execute(
            "SELECT movement_type, reason_code FROM stock_movements "
            "WHERE movement_type IN ('transfer_out', 'transfer_in')").fetchall()
    finally:
        conn.close()
    assert {(f["movement_type"], f["reason_code"]) for f in filas} == {
        ("transfer_out", "transferencia_salida"), ("transfer_in", "transferencia_entrada"),
    }, "el reason_code tiene que quedar escrito en la fila, que es lo que lee la pantalla"


def test_la_observacion_de_la_transferencia_queda_en_el_movimiento(client):
    p, origen, destino = _transferencia_con_stock(client)
    client.post("/api/depositos/transferir", json={
        "producto_id": p["id"], "origen_id": origen["id"], "destino_id": destino["id"], "cantidad": 5,
        "observaciones": "Remito 5054 para Concordia"})
    referencias = {m.get("referencia") for m in client.get(f"/api/stock/movimientos?producto_id={p['id']}").json()}
    assert "Remito 5054 para Concordia" in referencias
    # El control: la carga inicial tambien esta, con la suya. Sin esto, un
    # listado que devolviera la misma referencia en todas las filas pasaria.
    assert "Carga inicial" in referencias


def test_el_stock_de_cada_deposito_y_el_listado_de_depositos(client):
    """Las dos lecturas que los productos usan para ver una transferencia, y
    que hasta el 2026-09-11 solo se ejercitaban desde sus suites:
    `/api/depositos/{id}/stock` (el stock de un deposito) y `/api/depositos`
    (el listado, con `total_productos`).

    Se midio al sacar `test_transferencias_deposito.py` de los productos: las
    doce lineas de estas dos rutas eran las unicas del motor que esos tests
    recorrian y la suite del motor no.
    """
    p, origen, destino = _transferencia_con_stock(client)
    client.post("/api/depositos/transferir", json={
        "producto_id": p["id"], "origen_id": origen["id"], "destino_id": destino["id"], "cantidad": 40})

    def stock_en(deposito_id):
        return {f["id"]: float(f["stock_actual"]) for f in client.get(f"/api/depositos/{deposito_id}/stock").json()}

    assert stock_en(origen["id"])[p["id"]] == 60
    assert stock_en(destino["id"])[p["id"]] == 40
    assert client.get("/api/depositos/99999/stock").status_code == 404

    listado = {d["id"]: d for d in client.get("/api/depositos").json()}
    assert listado[origen["id"]]["total_productos"] == 1
    assert listado[destino["id"]]["total_productos"] == 1


def test_deposito_default_y_borrado(client):
    nuevo = client.post("/api/depositos", json={"nombre": "Galpón", "descripcion": "atrás"}).json()
    assert client.put(f"/api/depositos/{nuevo['id']}", json={"nombre": "Galpón 2", "activo": True}).json()["nombre"] == "Galpón 2"
    assert client.post(f"/api/depositos/{nuevo['id']}/set-default").json()["es_default"]
    assert client.delete(f"/api/depositos/{nuevo['id']}").status_code == 422  # es el default
    otro = client.post("/api/depositos", json={"nombre": "Efímero"}).json()
    assert client.delete(f"/api/depositos/{otro['id']}").status_code == 200
    assert client.delete("/api/depositos/99999").status_code == 404


# ── F4 (v0.16.3): un deposito_id inexistente/inactivo en /transferir ──────
#
# Mismo hallazgo que en /api/ventas y /devolver: `catalogo.transferir_stock`
# no validaba `origen_id`/`destino_id` — un `destino_id` inexistente escribía
# la pata de salida y recién ahí reventaba con la IntegrityError de la FK
# (500, sin manejar); uno inactivo se aceptaba en silencio; y un `origen_id`
# inexistente terminaba en el 422 de "Stock insuficiente", engañoso.


def test_transferir_a_deposito_inexistente_da_422_no_500(client):
    p = _crear_producto(client, "Plug RJ45")
    client.post(f"/api/stock/{p['id']}/ajuste", json={"modo": "absoluto", "cantidad": 10})
    origen = client.get("/api/depositos").json()[0]
    resp = client.post("/api/depositos/transferir", json={
        "producto_id": p["id"], "origen_id": origen["id"], "destino_id": 999999, "cantidad": 1})
    assert resp.status_code == 422, resp.text
    assert "999999" in resp.json()["detail"]
    # Nada se movió: ni la pata de salida quedó escrita.
    assert _stock_de(client, p["id"]) == 10


def test_transferir_desde_deposito_inexistente_da_422_no_stock_insuficiente(client):
    """El mensaje tiene que nombrar el depósito que no existe, no el genérico
    de stock insuficiente (que hoy salía porque `current_stock` de un
    depósito inexistente da 0, indistinguible de "no hay stock")."""
    p = _crear_producto(client, "Plug RJ45")
    destino = client.post("/api/depositos", json={"nombre": "Sucursal"}).json()
    resp = client.post("/api/depositos/transferir", json={
        "producto_id": p["id"], "origen_id": 999999, "destino_id": destino["id"], "cantidad": 1})
    assert resp.status_code == 422, resp.text
    assert "999999" in resp.json()["detail"]
    assert "Stock insuficiente" not in resp.json()["detail"]


def test_transferir_a_deposito_inactivo_da_422(client):
    p = _crear_producto(client, "Plug RJ45")
    client.post(f"/api/stock/{p['id']}/ajuste", json={"modo": "absoluto", "cantidad": 10})
    origen = client.get("/api/depositos").json()[0]
    destino = client.post("/api/depositos", json={"nombre": "Baja"}).json()
    client.put(f"/api/depositos/{destino['id']}", json={"nombre": "Baja", "activo": False})
    resp = client.post("/api/depositos/transferir", json={
        "producto_id": p["id"], "origen_id": origen["id"], "destino_id": destino["id"], "cantidad": 1})
    assert resp.status_code == 422, resp.text
    assert _stock_de(client, p["id"]) == 10  # nada se movió


def test_transferir_desde_deposito_inactivo_da_422(client):
    p = _crear_producto(client, "Plug RJ45")
    client.post(f"/api/stock/{p['id']}/ajuste", json={"modo": "absoluto", "cantidad": 10})
    origen = client.get("/api/depositos").json()[0]  # el default original, con el stock
    otro = client.post("/api/depositos", json={"nombre": "Otro default"}).json()
    client.post(f"/api/depositos/{otro['id']}/set-default")  # origen deja de ser el default...
    client.put(f"/api/depositos/{origen['id']}", json={"nombre": origen["nombre"], "activo": False})  # ...y se puede desactivar
    resp = client.post("/api/depositos/transferir", json={
        "producto_id": p["id"], "origen_id": origen["id"], "destino_id": otro["id"], "cantidad": 1})
    assert resp.status_code == 422, resp.text


def test_transferir_stock_a_deposito_inexistente_erp_no_escribe_nada(abrir):
    """A nivel `erp.catalogo` (no HTTP): confirma que `DepositoInexistente`
    se levanta ANTES de escribir la pata de salida — no una `IntegrityError`
    de la FK a mitad de camino."""
    with abrir() as conn:
        pid = erp_catalogo.create_producto(conn, "Plug", precio_venta=10.0, precio_costo=5.0)
        origen_id = erp_catalogo.get_default_deposito_id(conn)
        erp_stock.add_movimiento_stock(conn, producto_id=pid, tipo="entrada", cantidad=10, deposito_id=origen_id)
        conn.commit()
    with abrir() as conn:
        n_antes = conn.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0]
        with pytest.raises(erp_catalogo.DepositoInexistente) as exc_info:
            erp_catalogo.transferir_stock(conn, producto_id=pid, origen_id=origen_id, destino_id=999999, cantidad=1)
        assert "999999" in str(exc_info.value)
        n_despues = conn.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0]
        assert n_despues == n_antes  # ni la pata de salida quedó escrita
        assert erp_stock.get_stock_actual(conn, pid) == 10.0


def test_transferir_stock_desde_deposito_inexistente_erp_no_stock_insuficiente(abrir):
    with abrir() as conn:
        pid = erp_catalogo.create_producto(conn, "Plug", precio_venta=10.0, precio_costo=5.0)
        destino_id = erp_catalogo.create_deposito(conn, "Sucursal")
        conn.commit()
    with abrir() as conn:
        with pytest.raises(erp_catalogo.DepositoInexistente) as exc_info:
            erp_catalogo.transferir_stock(conn, producto_id=pid, origen_id=999999, destino_id=destino_id, cantidad=1)
        assert "999999" in str(exc_info.value)
        assert "Stock insuficiente" not in str(exc_info.value)


def test_transferir_stock_a_deposito_inactivo_erp(abrir):
    with abrir() as conn:
        pid = erp_catalogo.create_producto(conn, "Plug", precio_venta=10.0, precio_costo=5.0)
        origen_id = erp_catalogo.get_default_deposito_id(conn)
        destino_id = erp_catalogo.create_deposito(conn, "Baja")
        erp_catalogo.update_deposito(conn, destino_id, "Baja", "", 0)  # inactivo, no default: se puede
        erp_stock.add_movimiento_stock(conn, producto_id=pid, tipo="entrada", cantidad=10, deposito_id=origen_id)
        conn.commit()
    with abrir() as conn:
        with pytest.raises(erp_catalogo.DepositoInexistente):
            erp_catalogo.transferir_stock(conn, producto_id=pid, origen_id=origen_id, destino_id=destino_id, cantidad=1)
        assert erp_stock.get_stock_actual(conn, pid) == 10.0


def test_transferir_stock_desde_deposito_inactivo_erp(abrir):
    with abrir() as conn:
        pid = erp_catalogo.create_producto(conn, "Plug", precio_venta=10.0, precio_costo=5.0)
        origen_id = erp_catalogo.create_deposito(conn, "Baja")
        destino_id = erp_catalogo.create_deposito(conn, "Sucursal")
        erp_stock.add_movimiento_stock(conn, producto_id=pid, tipo="entrada", cantidad=10, deposito_id=origen_id)
        erp_catalogo.update_deposito(conn, origen_id, "Baja", "", 0)  # no es default: se puede
        conn.commit()
    with abrir() as conn:
        with pytest.raises(erp_catalogo.DepositoInexistente):
            erp_catalogo.transferir_stock(conn, producto_id=pid, origen_id=origen_id, destino_id=destino_id, cantidad=1)
        assert erp_stock.get_stock_actual(conn, pid) == 10.0


# ── F4 (v0.16.3): un depósito default inactivo sigue recibiendo stock ─────
#
# `get_default_deposito_id` no mira `active`: sin estas guardas, un
# `update_deposito`/`set_default_deposito` podía dejar el default marcado
# inactivo, y toda venta/ajuste SIN `deposito_id` explícito le seguía
# cargando stock en silencio a un depósito que la pantalla mostraba dado de
# baja. `get_default_deposito_id` en sí NO se toca — la guarda va en quien
# escribe el estado del depósito, no en quien lo resuelve.


def test_no_se_puede_desactivar_el_deposito_default_por_http(client):
    default_id = client.get("/api/depositos").json()[0]["id"]
    resp = client.put(f"/api/depositos/{default_id}", json={"nombre": "Depósito principal", "activo": False})
    assert resp.status_code == 422, resp.text
    # Sigue activo: el rechazo no aplicó el cambio a medias.
    assert client.get("/api/depositos").json()[0]["activo"]


def test_no_se_puede_marcar_default_un_deposito_inactivo_por_http(client):
    nuevo = client.post("/api/depositos", json={"nombre": "Sucursal"}).json()
    client.put(f"/api/depositos/{nuevo['id']}", json={"nombre": "Sucursal", "activo": False})
    resp = client.post(f"/api/depositos/{nuevo['id']}/set-default")
    assert resp.status_code == 422, resp.text
    # El default sigue siendo el de antes, no el inactivo.
    assert not any(d["id"] == nuevo["id"] and d["es_default"] for d in client.get("/api/depositos").json())


def test_desactivar_un_deposito_que_no_es_el_default_sigue_andando(client):
    """Regresión: el caso válido (desactivar uno que no es default) no cambia."""
    nuevo = client.post("/api/depositos", json={"nombre": "Sucursal"}).json()
    resp = client.put(f"/api/depositos/{nuevo['id']}", json={"nombre": "Sucursal", "activo": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["activo"] is False or resp.json()["activo"] == 0


def test_marcar_default_un_deposito_activo_sigue_andando(client):
    """Regresión: el caso válido (marcar default uno activo) no cambia."""
    nuevo = client.post("/api/depositos", json={"nombre": "Sucursal"}).json()
    resp = client.post(f"/api/depositos/{nuevo['id']}/set-default")
    assert resp.status_code == 200, resp.text
    assert resp.json()["es_default"]


def test_no_se_puede_desactivar_el_deposito_default_erp(abrir):
    """🔑 mutación: sacar esta guarda de `update_deposito` deja el default
    marcado inactivo, y este test da rojo."""
    with abrir() as conn:
        default_id = erp_catalogo.get_default_deposito_id(conn)
        with pytest.raises(ValueError):
            erp_catalogo.update_deposito(conn, default_id, "Depósito principal", "", 0)
        # No se aplicó nada: sigue activo.
        assert erp_catalogo.get_deposito(conn, default_id)["activo"]


def test_no_se_puede_marcar_default_un_deposito_inactivo_erp(abrir):
    with abrir() as conn:
        default_id = erp_catalogo.get_default_deposito_id(conn)
        nuevo_id = erp_catalogo.create_deposito(conn, "Sucursal")
        erp_catalogo.update_deposito(conn, nuevo_id, "Sucursal", "", 0)
        conn.commit()
    with abrir() as conn:
        with pytest.raises(ValueError):
            erp_catalogo.set_default_deposito(conn, nuevo_id)
        # El default sigue siendo el de siempre.
        assert erp_catalogo.get_default_deposito_id(conn) == default_id


def test_desactivar_un_deposito_que_no_es_default_sigue_andando_erp(abrir):
    """Regresión: el caso válido no cambia."""
    with abrir() as conn:
        nuevo_id = erp_catalogo.create_deposito(conn, "Sucursal")
        erp_catalogo.update_deposito(conn, nuevo_id, "Sucursal", "", 0)
        conn.commit()
        assert not erp_catalogo.get_deposito(conn, nuevo_id)["activo"]


def test_marcar_default_un_deposito_activo_sigue_andando_erp(abrir):
    """Regresión: el caso válido no cambia."""
    with abrir() as conn:
        nuevo_id = erp_catalogo.create_deposito(conn, "Sucursal")
        erp_catalogo.set_default_deposito(conn, nuevo_id)
        conn.commit()
        assert erp_catalogo.get_default_deposito_id(conn) == nuevo_id


# ── Lo que existía en un solo producto ───────────────────────────────────


def test_el_codigo_se_autogenera_solo_si_el_producto_lo_pide(client, client_gastronomico):
    """Restolibra genera `BEB-0001`; Contalibra deja el código vacío."""
    assert _crear_producto(client, "Sin código")["codigo"] in (None, "")
    p1 = _crear_producto(client_gastronomico, "Coca", categoria="Bebidas")
    p2 = _crear_producto(client_gastronomico, "Sprite", categoria="Bebidas")
    assert (p1["codigo"], p2["codigo"]) == ("BEB-0001", "BEB-0002")


def test_estacion_y_vendible_viajan_y_vuelven(client_gastronomico):
    p = _crear_producto(client_gastronomico, "Harina", estacion="cocina", vendible=False)
    assert (p["estacion"], p["vendible"]) in (("cocina", 0), ("cocina", False))
    # Y un producto que no manda esos campos queda con los defaults históricos.
    q = _crear_producto(client_gastronomico, "Agua")
    assert q["estacion"] == "" and q["vendible"]


def test_merma_solo_con_motivos_declarados(client, client_gastronomico):
    p = _crear_producto(client, "Pan")
    client.post(f"/api/stock/{p['id']}/ajuste", json={"modo": "absoluto", "cantidad": 10})
    assert client.get("/api/stock/motivos-merma").json() == []
    assert client.post(f"/api/stock/{p['id']}/ajuste", json={"modo": "merma", "cantidad": 2}).status_code == 422

    g = _crear_producto(client_gastronomico, "Pan de campo")
    client_gastronomico.post(f"/api/stock/{g['id']}/ajuste", json={"modo": "absoluto", "cantidad": 10})
    assert "Quemado" in client_gastronomico.get("/api/stock/motivos-merma").json()
    resp = client_gastronomico.post(f"/api/stock/{g['id']}/ajuste",
                                    json={"modo": "merma", "cantidad": 2, "motivo": "Quemado"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["stock_actual"] == 8
    mov = client_gastronomico.get(f"/api/stock/movimientos?producto_id={g['id']}").json()[0]
    assert mov["tipo"] == "merma" and mov["referencia"] == "Merma: Quemado"


def test_entrada_con_conversion_de_unidad_de_compra(client_gastronomico):
    g = _crear_producto(client_gastronomico, "Queso rallado", unidad="g")
    resp = client_gastronomico.post(f"/api/stock/{g['id']}/ajuste", json={
        "modo": "entrada", "cantidad": 2, "unidad_compra": "bolsa", "factor": 500, "referencia": "Compra"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["stock_actual"] == 1000
    mov = client_gastronomico.get(f"/api/stock/movimientos?producto_id={g['id']}").json()[0]
    assert mov["referencia"] == "Compra (2 bolsa × 500)"
    assert client_gastronomico.post(f"/api/stock/{g['id']}/ajuste",
                                    json={"modo": "entrada", "cantidad": 1, "factor": -1}).status_code == 422  # 0 cae a 1, como en Restolibra


def test_fijar_en_negativo_es_422(client):
    p = _crear_producto(client, "Negativo")
    assert client.post(f"/api/stock/{p['id']}/ajuste", json={"modo": "absoluto", "cantidad": -1}).status_code == 422


def test_el_detalle_de_stock_trae_el_producto(client):
    p = _crear_producto(client, "Detallado")
    data = client.get(f"/api/stock/{p['id']}").json()
    assert data["producto"]["nombre"] == "Detallado" and data["stock_actual"] == 0
    assert client.get("/api/stock/99999").status_code == 404


def test_unidades_y_tipos(client):
    assert "kg" in client.get("/api/productos/unidades").json()
    assert client.get("/api/stock/tipos").json()["merma"] == "Merma"


# ── El gancho de recetas, sobre el caso de uso ───────────────────────────


def test_descontar_stock_venta_por_receta_o_por_el_producto(abrir, client_gastronomico):
    plato = _crear_producto(client_gastronomico, "Milanesa")
    carne = _crear_producto(client_gastronomico, "Carne", unidad="kg", vendible=False)
    pan = _crear_producto(client_gastronomico, "Pan rallado", unidad="kg", vendible=False)
    gaseosa = _crear_producto(client_gastronomico, "Gaseosa")
    for p, q in ((carne, 10), (pan, 5), (gaseosa, 20)):
        client_gastronomico.post(f"/api/stock/{p['id']}/ajuste", json={"modo": "absoluto", "cantidad": q})

    recibido = []

    def resolver(item_id, item):
        recibido.append((item_id, dict(item)))
        if item_id != plato["id"]:
            return None
        # "sin pan" viene en la línea: el gancho aplica los modificadores.
        insumos = [Insumo(carne["id"], Decimal("0.200"))]
        if item.get("modificadores") != "sin pan":
            insumos.append(Insumo(pan["id"], Decimal("0.050")))
        return insumos

    with abrir() as conn:
        erp_stock.descontar_stock_venta(conn, 123, [
            {"producto_id": plato["id"], "qty": 2, "modificadores": "sin pan"},
            {"producto_id": gaseosa["id"], "qty": 3},
            {"producto_id": None, "qty": 1},
        ], usuario_id=USUARIO["id"], hooks=Hooks(resolver_receta=resolver))
        conn.commit()

    assert [r[0] for r in recibido] == [plato["id"], gaseosa["id"]]
    assert recibido[0][1]["modificadores"] == "sin pan"
    assert _stock_de(client_gastronomico, carne["id"]) == pytest.approx(9.6)   # 0.200 × 2
    assert _stock_de(client_gastronomico, pan["id"]) == 5                      # sin pan
    assert _stock_de(client_gastronomico, plato["id"]) == 0                    # el plato no se descuenta
    assert _stock_de(client_gastronomico, gaseosa["id"]) == 17                 # sin receta: el producto
    movs = client_gastronomico.get(f"/api/stock/movimientos?producto_id={carne['id']}").json()
    assert movs[0]["tipo"] == "venta" and movs[0]["venta_id"] == 123 and "(receta)" in movs[0]["referencia"]


def test_sin_gancho_se_descuenta_el_producto(abrir, client):
    p = _crear_producto(client, "Reventa")
    client.post(f"/api/stock/{p['id']}/ajuste", json={"modo": "absoluto", "cantidad": 5})
    with abrir() as conn:
        erp_stock.descontar_stock_venta(conn, 9, [{"producto_id": p["id"], "qty": 2}])
        conn.commit()
    assert _stock_de(client, p["id"]) == 3


def test_el_vocabulario_de_tipos_es_la_union():
    assert {"merma", "produccion", "entrada", "salida", "ajuste", "venta"} <= set(erp_stock.TIPOS)
    assert erp_stock._tipo_de_row("waste", None) == "merma"
    assert erp_stock._tipo_de_row("waste", "salida") == "salida"  # el reason_code manda


# ── F1 de VentaLibra a LibraCommerce (2026-09-14): variantes ──────────────
# Réplica de `ventalibra/app/services/catalog.py::CatalogService` y su router
# (`add_variant`/`list_variants`/`get_variant`), aditivo sobre `item_variants`.


def test_listado_de_productos_no_cambia_si_no_se_piden_variantes(client):
    """El control del punto 2: Contalibra y Restolibra no mandan
    `incluir_variantes`, así que la respuesta no puede llevar la clave nueva."""
    p = _crear_producto(client, "Remera")
    client.post(f"/api/productos/{p['id']}/variantes", json={"sku": "REM-M", "nombre": "Talle M"})
    encontrado = next(x for x in client.get("/api/productos").json() if x["id"] == p["id"])
    assert "variantes" not in encontrado


def test_listado_de_productos_incluye_variantes_si_se_pide(client):
    p = _crear_producto(client, "Remera")
    client.post(f"/api/productos/{p['id']}/variantes",
                json={"sku": "REM-M", "nombre": "Talle M", "atributos": {"talle": "M"}})
    encontrado = next(x for x in client.get("/api/productos?incluir_variantes=true").json() if x["id"] == p["id"])
    assert [v["sku"] for v in encontrado["variantes"]] == ["REM-M"]
    assert encontrado["variantes"][0]["atributos"] == {"talle": "M"}


def test_variantes_crud(client):
    p = _crear_producto(client, "Zapatilla")
    otro = _crear_producto(client, "Otro producto")

    r = client.post(f"/api/productos/{p['id']}/variantes", json={"sku": "ZAP-40", "nombre": "Talle 40"})
    assert r.status_code == 200, r.text
    vid = r.json()["id"]
    assert [v["sku"] for v in client.get(f"/api/productos/{p['id']}/variantes").json()] == ["ZAP-40"]

    r = client.put(f"/api/productos/{p['id']}/variantes/{vid}",
                    json={"sku": "ZAP-40", "nombre": "Talle 40 (agotado)", "activa": False})
    assert r.status_code == 200, r.text
    assert r.json()["nombre"] == "Talle 40 (agotado)" and not r.json()["activa"]

    # SKU duplicado: error de datos del cliente, no del server.
    assert client.post(f"/api/productos/{p['id']}/variantes", json={"sku": "ZAP-40", "nombre": "Otra"}).status_code == 409
    # La variante es de `p`, no de `otro`.
    assert client.put(f"/api/productos/{otro['id']}/variantes/{vid}",
                       json={"sku": "X", "nombre": "Y"}).status_code == 404
    assert client.get("/api/productos/99999/variantes").status_code == 404
    assert client.post("/api/productos/99999/variantes", json={"sku": "X", "nombre": "Y"}).status_code == 404


# ── F1 de VentaLibra a LibraCommerce: escaneo (balanza y código común) ────
# Réplica de `ventalibra/app/services/scale.py::ScaleService.scan`.

PESO = ScaleFormat()  # prefijo "20", 5+5 dígitos, peso, igual que test_scale.py
IMPORTE = ScaleFormat(value_kind=ScaleValueKind.AMOUNT, divisor=100)


def test_escanear_codigo_de_barras_comun(client):
    p = _crear_producto(client, "Fideos", codigo="7791234567890")
    r = client.get("/api/productos/escanear?code=7791234567890")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["producto"]["id"] == p["id"]
    assert data["cantidad"] == 1.0
    assert data["precio_unitario"] is None
    assert data["de_balanza"] is False
    assert "variante" not in data


def test_escanear_codigo_inexistente_es_404(client):
    assert client.get("/api/productos/escanear?code=0000000000000").status_code == 404


def test_escanear_sku_de_una_variante(client):
    p = _crear_producto(client, "Remera")
    client.post(f"/api/productos/{p['id']}/variantes", json={"sku": "REM-M", "nombre": "Talle M"})
    r = client.get("/api/productos/escanear?code=REM-M")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["producto"]["id"] == p["id"]
    assert data["variante"]["sku"] == "REM-M"


def test_escanear_etiqueta_de_balanza_por_peso(client, abrir):
    # `permite_fraccion` es del código de unidad ("kg"), no del producto (ver
    # el comentario en `erp/catalogo.py::_producto_dict`), pero se declara acá
    # mismo en el alta por HTTP.
    p = _crear_producto(client, "Jamón cocido", unidad="kg", permite_fraccion=True)
    with abrir() as conn:
        erp_catalogo.set_formato_balanza(conn, PESO)
        erp_catalogo.agregar_codigo_balanza(conn, p["id"], "123")
        conn.commit()

    # 20 | 00123 | 00750 | 4 -> producto 123, 750 gramos
    r = client.get("/api/productos/escanear?code=2000123007504")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["producto"]["id"] == p["id"]
    assert data["cantidad"] == pytest.approx(0.750)
    assert data["precio_unitario"] is None
    assert data["de_balanza"] is True


def test_escanear_etiqueta_de_balanza_por_importe(client, abrir):
    p = _crear_producto(client, "Queso", unidad="kg")
    with abrir() as conn:
        erp_catalogo.set_formato_balanza(conn, IMPORTE)
        erp_catalogo.agregar_codigo_balanza(conn, p["id"], "123")

    # mismos dígitos que el de peso, pero con la balanza en modo importe:
    # 00750 -> $7,50 (divisor 100)
    r = client.get("/api/productos/escanear?code=2000123007504")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["cantidad"] == 1.0
    assert data["precio_unitario"] == pytest.approx(7.50)
    assert data["de_balanza"] is True


def test_escanear_etiqueta_de_producto_no_cargado_es_422(client, abrir):
    """El código se leyó bien -- lo que no existe es el producto 123 en la
    balanza. Un 404 mandaría a buscar un código mal escaneado que en realidad
    está bien."""
    with abrir() as conn:
        erp_catalogo.set_formato_balanza(conn, PESO)
    r = client.get("/api/productos/escanear?code=2000123007504")
    assert r.status_code == 422
    assert "123" in r.json()["detail"]


def test_escanear_peso_sobre_producto_que_no_admite_fraccion_es_422(client, abrir):
    """Guarda contra el error de carga: un código de balanza cargado en un
    producto que se vende por unidad, no por peso."""
    p = _crear_producto(client, "Six pack", unidad="u")  # "u" no admite fracción
    with abrir() as conn:
        erp_catalogo.set_formato_balanza(conn, PESO)
        erp_catalogo.agregar_codigo_balanza(conn, p["id"], "123")
    r = client.get("/api/productos/escanear?code=2000123007504")
    assert r.status_code == 422
    assert "fracciones" in r.json()["detail"]


def test_sin_balanza_configurada_todo_se_lee_como_codigo_comun(client):
    """Sin `set_formato_balanza`, ni siquiera un EAN con la forma de una
    etiqueta de balanza se interpreta como tal: se busca tal cual entre los
    códigos de barra, y no matchea nada -> 404 (no 422)."""
    assert client.get("/api/productos/escanear?code=2000123007504").status_code == 404


# ── Corrección de revisión: `permite_fraccion` no se resetea al editar ────
# `_upsert_unit` (repository.py) reescribe `units.allows_fraction` en CADA
# `save_catalog_item`; sin esto, cualquier PUT de un producto en "kg" sin
# mandar el campo apagaría la balanza por peso para TODOS los productos en
# "kg" (hallazgo del propio F1, corregido acá).


def _puede_pesar(client, abrir, pid, codigo_balanza):
    """True si un código de balanza por peso resuelve para `pid` (o sea, si
    la unidad de `pid` sigue admitiendo fracciones)."""
    with abrir() as conn:
        erp_catalogo.set_formato_balanza(conn, PESO)
        erp_catalogo.agregar_codigo_balanza(conn, pid, codigo_balanza)
        conn.commit()
    codigo = f"20{codigo_balanza.zfill(5)}007504"[:13]
    r = client.get(f"/api/productos/escanear?code={codigo}")
    return r.status_code == 200


def test_editar_sin_permite_fraccion_no_apaga_la_unidad(client, abrir):
    p = _crear_producto(client, "Jamón cocido", unidad="kg", permite_fraccion=True)
    # PUT sin `permite_fraccion` en el payload -- el caso de Contalibra y
    # Restolibra, que nunca lo mandan.
    r = client.put(f"/api/productos/{p['id']}", json={
        "nombre": "Jamón cocido premium", "precio_venta": 2000.0, "precio_costo": 1200.0, "unidad": "kg"})
    assert r.status_code == 200, r.text
    assert _puede_pesar(client, abrir, p["id"], "111")


def test_editar_con_permite_fraccion_false_explicito_si_apaga(client, abrir):
    p = _crear_producto(client, "Queso", unidad="kg", permite_fraccion=True)
    r = client.put(f"/api/productos/{p['id']}", json={
        "nombre": "Queso", "precio_venta": 100.0, "precio_costo": 60.0, "unidad": "kg",
        "permite_fraccion": False})
    assert r.status_code == 200, r.text
    assert not _puede_pesar(client, abrir, p["id"], "112")


def test_crear_sin_permite_fraccion_conserva_lo_que_ya_tenia_la_unidad(client, abrir):
    """Alta nueva sin declarar el campo: si "kg" ya venía en `True` por otro
    producto, no se resetea a `False`."""
    _crear_producto(client, "Primero en kg", unidad="kg", permite_fraccion=True)
    segundo = _crear_producto(client, "Segundo en kg", unidad="kg")  # sin el campo
    assert _puede_pesar(client, abrir, segundo["id"], "113")

