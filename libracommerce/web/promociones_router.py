"""Las factories de router de promociones (2026-09-28): "llevá N pagá M" y combos.

Construcción nueva (ver `erp.promociones`). Dos factories, porque el producto las
gatea distinto y comparten prefijo:

- `build_promociones_router`: el CRUD de las reglas. Es de admin: cambia lo que se
  cobra.
- `build_promociones_calculo_router`: `POST /calcular`, qué promociones aplican a
  un carrito. Lo usa el cajero en el punto de venta, así que va con la sesión a
  secas. Sólo lee.

El ahorro que muestra `/calcular` es el mismo que el servidor va a sumar al
descuento al registrar la venta (`OpcionesVentas.promociones`): las dos salen de
`erp.promociones.calcular`.
"""

from typing import Literal

from ..erp import promociones as promos
from . import fastapi as _fastapi
from .catalogo_router import Conexion, _deps

_fastapi()
from fastapi import APIRouter, HTTPException  # noqa: E402
from pydantic import BaseModel  # noqa: E402


class ItemPromocionPayload(BaseModel):
    producto_id: int
    cantidad: float


class PromocionPayload(BaseModel):
    nombre: str
    tipo: Literal["nxm", "combo"]
    items: list[ItemPromocionPayload]
    #: `nxm`: las unidades que se pagan (`cantidad` del producto es las que se llevan).
    paga: float | None = None
    #: `combo`: el precio cerrado del paquete.
    precio: float | None = None
    desde: str = ""
    hasta: str = ""
    activa: bool = True


class LineaCalculoPayload(BaseModel):
    producto_id: int | None = None
    qty: float
    precio: float


class CalculoPayload(BaseModel):
    items: list[LineaCalculoPayload]
    #: ISO, vacío = ahora. Admite un instante con zona (`...Z`).
    en: str = ""


def _campos(payload: PromocionPayload) -> dict:
    return {
        "nombre": payload.nombre, "tipo": payload.tipo,
        "items": [i.model_dump() for i in payload.items],
        "paga": payload.paga, "precio": payload.precio,
        "desde": payload.desde, "hasta": payload.hasta, "activa": payload.activa,
    }


def build_promociones_router(
    *,
    conexion: Conexion | None = None,
    prefix: str = "/api/promociones",
):
    """`GET ""`, `GET /{id}`, `POST ""`, `PUT /{id}`, `DELETE /{id}`. El gate
    (admin) lo pone el producto al montarla."""
    abrir, _ = _deps(None, conexion)
    router = APIRouter(prefix=prefix, tags=["promociones"])

    @router.get("")
    def listar(solo_activas: bool = False):
        with abrir() as conn:
            return promos.listar_promociones(conn, solo_activas=solo_activas)

    @router.get("/{promocion_id}")
    def obtener(promocion_id: int):
        with abrir() as conn:
            promo = promos.get_promocion(conn, promocion_id)
        if promo is None:
            raise HTTPException(404, "promoción no encontrada")
        return promo

    @router.post("")
    def crear(payload: PromocionPayload):
        with abrir() as conn:
            try:
                promocion_id = promos.crear_promocion(conn, **_campos(payload))
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
            conn.commit()
            return promos.get_promocion(conn, promocion_id)

    @router.put("/{promocion_id}")
    def actualizar(promocion_id: int, payload: PromocionPayload):
        with abrir() as conn:
            try:
                existe = promos.actualizar_promocion(conn, promocion_id, **_campos(payload))
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
            if not existe:
                raise HTTPException(404, "promoción no encontrada")
            conn.commit()
            return promos.get_promocion(conn, promocion_id)

    @router.delete("/{promocion_id}")
    def borrar(promocion_id: int):
        with abrir() as conn:
            if not promos.borrar_promocion(conn, promocion_id):
                raise HTTPException(404, "promoción no encontrada")
            conn.commit()
        return {"ok": True}

    return router


def build_promociones_calculo_router(
    *,
    conexion: Conexion | None = None,
    prefix: str = "/api/promociones",
):
    """`POST /calcular`: las promociones que aplican a un carrito y cuánto ahorran.
    Sólo lee. El gate lo pone el producto (cualquier usuario con sesión)."""
    abrir, _ = _deps(None, conexion)
    router = APIRouter(prefix=prefix, tags=["promociones"])

    @router.post("/calcular")
    def calcular(payload: CalculoPayload):
        try:
            with abrir() as conn:
                return promos.calcular(
                    conn, [i.model_dump() for i in payload.items], en=payload.en,
                )
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    return router
