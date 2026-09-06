"""El contrato HTTP del punto de venta, contra los DOS motores (P9-M3): lo que
`tests/test_ventas_caja.py` de Contalibra y Restolibra esperan de
`/api/ventas`, sin el gate por módulo (lo pone el producto al montar)."""

from __future__ import annotations

import datetime

import pytest
from conftest import USUARIO, _usuario
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from libracommerce.erp import Hooks, ventas
from libracommerce.web.catalogo_router import build_productos_router, build_stock_router
from libracommerce.web.ventas_router import OpcionesVentas, build_ventas_router

HOY = datetime.date.today().isoformat()


def _solo_admin(user: dict = Depends(_usuario)):
    if user.get("role") != "admin":
        raise HTTPException(403, "Sólo admin")


def _app(abrir, opciones=None) -> TestClient:
    app = FastAPI()
    app.include_router(build_productos_router(conexion=abrir, usuario_actual=_usuario))
    app.include_router(build_stock_router(conexion=abrir, usuario_actual=_usuario))
    app.include_router(build_ventas_router(conexion=abrir, usuario_actual=_usuario,
                                           solo_admin=_solo_admin, opciones=opciones))
    return TestClient(app)


@pytest.fixture
def client(abrir_ventas):
    return _app(abrir_ventas)


def _venta(client, items=None, pagos=None, **extra):
    payload = {
        "fecha": HOY,
        "items": items or [{"nombre": "Producto suelto", "qty": 2, "precio": 100.0}],
        "pagos": pagos or [{"medio": "efectivo", "monto": 200.0}],
    }
    payload.update(extra)
    resp = client.post("/api/ventas", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _con_rol(rol):
    USUARIO["role"] = rol


@pytest.fixture(autouse=True)
def _admin():
    _con_rol("admin")
    yield
    USUARIO.pop("role", None)


# ── El contrato de los productos ─────────────────────────────────────────


def test_medios_pago(client):
    medios = client.get("/api/ventas/medios-pago").json()
    assert any(m["id"] == "efectivo" for m in medios)


def test_venta_cobrada(client):
    venta = _venta(client)
    assert venta["total"] == 200.0 and venta["estado"] == "cobrada"
    assert venta["numero"] == "V-00001" and venta["usuario_id"] == USUARIO["id"]
    assert venta["pagos"][0]["estado"] == "aprobado"


def test_venta_sin_items_422(client):
    resp = client.post("/api/ventas", json={
        "fecha": HOY, "items": [], "pagos": [{"medio": "efectivo", "monto": 100}]})
    assert resp.status_code == 422
    # Un ítem sin nombre o sin cantidad tampoco cuenta.
    resp = client.post("/api/ventas", json={
        "fecha": HOY, "items": [{"nombre": "  ", "qty": 1, "precio": 10}, {"nombre": "X", "qty": 0, "precio": 10}],
        "pagos": [{"medio": "efectivo", "monto": 100}]})
    assert resp.status_code == 422


def test_venta_sin_pagos_422(client):
    resp = client.post("/api/ventas", json={
        "fecha": HOY, "items": [{"nombre": "X", "qty": 1, "precio": 100}], "pagos": []})
    assert resp.status_code == 422
    resp = client.post("/api/ventas", json={
        "fecha": HOY, "items": [{"nombre": "X", "qty": 1, "precio": 100}], "pagos": [{"medio": "efectivo", "monto": 0}]})
    assert resp.status_code == 422


def test_medio_inventado_422(client):
    resp = client.post("/api/ventas", json={
        "fecha": HOY, "items": [{"nombre": "X", "qty": 1, "precio": 100}],
        "pagos": [{"medio": "criptomoneda", "monto": 100}]})
    assert resp.status_code == 422


def test_cobrar_con_qr_solo_con_mercadopago(client):
    resp = client.post("/api/ventas", json={
        "fecha": HOY, "items": [{"nombre": "X", "qty": 1, "precio": 100}],
        "pagos": [{"medio": "efectivo", "monto": 100, "cobrar_con_qr": True}]})
    assert resp.status_code == 422
    venta = _venta(client, pagos=[{"medio": "mercadopago", "monto": 200.0, "cobrar_con_qr": True}])
    assert venta["estado"] == "pendiente"
    assert venta["pagos"][0]["estado"] == "pendiente"


def test_venta_parcial_y_pendiente(client):
    parcial = _venta(client, pagos=[{"medio": "efectivo", "monto": 50.0}])
    assert parcial["estado"] == "parcial"
    pendiente = _venta(client, pagos=[{"medio": "efectivo", "monto": 50.0}], descuento=150)
    # 200 - 150 = 50: cubierta.
    assert pendiente["total"] == 50.0 and pendiente["estado"] == "cobrada"
    # El descuento no puede superar el subtotal, ni el precio ser negativo.
    v = _venta(client, items=[{"nombre": "X", "qty": 1, "precio": -5}], pagos=[{"medio": "efectivo", "monto": 1}], descuento=999)
    assert v["total"] == 0.0 and v["subtotal"] == 0.0


def test_cliente_por_id_snapshotea_el_nombre(abrir_ventas):
    client = _app(abrir_ventas, OpcionesVentas(nombre_de_cliente=lambda cid: f"Cliente {cid}"))
    with abrir_ventas() as conn:
        conn.execute("INSERT INTO clients (id, name) VALUES (3, 'Registrado')")
        conn.execute("INSERT INTO parties (id, party_type, display_name) VALUES (3, 'customer', 'Registrado')")
        conn.commit()
    venta = _venta(client, cliente_id=3, cliente_nombre="lo que escribió el cajero")
    assert venta["cliente_id"] == 3 and venta["cliente_nombre"] == "Cliente 3"
    # Sin la opción, el registro de clientes de LibraCore.
    venta = _venta(_app(abrir_ventas), cliente_id=3)
    assert venta["cliente_nombre"] == "Registrado"
    with abrir_ventas() as conn:
        conn.execute("INSERT INTO parties (id, party_type, display_name) VALUES (9, 'customer', 'Sin ficha')")
        conn.commit()
    venta = _venta(_app(abrir_ventas), cliente_id=9, cliente_nombre="Ocasional")
    assert venta["cliente_nombre"] == "Ocasional"


def test_stock_segun_la_opcion(abrir_ventas):
    client = _app(abrir_ventas)
    pid = client.post("/api/productos", json={"nombre": "Yerba", "precio_venta": 100.0, "precio_costo": 60.0}).json()["id"]
    client.post(f"/api/stock/{pid}/ajuste", json={"modo": "absoluto", "cantidad": 10})
    _venta(client, items=[{"nombre": "Yerba", "qty": 2, "precio": 100.0, "producto_id": pid}])
    assert client.get(f"/api/stock/{pid}").json()["stock_actual"] == 8.0
    sin_stock = _app(abrir_ventas, OpcionesVentas(stock_habilitado=lambda: False))
    _venta(sin_stock, items=[{"nombre": "Yerba", "qty": 2, "precio": 100.0, "producto_id": pid}])
    assert client.get(f"/api/stock/{pid}").json()["stock_actual"] == 8.0


def test_listado_tabs_y_busqueda(client):
    _venta(client, cliente_nombre="Ana")
    _venta(client, cliente_nombre="Beto")
    assert [v["cliente_nombre"] for v in client.get("/api/ventas").json()] == ["Beto", "Ana"]
    assert [v["cliente_nombre"] for v in client.get("/api/ventas?q=ana").json()] == ["Ana"]
    assert len(client.get("/api/ventas?tab=sin_facturar").json()) == 2
    assert client.get("/api/ventas?tab=facturadas").json() == []
    assert len(client.get("/api/ventas?tab=cualquiera").json()) == 2
    fila = client.get("/api/ventas").json()[0]
    assert fila["factura_display"] is None and "fac_tipo" in fila


def test_detalle_y_404(client):
    venta = _venta(client)
    d = client.get(f"/api/ventas/{venta['id']}").json()
    assert d["numero"] == venta["numero"] and d["factura_display"] is None and d["mp_order_id"] == ""
    assert client.get("/api/ventas/999").status_code == 404
    assert client.post("/api/ventas/999/anular").status_code == 404


def test_anular_solo_admin(client):
    venta = _venta(client)
    _con_rol("operador")
    assert client.post(f"/api/ventas/{venta['id']}/anular").status_code == 403
    _con_rol("admin")
    r = client.post(f"/api/ventas/{venta['id']}/anular")
    assert r.status_code == 200 and r.json()["estado"] == "anulada"
    # Repetir no revierte dos veces.
    assert client.post(f"/api/ventas/{venta['id']}/anular").json()["estado"] == "anulada"
    assert [v["estado"] for v in client.get("/api/ventas?tab=sin_facturar").json()] == []


def test_los_ganchos_llegan_desde_las_opciones(abrir_ventas):
    eventos = []
    ganchos = Hooks(al_confirmar_venta=lambda c, v: eventos.append(("confirmada", v["numero"])),
                    al_anular_venta=lambda c, v: eventos.append(("anulada", v["numero"])))
    client = _app(abrir_ventas, OpcionesVentas(hooks=ganchos))
    venta = _venta(client)
    client.post(f"/api/ventas/{venta['id']}/anular")
    assert eventos == [("confirmada", "V-00001"), ("anulada", "V-00001")]


def test_conflicto_de_numero_es_409(abrir_ventas, monkeypatch):
    client = _app(abrir_ventas)
    _venta(client)
    monkeypatch.setattr(ventas, "siguiente_numero", lambda conn: "V-00001")
    r = client.post("/api/ventas", json={
        "fecha": HOY, "items": [{"nombre": "X", "qty": 1, "precio": 100}], "pagos": [{"medio": "efectivo", "monto": 100}]})
    assert r.status_code == 409
    assert len(client.get("/api/ventas").json()) == 1
