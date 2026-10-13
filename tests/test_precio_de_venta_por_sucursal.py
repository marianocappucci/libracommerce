"""El precio de venta por sucursal en la validación del servidor (ADR-058), contra los dos motores.

La lista de precios puede tener un precio propio de una sucursal (`item_prices.branch_id`) y el POS lo cotiza con `sucursal_id` (`POST /api/listas-precio/{id}/precios`). Pero
`OpcionesVentas.validar_precios` (ADR-049) validaba SIEMPRE contra el precio general: un POS que cotizaba bien por sucursal recibía un 409. Ahora el pedido puede declarar la
`sucursal_id` por la que cotizó, y el servidor valida contra el precio de la sucursal de la venta, que sale de la costura `OpcionesVentas.sucursal_de_la_venta` (por default, la
del depósito del que sale la venta).

La transición, para no romper a un POS anterior (una pantalla con el bundle viejo en el navegador): quien NO declara `sucursal_id` se valida contra el precio general, como hasta
v0.58. `exigir_precio_de_sucursal` cierra la transición cuando ya no quedan POS viejos.

Escenario de casi todas las pruebas: una yerba con precio general 100 en la lista predeterminada y un precio propio de 90 en la sucursal Centro; la sucursal Norte no tiene precio
propio. Cada sucursal tiene su depósito.
"""

from __future__ import annotations

import pytest
import test_validar_precio_de_venta as _vpv
from test_validar_precio_de_venta import (
    _app,
    _conteos,
    _item,
    _lista_default,
    _no_escribio_nada,
    _post,
    _producto,
)

from libracommerce.erp import catalogo
from libracommerce.web.ventas_router import OpcionesVentas, sucursal_del_deposito

_admin = _vpv._admin          # el usuario administrador de la suite hermana (autouse)
abrir = _vpv.abrir


@pytest.fixture
def mundo(abrir):
    """`(client, pid, centro, norte, dep_centro, dep_norte)`: yerba a 100 en la lista, 90 en Centro."""
    client = _app(abrir, OpcionesVentas(validar_precios=True))
    pid = _producto(client, precio=100.0)
    lid = _lista_default(client, abrir, {pid: 100.0})
    with abrir() as conn:
        centro = catalogo.create_sucursal(conn, "Centro")
        norte = catalogo.create_sucursal(conn, "Norte")
        dep_centro, dep_norte = catalogo.get_deposito_de_venta(conn, centro), catalogo.get_deposito_de_venta(conn, norte)
        conn.commit()
    r = client.post(f"/api/listas-precio/{lid}/items/{pid}/precio-vigente", json={"monto": 90.0, "sucursal_id": centro})
    assert r.status_code == 200, r.text
    client.post(f"/api/stock/{pid}/ajuste", json={"modo": "absoluto", "cantidad": 50})
    return {"client": client, "pid": pid, "lista": lid, "centro": centro, "norte": norte, "dep_centro": dep_centro, "dep_norte": dep_norte}


def _precios(r):
    return r.json()["detail"]["precios"]


# ── (a) sin precio de sucursal, nada cambia ──────────────────────────────


def test_a_sin_precio_propio_de_la_sucursal_rige_el_general_declare_o_no_la_sucursal(mundo, abrir):
    m = mundo
    for extra in ({}, {"sucursal_id": m["norte"], "deposito_id": m["dep_norte"]}, {"deposito_id": m["dep_norte"]}):
        r = _post(m["client"], [_item(m["pid"], 2, precio=100.0)], **extra)
        assert r.status_code == 200, (extra, r.text)
    antes = _conteos(abrir)
    r = _post(m["client"], [_item(m["pid"], 2, precio=90.0)], sucursal_id=m["norte"], deposito_id=m["dep_norte"])
    assert r.status_code == 409 and _precios(r)[0]["esperado"] == 100.0       # el precio de Centro no vale en Norte
    assert r.json()["detail"]["sucursal_id"] == m["norte"]
    assert _conteos(abrir) == antes


def test_a_sin_ningun_precio_por_sucursal_en_la_instancia_el_409_es_exactamente_el_de_siempre(abrir):
    """Ni una clave de más en el cuerpo del 409 cuando el pedido no declara sucursal: lo que lee un POS viejo es lo mismo."""
    client = _app(abrir, OpcionesVentas(validar_precios=True))
    pid = _producto(client, precio=100.0)
    _lista_default(client, abrir, {pid: 100.0})
    r = _post(client, [_item(pid, 1, precio=5.0)])
    assert r.status_code == 409
    assert set(r.json()["detail"]) == {"mensaje", "codigo", "precios"}


# ── (b) con precio de sucursal y un POS que manda sucursal ───────────────


def test_b_el_pos_que_manda_la_sucursal_paga_el_precio_de_la_sucursal(mundo):
    m = mundo
    r = _post(m["client"], [_item(m["pid"], 2, precio=90.0)], sucursal_id=m["centro"], deposito_id=m["dep_centro"])
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 180.0


def test_b_el_precio_general_en_esa_sucursal_es_409_con_el_esperado_y_no_escribe_nada(mundo, abrir):
    m = mundo
    antes = _conteos(abrir)
    r = _post(m["client"], [_item(m["pid"], 2, precio=100.0)], sucursal_id=m["centro"], deposito_id=m["dep_centro"])
    assert r.status_code == 409, r.text
    assert _precios(r) == [{"indice": 0, "producto_id": m["pid"], "variante_id": None, "enviado": 100.0, "esperado": 90.0}]
    assert r.json()["detail"]["sucursal_id"] == m["centro"]          # la sucursal con la que el servidor validó: el POS cotiza por ésa
    _no_escribio_nada(abrir, antes, m["client"], m["pid"], stock=50.0)


def test_b_la_cotizacion_del_pos_y_la_validacion_coinciden(mundo):
    """Lo que el POS cotiza con `sucursal_id` es lo que el servidor acepta: ida y vuelta completa."""
    m = mundo
    c = m["client"]
    linea = {"producto_id": m["pid"], "cantidad": 2}
    con = c.post(f"/api/listas-precio/{m['lista']}/precios", json={"lineas": [linea], "sucursal_id": m["centro"]}).json()["precios"][0]["precio"]
    sin = c.post(f"/api/listas-precio/{m['lista']}/precios", json={"lineas": [linea]}).json()["precios"][0]["precio"]
    assert (con, sin) == (90.0, 100.0)
    assert _post(c, [_item(m["pid"], 2, precio=con)], sucursal_id=m["centro"], deposito_id=m["dep_centro"]).status_code == 200
    assert _post(c, [_item(m["pid"], 2, precio=sin)], deposito_id=m["dep_norte"]).status_code == 200


# ── (c) depósito de otra sucursal ────────────────────────────────────────


def test_c_el_servidor_decide_la_sucursal_por_el_deposito_no_por_lo_que_diga_el_pedido(mundo):
    """El pedido dice «Centro» pero el depósito es de Norte (sin precio propio): rige el general. Un cliente no elige la sucursal de su depósito."""
    m = mundo
    pedido = dict(sucursal_id=m["centro"], deposito_id=m["dep_norte"])
    assert _post(m["client"], [_item(m["pid"], 1, precio=100.0)], **pedido).status_code == 200
    r = _post(m["client"], [_item(m["pid"], 1, precio=90.0)], **pedido)
    assert r.status_code == 409 and _precios(r)[0]["esperado"] == 100.0
    assert r.json()["detail"]["sucursal_id"] == m["norte"]


def test_c_sin_deposito_rige_la_sucursal_del_deposito_por_defecto(mundo, abrir):
    m = mundo
    with abrir() as conn:
        principal = catalogo.get_default_deposito_id(conn)
        sucursal = conn.execute("SELECT branch_id FROM locations WHERE id=?", (principal,)).fetchone()[0]
    esperado = 90.0 if sucursal == m["centro"] else 100.0
    r = _post(m["client"], [_item(m["pid"], 1, precio=esperado)], sucursal_id=m["centro"])
    assert r.status_code == 200, r.text


def test_c_un_deposito_sin_sucursal_vale_el_precio_general(mundo, abrir):
    m = mundo
    with abrir() as conn:
        suelto = catalogo.create_deposito(conn, "Depósito suelto")
        assert conn.execute("SELECT branch_id FROM locations WHERE id=?", (suelto,)).fetchone()[0] is None
        conn.commit()
    pedido = dict(sucursal_id=m["centro"], deposito_id=suelto)
    assert _post(m["client"], [_item(m["pid"], 1, precio=100.0)], **pedido).status_code == 200
    assert _post(m["client"], [_item(m["pid"], 1, precio=90.0)], **pedido).status_code == 409


# ── (d) un POS viejo, que no manda sucursal, no se rompe ─────────────────


def test_d_el_pos_viejo_sin_sucursal_sigue_validando_contra_el_precio_general(mundo, abrir):
    m = mundo
    # Vende desde el depósito de Centro, que tiene precio propio de 90, pero cotizó sin sucursal: 100. Hasta v0.58 era 200 y sigue siéndolo.
    r = _post(m["client"], [_item(m["pid"], 2, precio=100.0)], deposito_id=m["dep_centro"])
    assert r.status_code == 200, r.text
    antes = _conteos(abrir)
    r = _post(m["client"], [_item(m["pid"], 2, precio=90.0)], deposito_id=m["dep_centro"])
    assert r.status_code == 409 and _precios(r)[0]["esperado"] == 100.0       # y no se acepta "el que más le convenga": sin declarar, el general
    assert "sucursal_id" not in r.json()["detail"]
    assert _conteos(abrir) == antes


# ── la transición se cierra con la opción ────────────────────────────────


def test_exigir_precio_de_sucursal_valida_contra_la_sucursal_aunque_el_pos_no_la_declare(abrir):
    client = _app(abrir, OpcionesVentas(validar_precios=True, exigir_precio_de_sucursal=True))
    pid = _producto(client, precio=100.0)
    lid = _lista_default(client, abrir, {pid: 100.0})
    with abrir() as conn:
        centro = catalogo.create_sucursal(conn, "Centro")
        dep = catalogo.get_deposito_de_venta(conn, centro)
        conn.commit()
    client.post(f"/api/listas-precio/{lid}/items/{pid}/precio-vigente", json={"monto": 90.0, "sucursal_id": centro})
    r = _post(client, [_item(pid, 1, precio=100.0)], deposito_id=dep)           # el POS viejo ya no engaña al servidor
    assert r.status_code == 409 and _precios(r)[0]["esperado"] == 90.0
    assert _post(client, [_item(pid, 1, precio=90.0)], deposito_id=dep).status_code == 200


# ── la costura ───────────────────────────────────────────────────────────


def test_una_costura_propia_decide_la_sucursal(abrir):
    """Un producto con otra regla (por ejemplo, la sucursal del cajero) pasa la suya; recibe la conexión y el pedido."""
    vistos = []

    def del_cajero(conn, payload):
        vistos.append((payload.deposito_id, payload.sucursal_id))
        return norte[0]

    norte: list[int] = []
    client = _app(abrir, OpcionesVentas(validar_precios=True, sucursal_de_la_venta=del_cajero))
    pid = _producto(client, precio=100.0)
    lid = _lista_default(client, abrir, {pid: 100.0})
    with abrir() as conn:
        norte.append(catalogo.create_sucursal(conn, "Norte"))
        conn.commit()
    client.post(f"/api/listas-precio/{lid}/items/{pid}/precio-vigente", json={"monto": 80.0, "sucursal_id": norte[0]})
    assert _post(client, [_item(pid, 1, precio=80.0)], sucursal_id=norte[0]).status_code == 200
    assert vistos == [(None, norte[0])]
    assert _post(client, [_item(pid, 1, precio=100.0)]).status_code == 200          # sin declarar, ni se consulta la costura
    assert len(vistos) == 1


def test_el_default_de_la_costura_es_la_sucursal_del_deposito(mundo, abrir):
    m = mundo

    class Pedido:
        def __init__(self, deposito_id):
            self.deposito_id = deposito_id

    with abrir() as conn:
        assert sucursal_del_deposito(conn, Pedido(m["dep_centro"])) == m["centro"]
        assert sucursal_del_deposito(conn, Pedido(m["dep_norte"])) == m["norte"]
        suelto = catalogo.create_deposito(conn, "Suelto")
        assert sucursal_del_deposito(conn, Pedido(suelto)) is None
        principal = catalogo.get_default_deposito_id(conn)
        esperado = conn.execute("SELECT branch_id FROM locations WHERE id=?", (principal,)).fetchone()[0]
        assert sucursal_del_deposito(conn, Pedido(None)) == esperado                  # sin depósito, el por defecto
        assert sucursal_del_deposito(conn, Pedido(99999)) is None                      # inexistente: lo rechaza la venta, no esta costura


# ── lo que se arrastra: promociones, quiebres, balanza ───────────────────


def test_las_promociones_se_aplican_sobre_el_precio_de_la_sucursal(abrir):
    """`promociones.calcular` trabaja con el precio de las líneas, que ya está validado: en la sucursal con precio propio, un 2x1 ahorra UNA unidad a ese precio."""
    client = _app(abrir, OpcionesVentas(validar_precios=True, promociones=True))
    pid = _producto(client, "Alfajor", 100.0)
    lid = _lista_default(client, abrir, {pid: 100.0})
    with abrir() as conn:
        centro = catalogo.create_sucursal(conn, "Centro")
        dep = catalogo.get_deposito_de_venta(conn, centro)
        conn.commit()
    client.post(f"/api/listas-precio/{lid}/items/{pid}/precio-vigente", json={"monto": 90.0, "sucursal_id": centro})
    r = client.post("/api/promociones", json={"nombre": "2x1", "tipo": "nxm", "paga": 1, "items": [{"producto_id": pid, "cantidad": 2}]})
    assert r.status_code == 200, r.text
    r = _post(client, [_item(pid, 2, 90.0)], monto=90.0, sucursal_id=centro, deposito_id=dep)
    assert r.status_code == 200, r.text
    assert (r.json()["subtotal"], r.json()["descuento"], r.json()["total"]) == (180.0, 90.0, 90.0)
    assert [p["ahorro"] for p in r.json()["promociones"]] == [90.0]


def test_el_quiebre_por_cantidad_convive_con_el_precio_de_la_sucursal(mundo, abrir):
    """Entre un precio de sucursal y un quiebre de cantidad, `resolve_price` prefiere el más específico (la sucursal): la validación hace la misma cuenta que la cotización."""
    m = mundo
    from libracommerce.erp import listas_precio as lp

    with abrir() as conn:
        lp.set_quiebres(conn, m["lista"], m["pid"], [{"min_quantity": 5, "amount": 70.0}])
        conn.commit()
    cot = m["client"].post(f"/api/listas-precio/{m['lista']}/precios", json={"lineas": [{"producto_id": m["pid"], "cantidad": 6}], "sucursal_id": m["centro"]})
    cotizado = cot.json()["precios"][0]["precio"]
    r = _post(m["client"], [_item(m["pid"], 6, precio=cotizado)], sucursal_id=m["centro"], deposito_id=m["dep_centro"])
    assert r.status_code == 200, r.text
    # En Norte (sin precio propio) rige el quiebre general.
    assert _post(m["client"], [_item(m["pid"], 6, precio=70.0)], sucursal_id=m["norte"], deposito_id=m["dep_norte"]).status_code == 200
