"""Reposición sugerida como factory de router, sólo lectura (2026-09-30, ADR-017).

`GET /api/reportes/reposicion` y su export CSV. Sobre `libracommerce.erp.reposicion`: qué pedir, por producto,
según lo que se vende, lo que hay y lo que ya viene. **Sólo sugiere**: no genera ninguna orden de compra.

```python
app.include_router(
    build_reposicion_router(conexion=get_connection),
    dependencies=admin_o_encargado,
)
```

**El gate lo pone el producto** al montarlo, como en el margen. El export vive bajo el mismo prefijo y no en
`/reportes/export/*` (donde `libracore.reportes_router` deja los suyos), por la misma razón que el del margen: un
producto que ya proxea `/api` no necesita una ruta más.

Cuelga de `/api/reportes/reposicion`, que no choca con `libracore.reportes_router` (`/api/reportes` y
`/api/reportes/caja-medios`) ni con `/api/reportes/margen`.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Sequence
from typing import Any

from ..erp import compras, reposicion, reposicion_ordenes
from . import fastapi as _fastapi
from .catalogo_router import Conexion, _deps
from .compras_router import _sin_traduccion
from .margen_router import _csv

_fastapi()
from fastapi import APIRouter, Depends, HTTPException, Query  # noqa: E402
from pydantic import BaseModel, ConfigDict  # noqa: E402

_CAMPOS = [
    "producto_id", "codigo", "nombre", "categoria", "unidad", "stock", "vencido", "en_camino", "en_camino_sin_sucursal",
    "stock_minimo", "unidades_vendidas", "dias_con_stock", "rotacion_diaria", "cobertura_dias", "sugerido",
    "motivo", "sin_ventas", "posible_quiebre", "variantes", "plazo_entrega_dias", "plazo_propio", "stock_maximo",
    "limitado_por_maximo", "proveedor_id", "proveedor",
]


class ParametrosDeReposicion(BaseModel):
    """El cuerpo del `PUT`: los dos valores completos, `null` para borrar. Sin campos de más."""
    model_config = ConfigDict(extra="forbid")
    plazo_entrega_dias: int | None
    stock_maximo: float | None
    #: Opcional: si la clave no viene, el proveedor queda como estaba; `null` lo borra (ADR-021).
    proveedor_id: int | None = None


def build_reposicion_router(
    *,
    conexion: Conexion | None = None,
    prefix: str = "/api/reportes/reposicion",
    resolver_proveedor: Callable[[Any, int], int] = _sin_traduccion,
    proveedor_de: Callable[[Any, int], int] = _sin_traduccion,
):
    """`GET ""` (los parámetros y la lista de productos a pedir) y `GET /export` (CSV). Sólo lee. Parámetros:
    `dias_rotacion` (30), `dias_cobertura` (15) y `plazo_entrega_dias` (3): enteros de 1 hasta su tope
    (`erp.reposicion.MAX_*`); `sucursal_id` (sin él, toda la instancia), `categoria`, `producto_id` y `solo_a_pedir`
    (`true` por default: sólo los de `sugerido > 0`) y `descontar_vencido` (`true` por default: lo que está en lotes
    vencidos no cuenta como stock) y `proveedor_id` (sólo los productos de ese proveedor habitual, ADR-021). `resolver_proveedor(conn, proveedor_id) -> party_id` y
    `proveedor_de(conn, party_id) -> proveedor_id` son los mismos ganchos que `OpcionesCompras`, para un producto cuyos proveedores no son el `party_id`
    del motor (VentaLibra): el `proveedor_id` del filtro y el de cada fila hablan en los ids del producto. Sin ellos, identidad. Un parámetro inválido, o una sucursal que no existe, es 422."""
    abrir, _ = _deps(None, conexion)
    router = APIRouter(prefix=prefix, tags=["reportes"])

    def _reporte(dias_rotacion: int, dias_cobertura: int, plazo_entrega_dias: int, sucursal_id: int | None,
                 categoria: str | None, producto_id: int | None, solo_a_pedir: bool,
                 descontar_vencido: bool, proveedor_id: int | None) -> list[dict]:
        try:
            with abrir() as conn:
                party_id = resolver_proveedor(conn, proveedor_id) if proveedor_id is not None else None
                filas = reposicion.sugerencia_reposicion(
                    conn, dias_rotacion=dias_rotacion, dias_cobertura=dias_cobertura,
                    plazo_entrega_dias=plazo_entrega_dias, sucursal_id=sucursal_id, categoria=categoria or None,
                    producto_id=producto_id, solo_a_pedir=solo_a_pedir,
                    descontar_vencido=descontar_vencido, proveedor_id=party_id,
                )
                # El `proveedor_id` de cada fila, en los ids del producto.
                return [dict(f, proveedor_id=proveedor_de(conn, f["proveedor_id"]) if f["proveedor_id"] is not None else None)
                        for f in filas]
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    @router.get("")
    def obtener(dias_rotacion: int = Query(reposicion.DIAS_ROTACION, ge=1, le=reposicion.MAX_DIAS_ROTACION),
                dias_cobertura: int = Query(reposicion.DIAS_COBERTURA, ge=1, le=reposicion.MAX_DIAS_COBERTURA),
                plazo_entrega_dias: int = Query(reposicion.PLAZO_ENTREGA_DIAS, ge=1,
                                                le=reposicion.MAX_PLAZO_ENTREGA_DIAS),
                sucursal_id: int | None = None, categoria: str | None = None, producto_id: int | None = None,
                solo_a_pedir: bool = True, descontar_vencido: bool = True,
                proveedor_id: int | None = None):
        productos = _reporte(dias_rotacion, dias_cobertura, plazo_entrega_dias, sucursal_id, categoria, producto_id,
                             solo_a_pedir, descontar_vencido, proveedor_id)
        return {
            "dias_rotacion": dias_rotacion, "dias_cobertura": dias_cobertura,
            "plazo_entrega_dias": plazo_entrega_dias, "sucursal_id": sucursal_id, "categoria": categoria,
            "producto_id": producto_id, "solo_a_pedir": solo_a_pedir, "descontar_vencido": descontar_vencido,
            "proveedor_id": proveedor_id,
            "resumen": {
                "productos": len(productos),
                "a_pedir": sum(1 for p in productos if p["sugerido"] > 0),
                "posible_quiebre": sum(1 for p in productos if p["posible_quiebre"]),
                "sin_ventas": sum(1 for p in productos if p["sin_ventas"]),
            },
            "productos": productos,
        }

    @router.get("/export")
    def exportar(dias_rotacion: int = Query(reposicion.DIAS_ROTACION, ge=1, le=reposicion.MAX_DIAS_ROTACION),
                 dias_cobertura: int = Query(reposicion.DIAS_COBERTURA, ge=1, le=reposicion.MAX_DIAS_COBERTURA),
                 plazo_entrega_dias: int = Query(reposicion.PLAZO_ENTREGA_DIAS, ge=1,
                                                 le=reposicion.MAX_PLAZO_ENTREGA_DIAS),
                 sucursal_id: int | None = None, categoria: str | None = None, producto_id: int | None = None,
                 solo_a_pedir: bool = True, descontar_vencido: bool = True,
                proveedor_id: int | None = None):
        productos = _reporte(dias_rotacion, dias_cobertura, plazo_entrega_dias, sucursal_id, categoria, producto_id,
                             solo_a_pedir, descontar_vencido, proveedor_id)
        return _csv(productos, _CAMPOS, f"reposicion_{datetime.date.today().isoformat()}.csv")

    return router


def build_reposicion_parametros_router(
    *,
    conexion: Conexion | None = None,
    prefix: str = "/api/productos",
    dependencias_leer: Sequence[Any] | None = None,
    dependencias_escribir: Sequence[Any] | None = None,
    resolver_proveedor: Callable[[Any, int], int] = _sin_traduccion,
    proveedor_de: Callable[[Any, int], int] = _sin_traduccion,
):
    """`GET /{producto_id}/reposicion` y `PUT /{producto_id}/reposicion` (ADR-020): el plazo de entrega y el techo de
    stock propios de un producto, que la reposición sugerida usa en lugar del plazo general y para no pasarse de
    ese techo. El `PUT` lleva siempre los dos valores (`{plazo_entrega_dias, stock_maximo}`, cualquiera puede ser
    `null` para volver al general / sin techo). 404 si el producto no existe, 422 si un valor no es válido, 503 sin
    la revisión `0003`. **Escribe el catálogo, así que la factory FALLA al construirse (`ValueError`) sin
    `dependencias_escribir`** (una lista no vacía de `Depends(...)`); `dependencias_leer` es opcional. `resolver_proveedor` y `proveedor_de`: los ganchos de `OpcionesCompras` (ids del producto <-> `party_id`);
    sin ellos, identidad (ADR-021)."""
    if not isinstance(dependencias_escribir, (list, tuple)) or not dependencias_escribir:
        raise ValueError("build_reposicion_parametros_router necesita dependencias_escribir, una lista no vacía de "
                         "Depends(...): no se expone una escritura del catálogo sin autorización")
    abrir, _ = _deps(None, conexion)
    router = APIRouter(prefix=prefix, tags=["reposicion"])

    def _atajar(operacion):
        try:
            return operacion()
        except reposicion.ProductoNoEncontrado as e:
            raise HTTPException(404, str(e)) from e
        except reposicion.SinRevision as e:
            raise HTTPException(503, str(e)) from e
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    def _en_ids_del_producto(conn, parametros: dict) -> dict:
        """El `proveedor_id` de la respuesta, en los ids del producto (identidad sin `proveedor_de`)."""
        if parametros.get("proveedor_id") is not None:
            parametros = dict(parametros, proveedor_id=proveedor_de(conn, parametros["proveedor_id"]))
        return parametros

    @router.get("/{producto_id}/reposicion", dependencies=list(dependencias_leer or []))
    def leer(producto_id: int):
        def _op():
            with abrir() as conn:
                return _en_ids_del_producto(conn, reposicion.parametros_de(conn, producto_id))
        return _atajar(_op)

    @router.put("/{producto_id}/reposicion", dependencies=list(dependencias_escribir))
    def fijar(producto_id: int, cuerpo: ParametrosDeReposicion):
        def _op():
            with abrir() as conn:
                # `proveedor_id` sólo se toca si la clave vino en el cuerpo (con `null` se borra).
                extra = {}
                if "proveedor_id" in cuerpo.model_fields_set:
                    extra["proveedor_id"] = resolver_proveedor(conn, cuerpo.proveedor_id) if cuerpo.proveedor_id is not None else None
                resultado = reposicion.fijar_parametros(conn, producto_id, plazo_entrega_dias=cuerpo.plazo_entrega_dias,
                                                        stock_maximo=cuerpo.stock_maximo, **extra)
                # La respuesta se traduce ANTES de confirmar: si el gancho falla (un party sin proveedor), el PUT no queda escrito a medias.
                respuesta = _en_ids_del_producto(conn, resultado)
                conn.commit()
                return respuesta
        return _atajar(_op)

    return router


class GenerarOrdenes(BaseModel):
    """El cuerpo del `POST` que genera las órdenes en borrador: los parámetros de la reposición (los que no vienen, los defaults), la clave
    de la operación y, opcionalmente, los productos a pedir. Sin campos de más."""
    model_config = ConfigDict(extra="forbid")
    clave_operacion: str
    dias_rotacion: int = reposicion.DIAS_ROTACION
    dias_cobertura: int = reposicion.DIAS_COBERTURA
    plazo_entrega_dias: int = reposicion.PLAZO_ENTREGA_DIAS
    sucursal_id: int | None = None
    categoria: str | None = None
    proveedor_id: int | None = None
    producto_ids: list[int] | None = None
    #: `{producto_id: cantidad}`: lo que la persona vio y confirmó; la orden no se pasa de eso (ver `generar_ordenes_borrador`).
    topes: dict[int, float] | None = None
    descontar_vencido: bool = True


def build_reposicion_ordenes_router(
    *,
    conexion: Conexion | None = None,
    usuario_actual: Callable[..., Any] | None = None,
    prefix: str = "/api/reportes/reposicion/ordenes",
    dependencias_escribir: Sequence[Any] | None = None,
    numerador: compras.Numerador = compras.numero_por_defecto,
    resolver_proveedor: Callable[[Any, int], int] = _sin_traduccion,
    proveedor_de: Callable[[Any, int], int] = _sin_traduccion,
):
    """`POST ""` (ADR-022): genera **una orden de compra en borrador por proveedor habitual** con lo que la reposición sugiere pedir, con los
    parámetros del cuerpo (los de `GET /api/reportes/reposicion`). Nunca envía ni confirma: son borradores para revisar. Devuelve
    `{ordenes, sin_proveedor, omitidos, repetida}`: las órdenes creadas (`proveedor_id` en los ids del producto), los productos a pedir sin proveedor
    habitual, los `producto_ids` sin nada que pedir, y si es el reintento de una `clave_operacion` ya usada (devuelve las mismas órdenes sin crear
    otras). Todo o nada: si algo falla no queda ninguna. 422 con un parámetro inválido, 503 sin la revisión `0004`. `numerador`, `resolver_proveedor` y
    `proveedor_de` son los ganchos de `OpcionesCompras`.

    **Escribe órdenes de compra, así que la factory FALLA al construirse (`ValueError`) sin `dependencias_escribir`** (una lista no vacía de
    `Depends(...)`: el producto pone acá la misma capacidad que protege la escritura de Compras) ni sin `usuario_actual` (la orden queda a nombre de quien
    la generó)."""
    if usuario_actual is None:
        raise ValueError("build_reposicion_ordenes_router necesita usuario_actual: las órdenes quedan a nombre de quien las generó")
    if not isinstance(dependencias_escribir, (list, tuple)) or not dependencias_escribir:
        raise ValueError("build_reposicion_ordenes_router necesita dependencias_escribir, una lista no vacía de Depends(...): "
                         "no se exponen escrituras de órdenes de compra sin autorización")
    abrir, _ = _deps(None, conexion)
    router = APIRouter(prefix=prefix, tags=["reposicion"])

    @router.post("", dependencies=list(dependencias_escribir))
    def generar(cuerpo: GenerarOrdenes, user: dict = Depends(usuario_actual)):
        try:
            with abrir() as conn:
                party = resolver_proveedor(conn, cuerpo.proveedor_id) if cuerpo.proveedor_id is not None else None
                resultado = reposicion_ordenes.generar_ordenes_borrador(
                    conn, clave_operacion=cuerpo.clave_operacion, producto_ids=cuerpo.producto_ids, topes=cuerpo.topes, usuario_id=user.get("id"),
                    numerador=numerador, dias_rotacion=cuerpo.dias_rotacion, dias_cobertura=cuerpo.dias_cobertura,
                    plazo_entrega_dias=cuerpo.plazo_entrega_dias, sucursal_id=cuerpo.sucursal_id, categoria=cuerpo.categoria or None,
                    proveedor_id=party, descontar_vencido=cuerpo.descontar_vencido,
                )
                # Los proveedores de las órdenes, en los ids del producto, ANTES de confirmar (si el gancho falla no queda nada escrito).
                resultado["ordenes"] = [
                    {**{k: v for k, v in o.items() if k != "supplier_party_id"}, "proveedor_id": proveedor_de(conn, o["supplier_party_id"])}
                    for o in resultado["ordenes"]
                ]
                conn.commit()
                return resultado
        except reposicion.SinRevision as e:
            raise HTTPException(503, str(e)) from e
        except reposicion_ordenes.ClaveReusada as e:
            raise HTTPException(409, str(e)) from e
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    return router
