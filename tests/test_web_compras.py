"""El router de Compras (P9 de VentaLibra, 2026-09-27), contra los dos motores de base.

Es el primer consumidor: no hay un "cómo lo monta Contalibra" que replicar, así que estos tests fijan el contrato
en sí mismo (a diferencia de `test_web_catalogo.py`, que porta el contrato ya fijado por los productos).
"""

from __future__ import annotations

from decimal import Decimal

from conftest import USUARIO, _usuario
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from libracommerce.domain.entities import Party, PartyType
from libracommerce.erp import catalogo
from libracommerce.web.compras_router import OpcionesCompras, build_compras_router


def _app(abrir, *, opciones=None) -> TestClient:
    app = FastAPI()
    app.include_router(build_compras_router(conexion=abrir, usuario_actual=_usuario, opciones=opciones))
    return TestClient(app)


def _proveedor(abrir, nombre="Distribuidora SA") -> int:
    with abrir() as conn:
        from libracommerce.db.repository import repositorio_de

        party = repositorio_de(conn).save_party(Party(None, PartyType.ORGANIZATION, nombre))
        return party.id


def _producto(abrir, nombre="Yerba 1kg") -> int:
    with abrir() as conn:
        return catalogo.create_producto(conn, nombre, precio_venta=1500.0, precio_costo=900.0)


def _deposito(abrir) -> int:
    with abrir() as conn:
        return catalogo.create_deposito(conn, "Depósito test")


def test_crear_una_orden_y_agregarle_lineas(abrir):
    proveedor = _proveedor(abrir)
    producto = _producto(abrir)
    client = _app(abrir)

    creada = client.post("/api/purchase-orders", json={"proveedor_id": proveedor})
    assert creada.status_code == 200, creada.text
    orden = creada.json()
    assert orden["number"] == "OC-000001" and orden["proveedor_id"] == proveedor
    assert orden["status"] == "draft" and orden["items"] == [] and "supplier_party_id" not in orden

    con_linea = client.post(f"/api/purchase-orders/{orden['id']}/items", json={
        "item_id": producto, "quantity_ordered": 10, "unit_cost": 100,
    })
    assert con_linea.status_code == 200, con_linea.text
    linea = con_linea.json()["items"][0]
    # `Decimal(...)` y no comparar el string tal cual: PostgreSQL devuelve "10.0" para lo que SQLite guarda "10".
    assert linea["item_id"] == producto
    assert Decimal(linea["quantity_ordered"]) == Decimal(linea["pending_quantity"]) == 10
    assert con_linea.json()["is_fully_received"] is False

    otra = client.post("/api/purchase-orders", json={"proveedor_id": proveedor})
    assert otra.json()["number"] == "OC-000002"  # la numeración por default sigue del máximo, sin reusar
    assert len(client.get("/api/purchase-orders").json()) == 2
    assert client.get(f"/api/purchase-orders/{orden['id']}").json()["id"] == orden["id"]
    assert client.get("/api/purchase-orders/999").status_code == 404


def test_una_orden_recibida_no_admite_nuevas_lineas(abrir):
    proveedor = _proveedor(abrir)
    producto = _producto(abrir)
    deposito = _deposito(abrir)
    client = _app(abrir)
    orden = client.post("/api/purchase-orders", json={"proveedor_id": proveedor}).json()
    client.post(f"/api/purchase-orders/{orden['id']}/items", json={
        "item_id": producto, "quantity_ordered": 5, "unit_cost": 100,
    })
    recepcion = client.post("/api/purchase-receipts", json={
        "proveedor_id": proveedor, "purchase_order_id": orden["id"],
    }).json()
    client.post(f"/api/purchase-receipts/{recepcion['id']}/items", json={
        "item_id": producto, "quantity": 5, "unit_cost": 100,
    })
    confirmada = client.post(f"/api/purchase-receipts/{recepcion['id']}/confirm", json={"deposito_id": deposito})
    assert confirmada.status_code == 200 and confirmada.json()["status"] == "confirmed"

    orden_actualizada = client.get(f"/api/purchase-orders/{orden['id']}").json()
    assert orden_actualizada["status"] == "received" and orden_actualizada["is_fully_received"] is True
    assert Decimal(orden_actualizada["items"][0]["quantity_received"]) == 5

    r = client.post(f"/api/purchase-orders/{orden['id']}/items", json={
        "item_id": producto, "quantity_ordered": 1, "unit_cost": 100,
    })
    assert r.status_code == 409 and "received" in r.json()["detail"]


def test_la_recepcion_confirmada_mueve_stock_y_actualiza_el_costo(abrir):
    proveedor = _proveedor(abrir)
    producto = _producto(abrir)
    deposito = _deposito(abrir)
    client = _app(abrir)

    recepcion = client.post("/api/purchase-receipts", json={
        "proveedor_id": proveedor, "document_reference": "REM-0001",
    }).json()
    assert recepcion["purchase_order_id"] is None and recepcion["status"] == "draft"
    client.post(f"/api/purchase-receipts/{recepcion['id']}/items", json={
        "item_id": producto, "quantity": 8, "unit_cost": 250, "lot_code": "L1",
    })
    sin_lineas = client.post("/api/purchase-receipts", json={"proveedor_id": proveedor}).json()
    r = client.post(f"/api/purchase-receipts/{sin_lineas['id']}/confirm", json={"deposito_id": deposito})
    assert r.status_code == 409 and "sin líneas" in r.json()["detail"]

    confirmada = client.post(f"/api/purchase-receipts/{recepcion['id']}/confirm", json={"deposito_id": deposito})
    assert confirmada.status_code == 200, confirmada.text
    assert confirmada.json()["received_at"] is not None

    with abrir() as conn:
        stock = conn.execute(
            "SELECT COALESCE(SUM(quantity_delta),0) FROM stock_movements WHERE item_id=? AND location_id=?",
            (producto, deposito),
        ).fetchone()[0]
        costo = conn.execute("SELECT default_cost FROM catalog_items WHERE id=?", (producto,)).fetchone()[0]
    assert float(stock) == 8.0 and float(costo) == 250.0

    # Ya confirmada, no se le agregan más líneas ni se confirma dos veces.
    assert client.post(f"/api/purchase-receipts/{recepcion['id']}/items", json={
        "item_id": producto, "quantity": 1, "unit_cost": 250}).status_code == 409
    assert client.post(f"/api/purchase-receipts/{recepcion['id']}/confirm",
                       json={"deposito_id": deposito}).status_code == 409
    assert client.get("/api/purchase-receipts/999").status_code == 404


def test_una_recepcion_de_una_orden_inexistente_da_404(abrir):
    proveedor = _proveedor(abrir)
    client = _app(abrir)
    r = client.post("/api/purchase-receipts", json={"proveedor_id": proveedor, "purchase_order_id": 999})
    assert r.status_code == 404


def test_el_listado_de_recepciones_filtra_por_orden(abrir):
    proveedor = _proveedor(abrir)
    client = _app(abrir)
    orden = client.post("/api/purchase-orders", json={"proveedor_id": proveedor}).json()
    de_la_orden = client.post("/api/purchase-receipts", json={
        "proveedor_id": proveedor, "purchase_order_id": orden["id"]}).json()
    suelta = client.post("/api/purchase-receipts", json={"proveedor_id": proveedor}).json()

    todas = client.get("/api/purchase-receipts").json()
    assert {r["id"] for r in todas} == {de_la_orden["id"], suelta["id"]}
    de_esa_orden = client.get("/api/purchase-receipts", params={"purchase_order_id": orden["id"]}).json()
    assert [r["id"] for r in de_esa_orden] == [de_la_orden["id"]]


def test_sin_opciones_proveedor_id_es_el_party_id(abrir):
    proveedor = _proveedor(abrir)
    client = _app(abrir)
    orden = client.post("/api/purchase-orders", json={"proveedor_id": proveedor}).json()
    assert orden["proveedor_id"] == proveedor


def test_los_ganchos_de_proveedor_traducen_en_los_dos_sentidos(abrir):
    """El `proveedor_id` que ve el producto no tiene por qué coincidir con el `party_id` real (VentaLibra: es
    `proveedores.id`, con un offset). Acá el "id externo" (555) es arbitrario, sin relación aritmética con el
    `party_id` que devuelve `_proveedor` — lo único que importa es que los ganchos son quienes traducen."""
    party_id = _proveedor(abrir)
    externo = 555
    llamadas = []

    def resolver(conn, proveedor_id):
        llamadas.append(("resolver", proveedor_id))
        if proveedor_id != externo:
            raise HTTPException(404, "no existe ese proveedor")
        return party_id

    def proveedor_de(conn, pid):
        llamadas.append(("proveedor_de", pid))
        assert pid == party_id
        return externo

    client = _app(abrir, opciones=OpcionesCompras(resolver_proveedor=resolver, proveedor_de=proveedor_de))

    assert client.post("/api/purchase-orders", json={"proveedor_id": 999}).status_code == 404
    orden = client.post("/api/purchase-orders", json={"proveedor_id": externo}).json()
    assert orden["proveedor_id"] == externo  # ida y vuelta: vuelve al mismo id externo, no al party_id real
    assert ("resolver", externo) in llamadas and ("proveedor_de", party_id) in llamadas


def test_el_numerador_es_configurable(abrir):
    contador = iter(["A-1", "A-2"])
    proveedor = _proveedor(abrir)
    client = _app(abrir, opciones=OpcionesCompras(numerador=lambda conn: next(contador)))
    assert client.post("/api/purchase-orders", json={"proveedor_id": proveedor}).json()["number"] == "A-1"
    assert client.post("/api/purchase-orders", json={"proveedor_id": proveedor}).json()["number"] == "A-2"


def test_autorizar_escritura_protege_las_escrituras_y_no_la_lectura(abrir):
    proveedor = _proveedor(abrir)
    producto = _producto(abrir)
    rol = {"actual": "admin"}

    def solo_admin():
        if rol["actual"] != "admin":
            raise HTTPException(403, "Sólo el administrador.")

    client = _app(abrir, opciones=OpcionesCompras(autorizar_escritura=Depends(solo_admin)))
    orden = client.post("/api/purchase-orders", json={"proveedor_id": proveedor}).json()
    rol["actual"] = "cajero"
    assert client.get("/api/purchase-orders").status_code == 200
    assert client.post("/api/purchase-orders", json={"proveedor_id": proveedor}).status_code == 403
    assert client.post(f"/api/purchase-orders/{orden['id']}/items", json={
        "item_id": producto, "quantity_ordered": 1, "unit_cost": 1}).status_code == 403
    assert client.post("/api/purchase-receipts", json={"proveedor_id": proveedor}).status_code == 403
