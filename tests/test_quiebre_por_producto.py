"""El quiebre por cantidad cuenta por producto (ADR-048): `POST /api/listas-precio/{id}/precios`.

El quiebre (`item_prices.min_quantity`) se resolvía **por línea**: dos talles de una remera, de a una unidad, no
llegaban a un quiebre de 2, y un producto sin variantes escaneado dos veces en dos líneas tampoco. Las promociones
«llevá N pagá M» (ADR-045) ya agrupaban por `producto_id`; ahora el quiebre también. Estos tests corren contra los
dos motores (la fixture `abrir`) y fijan: la suma por producto con y sin variantes, que los productos distintos no se
suman, que se respeta la vigencia (`en`) y la sucursal, y que `GET /precio` (una línea, su cantidad) quedó como estaba.
"""

from __future__ import annotations

import pytest
from test_web_listas import _app, _lista, _producto

from libracommerce.erp import listas_precio as lp


@pytest.fixture
def client(abrir):
    return _app(abrir)


def _variantes(client, pid, *nombres):
    ids = []
    for n in nombres:
        r = client.post(f"/api/productos/{pid}/variantes", json={"sku": f"SKU-{pid}-{n}", "nombre": n})
        assert r.status_code == 200, r.text
        ids.append(r.json()["id"])
    return ids


def _producto_con_quiebre(client, abrir, lid, nombre="Remera", base=100.0, quiebre=(2, 80.0)):
    """Un producto con `base` en la lista y un quiebre `(cantidad mínima, precio)`."""
    p = _producto(client, nombre, precio_venta=999.0)
    client.put(f"/api/listas-precio/{lid}/items", json={"precios": {str(p["id"]): base}})
    if quiebre:
        with abrir() as conn:
            lp.set_quiebres(conn, lid, p["id"], [{"min_quantity": quiebre[0], "amount": quiebre[1]}])
    return p["id"]


def _precios(client, lid, lineas, **extra):
    r = client.post(f"/api/listas-precio/{lid}/precios", json={"lineas": lineas, **extra})
    assert r.status_code == 200, r.text
    return r.json()["precios"]


def _l(pid, cantidad=1, variante_id=None):
    return {"producto_id": pid, "variante_id": variante_id, "cantidad": cantidad}


# ── la regla ─────────────────────────────────────────────────────────────


def test_dos_variantes_de_a_una_unidad_llegan_al_quiebre_de_dos(client, abrir):
    lid = _lista(client)["id"]
    pid = _producto_con_quiebre(client, abrir, lid)
    m, l_ = _variantes(client, pid, "Talle M", "Talle L")
    precios = _precios(client, lid, [_l(pid, 1, m), _l(pid, 1, l_)])
    assert [p["precio"] for p in precios] == [80.0, 80.0]
    assert [p["cantidad_producto"] for p in precios] == [2.0, 2.0]
    assert [p["variante_id"] for p in precios] == [m, l_]


def test_un_producto_sin_variantes_en_dos_lineas_llega_al_quiebre(client, abrir):
    lid = _lista(client)["id"]
    pid = _producto_con_quiebre(client, abrir, lid)
    assert [p["precio"] for p in _precios(client, lid, [_l(pid), _l(pid)])] == [80.0, 80.0]


def test_una_sola_unidad_no_llega_al_quiebre(client, abrir):
    lid = _lista(client)["id"]
    pid = _producto_con_quiebre(client, abrir, lid)
    assert _precios(client, lid, [_l(pid)]) == [
        {"producto_id": pid, "variante_id": None, "cantidad_producto": 1.0, "precio": 100.0}]


def test_los_productos_distintos_no_se_suman(client, abrir):
    lid = _lista(client)["id"]
    remera = _producto_con_quiebre(client, abrir, lid, "Remera")
    pantalon = _producto_con_quiebre(client, abrir, lid, "Pantalón", base=200.0, quiebre=(2, 150.0))
    precios = _precios(client, lid, [_l(remera), _l(pantalon)])
    assert [p["precio"] for p in precios] == [100.0, 200.0]
    # Y mezclados: el quiebre de cada producto lo decide sólo su propia suma.
    precios = _precios(client, lid, [_l(remera), _l(pantalon), _l(remera), _l(pantalon, 3)])
    assert [p["precio"] for p in precios] == [80.0, 150.0, 80.0, 150.0]
    assert [p["cantidad_producto"] for p in precios] == [2.0, 4.0, 2.0, 4.0]


def test_la_cantidad_de_una_linea_suma_a_la_de_las_otras(client, abrir):
    lid = _lista(client)["id"]
    pid = _producto_con_quiebre(client, abrir, lid, quiebre=(5, 70.0))
    m, l_ = _variantes(client, pid, "Talle M", "Talle L")
    assert [p["precio"] for p in _precios(client, lid, [_l(pid, 3, m), _l(pid, 1, l_)])] == [100.0, 100.0]
    assert [p["precio"] for p in _precios(client, lid, [_l(pid, 3, m), _l(pid, 2, l_)])] == [70.0, 70.0]


def test_cada_quiebre_rige_para_el_total_del_producto(client, abrir):
    lid = _lista(client)["id"]
    pid = _producto_con_quiebre(client, abrir, lid, quiebre=None)
    with abrir() as conn:
        lp.set_quiebres(conn, lid, pid, [{"min_quantity": 2, "amount": 90}, {"min_quantity": 10, "amount": 60}])
    for lineas, esperado in (([1], 100.0), ([1, 1], 90.0), ([4, 5], 90.0), ([4, 6], 60.0), ([2, 2, 2, 2, 2], 60.0)):
        assert {p["precio"] for p in _precios(client, lid, [_l(pid, c) for c in lineas])} == {esperado}, lineas


def test_las_cantidades_se_suman_en_decimal(client, abrir):
    """0,1 + 0,2 en float es 0,30000000000000004: con un quiebre de 0,3 no se puede perder por eso (ni ganar por el redondeo)."""
    lid = _lista(client)["id"]
    pid = _producto_con_quiebre(client, abrir, lid, quiebre=None)
    with abrir() as conn:
        lp.set_quiebres(conn, lid, pid, [{"min_quantity": 0.3, "amount": 80}])
    precios = _precios(client, lid, [_l(pid, 0.1), _l(pid, 0.2)])
    assert [p["precio"] for p in precios] == [80.0, 80.0] and precios[0]["cantidad_producto"] == 0.3


def test_una_cantidad_que_no_es_positiva_no_suma(client, abrir):
    lid = _lista(client)["id"]
    pid = _producto_con_quiebre(client, abrir, lid)
    assert [p["precio"] for p in _precios(client, lid, [_l(pid, 1), _l(pid, 0), _l(pid, -3)])] == [100.0] * 3
    assert [p["precio"] for p in _precios(client, lid, [_l(pid, 0)])] == [100.0]  # no suma nada: el precio base


def test_un_producto_sin_precio_en_la_lista_devuelve_null(client, abrir):
    lid = _lista(client)["id"]
    sin_precio = _producto(client, "Sin precio")["id"]
    assert [p["precio"] for p in _precios(client, lid, [_l(sin_precio), _l(9999)])] == [None, None]


def test_sin_lineas_devuelve_una_lista_vacia(client, abrir):
    lid = _lista(client)["id"]
    assert _precios(client, lid, []) == []


# ── lo que ya manejaba /precio: vigencia y sucursal ──────────────────────


def test_respeta_la_vigencia_de_en(client, abrir):
    lid = _lista(client)["id"]
    pid = _producto_con_quiebre(client, abrir, lid, quiebre=None)
    # Un quiebre que rigió sólo en enero de 2020 (hasta las 23:30 del 31).
    client.post(f"/api/listas-precio/{lid}/items/{pid}/precio-vigente",
                json={"monto": 50, "cantidad_minima": 2, "desde": "2020-01-01T00:00:00", "hasta": "2020-01-31T23:30:00"})
    lineas = [_l(pid), _l(pid)]
    assert [p["precio"] for p in _precios(client, lid, lineas)] == [100.0, 100.0]  # ahora: vencido
    assert [p["precio"] for p in _precios(client, lid, lineas, en="2020-01-15T00:00:00")] == [50.0, 50.0]
    # `en` con zona (lo que manda `new Date().toISOString()`): 02:00 UTC del 1-feb es la noche del 31-ene en Argentina.
    assert [p["precio"] for p in _precios(client, lid, lineas, en="2020-02-01T02:00:00.000Z")] == [50.0, 50.0]


def test_respeta_el_precio_por_sucursal(client, abrir):
    lid = _lista(client)["id"]
    pid = _producto_con_quiebre(client, abrir, lid, quiebre=(2, 80.0))
    client.post(f"/api/listas-precio/{lid}/items/{pid}/precio-vigente",
                json={"monto": 60, "cantidad_minima": 2, "sucursal_id": 5})
    lineas = [_l(pid), _l(pid)]
    assert [p["precio"] for p in _precios(client, lid, lineas)] == [80.0, 80.0]
    assert [p["precio"] for p in _precios(client, lid, lineas, sucursal_id=5)] == [60.0, 60.0]
    assert [p["precio"] for p in _precios(client, lid, lineas, sucursal_id=9)] == [80.0, 80.0]


# ── errores ──────────────────────────────────────────────────────────────


def test_una_variante_que_no_es_del_producto_es_422(client, abrir):
    lid = _lista(client)["id"]
    remera = _producto_con_quiebre(client, abrir, lid, "Remera")
    pantalon = _producto_con_quiebre(client, abrir, lid, "Pantalón")
    (talle_del_pantalon,) = _variantes(client, pantalon, "Talle M")
    r = client.post(f"/api/listas-precio/{lid}/precios", json={"lineas": [_l(remera, 1, talle_del_pantalon)]})
    assert r.status_code == 422 and "no pertenece" in r.json()["detail"]
    r = client.post(f"/api/listas-precio/{lid}/precios", json={"lineas": [_l(remera, 1, 99999)]})
    assert r.status_code == 422


def test_una_lista_inexistente_es_404(client, abrir):
    r = client.post("/api/listas-precio/999/precios", json={"lineas": []})
    assert r.status_code == 404


def test_un_en_que_no_es_una_fecha_es_422(client, abrir):
    lid = _lista(client)["id"]
    r = client.post(f"/api/listas-precio/{lid}/precios", json={"lineas": [], "en": "ayer"})
    assert r.status_code == 422


def test_los_campos_numericos_rechazan_booleanos_y_el_tope_de_lineas(client, abrir):
    lid = _lista(client)["id"]
    url = f"/api/listas-precio/{lid}/precios"
    assert client.post(url, json={"lineas": [{"producto_id": True, "cantidad": 1}]}).status_code == 422
    assert client.post(url, json={"lineas": [{"producto_id": 1, "cantidad": True}]}).status_code == 422
    assert client.post(url, json={"lineas": [{"producto_id": 1, "variante_id": True}]}).status_code == 422
    assert client.post(url, json={"lineas": [], "sucursal_id": True}).status_code == 422
    assert client.post(url, json={"lineas": [_l(1)] * 501}).status_code == 422
    assert client.post(url, json={}).status_code == 422


# ── lo que no cambió ─────────────────────────────────────────────────────


def test_get_precio_sigue_resolviendo_una_linea_por_su_cantidad(client, abrir):
    """El contrato de siempre (Contalibra lo usa para re-cotizar un renglón): dos consultas de a una unidad dan el precio base."""
    lid = _lista(client)["id"]
    pid = _producto_con_quiebre(client, abrir, lid)
    assert client.get(f"/api/listas-precio/{lid}/precio?producto_id={pid}&cantidad=1").json() == {"precio": 100.0}
    assert client.get(f"/api/listas-precio/{lid}/precio?producto_id={pid}&cantidad=2").json() == {"precio": 80.0}
    # ...y el endpoint nuevo, con esas dos mismas líneas, da el del quiebre en las dos.
    assert [p["precio"] for p in _precios(client, lid, [_l(pid), _l(pid)])] == [80.0, 80.0]


def test_la_funcion_del_motor_se_puede_llamar_sin_http(abrir, client):
    lid = _lista(client)["id"]
    pid = _producto_con_quiebre(client, abrir, lid)
    with abrir() as conn:
        precios = lp.precios_por_producto(conn, lid, [{"producto_id": pid, "cantidad": 1}, {"producto_id": pid, "cantidad": 1}])
    assert [p["precio"] for p in precios] == [80.0, 80.0]
