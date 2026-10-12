"""El servidor valida el precio de cada línea de la venta (ADR-049): `OpcionesVentas.validar_precios`.

`POST /api/ventas` guardaba el `precio` que mandaba el cliente. Con la opción prendida, antes de escribir nada el motor
calcula el precio de cada línea como lo hace el POS (lista predeterminada con el quiebre por producto, o el plano) y, si
alguna no coincide, contesta 409 con los precios esperados. Contra los dos motores (`abrir_ventas`).

Lo que fijan: la opción apagada no cambia nada; un precio correcto pasa; uno distinto da 409 **y no escribe nada** (ni
venta, ni stock, ni caja, ni pagos); el quiebre cuenta por producto entre variantes; sin lista (o sin precio en ella) rige
el plano; la balanza por importe y por peso, con la etiqueta válida y con las que no valen; el ítem libre y el descuento
manual se rechazan; y que las promociones sigan calculándolas el servidor.
"""

from __future__ import annotations

import datetime
import json

import pytest
from conftest import USUARIO, _usuario
from fastapi import FastAPI
from fastapi.testclient import TestClient

from libracommerce.domain.scale import ScaleFormat, ScaleValueKind
from libracommerce.erp import catalogo as erp_catalogo
from libracommerce.erp import listas_precio as lp
from libracommerce.web.catalogo_router import build_productos_router, build_stock_router
from libracommerce.web.listas_router import (
    build_listas_precio_router,
    build_precios_vigentes_router,
    build_quiebres_router,
)
from libracommerce.web.promociones_router import build_promociones_router
from libracommerce.web.ventas_router import OpcionesVentas, build_ventas_router

HOY = datetime.date.today().isoformat()

PESO = ScaleFormat()  # prefijo "20", 5+5 dígitos, por peso, divisor 1000
IMPORTE = ScaleFormat(value_kind=ScaleValueKind.AMOUNT, divisor=100)

#: Las tablas que una venta escribe: ninguna puede cambiar cuando se la rechaza.
_TABLAS = ("sales", "sale_items", "stock_movements", "caja_movimientos", "ventas_pagos")


@pytest.fixture(autouse=True)
def _admin():
    USUARIO["role"] = "admin"
    yield
    USUARIO.pop("role", None)


def _app(abrir, opciones=None) -> TestClient:
    app = FastAPI()
    app.include_router(build_productos_router(conexion=abrir, usuario_actual=_usuario))
    app.include_router(build_stock_router(conexion=abrir, usuario_actual=_usuario))
    app.include_router(build_listas_precio_router(conexion=abrir))
    app.include_router(build_quiebres_router(conexion=abrir))
    app.include_router(build_precios_vigentes_router(conexion=abrir))
    app.include_router(build_promociones_router(conexion=abrir))
    app.include_router(build_ventas_router(conexion=abrir, usuario_actual=_usuario, opciones=opciones))
    return TestClient(app)


@pytest.fixture
def abrir(abrir_ventas):
    return abrir_ventas


@pytest.fixture
def client(abrir):
    """El POS de VentaLibra: con la validación prendida."""
    return _app(abrir, OpcionesVentas(validar_precios=True))


def _producto(client, nombre="Yerba", precio=100.0, **extra):
    r = client.post("/api/productos", json={"nombre": nombre, "precio_venta": precio, "precio_costo": 1.0, **extra})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _con_stock(client, pid, cantidad=50):
    r = client.post(f"/api/stock/{pid}/ajuste", json={"modo": "absoluto", "cantidad": cantidad})
    assert r.status_code == 200, r.text


def _lista_default(client, abrir, precios: dict[int, float] | None = None, quiebres: dict[int, tuple[float, float]] | None = None):
    """Una lista predeterminada activa con `precios` y, por producto, un quiebre `(cantidad mínima, precio)`."""
    lid = client.post("/api/listas-precio", json={"nombre": "Mostrador", "descripcion": ""}).json()["id"]
    if precios:
        r = client.put(f"/api/listas-precio/{lid}/items", json={"precios": {str(k): v for k, v in precios.items()}})
        assert r.status_code == 200, r.text
    with abrir() as conn:
        lp.set_lista_precio_default(conn, lid)
        for pid, (minimo, monto) in (quiebres or {}).items():
            lp.set_quiebres(conn, lid, pid, [{"min_quantity": minimo, "amount": monto}])
    return lid


def _variantes(client, pid, *nombres):
    ids = []
    for n in nombres:
        r = client.post(f"/api/productos/{pid}/variantes", json={"sku": f"SKU-{pid}-{n}", "nombre": n})
        assert r.status_code == 200, r.text
        ids.append(r.json()["id"])
    return ids


def _item(pid, qty=1, precio=100.0, nombre="Producto", **extra):
    return {"nombre": nombre, "qty": qty, "precio": precio, "producto_id": pid, **extra}


def _post(client, items, monto=None, **extra):
    total = monto if monto is not None else sum(i["qty"] * i["precio"] for i in items)
    return client.post("/api/ventas", json={
        "fecha": HOY, "items": items, "pagos": [{"medio": "efectivo", "monto": total}], **extra})


def _conteos(abrir) -> dict[str, int]:
    with abrir() as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in _TABLAS}


def _no_escribio_nada(abrir, antes: dict[str, int], client=None, pid=None, stock=None):
    assert _conteos(abrir) == antes
    if stock is not None:
        assert client.get(f"/api/stock/{pid}").json()["stock_actual"] == stock


# ── la opción apagada no cambia nada ─────────────────────────────────────


def test_apagada_guarda_el_precio_del_cliente_como_siempre(abrir):
    client = _app(abrir)  # opciones por defecto: validar_precios=False
    pid = _producto(client, precio=100.0)
    _lista_default(client, abrir, {pid: 90.0})
    r = _post(client, [_item(pid, 2, precio=1.0)], descuento=0.5)  # ni el precio ni el descuento se miran
    assert r.status_code == 200, r.text
    venta = r.json()
    assert venta["subtotal"] == 2.0 and venta["descuento"] == 0.5
    assert "precios" not in venta and "detail" not in venta


def test_apagada_acepta_items_libres_y_una_etiqueta_cualquiera(abrir):
    client = _app(abrir)
    r = _post(client, [{"nombre": "Suelto", "qty": 1, "precio": 10.0, "etiqueta": "no-es-una-etiqueta"}])
    assert r.status_code == 200, r.text


# ── el precio correcto pasa; el distinto da 409 y no escribe nada ────────


def test_el_precio_de_la_lista_pasa(client, abrir):
    pid = _producto(client, precio=100.0)
    _lista_default(client, abrir, {pid: 90.0})
    r = _post(client, [_item(pid, 2, precio=90.0)])
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 180.0


@pytest.mark.parametrize("enviado", [1.0, 0.0, 89.0, 91.0, 100.0, 9000.0])
def test_un_precio_distinto_es_409_con_el_esperado_y_no_escribe_nada(client, abrir, enviado):
    pid = _producto(client, precio=100.0)
    _con_stock(client, pid)
    _lista_default(client, abrir, {pid: 90.0})
    antes = _conteos(abrir)
    r = _post(client, [_item(pid, 2, precio=enviado)], monto=500.0)
    assert r.status_code == 409, r.text
    # El `detail` es un objeto: el `ApiError` del kit sólo conserva el `detail` (su texto es el `mensaje`).
    detalle = r.json()["detail"]
    assert detalle["codigo"] == "precios_distintos"
    assert detalle["mensaje"] == "Cambiaron los precios de 1 producto: revisá el total y volvé a cobrar."
    assert detalle["precios"] == [{"indice": 0, "producto_id": pid, "variante_id": None, "enviado": enviado, "esperado": 90.0}]
    _no_escribio_nada(abrir, antes, client, pid, stock=50.0)


def test_el_409_cuenta_los_productos_y_nombra_la_posicion_de_cada_linea(client, abrir):
    a, b, c = _producto(client, "A", 10.0), _producto(client, "B", 20.0), _producto(client, "C", 30.0)
    antes = _conteos(abrir)
    r = _post(client, [_item(a, 1, 10.0), _item(b, 1, 5.0), _item(c, 1, 1.0)], monto=100.0)
    assert r.status_code == 409
    assert r.json()["detail"]["mensaje"].startswith("Cambiaron los precios de 2 productos")
    assert [(p["indice"], p["producto_id"], p["esperado"]) for p in r.json()["detail"]["precios"]] == [(1, b, 20.0), (2, c, 30.0)]
    assert _conteos(abrir) == antes


def test_el_indice_es_la_posicion_en_el_pedido_aunque_se_descarten_lineas(client, abrir):
    """Una línea sin cantidad no se guarda, pero no corre las posiciones: el POS corrige la que le dicen."""
    pid = _producto(client, precio=100.0)
    r = _post(client, [_item(pid, 0, 100.0), _item(pid, 1, 50.0)], monto=50.0)
    assert r.status_code == 409
    assert [p["indice"] for p in r.json()["detail"]["precios"]] == [1]


def test_la_tolerancia_es_de_un_centavo(client, abrir):
    pid = _producto(client, precio=100.0)
    assert _post(client, [_item(pid, 1, 100.01)], monto=100.01).status_code == 200
    assert _post(client, [_item(pid, 1, 99.99)], monto=99.99).status_code == 200
    r = _post(client, [_item(pid, 1, 100.02)], monto=100.02)
    assert r.status_code == 409


def test_un_precio_negativo_se_rechaza_igual(client, abrir):
    pid = _producto(client, precio=100.0)
    antes = _conteos(abrir)
    r = _post(client, [_item(pid, 1, -100.0)], monto=100.0)
    assert r.status_code == 409 and r.json()["detail"]["precios"][0]["esperado"] == 100.0
    assert _conteos(abrir) == antes


# ── sin lista, el precio plano ───────────────────────────────────────────


def test_sin_lista_predeterminada_rige_el_precio_plano(client, abrir):
    pid = _producto(client, precio=100.0)
    assert _post(client, [_item(pid, 1, 100.0)]).status_code == 200
    r = _post(client, [_item(pid, 1, 80.0)], monto=80.0)
    assert r.status_code == 409 and r.json()["detail"]["precios"][0]["esperado"] == 100.0


def test_una_lista_que_no_es_la_predeterminada_no_cuenta(client, abrir):
    pid = _producto(client, precio=100.0)
    client.post("/api/listas-precio", json={"nombre": "Mayorista", "descripcion": ""})
    lid = client.get("/api/listas-precio").json()[0]["id"]
    client.put(f"/api/listas-precio/{lid}/items", json={"precios": {str(pid): 70.0}})
    assert _post(client, [_item(pid, 1, 100.0)]).status_code == 200
    assert _post(client, [_item(pid, 1, 70.0)], monto=70.0).status_code == 409


def test_una_lista_predeterminada_desactivada_no_cuenta(client, abrir):
    pid = _producto(client, precio=100.0)
    lid = _lista_default(client, abrir, {pid: 90.0})
    with abrir() as conn:
        conn.execute("UPDATE price_lists SET active=0 WHERE id=?", (lid,))
    assert _post(client, [_item(pid, 1, 100.0)]).status_code == 200
    assert _post(client, [_item(pid, 1, 90.0)], monto=90.0).status_code == 409


def test_si_la_lista_no_tiene_precio_para_el_producto_rige_el_plano(client, abrir):
    en_lista, fuera = _producto(client, "En lista", 100.0), _producto(client, "Fuera", 50.0)
    _lista_default(client, abrir, {en_lista: 90.0})
    assert _post(client, [_item(en_lista, 1, 90.0), _item(fuera, 1, 50.0)]).status_code == 200
    r = _post(client, [_item(en_lista, 1, 90.0), _item(fuera, 1, 45.0)], monto=135.0)
    assert r.status_code == 409
    assert [(p["indice"], p["esperado"]) for p in r.json()["detail"]["precios"]] == [(1, 50.0)]


def test_una_vigencia_futura_no_rige_todavia(client, abrir):
    """El precio de la lista se calcula a ahora: una fila que empieza mañana no es el precio de hoy."""
    pid = _producto(client, precio=100.0)
    lid = _lista_default(client, abrir, {pid: 90.0})
    manana = (datetime.datetime.now() + datetime.timedelta(days=1)).replace(microsecond=0).isoformat()
    with abrir() as conn:
        lp.set_precio_vigente(conn, lid, pid, 50.0, desde=manana)
    assert _post(client, [_item(pid, 1, 90.0)]).status_code == 200


# ── el quiebre por cantidad cuenta por producto ──────────────────────────


def test_dos_variantes_de_a_una_unidad_pagan_el_quiebre(client, abrir):
    pid = _producto(client, "Remera", 100.0)
    m, l_ = _variantes(client, pid, "M", "L")
    _lista_default(client, abrir, {pid: 100.0}, {pid: (2, 80.0)})
    r = _post(client, [_item(pid, 1, 80.0, variante_id=m), _item(pid, 1, 80.0, variante_id=l_)])
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 160.0


def test_dos_variantes_al_precio_base_cuando_corresponde_el_quiebre_son_409(client, abrir):
    pid = _producto(client, "Remera", 100.0)
    m, l_ = _variantes(client, pid, "M", "L")
    _lista_default(client, abrir, {pid: 100.0}, {pid: (2, 80.0)})
    antes = _conteos(abrir)
    r = _post(client, [_item(pid, 1, 100.0, variante_id=m), _item(pid, 1, 80.0, variante_id=l_)], monto=180.0)
    assert r.status_code == 409
    assert r.json()["detail"]["precios"] == [
        {"indice": 0, "producto_id": pid, "variante_id": m, "enviado": 100.0, "esperado": 80.0}]
    assert _conteos(abrir) == antes


def test_una_sola_variante_paga_el_precio_base(client, abrir):
    pid = _producto(client, "Remera", 100.0)
    m, _ = _variantes(client, pid, "M", "L")
    _lista_default(client, abrir, {pid: 100.0}, {pid: (2, 80.0)})
    assert _post(client, [_item(pid, 1, 100.0, variante_id=m)]).status_code == 200
    assert _post(client, [_item(pid, 1, 80.0, variante_id=m)], monto=80.0).status_code == 409


def test_un_producto_sin_variantes_en_dos_lineas_llega_al_quiebre(client, abrir):
    pid = _producto(client, "Alfajor", 100.0)
    _lista_default(client, abrir, {pid: 100.0}, {pid: (2, 80.0)})
    assert _post(client, [_item(pid, 1, 80.0), _item(pid, 1, 80.0)]).status_code == 200


def test_los_productos_distintos_no_se_suman_para_el_quiebre(client, abrir):
    a, b = _producto(client, "A", 100.0), _producto(client, "B", 100.0)
    _lista_default(client, abrir, {a: 100.0, b: 100.0}, {a: (2, 80.0), b: (2, 80.0)})
    assert _post(client, [_item(a, 1, 100.0), _item(b, 1, 100.0)]).status_code == 200


def test_una_variante_ajena_al_producto_es_422(client, abrir):
    a, b = _producto(client, "A", 100.0), _producto(client, "B", 100.0)
    (de_b,) = _variantes(client, b, "X")
    _lista_default(client, abrir, {a: 100.0})
    antes = _conteos(abrir)
    r = _post(client, [_item(a, 1, 100.0, variante_id=de_b)])
    assert r.status_code == 422 and "no pertenece" in r.json()["detail"]
    assert _conteos(abrir) == antes


# ── ítem libre, descuento manual y producto inexistente ──────────────────


def test_un_item_libre_se_rechaza(client, abrir):
    pid = _producto(client, precio=100.0)
    antes = _conteos(abrir)
    r = _post(client, [_item(pid, 1, 100.0), {"nombre": "Suelto", "qty": 1, "precio": 10.0}])
    assert r.status_code == 422
    assert "«Suelto» no es un producto del catálogo" in r.json()["detail"]
    assert _conteos(abrir) == antes


def test_un_descuento_manual_se_rechaza(client, abrir):
    pid = _producto(client, precio=100.0)
    antes = _conteos(abrir)
    r = _post(client, [_item(pid, 2, 100.0)], monto=100.0, descuento=100.0)
    assert r.status_code == 422 and "descuento manual" in r.json()["detail"]
    assert _conteos(abrir) == antes
    # El descuento en cero (lo que manda el POS) es lo de siempre.
    assert _post(client, [_item(pid, 2, 100.0)], descuento=0).status_code == 200


def test_una_cantidad_o_un_precio_infinito_es_422_y_no_revienta(client, abrir):
    pid = _producto(client, precio=100.0)
    antes = _conteos(abrir)
    for linea in ({"qty": float("inf"), "precio": 100.0}, {"qty": 1, "precio": float("inf")}):
        r = client.post("/api/ventas", content=json.dumps({
            "fecha": HOY, "items": [{"nombre": "X", "producto_id": pid, **linea}],
            "pagos": [{"medio": "efectivo", "monto": 100.0}]}), headers={"content-type": "application/json"})
        assert r.status_code == 422, r.text
    assert _conteos(abrir) == antes


def test_un_producto_inexistente_sigue_siendo_el_422_de_siempre(client, abrir):
    antes = _conteos(abrir)
    r = _post(client, [_item(99999, 1, 10.0)])
    assert r.status_code == 422 and "No existe el producto 99999" in r.json()["detail"]
    assert _conteos(abrir) == antes


def test_las_promociones_las_sigue_calculando_el_servidor(abrir):
    client = _app(abrir, OpcionesVentas(validar_precios=True, promociones=True))
    pid = _producto(client, "Alfajor", 100.0)
    r = client.post("/api/promociones", json={
        "nombre": "2x1", "tipo": "nxm", "paga": 1, "items": [{"producto_id": pid, "cantidad": 2}]})
    assert r.status_code == 200, r.text
    r = _post(client, [_item(pid, 2, 100.0)], monto=100.0)
    assert r.status_code == 200, r.text
    assert r.json()["subtotal"] == 200.0 and r.json()["descuento"] == 100.0 and r.json()["total"] == 100.0


# ── la balanza ───────────────────────────────────────────────────────────


def _con_balanza(client, abrir, fmt, nombre, precio, codigo="123", **extra):
    pid = _producto(client, nombre, precio, unidad="kg", permite_fraccion=True, **extra)
    with abrir() as conn:
        erp_catalogo.set_formato_balanza(conn, fmt)
        erp_catalogo.agregar_codigo_balanza(conn, pid, codigo)
    return pid


def test_balanza_por_importe_se_cobra_lo_que_dice_la_etiqueta(client, abrir):
    pid = _con_balanza(client, abrir, IMPORTE, "Queso", 1500.0)
    _lista_default(client, abrir, {pid: 1500.0})
    # 20 | 00123 | 00750 | 4 -> $7,50 (divisor 100): ni la lista ni el plano.
    r = _post(client, [_item(pid, 1, 7.5, etiqueta="2000123007504")])
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 7.5


def test_balanza_por_importe_con_otro_precio_es_409_y_lo_esperado_es_el_de_la_etiqueta(client, abrir):
    pid = _con_balanza(client, abrir, IMPORTE, "Queso", 1500.0)
    _con_stock(client, pid, 10)
    antes = _conteos(abrir)
    r = _post(client, [_item(pid, 1, 1500.0, etiqueta="2000123007504")], monto=1500.0)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["precios"] == [
        {"indice": 0, "producto_id": pid, "variante_id": None, "enviado": 1500.0, "esperado": 7.5}]
    _no_escribio_nada(abrir, antes, client, pid, stock=10.0)


def test_balanza_por_importe_sin_la_etiqueta_se_cobra_a_la_lista(client, abrir):
    """Sin etiqueta no hay importe de balanza al que acogerse: manda el precio de la lista o el plano."""
    pid = _con_balanza(client, abrir, IMPORTE, "Queso", 1500.0)
    r = _post(client, [_item(pid, 1, 7.5)], monto=7.5)
    assert r.status_code == 409 and r.json()["detail"]["precios"][0]["esperado"] == 1500.0


def test_balanza_por_importe_con_cantidad_distinta_de_uno_es_422(client, abrir):
    pid = _con_balanza(client, abrir, IMPORTE, "Queso", 1500.0)
    antes = _conteos(abrir)
    r = _post(client, [_item(pid, 2, 7.5, etiqueta="2000123007504")])
    assert r.status_code == 422 and "cantidad es 1" in r.json()["detail"]
    assert _conteos(abrir) == antes


def test_balanza_por_peso_valida_la_cantidad_y_el_precio_de_lista(client, abrir):
    pid = _con_balanza(client, abrir, PESO, "Jamón", 1500.0)
    _lista_default(client, abrir, {pid: 1200.0})
    # 20 | 00123 | 00750 | 4 -> 0,750 kg al precio de lista por kilo.
    r = _post(client, [_item(pid, 0.75, 1200.0, etiqueta="2000123007504")])
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 900.0


def test_balanza_por_peso_con_otra_cantidad_es_422(client, abrir):
    pid = _con_balanza(client, abrir, PESO, "Jamón", 1500.0)
    antes = _conteos(abrir)
    r = _post(client, [_item(pid, 1.5, 1500.0, etiqueta="2000123007504")])
    assert r.status_code == 422 and "no es la que dice la etiqueta" in r.json()["detail"]
    assert _conteos(abrir) == antes


def test_balanza_por_peso_con_otro_precio_por_kilo_es_409(client, abrir):
    pid = _con_balanza(client, abrir, PESO, "Jamón", 1500.0)
    r = _post(client, [_item(pid, 0.75, 1000.0, etiqueta="2000123007504")], monto=750.0)
    assert r.status_code == 409 and r.json()["detail"]["precios"][0]["esperado"] == 1500.0


def test_el_peso_de_la_etiqueta_suma_al_quiebre_del_producto(client, abrir):
    pid = _con_balanza(client, abrir, PESO, "Jamón", 1500.0)
    _lista_default(client, abrir, {pid: 1500.0}, {pid: (1, 1400.0)})
    # 0,750 + 0,750 = 1,5 kg: llega al quiebre de 1 kg.
    etiqueta = "2000123007504"
    r = _post(client, [_item(pid, 0.75, 1400.0, etiqueta=etiqueta), _item(pid, 0.75, 1400.0, etiqueta=etiqueta)])
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("etiqueta, motivo", [
    ("no-es-un-ean", "no se puede leer"),
    ("7790123456789", "no se puede leer"),     # un código de barras común, no del formato de la balanza
    ("2000123007", "no se puede leer"),        # le faltan dígitos
    ("2000999007504", "es de otro producto"),  # el producto 999 de la balanza, que no es el de la línea
])
def test_una_etiqueta_que_no_vale_es_422_y_no_escribe_nada(client, abrir, etiqueta, motivo):
    pid = _con_balanza(client, abrir, PESO, "Jamón", 1500.0)
    otro = _producto(client, "Otro", 10.0)
    with abrir() as conn:
        erp_catalogo.agregar_codigo_balanza(conn, otro, "999")
    antes = _conteos(abrir)
    r = _post(client, [_item(pid, 0.75, 1500.0, etiqueta=etiqueta)])
    assert r.status_code == 422 and motivo in r.json()["detail"], r.text
    assert _conteos(abrir) == antes


def test_una_etiqueta_con_codigo_de_balanza_que_no_esta_cargado_es_422(client, abrir):
    pid = _con_balanza(client, abrir, PESO, "Jamón", 1500.0)
    r = _post(client, [_item(pid, 0.75, 1500.0, etiqueta="2000777007504")])
    assert r.status_code == 422 and "es de otro producto" in r.json()["detail"]


def test_la_etiqueta_de_otro_producto_en_la_linea_es_422(client, abrir):
    jamon = _con_balanza(client, abrir, PESO, "Jamón", 1500.0)
    queso = _producto(client, "Queso", 2000.0, unidad="kg")
    with abrir() as conn:
        erp_catalogo.agregar_codigo_balanza(conn, queso, "456")
    # La etiqueta del queso (456) en una línea del jamón: el precio de un producto no se cobra por el otro.
    r = _post(client, [_item(jamon, 0.75, 1500.0, etiqueta="2000456007504")])
    assert r.status_code == 422 and "es de otro producto" in r.json()["detail"]


def test_una_etiqueta_con_la_balanza_apagada_es_422(client, abrir):
    pid = _con_balanza(client, abrir, PESO, "Jamón", 1500.0)
    with abrir() as conn:
        erp_catalogo.set_formato_balanza(conn, None)
    r = _post(client, [_item(pid, 0.75, 1500.0, etiqueta="2000123007504")])
    assert r.status_code == 422 and "no se puede leer" in r.json()["detail"]


def test_la_etiqueta_no_puede_ser_enorme(client, abrir):
    pid = _producto(client, precio=100.0)
    r = _post(client, [_item(pid, 1, 100.0, etiqueta="2" * 65)])
    assert r.status_code == 422
