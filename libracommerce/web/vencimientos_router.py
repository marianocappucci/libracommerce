"""Vencimientos y lotes como factories de router (A-1, ADR-018, 2026-09-30).

Sobre `libracommerce.erp.vencimientos`. **Dos routers, para que el producto ponga capacidades distintas a leer y a
escribir**: quien mira el reporte de próximos a vencer no es necesariamente quien marca un producto como perecedero o
da de baja un lote.

- `build_vencimientos_router`, **sólo lectura**: `GET /api/vencimientos` (próximos a vencer y vencidos),
  `GET /api/vencimientos/export` (CSV) y `GET /api/vencimientos/productos/{id}/lotes` (las existencias por lote).
- `build_vencimientos_escritura_router`: `PUT /api/vencimientos/productos/{id}` (marcar «vence»),
  `POST /api/vencimientos/asignar` (ponerle lote y vencimiento a saldo sin lote) y `POST /api/vencimientos/merma`
  (dar de baja un lote). Además de lo que el producto ponga al montarlo (`include_router(..., dependencies=...)`),
  acepta gates **por operación**: `dependencias_marcar` (marcar un producto) y `dependencias_movimientos` (asignar y
  dar de baja, las dos que mueven el ledger). Así, por ejemplo, un producto puede dejar que el encargado marque y que
  el encargado **y el depósito** asignen y den de baja.

```python
app.include_router(build_vencimientos_router(conexion=get_connection), dependencies=encargado_o_deposito)
app.include_router(
    build_vencimientos_escritura_router(
        conexion=get_connection, usuario_actual=usuario_actual,
        dependencias_marcar=encargado, dependencias_movimientos=encargado_o_deposito,
    ),
    dependencies=sesion_iniciada,
)
```

**El gate lo pone el producto**, como en el margen y la reposición. Cuelgan de `/api/vencimientos`, que no choca con
ninguna otra factory del motor. Errores: parámetros o cuerpo inválidos y datos que no existen (sucursal, depósito), 422;
producto que no existe, 404; una regla de negocio (saldo insuficiente, producto sin marcar, servicio), 409; base sin la
revisión `0002` del motor, 503 con el comando que falta.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

from ..erp import vencimientos
from . import fastapi as _fastapi
from .catalogo_router import Conexion, _deps
from .margen_router import _csv

_fastapi()
from fastapi import APIRouter, Depends, HTTPException, Query  # noqa: E402
from pydantic import BaseModel, Field, StrictBool  # noqa: E402

_CAMPOS = [
    "producto_id", "codigo", "nombre", "categoria", "unidad", "sucursal", "deposito", "variante", "lote", "vence",
    "dias_para_vencer", "saldo", "estado",
]


class MarcaPayload(BaseModel):
    #: Estricto: `"si"` o `1` no son un booleano; una marca por error no debería pasar.
    vence: StrictBool


class AsignarPayload(BaseModel):
    producto_id: int
    deposito_id: int
    lote: str
    vence: str
    cantidad: Decimal = Field(gt=0)
    variante_id: int | None = None
    nota: str = ""


class MermaPayload(BaseModel):
    producto_id: int
    deposito_id: int
    #: Al menos uno de los dos: los del bucket tal como los devuelve `GET /productos/{id}/lotes`.
    lote: str | None = None
    vence: str | None = None
    cantidad: Decimal = Field(gt=0)
    variante_id: int | None = None
    motivo: str = "Vencimiento"
    nota: str = ""


def _http(error: Exception) -> HTTPException:
    """El código HTTP de cada error del módulo (ver el docstring de arriba)."""
    if isinstance(error, vencimientos.SinRevision):
        return HTTPException(503, str(error))
    if isinstance(error, vencimientos.ProductoNoEncontrado):
        return HTTPException(404, str(error))
    if isinstance(error, vencimientos.ReglaDeNegocio):
        return HTTPException(409, str(error))
    return HTTPException(422, str(error))


_ERRORES = (ValueError, vencimientos.VencimientosError)


def build_vencimientos_router(
    *,
    conexion: Conexion | None = None,
    usuario_actual: Callable[..., Any] | None = None,
    prefix: str = "/api/vencimientos",
):
    """`GET ""`, `GET /export` (CSV) y `GET /productos/{producto_id}/lotes`. Sólo lee. `GET ""`: `dias` (15, de 1 a
    365), `sucursal_id`, `deposito_id`, `categoria`, `producto_id` e `incluir_vencidos` (`true` por default); devuelve
    `{hoy, dias, hasta, resumen, lotes, sin_lote}` (ver `erp.vencimientos.proximos_a_vencer`). Un parámetro inválido o
    una sucursal o un depósito que no existen es 422. `usuario_actual` no se usa acá (es del router de escritura);
    se acepta para que los dos se construyan igual."""
    abrir, _ = _deps(usuario_actual, conexion)
    router = APIRouter(prefix=prefix, tags=["vencimientos"])

    def _reporte(dias, sucursal_id, deposito_id, categoria, producto_id, incluir_vencidos) -> dict:
        try:
            with abrir() as conn:
                return vencimientos.proximos_a_vencer(
                    conn, dias=dias, sucursal_id=sucursal_id, deposito_id=deposito_id, categoria=categoria or None,
                    producto_id=producto_id, incluir_vencidos=incluir_vencidos,
                )
        except _ERRORES as e:
            raise _http(e) from e

    @router.get("")
    def obtener(dias: int = Query(vencimientos.DIAS_AVISO, ge=1, le=vencimientos.MAX_DIAS_AVISO),
                sucursal_id: int | None = None, deposito_id: int | None = None, categoria: str | None = None,
                producto_id: int | None = None, incluir_vencidos: bool = True):
        reporte = _reporte(dias, sucursal_id, deposito_id, categoria, producto_id, incluir_vencidos)
        return {"sucursal_id": sucursal_id, "deposito_id": deposito_id, "categoria": categoria,
                "producto_id": producto_id, "incluir_vencidos": incluir_vencidos, **reporte}

    @router.get("/export")
    def exportar(dias: int = Query(vencimientos.DIAS_AVISO, ge=1, le=vencimientos.MAX_DIAS_AVISO),
                 sucursal_id: int | None = None, deposito_id: int | None = None, categoria: str | None = None,
                 producto_id: int | None = None, incluir_vencidos: bool = True):
        reporte = _reporte(dias, sucursal_id, deposito_id, categoria, producto_id, incluir_vencidos)
        return _csv(reporte["lotes"], _CAMPOS, f"vencimientos_{reporte['hoy']}.csv")

    @router.get("/productos/{producto_id}/lotes")
    def lotes(producto_id: int, sucursal_id: int | None = None, deposito_id: int | None = None):
        """`{producto, lotes}`: la ficha (`vence` dice si está marcado) y las existencias por lote con saldo ≠ 0,
        incluido el bucket sin lote."""
        try:
            with abrir() as conn:
                ficha = vencimientos.ficha(conn, producto_id)
                filas = vencimientos.lotes_de(conn, producto_id, sucursal_id=sucursal_id, deposito_id=deposito_id)
        except _ERRORES as e:
            raise _http(e) from e
        return {"producto": ficha, "hoy": vencimientos.hoy_argentina().isoformat(), "lotes": filas}

    return router


def build_vencimientos_escritura_router(
    *,
    conexion: Conexion | None = None,
    usuario_actual: Callable[..., Any] | None = None,
    prefix: str = "/api/vencimientos",
    dependencias_marcar: Sequence[Any] = (),
    dependencias_movimientos: Sequence[Any] = (),
):
    """`PUT /productos/{producto_id}` (`{vence: bool}`), `POST /asignar` y `POST /merma`. Cada operación es una
    transacción: un error de negocio no deja nada escrito. Las dos que mueven el ledger escriben filas nuevas, nunca
    un `UPDATE` de una ya escrita.

    `dependencias_marcar` y `dependencias_movimientos` (listas de `Depends(...)`, vacías por default) van **por
    operación**, encima de las que el producto ponga al montar el router: las primeras a la marca de un producto, las
    segundas a `asignar` y `merma`. `usuario_actual` (una `Depends`-able que devuelve un `dict` con `id`) sale como
    `created_by` de los movimientos."""
    abrir, usuario = _deps(usuario_actual, conexion)
    router = APIRouter(prefix=prefix, tags=["vencimientos"])

    @router.put("/productos/{producto_id}", dependencies=list(dependencias_marcar))
    def marcar(producto_id: int, payload: MarcaPayload):
        try:
            with abrir() as conn:
                return vencimientos.marcar_vence(conn, producto_id, payload.vence)
        except _ERRORES as e:
            raise _http(e) from e

    @router.post("/asignar", dependencies=list(dependencias_movimientos))
    def asignar(payload: AsignarPayload, user: dict = Depends(usuario)):
        try:
            with abrir() as conn:
                return vencimientos.asignar_vencimiento_a_saldo(
                    conn, payload.producto_id, payload.deposito_id, payload.lote, payload.vence, payload.cantidad,
                    variante_id=payload.variante_id, usuario_id=user.get("id"), nota=payload.nota,
                )
        except _ERRORES as e:
            raise _http(e) from e

    @router.post("/merma", dependencies=list(dependencias_movimientos))
    def merma(payload: MermaPayload, user: dict = Depends(usuario)):
        try:
            with abrir() as conn:
                return vencimientos.dar_de_baja_lote(
                    conn, payload.producto_id, payload.deposito_id, payload.lote, payload.vence, payload.cantidad,
                    variante_id=payload.variante_id, motivo=payload.motivo, usuario_id=user.get("id"),
                    nota=payload.nota,
                )
        except _ERRORES as e:
            raise _http(e) from e

    return router

