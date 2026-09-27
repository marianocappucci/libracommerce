"""Órdenes de compra y recepciones como factory de router (2026-09-27).

Extraído de VentaLibra, el único producto que lo tenía montado — ver el docstring de `erp/compras.py`. La factory
no sabe nada de "proveedores": recibe y devuelve `proveedor_id`, y es el producto quien dice —vía `OpcionesCompras`—
cómo se traduce eso al `supplier_party_id` que exige la FK del dominio (`purchase_orders.supplier_party_id
REFERENCES parties(id)`). Sin la opción, `proveedor_id` **es** el `party_id`: el caso más simple, sin traducción.

```python
app.include_router(build_compras_router(conexion=_abrir_conexion, usuario_actual=get_current_user_json))
```
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ..erp import compras
from . import fastapi as _fastapi
from .catalogo_router import Conexion, _deps

_fastapi()
from fastapi import APIRouter, Depends, HTTPException  # noqa: E402
from pydantic import BaseModel  # noqa: E402


class OrdenCreatePayload(BaseModel):
    #: El id con el que el producto identifica al proveedor. Sin `OpcionesCompras.resolver_proveedor`, es
    #: directamente el `party_id` de la FK.
    proveedor_id: int
    branch_id: int | None = None


class OrdenItemPayload(BaseModel):
    item_id: int
    quantity_ordered: Decimal
    unit_cost: Decimal
    tax_rate: Decimal = Decimal("0")


class RecepcionCreatePayload(BaseModel):
    proveedor_id: int
    purchase_order_id: int | None = None
    document_reference: str | None = None


class RecepcionItemPayload(BaseModel):
    item_id: int
    quantity: Decimal
    unit_cost: Decimal
    lot_code: str | None = None
    expires_at: datetime | None = None


class ConfirmarPayload(BaseModel):
    #: El `id` del depósito de destino (`libracommerce.web.catalogo_router.build_depositos_router`).
    deposito_id: int


def _sin_traduccion(_conn: Any, id_: int) -> int:
    return id_


@dataclass(frozen=True)
class OpcionesCompras:
    """Lo que un producto le agrega a Compras. Sin nada, el router funciona con `proveedor_id == party_id` y sin
    restricción extra de quién escribe.

    - `autorizar_escritura`: quién crea órdenes/recepciones, agrega líneas y confirma (una `Depends(...)`, como
      `OpcionesCajas.autorizar_escritura`). La lectura queda con la protección con la que el producto monte el
      router entero.
    - `numerador(conn) -> str`: la numeración de una orden nueva (`"OC-000001"`). Default: `MAX(id)+1`, sin lock —
      ver `erp.compras.numero_por_defecto`. Un producto con alta concurrencia real pasa el suyo.
    - `resolver_proveedor(conn, proveedor_id) -> party_id`: traduce el `proveedor_id` del pedido al `party_id` que
      exige la FK. Levanta lo que quiera (`HTTPException` incluida: el motor no sabe qué código le toca a un
      proveedor inexistente para cada producto). Default: identidad.
    - `proveedor_de(conn, party_id) -> proveedor_id`: la inversa, para lo que se devuelve. Default: identidad.
    """

    autorizar_escritura: Any = None
    numerador: compras.Numerador = compras.numero_por_defecto
    resolver_proveedor: Callable[[Any, int], int] = field(default=_sin_traduccion)
    proveedor_de: Callable[[Any, int], int] = field(default=_sin_traduccion)


def build_compras_router(*, conexion: Conexion | None = None,
                         usuario_actual: Callable[..., Any] | None = None,
                         prefix: str = "/api",
                         opciones: OpcionesCompras | None = None) -> APIRouter:
    abrir, _usuario = _deps(usuario_actual, conexion)
    opt = opciones or OpcionesCompras()
    escribe = [opt.autorizar_escritura] if opt.autorizar_escritura is not None else []

    router = APIRouter(prefix=prefix, tags=["compras"])

    def _con_proveedor(conn, d: dict) -> dict:
        d = dict(d)
        d["proveedor_id"] = opt.proveedor_de(conn, d.pop("supplier_party_id"))
        return d

    # ── Órdenes de compra ──────────────────────────────────────────────

    @router.post("/purchase-orders", dependencies=escribe)
    def crear_orden(payload: OrdenCreatePayload):
        with abrir() as conn:
            party_id = opt.resolver_proveedor(conn, payload.proveedor_id)
            orden = compras.crear_orden(
                conn, supplier_party_id=party_id, branch_id=payload.branch_id, numerador=opt.numerador,
            )
            return _con_proveedor(conn, orden)

    @router.get("/purchase-orders")
    def listar_ordenes():
        with abrir() as conn:
            return [_con_proveedor(conn, o) for o in compras.listar_ordenes(conn)]

    @router.get("/purchase-orders/{orden_id}")
    def obtener_orden(orden_id: int):
        with abrir() as conn:
            try:
                return _con_proveedor(conn, compras.obtener_orden(conn, orden_id))
            except compras.OrdenNoEncontrada as e:
                raise HTTPException(404, "purchase order not found") from e

    @router.post("/purchase-orders/{orden_id}/items", dependencies=escribe)
    def agregar_linea_orden(orden_id: int, payload: OrdenItemPayload):
        with abrir() as conn:
            try:
                orden = compras.agregar_linea_orden(
                    conn, orden_id, item_id=payload.item_id, quantity_ordered=payload.quantity_ordered,
                    unit_cost=payload.unit_cost, tax_rate=payload.tax_rate,
                )
            except compras.OrdenNoEncontrada as e:
                raise HTTPException(404, "purchase order not found") from e
            except compras.EstadoInvalido as e:
                raise HTTPException(409, str(e)) from e
            return _con_proveedor(conn, orden)

    # ── Recepciones ────────────────────────────────────────────────────

    @router.post("/purchase-receipts", dependencies=escribe)
    def crear_recepcion(payload: RecepcionCreatePayload):
        with abrir() as conn:
            party_id = opt.resolver_proveedor(conn, payload.proveedor_id)
            try:
                recepcion = compras.crear_recepcion(
                    conn, supplier_party_id=party_id, purchase_order_id=payload.purchase_order_id,
                    document_reference=payload.document_reference,
                )
            except compras.OrdenNoEncontrada as e:
                raise HTTPException(404, "purchase order not found") from e
            return _con_proveedor(conn, recepcion)

    @router.get("/purchase-receipts")
    def listar_recepciones(purchase_order_id: int | None = None):
        with abrir() as conn:
            return [
                _con_proveedor(conn, r)
                for r in compras.listar_recepciones(conn, purchase_order_id=purchase_order_id)
            ]

    @router.get("/purchase-receipts/{recepcion_id}")
    def obtener_recepcion(recepcion_id: int):
        with abrir() as conn:
            try:
                return _con_proveedor(conn, compras.obtener_recepcion(conn, recepcion_id))
            except compras.RecepcionNoEncontrada as e:
                raise HTTPException(404, "purchase receipt not found") from e

    @router.post("/purchase-receipts/{recepcion_id}/items", dependencies=escribe)
    def agregar_linea_recepcion(recepcion_id: int, payload: RecepcionItemPayload):
        with abrir() as conn:
            try:
                recepcion = compras.agregar_linea_recepcion(
                    conn, recepcion_id, item_id=payload.item_id, quantity=payload.quantity,
                    unit_cost=payload.unit_cost, lot_code=payload.lot_code, expires_at=payload.expires_at,
                )
            except compras.RecepcionNoEncontrada as e:
                raise HTTPException(404, "purchase receipt not found") from e
            except compras.EstadoInvalido as e:
                raise HTTPException(409, str(e)) from e
            return _con_proveedor(conn, recepcion)

    @router.post("/purchase-receipts/{recepcion_id}/confirm", dependencies=escribe)
    def confirmar_recepcion(recepcion_id: int, payload: ConfirmarPayload):
        with abrir() as conn:
            try:
                recepcion = compras.confirmar_recepcion(
                    conn, recepcion_id, location_id=payload.deposito_id, occurred_at=datetime.now(UTC),
                )
            except compras.RecepcionNoEncontrada as e:
                raise HTTPException(404, "purchase receipt not found") from e
            except compras.EstadoInvalido as e:
                raise HTTPException(409, str(e)) from e
            return _con_proveedor(conn, recepcion)

    return router
