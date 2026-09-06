"""La API del punto de venta: `GET`/`POST /api/ventas`, el detalle y la
anulación, como factory (P9-M3).

Lo que `web/api/ventas.py` tenía escrito dos veces —idéntico salvo docstrings y
el orden de dos bloques—. Lo que NO está acá, a propósito: el cobro por QR de
MercadoPago y la factura desde la venta (`/mp-qr`, `/mp-status`, `/facturar`)
son de LibraCore —dinero y comprobantes— y viven en
`libracore.ventas_cobro_router`, que se monta con el mismo prefijo.

```python
app.include_router(
    build_ventas_router(
        conexion=get_connection,
        usuario_actual=get_current_user_json,
        solo_admin=require_role_json("admin"),
        opciones=OpcionesVentas(
            stock_habilitado=lambda: bool(get_modulos().get("stock")),
            hooks=GANCHOS,
        ),
    ),
    dependencies=[_auth_json, Depends(require_module("ventas"))],
)
```

Este módulo importa LibraCore al cargarse (los payloads validan el medio de
pago contra su vocabulario): además de `[web]` necesita `[erp]`, que es lo que
instala cualquier producto que monte ventas.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..erp import ventas
from ..erp.hooks import SIN_GANCHOS, Hooks
from . import fastapi as _fastapi
from .catalogo_router import Conexion, _deps

_fastapi()
from fastapi import APIRouter, Depends, HTTPException  # noqa: E402
from libracore import medios_pago  # noqa: E402
from libracore import pagos as acreditacion  # noqa: E402
from pydantic import BaseModel, field_validator, model_validator  # noqa: E402

#: El medio con el que cobra el QR de caja. Pasa por `medios_pago.validar` y no
#: es un literal suelto: la grafía se normalizó a `mercadopago` el 2026-08-25.
MEDIO_DEL_QR = medios_pago.validar("mercadopago")


class ItemPayload(BaseModel):
    nombre: str
    qty: float
    precio: float
    producto_id: int | None = None


class PagoPayload(BaseModel):
    #: 🔴 **Se valida.** Un medio inventado entraba, creaba su movimiento de
    #: caja y salía en el cierre como un bucket suelto con el nombre crudo: la
    #: plata bien contada y el reparto mal. Las grafías históricas siguen
    #: siendo válidas; lo que rebota es lo que nunca debió entrar.
    medio: str
    monto: float
    referencia: str = ""
    #: 🔴 **"Le voy a cobrar recién ahora", no "ya me pagó".** Es la única forma
    #: de distinguir las dos cosas que el mostrador puede querer decir con el
    #: medio `mercadopago`. Con esto en `true` el pago nace `PENDIENTE`: no
    #: cuenta para el estado de la venta y no toca la caja. Lo acredita el poll
    #: del QR o el webhook, cuando MercadoPago dice que la plata entró.
    cobrar_con_qr: bool = False

    @field_validator("medio")
    @classmethod
    def _medio_del_vocabulario(cls, v: str) -> str:
        return medios_pago.validar(v)

    @model_validator(mode="after")
    def _el_qr_es_de_mercadopago(self):
        """Un `cobrar_con_qr` sobre efectivo dejaría la venta pendiente **para
        siempre**: nada acredita un pago en efectivo. Mejor rebotarlo acá."""
        if self.cobrar_con_qr and self.medio != MEDIO_DEL_QR:
            raise ValueError(
                f"`cobrar_con_qr` sólo aplica al medio '{MEDIO_DEL_QR}': el QR "
                f"de MercadoPago no cobra un pago en '{self.medio}'."
            )
        return self


class VentaPayload(BaseModel):
    fecha: str
    items: list[ItemPayload]
    descuento: float = 0
    cliente_id: int | None = None
    cliente_nombre: str = ""
    observaciones: str = ""
    pagos: list[PagoPayload]


def _nombre_de_cliente_default(cliente_id: int) -> str | None:
    from libracore.db.clients import get_client

    c = get_client(cliente_id)
    return c["name"] if c else None


def _stock_siempre() -> bool:
    return True


@dataclass(frozen=True)
class OpcionesVentas:
    #: Si la venta descuenta stock. Los productos lo atan al módulo `stock`.
    stock_habilitado: Callable[[], bool] = _stock_siempre
    #: El nombre con el que se snapshotea un cliente elegido por id. Default:
    #: el registro de clientes de LibraCore.
    nombre_de_cliente: Callable[[int], str | None] = _nombre_de_cliente_default
    #: Los ganchos del producto (receta, pedido cobrado, ...).
    hooks: Hooks = field(default=SIN_GANCHOS)


def build_ventas_router(
    *,
    conexion: Conexion | None = None,
    usuario_actual: Callable[..., Any] | None = None,
    solo_admin: Callable[..., Any] | None = None,
    prefix: str = "/api/ventas",
    opciones: OpcionesVentas | None = None,
):
    """`GET /medios-pago`, `GET`/`POST ""`, `GET /{vid}`, `POST /{vid}/anular`.

    `solo_admin` es la dependencia que gatea la anulación (los dos productos:
    `require_role_json("admin")`); sin ella, anula cualquiera que pase el gate
    del router. El gate general lo pone el producto al montarlo.
    """
    abrir, usuario = _deps(usuario_actual, conexion)
    opciones = opciones or OpcionesVentas()
    router = APIRouter(prefix=prefix, tags=["ventas"])
    gate_anular = [Depends(solo_admin)] if solo_admin else []

    @router.get("/medios-pago")
    def listar_medios_pago():
        return medios_pago.para_selector()

    @router.get("")
    def listar(desde: str = "", hasta: str = "", q: str = "", tab: str = "todas"):
        if tab not in ("todas", "sin_facturar", "facturadas"):
            tab = "todas"
        with abrir() as conn:
            return ventas.listar_ventas(conn, desde=desde, hasta=hasta, q=q, tab=tab)

    @router.post("")
    def crear(payload: VentaPayload, user: dict = Depends(usuario)):
        items = [
            {
                "nombre": i.nombre.strip(), "qty": i.qty, "precio": max(0.0, i.precio),
                "subtotal": round(i.qty * max(0.0, i.precio), 2), "producto_id": i.producto_id,
            }
            for i in payload.items if i.nombre.strip() and i.qty > 0
        ]
        if not items:
            raise HTTPException(422, "Debe agregar al menos un ítem.")

        subtotal = round(sum(i["subtotal"] for i in items), 2)
        descuento = min(max(0.0, payload.descuento), subtotal)
        total = round(subtotal - descuento, 2)

        # 🔑 El mostrador declara si la plata entró o todavía no. Que el estado
        # se declare acá y no lo ponga la base es el punto: la columna tiene
        # default `'aprobado'` para el backfill, así que sin esta línea un pago
        # contaría como entrado sin que nadie lo decida.
        pagos = [
            {
                "medio": p.medio, "monto": p.monto, "referencia": p.referencia,
                "estado": (acreditacion.EstadoAcreditacion.PENDIENTE if p.cobrar_con_qr
                           else acreditacion.EstadoAcreditacion.APROBADO).value,
            }
            for p in payload.pagos if p.monto > 0
        ]
        if not pagos:
            raise HTTPException(422, "Debe registrar al menos un medio de pago.")

        cliente_nombre = payload.cliente_nombre.strip()
        if payload.cliente_id:
            cliente_nombre = opciones.nombre_de_cliente(payload.cliente_id) or cliente_nombre

        try:
            venta_id = ventas.crear_venta_directa(
                abrir, fecha=payload.fecha, items=items, subtotal=subtotal,
                descuento=descuento, total=total, cliente_id=payload.cliente_id,
                cliente_nombre=cliente_nombre, usuario_id=user.get("id"),
                observaciones=payload.observaciones.strip(),
                estado=ventas.estado_segun_pagos(total, pagos), pagos=pagos,
                stock_habilitado=bool(opciones.stock_habilitado()), hooks=opciones.hooks,
            )
        except (sqlite3.IntegrityError, RuntimeError):
            raise HTTPException(
                409, "No se pudo registrar la venta (conflicto con otra venta simultánea). Reintentá."
            ) from None

        with abrir() as conn:
            return ventas.obtener_venta(conn, venta_id)

    @router.get("/{vid}")
    def detalle(vid: int):
        with abrir() as conn:
            venta = ventas.obtener_venta(conn, vid)
        if not venta:
            raise HTTPException(404, "Venta no encontrada")
        return venta

    @router.post("/{vid}/anular", dependencies=gate_anular)
    def anular(vid: int, user: dict = Depends(usuario)):
        with abrir() as conn:
            if not ventas.obtener_venta(conn, vid):
                raise HTTPException(404, "Venta no encontrada")
            try:
                ventas.anular_venta(conn, vid, usuario_id=user.get("id"), hooks=opciones.hooks)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return ventas.obtener_venta(conn, vid)

    return router
