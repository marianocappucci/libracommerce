"""La lista de precios asignada a un cliente (`build_cliente_lista_router`),
extraída del add-on mayorista de Contalibra (2026-09-28). Usa `abrir_ventas`
porque necesita los dos motores: `clients` es de LibraCore.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from libracommerce.web.listas_router import build_cliente_lista_router, build_listas_precio_router


def _app(abrir) -> TestClient:
    app = FastAPI()
    app.include_router(build_listas_precio_router(conexion=abrir))
    app.include_router(build_cliente_lista_router(conexion=abrir))
    return TestClient(app)


def _cliente(abrir_ventas, cliente_id: int, nombre: str = "Cliente"):
    with abrir_ventas() as conn:
        conn.execute("INSERT INTO clients (id, name) VALUES (?, ?)", (cliente_id, nombre))
        conn.commit()


def _lista(client, nombre="Mayorista"):
    r = client.post("/api/listas-precio", json={"nombre": nombre, "descripcion": "x"})
    assert r.status_code == 200, r.text
    return r.json()


def test_cliente_sin_lista_asignada(abrir_ventas):
    _cliente(abrir_ventas, 1)
    client = _app(abrir_ventas)
    r = client.get("/api/clientes/1/lista-precio")
    assert r.status_code == 200
    assert r.json() == {"lista_id": None, "lista": None}


def test_asignar_reasignar_y_quitar(abrir_ventas):
    _cliente(abrir_ventas, 1)
    client = _app(abrir_ventas)
    mayorista = _lista(client, "Mayorista")
    otra = _lista(client, "Otra")

    r = client.put("/api/clientes/1/lista-precio", json={"lista_id": mayorista["id"]})
    assert r.status_code == 200
    assert r.json()["lista_id"] == mayorista["id"]
    assert r.json()["lista"]["nombre"] == "Mayorista"
    assert client.get("/api/clientes/1/lista-precio").json()["lista_id"] == mayorista["id"]

    # Reasignar es un upsert, no un conflicto.
    r = client.put("/api/clientes/1/lista-precio", json={"lista_id": otra["id"]})
    assert r.status_code == 200 and r.json()["lista_id"] == otra["id"]

    # `lista_id: null` limpia la asignación.
    r = client.put("/api/clientes/1/lista-precio", json={"lista_id": None})
    assert r.status_code == 200
    assert r.json() == {"lista_id": None, "lista": None}


def test_cliente_inexistente_da_404(abrir_ventas):
    client = _app(abrir_ventas)
    assert client.get("/api/clientes/999/lista-precio").status_code == 404
    assert client.put("/api/clientes/999/lista-precio", json={"lista_id": None}).status_code == 404


def test_lista_inexistente_da_422(abrir_ventas):
    _cliente(abrir_ventas, 1)
    client = _app(abrir_ventas)
    r = client.put("/api/clientes/1/lista-precio", json={"lista_id": 999})
    assert r.status_code == 422


def test_borrar_el_cliente_borra_la_asignacion(abrir_ventas):
    """La FK `ON DELETE CASCADE`: sin esto, `cliente_lista_precio` quedaría
    con una fila colgada apuntando a un cliente que ya no existe."""
    _cliente(abrir_ventas, 1)
    client = _app(abrir_ventas)
    mayorista = _lista(client)
    client.put("/api/clientes/1/lista-precio", json={"lista_id": mayorista["id"]})
    with abrir_ventas() as conn:
        conn.execute("DELETE FROM clients WHERE id=1")
        conn.commit()
        row = conn.execute("SELECT * FROM cliente_lista_precio WHERE cliente_id=1").fetchone()
    assert row is None
