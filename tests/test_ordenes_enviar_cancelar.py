"""Enviar y cancelar una orden de compra (ADR-055), contra los dos motores.

El motor tenía los estados de una orden (`draft`, `sent`, `partial`, `received`, `cancelled`) pero ninguna operación que la pasara a `sent` ni a `cancelled`: una orden
nacía `draft` y sólo una recepción la movía. Ahora `POST /api/purchase-orders/{id}/enviar` (`draft` -> `sent`) y `.../cancelar` (`draft`/`sent` -> `cancelled`).

Lo que fijan: las transiciones y que no pierdan las líneas; que sean idempotentes (repetir no falla ni vuelve a escribir `ordered_at`); el 409 de las transiciones que
no existen (enviar lo que ya recibió o está cancelado, cancelar lo que ya recibió); el 404; la misma guarda de escritura que el resto de Compras; y que una
cancelada no reciba mercadería (si no, la recepción la resucitaba como `partial`/`received`).
"""

from __future__ import annotations

import datetime

import pytest
import test_vencimientos as _vto
from conftest import _usuario
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from libracommerce.domain.entities import Party, PartyType
from libracommerce.erp import catalogo, compras
from libracommerce.web.compras_router import OpcionesCompras, build_compras_router

abrir_vto = _vto.abrir_vto
destino = _vto.destino


def _app(abrir, *, opciones=None) -> TestClient:
    app = FastAPI()
    app.include_router(build_compras_router(conexion=abrir, usuario_actual=_usuario, opciones=opciones))
    return TestClient(app)


def _proveedor(abrir, nombre="Distribuidora SA") -> int:
    with abrir() as conn:
        from libracommerce.db.repository import repositorio_de

        return repositorio_de(conn).save_party(Party(None, PartyType.ORGANIZATION, nombre)).id


def _producto(abrir, nombre="Yerba 1kg") -> int:
    with abrir() as conn:
        return catalogo.create_producto(conn, nombre, precio_venta=1500.0, precio_costo=900.0)


def _orden(client, abrir, cantidad=10) -> int:
    """Una orden en borrador con una línea."""
    oid = client.post("/api/purchase-orders", json={"proveedor_id": _proveedor(abrir)}).json()["id"]
    r = client.post(f"/api/purchase-orders/{oid}/items", json={"item_id": _producto(abrir), "quantity_ordered": cantidad, "unit_cost": 100})
    assert r.status_code == 200, r.text
    return oid


def _estado(abrir, oid) -> str:
    with abrir() as conn:
        return conn.execute("SELECT status FROM purchase_orders WHERE id=?", (oid,)).fetchone()[0]


def _ordered_at(abrir, oid):
    with abrir() as conn:
        return conn.execute("SELECT ordered_at FROM purchase_orders WHERE id=?", (oid,)).fetchone()[0]


def _recibir_todo(client, abrir, oid):
    """Una recepción confirmada que cubre la orden entera."""
    with abrir() as conn:
        orden = compras.obtener_orden(conn, oid)
        deposito = catalogo.create_deposito(conn, "Depósito recepción")
    rec = client.post("/api/purchase-receipts", json={"proveedor_id": orden["supplier_party_id"], "purchase_order_id": oid}).json()
    for linea in orden["items"]:
        client.post(f"/api/purchase-receipts/{rec['id']}/items", json={"item_id": linea["item_id"], "quantity": linea["quantity_ordered"], "unit_cost": 100})
    r = client.post(f"/api/purchase-receipts/{rec['id']}/confirm", json={"deposito_id": deposito})
    assert r.status_code == 200, r.text


# ── enviar ───────────────────────────────────────────────────────────────


def test_enviar_pasa_de_borrador_a_enviada_conserva_las_lineas_y_registra_cuando(abrir):
    client = _app(abrir)
    oid = _orden(client, abrir, cantidad=10)
    assert _ordered_at(abrir, oid) is None

    r = client.post(f"/api/purchase-orders/{oid}/enviar")
    assert r.status_code == 200, r.text
    orden = r.json()
    assert orden["id"] == oid and orden["status"] == "sent" and "supplier_party_id" not in orden       # la misma forma que el resto: con `proveedor_id`
    assert len(orden["items"]) == 1 and orden["items"][0]["quantity_ordered"] in ("10", "10.0")       # las líneas no se tocan
    assert _estado(abrir, oid) == "sent"
    cuando = _ordered_at(abrir, oid)
    assert cuando is not None and datetime.datetime.fromisoformat(cuando)                             # se guardó una fecha legible


def test_enviar_dos_veces_es_idempotente_y_no_pisa_la_fecha_del_primer_envio(abrir):
    client = _app(abrir)
    oid = _orden(client, abrir)
    primera = client.post(f"/api/purchase-orders/{oid}/enviar")
    cuando = _ordered_at(abrir, oid)
    segunda = client.post(f"/api/purchase-orders/{oid}/enviar")
    assert (primera.status_code, segunda.status_code) == (200, 200)
    assert segunda.json() == primera.json() and _ordered_at(abrir, oid) == cuando
    assert len(client.get("/api/purchase-orders").json()) == 1                                         # no duplicó nada


def test_enviar_lo_que_no_es_un_borrador_ni_una_enviada_es_409(abrir):
    client = _app(abrir)
    cancelada = _orden(client, abrir)
    client.post(f"/api/purchase-orders/{cancelada}/cancelar")
    r = client.post(f"/api/purchase-orders/{cancelada}/enviar")
    assert r.status_code == 409 and "cancelled" in r.json()["detail"]
    assert _estado(abrir, cancelada) == "cancelled"

    recibida = _orden(client, abrir)
    client.post(f"/api/purchase-orders/{recibida}/enviar")
    _recibir_todo(client, abrir, recibida)
    assert _estado(abrir, recibida) == "received"
    assert client.post(f"/api/purchase-orders/{recibida}/enviar").status_code == 409
    assert _estado(abrir, recibida) == "received"


# ── cancelar ─────────────────────────────────────────────────────────────


def test_cancelar_un_borrador_y_una_enviada(abrir):
    client = _app(abrir)
    borrador, enviada = _orden(client, abrir), _orden(client, abrir)
    client.post(f"/api/purchase-orders/{enviada}/enviar")
    for oid in (borrador, enviada):
        r = client.post(f"/api/purchase-orders/{oid}/cancelar")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "cancelled" and len(r.json()["items"]) == 1
        assert _estado(abrir, oid) == "cancelled"


def test_cancelar_dos_veces_es_idempotente(abrir):
    client = _app(abrir)
    oid = _orden(client, abrir)
    primera = client.post(f"/api/purchase-orders/{oid}/cancelar")
    segunda = client.post(f"/api/purchase-orders/{oid}/cancelar")
    assert (primera.status_code, segunda.status_code) == (200, 200) and segunda.json() == primera.json()


def test_cancelar_una_orden_con_mercaderia_recibida_es_409(abrir):
    """Parcial o completa: ya entró stock, y deshacerlo no es cancelar la orden sino devolverle al proveedor."""
    client = _app(abrir)
    completa = _orden(client, abrir, cantidad=5)
    _recibir_todo(client, abrir, completa)
    assert _estado(abrir, completa) == "received"
    r = client.post(f"/api/purchase-orders/{completa}/cancelar")
    assert r.status_code == 409 and "received" in r.json()["detail"]
    assert _estado(abrir, completa) == "received"

    parcial = _orden(client, abrir, cantidad=10)
    with abrir() as conn:
        orden = compras.obtener_orden(conn, parcial)
        deposito = catalogo.create_deposito(conn, "Depósito parcial")
    rec = client.post("/api/purchase-receipts", json={"proveedor_id": orden["supplier_party_id"], "purchase_order_id": parcial}).json()
    client.post(f"/api/purchase-receipts/{rec['id']}/items", json={"item_id": orden["items"][0]["item_id"], "quantity": 4, "unit_cost": 100})
    client.post(f"/api/purchase-receipts/{rec['id']}/confirm", json={"deposito_id": deposito})
    assert _estado(abrir, parcial) == "partial"
    assert client.post(f"/api/purchase-orders/{parcial}/cancelar").status_code == 409
    assert client.post(f"/api/purchase-orders/{parcial}/enviar").status_code == 409      # y tampoco se "envía": ya llegó parte
    assert _estado(abrir, parcial) == "partial"


@pytest.mark.parametrize("accion", ["enviar", "cancelar"])
def test_una_orden_que_no_existe_es_404(abrir, accion):
    assert _app(abrir).post(f"/api/purchase-orders/999/{accion}").status_code == 404


# ── la guarda ────────────────────────────────────────────────────────────


def test_enviar_y_cancelar_llevan_la_guarda_de_escritura_de_compras(abrir):
    rol = {"actual": "admin"}

    def solo_admin():
        if rol["actual"] != "admin":
            raise HTTPException(403, "Sólo el administrador.")

    client = _app(abrir, opciones=OpcionesCompras(autorizar_escritura=Depends(solo_admin)))
    oid = _orden(client, abrir)
    rol["actual"] = "cajero"
    assert client.post(f"/api/purchase-orders/{oid}/enviar").status_code == 403
    assert client.post(f"/api/purchase-orders/{oid}/cancelar").status_code == 403
    assert _estado(abrir, oid) == "draft"                                    # no se escribió nada
    assert client.get(f"/api/purchase-orders/{oid}").status_code == 200      # la lectura sigue abierta
    rol["actual"] = "admin"
    assert client.post(f"/api/purchase-orders/{oid}/enviar").status_code == 200


def test_sin_guarda_el_default_es_el_de_siempre_y_se_traduce_el_proveedor(abrir):
    """El default de la costura (`autorizar_escritura=None`) deja pasar, y los ganchos de proveedor del producto se aplican a la respuesta."""
    traducidos = []

    def proveedor_de(conn, party_id):
        traducidos.append(party_id)
        return party_id + 1000

    client = _app(abrir, opciones=OpcionesCompras(proveedor_de=proveedor_de))
    oid = _orden(client, abrir)
    traducidos.clear()
    r = client.post(f"/api/purchase-orders/{oid}/enviar")
    assert r.status_code == 200 and r.json()["proveedor_id"] == traducidos[0] + 1000


# ── una cancelada no recibe ──────────────────────────────────────────────


def test_una_orden_cancelada_no_recibe_mercaderia(abrir):
    """Sin esto, confirmar una recepción contra una cancelada la pasaba a `partial`/`received`: resucitaba."""
    client = _app(abrir)
    oid = _orden(client, abrir, cantidad=5)
    with abrir() as conn:
        orden = compras.obtener_orden(conn, oid)
        deposito = catalogo.create_deposito(conn, "Depósito cancelada")
    item = orden["items"][0]["item_id"]

    # Una recepción ya creada (borrador) antes de que se cancele la orden...
    rec = client.post("/api/purchase-receipts", json={"proveedor_id": orden["supplier_party_id"], "purchase_order_id": oid}).json()
    client.post(f"/api/purchase-receipts/{rec['id']}/items", json={"item_id": item, "quantity": 5, "unit_cost": 100})
    client.post(f"/api/purchase-orders/{oid}/cancelar")
    # ...no se puede confirmar,
    r = client.post(f"/api/purchase-receipts/{rec['id']}/confirm", json={"deposito_id": deposito})
    assert r.status_code == 409 and "cancelada" in r.json()["detail"]
    # ni abrir una nueva contra ella.
    assert client.post("/api/purchase-receipts", json={"proveedor_id": orden["supplier_party_id"], "purchase_order_id": oid}).status_code == 409
    assert _estado(abrir, oid) == "cancelled"
    with abrir() as conn:
        assert conn.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0] == 0           # y no entró stock


def test_recibir_de_una_vez_contra_una_cancelada_es_409(abrir_vto):
    """`POST /purchase-receipts/recibir` (ADR-046, pide la revisión 0006): la misma regla, y un reintento de una recepción ya hecha sigue contestando lo de la primera vez."""
    client = _app(abrir_vto)
    oid = _orden(client, abrir_vto, cantidad=5)
    with abrir_vto() as conn:
        orden = compras.obtener_orden(conn, oid)
        deposito = catalogo.create_deposito(conn, "Depósito cancelada")
    item = orden["items"][0]["item_id"]
    cuerpo = {"proveedor_id": orden["supplier_party_id"], "purchase_order_id": oid, "deposito_id": deposito,
              "items": [{"item_id": item, "quantity": 2, "unit_cost": 100}]}

    hecha = client.post("/api/purchase-receipts/recibir", json={**cuerpo, "clave_operacion": "antes-de-cancelar"})
    assert hecha.status_code == 200, hecha.text                          # esto entró antes de cancelar: la orden está `partial`...
    assert _estado(abrir_vto, oid) == "partial"
    assert client.post(f"/api/purchase-orders/{oid}/cancelar").status_code == 409     # ...y ya no se cancela

    otra = _orden(client, abrir_vto, cantidad=5)
    client.post(f"/api/purchase-orders/{otra}/cancelar")
    r = client.post("/api/purchase-receipts/recibir", json={**cuerpo, "purchase_order_id": otra, "clave_operacion": "contra-cancelada"})
    assert r.status_code == 409 and "cancelada" in r.json()["detail"]
    assert _estado(abrir_vto, otra) == "cancelled"


def test_agregar_lineas_a_una_cancelada_sigue_siendo_409(abrir):
    client = _app(abrir)
    oid = _orden(client, abrir)
    client.post(f"/api/purchase-orders/{oid}/cancelar")
    r = client.post(f"/api/purchase-orders/{oid}/items", json={"item_id": _producto(abrir, "Otro"), "quantity_ordered": 1, "unit_cost": 1})
    assert r.status_code == 409
