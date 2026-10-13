"""«Fijar el stock en N» sin decir el depósito (ADR-057), contra los dos motores.

`ajustar_stock` sin `deposito_id` compara N contra el total de TODOS los depósitos y escribe la diferencia en el depósito por defecto: con el stock repartido en dos, «fijar en 5»
sobre 16 (10 + 6) escribía −11 en el principal y dejaba el otro intacto. Con `OpcionesStock.por_deposito` (la pantalla elige el depósito) y más de un depósito activo, el router
ya no adivina: 422 «Indicá el depósito…». Con un solo depósito activo no hay a dónde equivocarse y sigue igual que siempre.
"""

from __future__ import annotations

from test_web_catalogo import _app, _con_sedes, _crear_producto, _producto_con_stock_en

from libracommerce.web.catalogo_router import OpcionesStock


def _repartir(client):
    """10 en Central y 6 en la sucursal: 16 en total, repartido en dos depósitos activos."""
    p, central, otro = _producto_con_stock_en(client, cantidad=10)
    r = client.post(f"/api/stock/{p['id']}/ajuste", json={"modo": "entrada", "cantidad": 6, "deposito_id": otro["id"]})
    assert r.status_code == 200, r.text
    return p, central, otro


def _por_deposito(client):
    return client.get("/api/stock").json()["productos"][0]["por_deposito"]


def test_absoluto_sin_deposito_con_stock_repartido_en_dos_es_422_y_no_escribe(abrir):
    client = _con_sedes(abrir)
    p, central, otro = _repartir(client)
    antes = _por_deposito(client)
    assert antes == {str(central["id"]): 10, str(otro["id"]): 6}

    r = client.post(f"/api/stock/{p['id']}/ajuste", json={"modo": "absoluto", "cantidad": 5})
    assert r.status_code == 422
    assert r.json()["detail"].startswith("Indicá el depósito")
    assert _por_deposito(client) == antes                       # nada se movió
    assert client.get(f"/api/stock/{p['id']}").json()["stock_actual"] == 16


def test_con_el_deposito_el_conteo_absoluto_va_a_ese_deposito(abrir):
    client = _con_sedes(abrir)
    p, central, otro = _repartir(client)
    r = client.post(f"/api/stock/{p['id']}/ajuste", json={"modo": "absoluto", "cantidad": 5, "deposito_id": otro["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["stock_deposito"] == 5 and r.json()["stock_actual"] == 15
    assert _por_deposito(client) == {str(central["id"]): 10, str(otro["id"]): 5}


def test_con_un_solo_deposito_activo_es_igual_que_siempre(abrir):
    """Un depósito activo (el otro dado de baja): no hay a dónde equivocarse, sin `deposito_id` ajusta ahí como hoy."""
    client = _con_sedes(abrir)
    p = _crear_producto(client)
    client.post(f"/api/stock/{p['id']}/ajuste", json={"modo": "entrada", "cantidad": 10})
    # La instancia de prueba tiene sólo el depósito principal; uno más, dado de baja, tampoco cuenta.
    viejo = client.post("/api/depositos", json={"nombre": "Viejo"}).json()
    client.put(f"/api/depositos/{viejo['id']}", json={"nombre": "Viejo", "activo": False})
    r = client.post(f"/api/stock/{p['id']}/ajuste", json={"modo": "absoluto", "cantidad": 4})
    assert r.status_code == 200, r.text
    assert r.json()["stock_actual"] == 4


def test_los_otros_modos_sin_deposito_no_cambian(abrir):
    """Entrada, salida y merma sin depósito van al por defecto, como siempre: la regla es sólo del conteo absoluto."""
    client = _con_sedes(abrir)
    p, _central, _otro = _repartir(client)
    assert client.post(f"/api/stock/{p['id']}/ajuste", json={"modo": "entrada", "cantidad": 1}).status_code == 200
    assert client.post(f"/api/stock/{p['id']}/ajuste", json={"modo": "salida", "cantidad": 2}).status_code == 200
    assert client.get(f"/api/stock/{p['id']}").json()["stock_actual"] == 15


def test_sin_por_deposito_el_ajuste_es_el_de_siempre_aunque_haya_varios_depositos(abrir):
    """Sin la opción (Contalibra, Restolibra) la pantalla no elige depósito: el ajuste absoluto es el de siempre."""
    client = _app(abrir, stock=OpcionesStock())
    p, _central, _otro = _producto_con_stock_en(client, cantidad=10)
    r = client.post(f"/api/stock/{p['id']}/ajuste", json={"modo": "absoluto", "cantidad": 5})
    assert r.status_code == 200, r.text
    assert r.json()["stock_actual"] == 5
