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
from libracommerce.web.catalogo_router import (
    build_depositos_router,
    build_productos_router,
    build_stock_router,
)
from libracommerce.web.ventas_router import OpcionesVentas, build_ventas_router

HOY = datetime.date.today().isoformat()


def _solo_admin(user: dict = Depends(_usuario)):
    if user.get("role") != "admin":
        raise HTTPException(403, "Sólo admin")


def _app(abrir, opciones=None) -> TestClient:
    app = FastAPI()
    app.include_router(build_productos_router(conexion=abrir, usuario_actual=_usuario))
    app.include_router(build_stock_router(conexion=abrir, usuario_actual=_usuario))
    app.include_router(build_depositos_router(conexion=abrir, usuario_actual=_usuario))
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


# ── F1: opciones nuevas (exigir_turno, caja_con_turno, numerador) ─────────


def test_exigir_turno_sin_turno_da_409(abrir_ventas):
    client = _app(abrir_ventas, OpcionesVentas(exigir_turno=True))
    r = client.post("/api/ventas", json={
        "fecha": HOY, "items": [{"nombre": "X", "qty": 1, "precio": 100}],
        "pagos": [{"medio": "efectivo", "monto": 100}]})
    assert r.status_code == 409
    assert client.get("/api/ventas").json() == []


def test_exigir_turno_falso_por_default_no_bloquea(client):
    """El comportamiento de hoy: sin la opción, una venta sin turno se
    registra igual (Contalibra y Restolibra no la prenden)."""
    venta = _venta(client)
    assert venta["estado"] == "cobrada"


def test_caja_con_turno_pasa_por_las_opciones(abrir_ventas):
    from libracore.db.turnos import create_turno

    with abrir_ventas() as conn:
        tid = create_turno(USUARIO["id"], 500.0)
        conn.commit()
    client = _app(abrir_ventas, OpcionesVentas(caja_con_turno=True))
    _venta(client)
    with abrir_ventas() as conn:
        row = conn.execute("SELECT turno_id FROM caja_movimientos").fetchone()
        assert row["turno_id"] == tid


def test_numerador_pasa_por_las_opciones(abrir_ventas):
    ganchos = Hooks(numerador=lambda conn: "POS-000001")
    client = _app(abrir_ventas, OpcionesVentas(hooks=ganchos))
    venta = _venta(client)
    assert venta["numero"] == "POS-000001"


# ── F1: variantes y vuelto en el contrato HTTP ────────────────────────────


def test_variante_id_viaja_en_el_payload(abrir_ventas):
    client = _app(abrir_ventas)
    pid = client.post("/api/productos", json={"nombre": "Yerba", "precio_venta": 100.0, "precio_costo": 60.0}).json()["id"]
    with abrir_ventas() as conn:
        vidr = conn.execute(
            "INSERT INTO item_variants (item_id, sku, name) VALUES (?, 'SKU-1', 'Chica')", (pid,)
        ).lastrowid
        conn.commit()
    venta = _venta(client, items=[
        {"nombre": "Yerba Chica", "qty": 1, "precio": 100.0, "producto_id": pid, "variante_id": vidr},
    ], pagos=[{"medio": "efectivo", "monto": 100.0}])
    assert venta["items"][0]["variante_id"] == vidr


def test_recibido_viaja_cuando_la_columna_existe(abrir_ventas):
    with abrir_ventas() as conn:
        conn.execute("ALTER TABLE ventas_pagos ADD COLUMN recibido NUMERIC")
        conn.commit()
    client = _app(abrir_ventas)
    venta = _venta(client, pagos=[{"medio": "efectivo", "monto": 200.0, "recibido": 500.0}])
    assert float(venta["pagos"][0]["recibido"]) == 500.0


# ── F1: devolución parcial por HTTP ────────────────────────────────────────


def test_devolver_reintegra_y_gatea_como_anular(abrir_ventas):
    client = _app(abrir_ventas)
    pid = client.post("/api/productos", json={"nombre": "Yerba", "precio_venta": 100.0, "precio_costo": 60.0}).json()["id"]
    client.post(f"/api/stock/{pid}/ajuste", json={"modo": "absoluto", "cantidad": 10})
    venta = _venta(client, items=[{"nombre": "Yerba", "qty": 4, "precio": 100.0, "producto_id": pid}],
                  pagos=[{"medio": "efectivo", "monto": 400.0}])
    with abrir_ventas() as conn:
        item_id_linea = conn.execute(
            "SELECT id FROM sale_items WHERE sale_id=?", (venta["id"],)
        ).fetchone()["id"]
        deposito_id = conn.execute(
            "SELECT location_id FROM stock_movements WHERE source_id=?", (venta["id"],)
        ).fetchone()["location_id"]

    _con_rol("operador")
    r = client.post(f"/api/ventas/{venta['id']}/devolver", json={
        "lineas": [{"sale_item_id": item_id_linea, "cantidad": 1}], "deposito_id": deposito_id})
    assert r.status_code == 403  # mismo gate que anular
    _con_rol("admin")

    r = client.post(f"/api/ventas/{venta['id']}/devolver", json={
        "lineas": [{"sale_item_id": item_id_linea, "cantidad": 1}], "deposito_id": deposito_id})
    assert r.status_code == 200, r.text
    assert r.json()["estado"] == "devuelta_parcial"
    assert client.get(f"/api/stock/{pid}").json()["stock_actual"] == 7.0  # 10 - 4 + 1

    # Pasarse del tope es 422, no 500.
    r = client.post(f"/api/ventas/{venta['id']}/devolver", json={
        "lineas": [{"sale_item_id": item_id_linea, "cantidad": 10}], "deposito_id": deposito_id})
    assert r.status_code == 422

    assert client.post("/api/ventas/999/devolver", json={
        "lineas": [{"sale_item_id": item_id_linea, "cantidad": 1}], "deposito_id": deposito_id}).status_code == 404


def test_anular_con_devoluciones_es_409(abrir_ventas):
    client = _app(abrir_ventas)
    pid = client.post("/api/productos", json={"nombre": "Yerba", "precio_venta": 100.0, "precio_costo": 60.0}).json()["id"]
    client.post(f"/api/stock/{pid}/ajuste", json={"modo": "absoluto", "cantidad": 10})
    venta = _venta(client, items=[{"nombre": "Yerba", "qty": 4, "precio": 100.0, "producto_id": pid}],
                  pagos=[{"medio": "efectivo", "monto": 400.0}])
    with abrir_ventas() as conn:
        item_id_linea = conn.execute(
            "SELECT id FROM sale_items WHERE sale_id=?", (venta["id"],)
        ).fetchone()["id"]
        deposito_id = conn.execute(
            "SELECT location_id FROM stock_movements WHERE source_id=?", (venta["id"],)
        ).fetchone()["location_id"]
    r = client.post(f"/api/ventas/{venta['id']}/devolver", json={
        "lineas": [{"sale_item_id": item_id_linea, "cantidad": 1}], "deposito_id": deposito_id})
    assert r.status_code == 200, r.text

    r = client.post(f"/api/ventas/{venta['id']}/anular")
    assert r.status_code == 409
    assert "devoluciones" in r.json()["detail"].lower()
    # La venta sigue como estaba: no se anuló.
    assert client.get(f"/api/ventas/{venta['id']}").json()["estado"] == "devuelta_parcial"


# ── Huecos de VentaLibra (v0.16.2) ─────────────────────────────────────────


def test_producto_inexistente_da_422_no_409(client):
    """Punto 1: antes de esto, un `producto_id` inventado se reintentaba 10
    veces y terminaba en 409 (el mensaje de "otra venta simultánea")."""
    resp = client.post("/api/ventas", json={
        "fecha": HOY, "items": [{"nombre": "Fantasma", "qty": 1, "precio": 50, "producto_id": 999999}],
        "pagos": [{"medio": "efectivo", "monto": 50}]})
    assert resp.status_code == 422, resp.text
    assert "999999" in resp.json()["detail"]
    assert client.get("/api/ventas").json() == []


def test_exigir_pago_completo_rechaza_pago_insuficiente(abrir_ventas):
    client = _app(abrir_ventas, OpcionesVentas(exigir_pago_completo=True))
    resp = client.post("/api/ventas", json={
        "fecha": HOY, "items": [{"nombre": "X", "qty": 1, "precio": 100}],
        "pagos": [{"medio": "efectivo", "monto": 50}]})
    assert resp.status_code == 422, resp.text
    assert client.get("/api/ventas").json() == []


def test_exigir_pago_completo_acepta_lo_declarado_aunque_sea_qr_pendiente(abrir_ventas):
    """Lo que cuenta es lo DECLARADO, no lo acreditado: un QR pendiente por
    el total completo no se rechaza."""
    client = _app(abrir_ventas, OpcionesVentas(exigir_pago_completo=True))
    venta = _venta(client, pagos=[{"medio": "mercadopago", "monto": 200.0, "cobrar_con_qr": True}])
    assert venta["estado"] == "pendiente"


def test_exigir_pago_completo_falso_por_default_no_bloquea(client):
    """El comportamiento de hoy: Contalibra y Restolibra no prenden la opción."""
    parcial = _venta(client, pagos=[{"medio": "efectivo", "monto": 50.0}])
    assert parcial["estado"] == "parcial"


def test_exigir_cliente_para_fiar_rechaza_cc_sin_cliente(abrir_ventas):
    client = _app(abrir_ventas, OpcionesVentas(exigir_cliente_para_fiar=True))
    resp = client.post("/api/ventas", json={
        "fecha": HOY, "items": [{"nombre": "X", "qty": 1, "precio": 100}],
        "pagos": [{"medio": "cuenta_corriente", "monto": 100}]})
    assert resp.status_code == 422, resp.text
    assert client.get("/api/ventas").json() == []


def test_exigir_cliente_para_fiar_acepta_con_cliente(abrir_ventas):
    client = _app(abrir_ventas, OpcionesVentas(exigir_cliente_para_fiar=True))
    with abrir_ventas() as conn:
        conn.execute("INSERT INTO clients (id, name) VALUES (3, 'Cliente')")
        conn.execute("INSERT INTO parties (id, party_type, display_name) VALUES (3, 'customer', 'Cliente')")
        conn.commit()
    venta = _venta(client, cliente_id=3, pagos=[{"medio": "cuenta_corriente", "monto": 200.0}])
    assert venta["estado"] == "cobrada"


def test_exigir_cliente_para_fiar_falso_por_default_permite(client):
    """El comportamiento de hoy: Contalibra y Restolibra no prenden la opción."""
    venta = _venta(client, pagos=[{"medio": "cuenta_corriente", "monto": 200.0}])
    assert venta["estado"] == "cobrada"


def test_recibido_menor_que_monto_422(client):
    """Punto 3, para todos: un vuelto negativo es un dato imposible."""
    resp = client.post("/api/ventas", json={
        "fecha": HOY, "items": [{"nombre": "X", "qty": 1, "precio": 100}],
        "pagos": [{"medio": "efectivo", "monto": 100, "recibido": 50}]})
    assert resp.status_code == 422
    assert client.get("/api/ventas").json() == []


def test_recibido_igual_o_mayor_que_monto_no_rebota(abrir_ventas):
    with abrir_ventas() as conn:
        conn.execute("ALTER TABLE ventas_pagos ADD COLUMN recibido NUMERIC")
        conn.commit()
    client = _app(abrir_ventas)
    venta = _venta(client, pagos=[{"medio": "efectivo", "monto": 200.0, "recibido": 200.0}])
    assert float(venta["pagos"][0]["recibido"]) == 200.0


# ── F4: `deposito_id` en el payload (VentaLibra multisucursal) ────────────


def test_deposito_id_en_el_payload_descuenta_de_ese_deposito(abrir_ventas):
    client = _app(abrir_ventas)
    pid = client.post("/api/productos", json={"nombre": "Yerba", "precio_venta": 100.0, "precio_costo": 60.0}).json()["id"]
    client.post(f"/api/stock/{pid}/ajuste", json={"modo": "absoluto", "cantidad": 10})
    sucursal_b = client.post("/api/depositos", json={"nombre": "Sucursal B"}).json()["id"]

    _venta(client, items=[{"nombre": "Yerba", "qty": 3, "precio": 100.0, "producto_id": pid}],
          deposito_id=sucursal_b)

    # El total (todos los depósitos) bajó igual que siempre.
    assert client.get(f"/api/stock/{pid}").json()["stock_actual"] == 7.0
    # Pero salió de Sucursal B, no del depósito por defecto.
    stock_b = client.get(f"/api/depositos/{sucursal_b}/stock").json()
    assert stock_b[0]["id"] == pid and stock_b[0]["stock_actual"] == -3.0


def test_sin_deposito_id_sigue_descontando_del_default(client):
    """El comportamiento de hoy — Contalibra y Restolibra no mandan
    `deposito_id` en el payload —: no cambia."""
    pid = client.post("/api/productos", json={"nombre": "Yerba", "precio_venta": 100.0, "precio_costo": 60.0}).json()["id"]
    client.post(f"/api/stock/{pid}/ajuste", json={"modo": "absoluto", "cantidad": 10})
    _venta(client, items=[{"nombre": "Yerba", "qty": 2, "precio": 100.0, "producto_id": pid}])
    assert client.get(f"/api/stock/{pid}").json()["stock_actual"] == 8.0


def test_deposito_id_inexistente_da_422_no_500_ni_409(client):
    """Mismo criterio que `test_producto_inexistente_da_422_no_409`: un
    `deposito_id` inventado no es un conflicto con otra venta ni un error de
    integridad sin manejar — es un dato del pedido, 422 con mensaje claro."""
    resp = client.post("/api/ventas", json={
        "fecha": HOY, "items": [{"nombre": "X", "qty": 1, "precio": 100}],
        "pagos": [{"medio": "efectivo", "monto": 100}], "deposito_id": 999999})
    assert resp.status_code == 422, resp.text
    assert "999999" in resp.json()["detail"]
    assert client.get("/api/ventas").json() == []


# ── F4 (corrección sobre la revisión): `deposito_id` también en /devolver ──


def test_devolver_a_deposito_inexistente_da_422_no_500(abrir_ventas):
    """Mismo hallazgo que `test_deposito_id_inexistente_da_422_no_500_ni_409`
    pero en `/devolver`: antes de este fix, `devolver_items` no validaba su
    `deposito_id` y un valor inventado reventaba como `IntegrityError` sin
    manejar (500), no como el 422 que corresponde."""
    client = _app(abrir_ventas)
    pid = client.post("/api/productos", json={"nombre": "Yerba", "precio_venta": 100.0, "precio_costo": 60.0}).json()["id"]
    client.post(f"/api/stock/{pid}/ajuste", json={"modo": "absoluto", "cantidad": 10})
    venta = _venta(client, items=[{"nombre": "Yerba", "qty": 4, "precio": 100.0, "producto_id": pid}],
                  pagos=[{"medio": "efectivo", "monto": 400.0}])
    with abrir_ventas() as conn:
        item_id_linea = conn.execute(
            "SELECT id FROM sale_items WHERE sale_id=?", (venta["id"],)
        ).fetchone()["id"]

    r = client.post(f"/api/ventas/{venta['id']}/devolver", json={
        "lineas": [{"sale_item_id": item_id_linea, "cantidad": 1}], "deposito_id": 999999})
    assert r.status_code == 422, r.text
    assert "999999" in r.json()["detail"]
    # Nada quedó escrito: ni el stock, ni el estado de la venta.
    assert client.get(f"/api/stock/{pid}").json()["stock_actual"] == 6.0
    assert client.get(f"/api/ventas/{venta['id']}").json()["estado"] == "cobrada"


def test_devolver_a_deposito_inactivo_da_422(abrir_ventas):
    client = _app(abrir_ventas)
    pid = client.post("/api/productos", json={"nombre": "Yerba", "precio_venta": 100.0, "precio_costo": 60.0}).json()["id"]
    client.post(f"/api/stock/{pid}/ajuste", json={"modo": "absoluto", "cantidad": 10})
    venta = _venta(client, items=[{"nombre": "Yerba", "qty": 1, "precio": 100.0, "producto_id": pid}],
                  pagos=[{"medio": "efectivo", "monto": 100.0}])
    sucursal_b = client.post("/api/depositos", json={"nombre": "Sucursal B"}).json()["id"]
    client.put(f"/api/depositos/{sucursal_b}", json={"nombre": "Sucursal B", "activo": False})
    with abrir_ventas() as conn:
        item_id_linea = conn.execute(
            "SELECT id FROM sale_items WHERE sale_id=?", (venta["id"],)
        ).fetchone()["id"]

    r = client.post(f"/api/ventas/{venta['id']}/devolver", json={
        "lineas": [{"sale_item_id": item_id_linea, "cantidad": 1}], "deposito_id": sucursal_b})
    assert r.status_code == 422, r.text
