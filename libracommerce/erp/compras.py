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

import hashlib
import sqlite3
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
from . import claves
from .catalogo import validar_deposito


class OrdenNoEncontrada(Exception):
    """404: no existe una orden de compra con ese id."""


class RecepcionNoEncontrada(Exception):
    """404: no existe una recepción con ese id."""


class EstadoInvalido(Exception):
    """409: la operación no es válida para el estado actual de la orden o la recepción."""


class SinRevision(Exception):
    """503: a la base le falta la revisión `0006_clave_operacion_recepciones` del motor (la columna `purchase_receipts.clave_operacion`)."""


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


# ── Recibir mercadería de una vez, idempotente (ADR-046) ────────────────────
#
# Las tres llamadas de arriba (crear, cargar líneas, confirmar) son tres escrituras con tres commits: si la respuesta de la última se pierde y la persona reintenta, el
# cliente vuelve a empezar desde la primera y el stock entra dos veces. `recibir_mercaderia` es la misma operación en UNA transacción y con una `clave_operacion`: un reintento no
# escribe nada y contesta la recepción de la primera vez.

#: Qué hace el candado de la clave; sólo cambia el texto que se hashea, para no chocar con el de otra operación que use la misma clave.
_CANDADO = "recepcion-de-mercaderia:"


def tiene_clave_operacion(conn) -> bool:
    """Si la base tiene la revisión `0006_clave_operacion_recepciones` (la columna `purchase_receipts.clave_operacion`). Se sondea por metadatos (`PRAGMA table_info`, que el
    adaptador de PostgreSQL traduce a `information_schema`) y no con un `SELECT` de la columna: en PostgreSQL un `SELECT` fallido aborta la transacción entera."""
    return "clave_operacion" in {f[1] for f in conn.execute("PRAGMA table_info(purchase_receipts)").fetchall()}


def _serializar(conn, clave: str) -> None:
    """Toma el candado de ESTA clave hasta el fin de la transacción: dos pedidos a la vez con la misma clave (un doble clic que salió dos veces) se hacen cola y el segundo ve la
    recepción del primero. PostgreSQL: un candado asesor (`pg_advisory_xact_lock`). SQLite (sólo pruebas y nodos sueltos): un `UPDATE` que no toca ninguna fila igual toma el candado de
    escritura de la base, que ya serializa a los escritores. Detrás hay además un índice único sobre la clave (revisión 0006): si el candado fallara, la base rechaza la segunda."""
    if isinstance(conn, sqlite3.Connection):
        conn.execute("UPDATE purchase_receipts SET clave_operacion = clave_operacion WHERE 0")
    else:
        numero = int.from_bytes(hashlib.sha256((_CANDADO + clave).encode()).digest()[:7], "big")
        conn.execute("SELECT pg_advisory_xact_lock(?)", (numero,))


def _texto_o_none(valor: str | None) -> str | None:
    """Un texto recortado, o `None` si no queda nada: `""`, `"  "` y `None` son lo mismo (no hay lote / no hay remito)."""
    return (valor or "").strip() or None


def _huella_del_pedido(supplier_party_id: int, purchase_order_id: int | None, document_reference: str | None, deposito_id: int,
                       lineas: list[PurchaseReceiptItem]) -> dict:
    """Lo que identifica a una recepción para decidir si un pedido con una clave ya usada es el mismo o es otro. Las cantidades y los costos son `Decimal`: `1` y `1.0` son lo mismo."""
    return {
        "proveedor": supplier_party_id, "orden": purchase_order_id, "remito": document_reference, "deposito": deposito_id,
        "lineas": [(x.item_id, x.quantity, x.unit_cost, x.lot_code, x.expires_at) for x in lineas],
    }


def recibir_mercaderia(conn, *, supplier_party_id: int, deposito_id: int, items: list[dict], clave_operacion: str,
                       occurred_at: datetime, purchase_order_id: int | None = None,
                       document_reference: str | None = None) -> dict:
    """Crea la recepción con sus líneas y la confirma (entra el stock, se actualiza el último costo y el avance de la orden) **en una sola transacción**: o queda todo o no queda nada.

    **Idempotente por `clave_operacion`** (obligatoria: un texto no vacío de hasta 64 caracteres, sin corchetes; p. ej. un UUID que el cliente genera una vez por intento). La clave se
    guarda en `purchase_receipts.clave_operacion` (índice único) y se busca con el candado de la clave tomado. Un **reintento con la misma clave y los mismos datos** no escribe nada y
    devuelve la recepción de la primera vez con `repetida: True`; con la misma clave y **otros** datos (otro proveedor, orden, remito, depósito o líneas), `claves.ClaveReusada` (409).

    `items` es una lista de `{item_id, quantity, unit_cost, lot_code?, expires_at?}`. `ValueError` con datos inválidos (clave mal formada, sin líneas, cantidad ≤ 0, costo < 0, un producto o
    el depósito inexistente); `OrdenNoEncontrada`; `SinRevision` sin la revisión `0006`. Confirma la transacción (las demás funciones de este módulo confirman cada escritura).
    Devuelve la recepción (`_recepcion_dict`) más `repetida`."""
    clave = claves.normalizar_clave(clave_operacion)
    if not items:
        raise ValueError("no se puede recibir mercadería sin líneas")
    lineas: list[PurchaseReceiptItem] = []
    for i in items:
        try:
            cantidad, costo = Decimal(str(i["quantity"])), Decimal(str(i["unit_cost"]))
        except ArithmeticError as e:
            raise ValueError(f"la cantidad y el costo del producto {i['item_id']} tienen que ser números: {i['quantity']!r}, {i['unit_cost']!r}") from e
        if not cantidad.is_finite() or cantidad <= 0:
            raise ValueError(f"la cantidad recibida del producto {i['item_id']} tiene que ser mayor que 0: {i['quantity']}")
        if not costo.is_finite() or costo < 0:
            raise ValueError(f"el costo unitario del producto {i['item_id']} no puede ser negativo: {i['unit_cost']}")
        lineas.append(PurchaseReceiptItem(item_id=i["item_id"], quantity=cantidad, unit_cost=costo,
                                          lot_code=_texto_o_none(i.get("lot_code")), expires_at=i.get("expires_at")))
    remito = _texto_o_none(document_reference)
    pedido = _huella_del_pedido(supplier_party_id, purchase_order_id, remito, deposito_id, lineas)
    if not tiene_clave_operacion(conn):
        raise SinRevision("Falta la revisión 0006_clave_operacion_recepciones del motor: corré `libracommerce-migrar upgrade` "
                          "(--prefijo del producto) antes de recibir mercadería con clave de operación.")

    repo = repositorio_de(conn)
    with repo.transaction():
        _serializar(conn, clave)
        previa = conn.execute("SELECT id FROM purchase_receipts WHERE clave_operacion = ?", (clave,)).fetchone()
        if previa is not None:
            return _recepcion_repetida(conn, previa[0], pedido)

        if purchase_order_id is not None:
            _obtener_orden_domain(conn, purchase_order_id)  # 404 temprano si no existe
        validar_deposito(conn, deposito_id)   # existe y está activo (ADR-051): `DepositoInexistente`, un `ValueError` (422)
        for linea in lineas:
            if not conn.execute("SELECT 1 FROM catalog_items WHERE id = ?", (linea.item_id,)).fetchall():
                raise ValueError(f"el producto {linea.item_id} no existe")
        recepcion = repo.save_purchase_receipt(PurchaseReceipt(
            id=None, supplier_party_id=supplier_party_id, items=tuple(lineas),
            purchase_order_id=purchase_order_id, document_reference=remito,
        ))
        # La clave se escribe aparte porque el repositorio no la conoce (la columna es de una revisión opt-in, no de `init_schema`).
        conn.execute("UPDATE purchase_receipts SET clave_operacion = ? WHERE id = ?", (clave, recepcion.id))
        confirm_purchase_receipt(repo, recepcion, deposito_id, occurred_at)
        # Se vuelve a leer lo escrito, en vez de devolver lo que se armó en memoria: así la respuesta de la primera vez y la de un reintento son la misma, letra por letra
        # (PostgreSQL devuelve `8.0` para lo que se mandó como `8`).
        return {**obtener_recepcion(conn, recepcion.id), "repetida": False}


def _recepcion_repetida(conn, recepcion_id: int, pedido: dict) -> dict:
    """La recepción que ya se escribió con esta clave, o `claves.ClaveReusada` si lo pedido ahora no es lo mismo. Sin escribir nada."""
    recepcion = _obtener_recepcion_domain(conn, recepcion_id)
    depositos = {f[0] for f in conn.execute(
        "SELECT DISTINCT location_id FROM stock_movements WHERE source_type = 'purchase_receipt' AND source_id = ?", (recepcion_id,)).fetchall()}
    previa = _huella_del_pedido(recepcion.supplier_party_id, recepcion.purchase_order_id, recepcion.document_reference,
                                depositos.pop() if len(depositos) == 1 else None, list(recepcion.items))
    if recepcion.status != PurchaseReceiptStatus.CONFIRMED or previa != pedido:
        raise claves.ClaveReusada("la clave_operacion ya se usó con otros datos: es otra recepción y necesita otra clave")
    return {**_recepcion_dict(recepcion), "repetida": True}
