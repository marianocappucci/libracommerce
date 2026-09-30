"""Margen y rotación como factory de router, sólo lectura (2026-09-29, ADR-015).

`GET /api/reportes/margen` y sus dos exports CSV. Sobre `libracommerce.erp.margen`:
ingreso, costo, margen y unidades por producto y por período.

```python
app.include_router(
    build_margen_router(conexion=get_connection),
    dependencies=admin_only,
)
```

**El gate lo pone el producto** al montarlo, como en el resto de esta capa: el costo y el
margen de un comercio son de su dueño, no del cajero. Los exports viven bajo el mismo
prefijo y no en `/reportes/export/*` (donde `libracore.reportes_router` deja los suyos): así
un producto que ya proxea `/api` no necesita una ruta más, y la sesión por cookie de la SPA
alcanza para el `<a href>` de descarga.

Cuelga de `/api/reportes/margen`, que no choca con `libracore.reportes_router`
(`/api/reportes` y `/api/reportes/caja-medios`).
"""

from __future__ import annotations

import csv
import datetime
import io

from ..erp import margen
from . import fastapi as _fastapi
from .catalogo_router import Conexion, _deps
from .csv_seguro import celda_segura

_fastapi()
from fastapi import APIRouter, HTTPException  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402

_CAMPOS_PRODUCTOS = [
    "producto_id", "nombre", "unidades", "unidades_por_dia", "ingreso", "costo", "margen", "margen_pct",
    "costo_estimado", "sin_costo",
]
_CAMPOS_PERIODOS = [
    "periodo", "unidades", "ingreso", "costo", "margen", "margen_pct", "costo_estimado", "sin_costo",
]


def _fechas_default(desde: str, hasta: str) -> tuple[str, str]:
    """Mismo default que `libracore.reportes_router`: del primero del mes a hoy."""
    if not desde:
        desde = datetime.date.today().replace(day=1).isoformat()
    if not hasta:
        hasta = datetime.date.today().isoformat()
    return desde, hasta


def _csv(filas: list[dict], campos: list[str], nombre: str) -> StreamingResponse:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=campos, extrasaction="ignore")
    w.writeheader()
    for fila in filas:
        # Un booleano como "True"/"False" no le dice nada a quien abre la planilla.
        # Los textos que una planilla leería como fórmula (`=`, `+`, `-`, `@`) salen con un `'` (`csv_seguro`).
        w.writerow({k: celda_segura(("si" if v else "no") if isinstance(v, bool) else ("" if v is None else v))
                    for k, v in fila.items()})
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{nombre}"'})


def build_margen_router(
    *,
    conexion: Conexion | None = None,
    prefix: str = "/api/reportes/margen",
):
    """`GET ""` (resumen, productos y períodos), `GET /export/productos` y
    `GET /export/periodos` (CSV). Sólo lee. Parámetros: `desde`, `hasta` (default: el mes
    en curso), `agrupacion` (`dia`, `semana`, `mes`), `producto_id`, `orden` y `sentido`
    (`erp.margen.ORDENES`/`SENTIDOS`); uno desconocido es 422."""
    abrir, _ = _deps(None, conexion)
    router = APIRouter(prefix=prefix, tags=["reportes"])

    def _reporte(desde: str, hasta: str, agrupacion: str, producto_id: int | None, orden: str, sentido: str):
        desde, hasta = _fechas_default(desde, hasta)
        try:
            with abrir() as conn:
                reporte = margen.reporte_margen(
                    conn, desde, hasta, agrupacion, producto_id=producto_id, orden=orden, sentido=sentido,
                )
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        return desde, hasta, reporte

    @router.get("")
    def obtener(desde: str = "", hasta: str = "", agrupacion: str = "dia", producto_id: int | None = None,
                orden: str = "margen", sentido: str = "desc"):
        desde, hasta, reporte = _reporte(desde, hasta, agrupacion, producto_id, orden, sentido)
        return {
            "desde": desde, "hasta": hasta, "agrupacion": agrupacion, "producto_id": producto_id,
            "orden": orden, "sentido": sentido, **reporte,
        }

    @router.get("/export/productos")
    def exportar_productos(desde: str = "", hasta: str = "", producto_id: int | None = None,
                           orden: str = "margen", sentido: str = "desc"):
        desde, hasta, reporte = _reporte(desde, hasta, "dia", producto_id, orden, sentido)
        return _csv(reporte["productos"], _CAMPOS_PRODUCTOS, f"margen_productos_{desde}_{hasta}.csv")

    @router.get("/export/periodos")
    def exportar_periodos(desde: str = "", hasta: str = "", agrupacion: str = "dia", producto_id: int | None = None):
        desde, hasta, reporte = _reporte(desde, hasta, agrupacion, producto_id, "margen", "desc")
        return _csv(reporte["periodos"], _CAMPOS_PERIODOS, f"margen_periodos_{desde}_{hasta}.csv")

    return router
