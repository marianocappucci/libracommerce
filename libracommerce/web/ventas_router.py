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

import math
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..erp import catalogo, lotes, ventas
from ..erp.hooks import SIN_GANCHOS, Hooks
from . import fastapi as _fastapi
from .catalogo_router import Conexion, _deps

_fastapi()
from fastapi import APIRouter, Depends, HTTPException  # noqa: E402
from libracore import medios_pago  # noqa: E402
from libracore import pagos as acreditacion  # noqa: E402
from libracore.db.caja import MEDIO_CUENTA_CORRIENTE  # noqa: E402
from pydantic import BaseModel, field_validator, model_validator  # noqa: E402

#: El medio con el que cobra el QR de caja. Pasa por `medios_pago.validar` y no
#: es un literal suelto: la grafía se normalizó a `mercadopago` el 2026-08-25.
MEDIO_DEL_QR = medios_pago.validar("mercadopago")


class ItemPayload(BaseModel):
    nombre: str
    qty: float
    precio: float
    producto_id: int | None = None
    #: La variante del catálogo (`item_variants.id`), si el ítem las tiene.
    #: `None` es "sin variante", que es lo único que mandan Contalibra y
    #: Restolibra hoy.
    variante_id: int | None = None


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
    #: Cuánto entregó el cliente, para el vuelto (D4). `None` (el default) no
    #: escribe la columna `ventas_pagos.recibido`: es lo que mantiene el
    #: `INSERT` idéntico al de hoy para Contalibra y Restolibra, que no la
    #: mandan.
    recibido: float | None = None

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

    @model_validator(mode="after")
    def _recibido_no_menor_que_monto(self):
        """🔴 Para todos: un `recibido` menor que `monto` es un vuelto
        negativo, un dato imposible. Contalibra y Restolibra nunca mandan
        `recibido` (queda `None`), así que esta validación no les cambia nada."""
        if self.recibido is not None and self.recibido < self.monto:
            raise ValueError(
                f"`recibido` ({self.recibido}) no puede ser menor que `monto` "
                f"({self.monto}): el vuelto no puede ser negativo."
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
    #: El depósito del que sale el stock de ESTA venta (F4, VentaLibra
    #: multisucursal: el POS elige la sucursal). `None` (el default, y lo
    #: único que mandan Contalibra y Restolibra hoy) es el comportamiento de
    #: siempre: el motor resuelve el depósito por defecto.
    deposito_id: int | None = None


class DevolucionLinea(BaseModel):
    #: El id de `sale_items`, no la posición: acá la línea tiene id estable.
    sale_item_id: int
    cantidad: float


class DevolucionPayload(BaseModel):
    lineas: list[DevolucionLinea]
    deposito_id: int
    #: Por dónde vuelve la plata; no tiene por qué ser el medio que cobró.
    medio_pago: str = "efectivo"

    @field_validator("medio_pago")
    @classmethod
    def _medio_elegible(cls, medio: str) -> str:
        return medios_pago.validar(medio)


class PlanSalidaLinea(BaseModel):
    """Una línea del plan de salida: lo mismo que la venta (`ItemPayload`) sin el precio, que no interviene."""

    producto_id: int
    qty: float
    #: La variante del catálogo, o `None` (el producto sin variantes).
    variante_id: int | None = None


class PlanSalidaPayload(BaseModel):
    items: list[PlanSalidaLinea]
    #: El depósito del que saldría la venta: el mismo `deposito_id` de `VentaPayload`. `None` = el por defecto.
    deposito_id: int | None = None


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
    #: Los ganchos del producto (receta, pedido cobrado, numerador, turno...).
    hooks: Hooks = field(default=SIN_GANCHOS)
    #: Sin turno abierto (según `hooks.turno_para`), la venta no se registra:
    #: `ventas.SinTurno` → 409. Default `False` (el comportamiento de hoy).
    exigir_turno: bool = False
    #: Los movimientos de caja de una venta llevan el `turno_id`. Default
    #: `False` (el comportamiento de hoy); VentaLibra lo prende porque arquea
    #: sumando `caja_movimientos` por turno, no por `venta_links`.
    caja_con_turno: bool = False
    #: Una venta cuyos pagos DECLARADOS (acreditados + pendientes de QR) no
    #: alcanzan el total se rechaza con 422 antes de escribir nada. Default
    #: `False` (el comportamiento de hoy: queda `parcial`/`pendiente`).
    exigir_pago_completo: bool = False
    #: Un pago en `cuenta_corriente` sin `cliente_id` se rechaza con 422 antes
    #: de escribir nada — una deuda que no es de nadie no se puede cobrar.
    #: Default `False` (el comportamiento de hoy).
    exigir_cliente_para_fiar: bool = False
    #: Aplicar las promociones vigentes (`erp.promociones`, "llevá N pagá M" y
    #: combos) a la venta. **El servidor las calcula con las líneas que llegan**,
    #: no confía en un descuento del cliente: el ahorro se SUMA al `descuento`
    #: del pedido (con tope en el subtotal) y queda registrado en
    #: `sale_promotions`. Requiere `erp.schema.crear_promociones`. Default
    #: `False` (el comportamiento de hoy: Contalibra y Restolibra no cambian).
    promociones: bool = False
    #: Guardar el costo vigente de cada línea de producto en
    #: `sale_items.unit_cost_snapshot` (ADR-016), para que el reporte de margen
    #: (`erp.margen`) use el costo de aquella venta y no el de hoy. Default
    #: `False` (el comportamiento de hoy: queda NULL; Contalibra y Restolibra no
    #: cambian). Sin migración: la columna ya existe.
    guardar_costo: bool = False
    #: Avisos de vencimiento (ADR-018, A-4 PR-3, opt-in). **Prendida:** `POST /api/ventas` y `GET /api/ventas/{id}`
    #: agregan la clave `avisos` a la respuesta **sólo si hay alguno** (`erp.lotes.avisos_de_venta`: lote vencido, por
    #: vencer —15 días— y faltante sin lote; `hoy` es la fecha de Argentina, no la de la venta), y existe
    #: `POST /api/ventas/plan-salida`: una LECTURA pura (no escribe ni bloquea) que dice de qué lote saldría cada línea
    #: (`erp.lotes.planificar_salida`) para que el POS confirme ANTES de cobrar. **Apagada (el default):** las respuestas
    #: son byte a byte las de hoy, la ruta no existe (404) y no figura en `/openapi.json` (Contalibra y Restolibra no
    #: cambian). Sin gate de plan ni permisos propios: el producto monta el router con sus dependencias.
    con_avisos_de_vencimiento: bool = False


def _agregar_avisos(conn, venta: dict | None, venta_id: int) -> None:
    """Agrega `avisos` a la venta **sólo si hay alguno** (sin avisos la respuesta es la de siempre)."""
    if venta is None:
        return
    avisos = lotes.avisos_de_venta(conn, venta_id)
    if avisos:
        venta["avisos"] = avisos


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
                "variante_id": i.variante_id,
            }
            for i in payload.items if i.nombre.strip() and i.qty > 0
        ]
        if not items:
            raise HTTPException(422, "Debe agregar al menos un ítem.")

        subtotal = round(sum(i["subtotal"] for i in items), 2)
        aplicadas: list[dict] | None = None
        ahorro = 0.0
        if opciones.promociones:
            from ..erp import promociones as promos

            with abrir() as conn:
                calculo = promos.calcular(conn, items)
            aplicadas = calculo["aplicadas"] or None
            ahorro = calculo["ahorro"]
        descuento = min(max(0.0, payload.descuento) + ahorro, subtotal)
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
                "recibido": p.recibido,
            }
            for p in payload.pagos if p.monto > 0
        ]
        if not pagos:
            raise HTTPException(422, "Debe registrar al menos un medio de pago.")

        if opciones.exigir_pago_completo:
            # "Declarado" y no "acreditado": un pago `cobrar_con_qr` todavía
            # no acreditó nada, pero el mostrador ya dijo cuánto va a entrar
            # por ahí — es lo que hay que exigir que cubra el total, antes de
            # escribir nada.
            declarado = round(sum(p["monto"] for p in pagos), 2)
            if declarado < total:
                raise HTTPException(
                    422,
                    f"Los pagos declarados (${declarado}) no cubren el total de "
                    f"la venta (${total})."
                )

        if opciones.exigir_cliente_para_fiar and not payload.cliente_id and any(
            p["medio"] == MEDIO_CUENTA_CORRIENTE for p in pagos
        ):
            raise HTTPException(
                422,
                "No se puede fiar sin cliente: una venta con un pago en "
                "cuenta corriente necesita `cliente_id`."
            )

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
                exigir_turno=opciones.exigir_turno, caja_con_turno=opciones.caja_con_turno,
                deposito_id=payload.deposito_id, promociones=aplicadas,
                guardar_costo=opciones.guardar_costo,
            )
        except ventas.SinTurno as exc:
            raise HTTPException(409, str(exc)) from None
        except ventas.ProductoInexistente as exc:
            # 🔴 No es un conflicto con otra venta: es un dato del pedido que
            # nunca iba a dejar de fallar. Va ANTES del catch-all de abajo,
            # que si no la atraparía como si lo fuera.
            raise HTTPException(422, str(exc)) from None
        except ventas.DepositoInexistente as exc:
            # Mismo criterio que ProductoInexistente: un depósito inventado
            # nunca se cura reintentando, así que va antes del catch-all.
            raise HTTPException(422, str(exc)) from None
        except ventas.DepositoNoPermitido as exc:
            # El depósito existe, pero `hooks.validar_deposito` lo rechazó
            # (VentaLibra multisucursal): mismo 422 que un depósito inventado.
            raise HTTPException(422, str(exc)) from None
        except (sqlite3.IntegrityError, RuntimeError):
            raise HTTPException(
                409, "No se pudo registrar la venta (conflicto con otra venta simultánea). Reintentá."
            ) from None

        with abrir() as conn:
            venta = ventas.obtener_venta(conn, venta_id)
            if opciones.promociones:
                venta["promociones"] = promos.promociones_de_venta(conn, venta_id)
            if opciones.con_avisos_de_vencimiento:
                _agregar_avisos(conn, venta, venta_id)
            return venta

    @router.get("/{vid}")
    def detalle(vid: int):
        with abrir() as conn:
            venta = ventas.obtener_venta(conn, vid)
            if venta and opciones.promociones:
                from ..erp import promociones as promos

                venta["promociones"] = promos.promociones_de_venta(conn, vid)
            if venta and opciones.con_avisos_de_vencimiento:
                _agregar_avisos(conn, venta, vid)
        if not venta:
            raise HTTPException(404, "Venta no encontrada")
        return venta

    if opciones.con_avisos_de_vencimiento:

        @router.post("/plan-salida")
        def plan_salida(payload: PlanSalidaPayload):
            """De qué lote saldría cada línea si se vendiera ahora, más los avisos de vencimiento, para confirmar
            ANTES de cobrar. Lectura pura: no escribe, no bloquea y no commitea. Es una simulación sobre el estado de
            ahora: otra venta concurrente puede cambiar el resultado antes de cobrar."""
            if not payload.items:
                raise HTTPException(422, "Debe agregar al menos un ítem.")
            if any(not (math.isfinite(i.qty) and i.qty > 0) for i in payload.items):
                raise HTTPException(422, "La cantidad de cada ítem debe ser mayor que cero.")
            items = [{"producto_id": i.producto_id, "qty": i.qty, "variante_id": i.variante_id}
                     for i in payload.items]
            with abrir() as conn:
                try:
                    catalogo.validar_deposito(conn, payload.deposito_id)
                except catalogo.DepositoInexistente as exc:
                    raise HTTPException(422, str(exc)) from None
                ids = sorted({i["producto_id"] for i in items})
                marcadores = ",".join("?" for _ in ids)
                existentes = {f[0] for f in conn.execute(
                    f"SELECT id FROM catalog_items WHERE id IN ({marcadores})", ids).fetchall()}
                if faltantes := [i for i in ids if i not in existentes]:
                    raise HTTPException(
                        422, "No existe el producto " + ", ".join(str(i) for i in faltantes) + ".")
                try:
                    return lotes.planificar_salida(conn, items, payload.deposito_id, hooks=opciones.hooks)
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from None

    @router.post("/{vid}/anular", dependencies=gate_anular)
    def anular(vid: int, user: dict = Depends(usuario)):
        with abrir() as conn:
            if not ventas.obtener_venta(conn, vid):
                raise HTTPException(404, "Venta no encontrada")
            try:
                ventas.anular_venta(conn, vid, usuario_id=user.get("id"), hooks=opciones.hooks,
                                    caja_con_turno=opciones.caja_con_turno)
                conn.commit()
            except ventas.VentaConDevoluciones as exc:
                conn.rollback()
                raise HTTPException(409, str(exc)) from None
            except Exception:
                conn.rollback()
                raise
            return ventas.obtener_venta(conn, vid)

    @router.post("/{vid}/devolver", dependencies=gate_anular)
    def devolver(vid: int, payload: DevolucionPayload, user: dict = Depends(usuario)):
        """Devuelve algunas líneas de una venta y reintegra su importe. Mismo
        gate que `anular`: es plata que sale, no una consulta."""
        with abrir() as conn:
            if not ventas.obtener_venta(conn, vid):
                raise HTTPException(404, "Venta no encontrada")
            try:
                resultado = ventas.devolver_items(
                    conn, vid,
                    devoluciones={linea.sale_item_id: linea.cantidad for linea in payload.lineas},
                    deposito_id=payload.deposito_id, medio_pago=payload.medio_pago,
                    usuario_id=user.get("id"), hooks=opciones.hooks,
                    caja_con_turno=opciones.caja_con_turno,
                )
                conn.commit()
            except ventas.DepositoInexistente as exc:
                # Explícito y no sólo cubierto por el `except ValueError` de
                # abajo (que también lo atraparía, por herencia): así queda
                # dicho con su nombre, igual que en `crear`.
                conn.rollback()
                raise HTTPException(422, str(exc)) from None
            except ventas.DepositoNoPermitido as exc:
                # Mismo criterio: el depósito existe, pero el gancho lo
                # rechazó — explícito, igual que en `crear`.
                conn.rollback()
                raise HTTPException(422, str(exc)) from None
            except ValueError as exc:
                conn.rollback()
                raise HTTPException(422, str(exc)) from None
            except Exception:
                conn.rollback()
                raise
            return resultado["venta"]

    return router
