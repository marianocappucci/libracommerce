"""Órdenes de compra y recepciones (2026-09-27): reponer inventario con seguimiento de lo pedido y lo recibido.

Extraído de VentaLibra (`app/services/purchasing.py`), que ya montaba el dominio y el caso de uso de este motor
(`domain/purchasing.py`, `usecases/purchasing.py`) sin reimplementar nada — sólo el cableado HTTP era propio. Es la
misma situación que catálogo, depósitos y stock antes de sus factories: el dominio y las tablas (`purchase_orders`,
`purchase_order_items`, `purchase_receipts`, `purchase_receipt_items`) ya viven en `libracommerce.db.schema`.

**Nadie más de la familia lo tiene montado hoy.** Contalibra y Restolibra resuelven "comprarle a un proveedor" con
`Egresos` (registrar el gasto, sin seguimiento de pedido/recibido ni movimiento de stock) — es un concepto distinto,
no un reemplazo, y una instancia puede montar los dos.

Los campos hablan el vocabulario del dominio (`quantity_ordered`, `status`…, en inglés, como ya estaba) salvo uno:
`proveedor_id`, que es lo único con lo que un producto necesita traducir (ver `OpcionesCompras` en
`web/compras_router.py`) — VentaLibra guarda proveedores en una tabla propia con un id que NO es el `party_id` que
esta tabla referencia (`supplier_party_id INTEGER NOT NULL REFERENCES parties(id)`).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Any

from ..db.repository import repositorio_de
from ..domain.purchasing import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
    PurchaseReceipt,
    PurchaseReceiptItem,
    PurchaseReceiptStatus,
)
from ..usecases.purchasing import confirm_purchase_receipt


class OrdenNoEncontrada(Exception):
    """404: no existe una orden de compra con ese id."""


class RecepcionNoEncontrada(Exception):
    """404: no existe una recepción con ese id."""


class EstadoInvalido(Exception):
    """409: la operación no es válida para el estado actual de la orden o la recepción."""


#: `conn -> "OC-000001"`. El default no toma un lock: lee `MAX(id)+1` sobre una tabla sin baja (no hay `DELETE`), es
#: el mismo criterio de `erp.catalogo.generar_codigo_producto`. Un producto con alta concurrencia real pasa el suyo
#: (VentaLibra: `next_sequence`, atómico dentro de la misma transacción).
Numerador = Callable[[Any], str]


def numero_por_defecto(conn: Any) -> str:
    fila = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM purchase_orders").fetchone()
    return f"OC-{fila[0]:06d}"


def _orden_dict(order: PurchaseOrder) -> dict:
    return {
        "id": order.id,
        "number": order.number,
        "supplier_party_id": order.supplier_party_id,
        "branch_id": order.branch_id,
        "status": order.status.value,
        "items": [
            {
                "item_id": i.item_id,
                "quantity_ordered": str(i.quantity_ordered),
                "quantity_received": str(i.quantity_received),
                "pending_quantity": str(i.pending_quantity),
                "unit_cost": str(i.unit_cost),
                "tax_rate": str(i.tax_rate),
                "subtotal": str(i.subtotal),
            }
            for i in order.items
        ],
        "is_fully_received": order.is_fully_received(),
    }


def _recepcion_dict(receipt: PurchaseReceipt) -> dict:
    return {
        "id": receipt.id,
        "supplier_party_id": receipt.supplier_party_id,
        "purchase_order_id": receipt.purchase_order_id,
        "status": receipt.status.value,
        "items": [
            {
                "item_id": i.item_id,
                "quantity": str(i.quantity),
                "unit_cost": str(i.unit_cost),
                "lot_code": i.lot_code,
                "expires_at": i.expires_at.isoformat() if i.expires_at else None,
            }
            for i in receipt.items
        ],
        "received_at": receipt.received_at.isoformat() if receipt.received_at else None,
        "document_reference": receipt.document_reference,
    }


# ── Órdenes de compra ─────────────────────────────────────────────────────


def crear_orden(conn, *, supplier_party_id: int, branch_id: int | None = None,
                numerador: Numerador = numero_por_defecto) -> dict:
    repo = repositorio_de(conn)
    orden = PurchaseOrder(
        id=None, number=numerador(conn), supplier_party_id=supplier_party_id,
        items=(), branch_id=branch_id,
    )
    return _orden_dict(repo.save_purchase_order(orden))


def obtener_orden(conn, orden_id: int) -> dict:
    orden = repositorio_de(conn).get_purchase_order(orden_id)
    if orden is None:
        raise OrdenNoEncontrada(orden_id)
    return _orden_dict(orden)


def _obtener_orden_domain(conn, orden_id: int) -> PurchaseOrder:
    orden = repositorio_de(conn).get_purchase_order(orden_id)
    if orden is None:
        raise OrdenNoEncontrada(orden_id)
    return orden


def listar_ordenes(conn) -> list[dict]:
    return [_orden_dict(o) for o in repositorio_de(conn).list_purchase_orders()]


def agregar_linea_orden(conn, orden_id: int, *, item_id: int, quantity_ordered: Decimal,
                        unit_cost: Decimal, tax_rate: Decimal = Decimal("0")) -> dict:
    orden = _obtener_orden_domain(conn, orden_id)
    if orden.status not in (PurchaseOrderStatus.DRAFT, PurchaseOrderStatus.SENT):
        raise EstadoInvalido(f"la orden {orden_id} no admite nuevas líneas (status={orden.status})")
    linea = PurchaseOrderItem(item_id=item_id, quantity_ordered=quantity_ordered, unit_cost=unit_cost,
                             tax_rate=tax_rate)
    actualizada = replace(orden, items=orden.items + (linea,))
    return _orden_dict(repositorio_de(conn).save_purchase_order(actualizada))


# ── Recepciones ──────────────────────────────────────────────────────────


def crear_recepcion(conn, *, supplier_party_id: int, purchase_order_id: int | None = None,
                    document_reference: str | None = None) -> dict:
    if purchase_order_id is not None:
        _obtener_orden_domain(conn, purchase_order_id)  # 404 temprano si no existe
    recepcion = PurchaseReceipt(
        id=None, supplier_party_id=supplier_party_id, items=(),
        purchase_order_id=purchase_order_id, document_reference=document_reference,
    )
    return _recepcion_dict(repositorio_de(conn).save_purchase_receipt(recepcion))


def _obtener_recepcion_domain(conn, recepcion_id: int) -> PurchaseReceipt:
    recepcion = repositorio_de(conn).get_purchase_receipt(recepcion_id)
    if recepcion is None:
        raise RecepcionNoEncontrada(recepcion_id)
    return recepcion


def obtener_recepcion(conn, recepcion_id: int) -> dict:
    return _recepcion_dict(_obtener_recepcion_domain(conn, recepcion_id))


def listar_recepciones(conn, *, purchase_order_id: int | None = None) -> list[dict]:
    recepciones = repositorio_de(conn).list_purchase_receipts()
    if purchase_order_id is not None:
        recepciones = [r for r in recepciones if r.purchase_order_id == purchase_order_id]
    return [_recepcion_dict(r) for r in recepciones]


def agregar_linea_recepcion(conn, recepcion_id: int, *, item_id: int, quantity: Decimal, unit_cost: Decimal,
                            lot_code: str | None = None, expires_at: datetime | None = None) -> dict:
    recepcion = _obtener_recepcion_domain(conn, recepcion_id)
    if recepcion.status != PurchaseReceiptStatus.DRAFT:
        raise EstadoInvalido(f"la recepción {recepcion_id} no está en borrador (status={recepcion.status})")
    linea = PurchaseReceiptItem(item_id=item_id, quantity=quantity, unit_cost=unit_cost,
                                lot_code=lot_code, expires_at=expires_at)
    actualizada = replace(recepcion, items=recepcion.items + (linea,))
    return _recepcion_dict(repositorio_de(conn).save_purchase_receipt(actualizada))


def confirmar_recepcion(conn, recepcion_id: int, *, location_id: int, occurred_at: datetime) -> dict:
    recepcion = _obtener_recepcion_domain(conn, recepcion_id)
    if recepcion.status != PurchaseReceiptStatus.DRAFT:
        raise EstadoInvalido(f"la recepción {recepcion_id} no está en borrador (status={recepcion.status})")
    if not recepcion.items:
        raise EstadoInvalido("no se puede confirmar una recepción sin líneas")
    confirmada = confirm_purchase_receipt(repositorio_de(conn), recepcion, location_id, occurred_at)
    return _recepcion_dict(confirmada)
