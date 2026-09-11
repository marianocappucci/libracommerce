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

from libracommerce.erp import Hooks, Insumo
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

