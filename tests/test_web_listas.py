"""Listas de precio, quiebres y el autocompletado del punto de venta, por HTTP
contra los DOS motores (P9-M2).

Los de listas son el contrato que los dos productos ya tenían; los de quiebres
son `tests/test_mayorista_quiebres.py` de Contalibra portados (sin el gate por
add-on, que lo pone el producto al montar); los del autocompletado fijan lo que
Ventas, Facturas, Presupuestos y Remitos esperan de `/productos/buscar`.
"""

from __future__ import annotations

import pytest
from conftest import _usuario  # (la fixture `abrir` la carga pytest desde conftest)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from libracommerce.erp import listas_precio as lp
from libracommerce.web.catalogo_router import build_productos_router
from libracommerce.web.listas_router import (
    build_buscar_productos_router,
    build_listas_precio_router,
    build_quiebres_router,
)


def _app(abrir, *, solo_vendibles=False) -> TestClient:
    app = FastAPI()
    app.include_router(build_productos_router(conexion=abrir, usuario_actual=_usuario))
    app.include_router(build_listas_precio_router(conexion=abrir))
    app.include_router(build_quiebres_router(conexion=abrir))
    app.include_router(build_buscar_productos_router(conexion=abrir, usuario_actual=_usuario, solo_vendibles=solo_vendibles))
    return TestClient(app)


@pytest.fixture
def client(abrir):
    return _app(abrir)


def _producto(client, nombre, **extra):
    payload = {"nombre": nombre, "precio_venta": 100.0, "precio_costo": 60.0}
    payload.update(extra)
    r = client.post("/api/productos", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def _lista(client, nombre="Mayorista"):
    r = client.post("/api/listas-precio", json={"nombre": nombre, "descripcion": "x"})
    assert r.status_code == 200, r.text
    return r.json()


# ── Listas ───────────────────────────────────────────────────────────────


def test_crud_de_listas(client):
    lista = _lista(client)
    assert lista["nombre"] == "Mayorista" and lista["activa"]
    assert [x["id"] for x in client.get("/api/listas-precio").json()] == [lista["id"]]
    r = client.put(f"/api/listas-precio/{lista['id']}", json={"nombre": "May", "activa": False})
    assert r.status_code == 200 and r.json()["nombre"] == "May" and not r.json()["activa"]
    assert client.put(f"/api/listas-precio/{lista['id']}", json={"nombre": " "}).status_code == 422
    assert client.post("/api/listas-precio", json={"nombre": " "}).status_code == 422
    assert client.delete(f"/api/listas-precio/{lista['id']}").status_code == 200
    assert client.delete(f"/api/listas-precio/{lista['id']}").status_code == 404
    assert client.get("/api/listas-precio/999/items").status_code == 404


def test_items_guardar_y_borrar_con_precio_cero(client):
    p = _producto(client, "Fideos", categoria="Almacén")
    q = _producto(client, "Arroz")
    lista = _lista(client)
    items = client.get(f"/api/listas-precio/{lista['id']}/items").json()
    assert {i["nombre"] for i in items} == {"Fideos", "Arroz"} and all(i["en_lista"] == 0 for i in items)
    r = client.put(f"/api/listas-precio/{lista['id']}/items", json={"precios": {str(p["id"]): 90, str(q["id"]): 80}})
    assert r.status_code == 200
    por_id = {i["id"]: i for i in r.json()}
    assert por_id[p["id"]]["precio_lista"] == 90 and por_id[p["id"]]["en_lista"] == 1
    # Filtro por categoría.
    assert [i["nombre"] for i in client.get(f"/api/listas-precio/{lista['id']}/items?categoria=Almac%C3%A9n").json()] == ["Fideos"]
    # Precio <= 0 saca el ítem.
    r = client.put(f"/api/listas-precio/{lista['id']}/items", json={"precios": {str(q["id"]): 0}})
    assert {i["id"]: i["en_lista"] for i in r.json()}[q["id"]] == 0


def test_ajuste_porcentual_e_importar(client):
    p = _producto(client, "Fideos", precio_venta=100, precio_costo=60)
    lista = _lista(client)
    client.put(f"/api/listas-precio/{lista['id']}/items", json={"precios": {str(p["id"]): 90}})
    r = client.post(f"/api/listas-precio/{lista['id']}/ajuste-porcentual", json={"porcentaje": 10, "base": "lista"})
    assert r.json() == {"actualizados": 1}
    assert {i["id"]: i["precio_lista"] for i in client.get(f"/api/listas-precio/{lista['id']}/items").json()}[p["id"]] == 99
    r = client.post(f"/api/listas-precio/{lista['id']}/ajuste-porcentual", json={"porcentaje": -50, "base": "costo"})
    assert r.json() == {"actualizados": 1}
    assert {i["id"]: i["precio_lista"] for i in client.get(f"/api/listas-precio/{lista['id']}/items").json()}[p["id"]] == 30
    otra = _lista(client, "Otra")
    r = client.post(f"/api/listas-precio/{otra['id']}/importar", json={"fuente": "lista", "fuente_lista_id": lista["id"]})
    assert {i["id"]: i["precio_lista"] for i in r.json()}[p["id"]] == 30
    r = client.post(f"/api/listas-precio/{otra['id']}/importar", json={"fuente": "venta"})
    assert {i["id"]: i["precio_lista"] for i in r.json()}[p["id"]] == 100


# ── Quiebres (los tests de Contalibra, sin el gate) ──────────────────────


def _lista_con_producto(client, abrir, base_lista=90.0, base_venta=100.0):
    p = _producto(client, "Fideos x500g", precio_venta=base_venta)
    lista = _lista(client)
    client.put(f"/api/listas-precio/{lista['id']}/items", json={"precios": {str(p["id"]): base_lista}})
    return lista["id"], p["id"]


def test_resuelve_el_precio_segun_la_cantidad(client, abrir):
    lid, pid = _lista_con_producto(client, abrir)
    with abrir() as conn:
        lp.set_quiebres(conn, lid, pid, [{"min_quantity": 10, "amount": 80}, {"min_quantity": 50, "amount": 70}])
        for cantidad, esperado in ((1, 90), (9, 90), (10, 80), (49, 80), (50, 70), (500, 70)):
            assert lp.resolver_precio_por_cantidad(conn, lid, pid, cantidad) == esperado, cantidad


def test_set_quiebres_reemplaza_y_no_toca_el_precio_base(client, abrir):
    lid, pid = _lista_con_producto(client, abrir)
    with abrir() as conn:
        lp.set_quiebres(conn, lid, pid, [{"min_quantity": 10, "amount": 80}])
        assert lp.get_quiebres(conn, lid, pid) == [{"min_quantity": 10.0, "amount": 80.0}]
        lp.set_quiebres(conn, lid, pid, [{"min_quantity": 20, "amount": 75}])
        assert lp.get_quiebres(conn, lid, pid) == [{"min_quantity": 20.0, "amount": 75.0}]
        assert lp.get_precio_en_lista(conn, lid, pid) == 90


def test_guardar_leer_y_resolver_por_la_api(client, abrir):
    lid, pid = _lista_con_producto(client, abrir)
    r = client.put(f"/api/listas-precio/{lid}/items/{pid}/quiebres", json={"quiebres": [{"min_quantity": 10, "amount": 80}]})
    assert r.status_code == 200 and r.json() == [{"min_quantity": 10.0, "amount": 80.0}]
    assert client.get(f"/api/listas-precio/{lid}/items/{pid}/quiebres").json() == [{"min_quantity": 10.0, "amount": 80.0}]
    assert client.get(f"/api/listas-precio/{lid}/precio?producto_id={pid}&cantidad=5").json() == {"precio": 90.0}
    assert client.get(f"/api/listas-precio/{lid}/precio?producto_id={pid}&cantidad=10").json() == {"precio": 80.0}
    assert client.get(f"/api/listas-precio/{lid}/precio?producto_id=9999&cantidad=10").json() == {"precio": None}
    assert client.get(f"/api/listas-precio/999/items/{pid}/quiebres").status_code == 404


def test_valida_los_quiebres(client, abrir):
    lid, pid = _lista_con_producto(client, abrir)
    url = f"/api/listas-precio/{lid}/items/{pid}/quiebres"
    assert client.put(url, json={"quiebres": [{"min_quantity": 1, "amount": 80}]}).status_code == 422
    assert client.put(url, json={"quiebres": [{"min_quantity": 10, "amount": 0}]}).status_code == 422
    r = client.put(url, json={"quiebres": [{"min_quantity": 10, "amount": 80}, {"min_quantity": 10, "amount": 70}]})
    assert r.status_code == 422 and "misma cantidad" in r.json()["detail"]


# ── El autocompletado ────────────────────────────────────────────────────


def test_buscar_devuelve_el_precio_de_la_lista_y_el_base(client):
    p = _producto(client, "Yerba", codigo="Y1", precio_venta=100)
    _producto(client, "Yerba vieja", activo=True)
    lista = _lista(client)
    client.put(f"/api/listas-precio/{lista['id']}/items", json={"precios": {str(p["id"]): 90}})
    r = client.get("/productos/buscar?q=Yerba")
    assert r.status_code == 200
    assert {x["nombre"] for x in r.json()} == {"Yerba", "Yerba vieja"}
    con_lista = {x["id"]: x for x in client.get(f"/productos/buscar?q=Yerba&lista_id={lista['id']}").json()}
    assert con_lista[p["id"]] == {"id": p["id"], "codigo": "Y1", "nombre": "Yerba", "precio_venta": 90, "precio_base": 100, "unidad": "u"}
    assert con_lista[[k for k in con_lista if k != p["id"]][0]]["precio_venta"] == 100  # sin precio en la lista: el base


def test_buscar_filtra_por_tipo_y_por_vendible(client, abrir):
    _producto(client, "Consultoría", tipo="servicio")
    _producto(client, "Harina", vendible=False)
    _producto(client, "Pan")
    assert {x["nombre"] for x in client.get("/productos/buscar?tipo=servicio").json()} == {"Consultoría"}
    assert {x["nombre"] for x in client.get("/productos/buscar").json()} == {"Consultoría", "Harina", "Pan"}
    # Como lo monta Restolibra: los insumos no aparecen en ningún punto de venta.
    gastronomico = _app(abrir, solo_vendibles=True)
    assert {x["nombre"] for x in gastronomico.get("/productos/buscar").json()} == {"Consultoría", "Pan"}


def test_buscar_tiene_tope(client):
    for i in range(25):
        _producto(client, f"Producto {i:02d}")
    assert len(client.get("/productos/buscar?q=Producto").json()) == 20
