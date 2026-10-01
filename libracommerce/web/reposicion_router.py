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

from ..erp import reposicion
from . import fastapi as _fastapi
from .catalogo_router import Conexion, _deps
from .margen_router import _csv

_fastapi()
from fastapi import APIRouter, HTTPException, Query  # noqa: E402

_CAMPOS = [
    "producto_id", "codigo", "nombre", "categoria", "unidad", "stock", "vencido", "en_camino", "en_camino_sin_sucursal",
    "stock_minimo", "unidades_vendidas", "dias_con_stock", "rotacion_diaria", "cobertura_dias", "sugerido",
    "motivo", "sin_ventas", "posible_quiebre", "variantes",
]


def build_reposicion_router(
    *,
    conexion: Conexion | None = None,
    prefix: str = "/api/reportes/reposicion",
):
    """`GET ""` (los parámetros y la lista de productos a pedir) y `GET /export` (CSV). Sólo lee. Parámetros:
    `dias_rotacion` (30), `dias_cobertura` (15) y `plazo_entrega_dias` (3): enteros de 1 hasta su tope
    (`erp.reposicion.MAX_*`); `sucursal_id` (sin él, toda la instancia), `categoria`, `producto_id` y `solo_a_pedir`
    (`true` por default: sólo los de `sugerido > 0`) y `descontar_vencido` (`true` por default: lo que está en lotes
    vencidos no cuenta como stock). Un parámetro inválido, o una sucursal que no existe, es 422."""
    abrir, _ = _deps(None, conexion)
    router = APIRouter(prefix=prefix, tags=["reportes"])

    def _reporte(dias_rotacion: int, dias_cobertura: int, plazo_entrega_dias: int, sucursal_id: int | None,
                 categoria: str | None, producto_id: int | None, solo_a_pedir: bool,
                 descontar_vencido: bool) -> list[dict]:
        try:
            with abrir() as conn:
                return reposicion.sugerencia_reposicion(
                    conn, dias_rotacion=dias_rotacion, dias_cobertura=dias_cobertura,
                    plazo_entrega_dias=plazo_entrega_dias, sucursal_id=sucursal_id, categoria=categoria or None,
                    producto_id=producto_id, solo_a_pedir=solo_a_pedir,
                    descontar_vencido=descontar_vencido,
                )
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    @router.get("")
    def obtener(dias_rotacion: int = Query(reposicion.DIAS_ROTACION, ge=1, le=reposicion.MAX_DIAS_ROTACION),
                dias_cobertura: int = Query(reposicion.DIAS_COBERTURA, ge=1, le=reposicion.MAX_DIAS_COBERTURA),
                plazo_entrega_dias: int = Query(reposicion.PLAZO_ENTREGA_DIAS, ge=1,
                                                le=reposicion.MAX_PLAZO_ENTREGA_DIAS),
                sucursal_id: int | None = None, categoria: str | None = None, producto_id: int | None = None,
                solo_a_pedir: bool = True, descontar_vencido: bool = True):
        productos = _reporte(dias_rotacion, dias_cobertura, plazo_entrega_dias, sucursal_id, categoria, producto_id,
                             solo_a_pedir, descontar_vencido)
        return {
            "dias_rotacion": dias_rotacion, "dias_cobertura": dias_cobertura,
            "plazo_entrega_dias": plazo_entrega_dias, "sucursal_id": sucursal_id, "categoria": categoria,
            "producto_id": producto_id, "solo_a_pedir": solo_a_pedir, "descontar_vencido": descontar_vencido,
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
                 solo_a_pedir: bool = True, descontar_vencido: bool = True):
        productos = _reporte(dias_rotacion, dias_cobertura, plazo_entrega_dias, sucursal_id, categoria, producto_id,
                             solo_a_pedir, descontar_vencido)
        return _csv(productos, _CAMPOS, f"reposicion_{datetime.date.today().isoformat()}.csv")

    return router
