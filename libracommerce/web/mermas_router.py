"""Mermas por período como factory de router, sólo lectura (2026-10-14, ADR-059).

`GET /api/reportes/mermas` y su export CSV. Sobre `libracommerce.erp.mermas`: por producto, cuánto se mermó, cuánto vale y cuántas
mermas quedaron sin costo.

```python
app.include_router(
    build_mermas_router(conexion=get_connection),
    dependencies=encargado_o_admin,
)
```

**El gate lo pone el producto** al montarlo, como en el margen: el costo de lo que se pierde es del dueño, no del cajero. El motor no decide quién ve los
importes. Todos los campos de plata llevan `costo` en el nombre (`costo_total`, `movimientos_sin_costo`, `cantidad_sin_costo`, `sin_costo`), que es lo que un
producto con un filtro de costos por clave (VentaLibra, `SinCostos`) saca solo; y la ruta cuelga de `/api/reportes/mermas`, un prefijo que ese
filtro puede cubrir entero. Los exports viven bajo el mismo prefijo (como en el margen).

Cuelga de `/api/reportes/mermas`, que no choca con `libracore.reportes_router` (`/api/reportes` y `/api/reportes/caja-medios`) ni con el margen.
Errores: una fecha ilegible, un rango al revés o un depósito que no existe, 422.
"""

from __future__ import annotations

from ..erp import mermas
from . import fastapi as _fastapi
from .catalogo_router import Conexion, _deps
from .margen_router import _csv, _fechas_default

_fastapi()
from fastapi import APIRouter, HTTPException  # noqa: E402

_CAMPOS = [
    "producto_id", "codigo", "nombre", "unidad", "cantidad", "costo_total", "movimientos", "movimientos_sin_costo",
    "cantidad_sin_costo",
]


def build_mermas_router(
    *,
    conexion: Conexion | None = None,
    prefix: str = "/api/reportes/mermas",
):
    """`GET ""` (`{desde, hasta, deposito_id, producto_id, resumen, productos}`, ver `erp.mermas.reporte_mermas`) y `GET /export` (CSV). Sólo lee.
    Parámetros: `desde`, `hasta` (default: el mes en curso, con «hoy» de Argentina), `deposito_id` y `producto_id`."""
    abrir, _ = _deps(None, conexion)
    router = APIRouter(prefix=prefix, tags=["reportes"])

    def _reporte(desde: str, hasta: str, deposito_id: int | None, producto_id: int | None):
        desde, hasta = _fechas_default(desde, hasta)
        try:
            with abrir() as conn:
                reporte = mermas.reporte_mermas(conn, desde, hasta, deposito_id=deposito_id, producto_id=producto_id)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        return desde, hasta, reporte

    @router.get("")
    def obtener(desde: str = "", hasta: str = "", deposito_id: int | None = None, producto_id: int | None = None):
        desde, hasta, reporte = _reporte(desde, hasta, deposito_id, producto_id)
        return {"desde": desde, "hasta": hasta, "deposito_id": deposito_id, "producto_id": producto_id, **reporte}

    @router.get("/export")
    def exportar(desde: str = "", hasta: str = "", deposito_id: int | None = None, producto_id: int | None = None):
        desde, hasta, reporte = _reporte(desde, hasta, deposito_id, producto_id)
        return _csv(reporte["productos"], _CAMPOS, f"mermas_{desde}_{hasta}.csv")

    return router
