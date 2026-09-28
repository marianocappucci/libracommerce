"""Actualización masiva de precios desde la planilla de un proveedor. Sube un
`.xlsx` con una columna de código (de barra u otro ya cargado en el producto)
y una de costo; el precio de venta se recalcula solo, manteniendo el margen
que cada producto ya tenía (ver `erp.actualizacion_masiva`).

```python
app.include_router(build_actualizacion_precios_router(usuario_actual=get_current_user))
```

Requiere el extra `[planillas]` (trae `openpyxl`, que el resto del motor no
necesita — de ahí el extra aparte y no sumarlo a `[web]`).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from io import BytesIO
from typing import Any

from . import fastapi as _fastapi
from . import openpyxl as _openpyxl
from .catalogo_router import Conexion, _deps

_fastapi()
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile  # noqa: E402

from ..erp import actualizacion_masiva as am  # noqa: E402


class PlanillaInvalida(ValueError):
    pass


#: Encabezados aceptados (normalizados: minúscula, sin acentos), suficientemente
#: amplios como para no exigirle al usuario que renombre columnas de una planilla
#: real de proveedor. "venta" se excluye a propósito del lado del costo: una
#: columna "precio de venta" no tiene que confundirse con la de costo.
_ALIAS_CODIGO = {"codigo", "cod", "ean", "codigodebarra", "codigodebarras", "barcode"}
_ALIAS_COSTO = {"costo", "precio", "preciocosto", "preciodecosto", "costounitario"}

_QUITAR_ACENTOS = (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ñ", "n"))


def _normalizar(texto: str) -> str:
    texto = (texto or "").strip().lower()
    for con_acento, sin_acento in _QUITAR_ACENTOS:
        texto = texto.replace(con_acento, sin_acento)
    return re.sub(r"[^a-z0-9]", "", texto)


def parsear_planilla(contenido: bytes) -> list[dict[str, Any]]:
    """Devuelve `[{"codigo": str, "costo": float}, ...]`. Levanta
    `PlanillaInvalida` (mensaje ya listo para mostrar) si el archivo no abre,
    no tiene las columnas necesarias, o alguna fila con código trae un costo
    que no es un número positivo."""
    openpyxl = _openpyxl()

    try:
        libro = openpyxl.load_workbook(BytesIO(contenido), read_only=True, data_only=True)
    except Exception as exc:
        raise PlanillaInvalida("El archivo no es una planilla de Excel válida (.xlsx).") from exc

    hoja = libro.active
    filas_iter = hoja.iter_rows(values_only=True)
    try:
        encabezado = next(filas_iter)
    except StopIteration:
        raise PlanillaInvalida("La planilla está vacía.") from None

    columnas = {_normalizar(str(c)): i for i, c in enumerate(encabezado) if c is not None}
    col_codigo = next((i for nombre, i in columnas.items() if nombre in _ALIAS_CODIGO), None)
    col_costo = next((i for nombre, i in columnas.items() if nombre in _ALIAS_COSTO), None)
    if col_codigo is None or col_costo is None:
        raise PlanillaInvalida(
            'No se encontraron las columnas necesarias. La primera fila tiene que tener una '
            'columna "Código" (o "EAN"/"Código de barra") y una "Costo" (o "Precio").'
        )

    filas: list[dict[str, Any]] = []
    for numero_fila, fila in enumerate(filas_iter, start=2):
        if fila is None or all(v is None for v in fila):
            continue
        codigo = fila[col_codigo] if col_codigo < len(fila) else None
        if codigo is None or not str(codigo).strip():
            continue
        costo = fila[col_costo] if col_costo < len(fila) else None
        try:
            costo = float(costo)
        except (TypeError, ValueError):
            raise PlanillaInvalida(f'Fila {numero_fila}: el costo "{costo}" no es un número.') from None
        if costo <= 0:
            raise PlanillaInvalida(f"Fila {numero_fila}: el costo tiene que ser mayor que cero.")
        filas.append({"codigo": str(codigo).strip(), "costo": costo})

    if not filas:
        raise PlanillaInvalida("La planilla no tiene ninguna fila con código y costo.")
    return filas


def _resultado_dict(resultado: am.ResultadoActualizacion) -> dict:
    return {
        "actualizaciones": [
            {
                "producto_id": a.item_id, "codigo": a.codigo, "nombre": a.nombre,
                "costo_actual": a.costo_actual, "costo_nuevo": a.costo_nuevo,
                "venta_actual": a.venta_actual, "venta_nueva": a.venta_nueva,
                "margen_calculado": a.margen_calculado,
            }
            for a in resultado.actualizaciones
        ],
        "no_encontrados": [
            {"codigo": n.codigo, "motivo": n.motivo} for n in resultado.no_encontrados
        ],
    }


def build_actualizacion_precios_router(
    *,
    conexion: Conexion | None = None,
    usuario_actual: Callable[..., Any] | None = None,
    prefix: str = "/api/actualizacion-masiva",
) -> APIRouter:
    abrir, usuario = _deps(usuario_actual, conexion)
    router = APIRouter(prefix=prefix, tags=["actualizacion-masiva"])

    @router.post("/precios/preview")
    def preview(archivo: UploadFile = File(...), user: dict = Depends(usuario)):  # noqa: ARG001
        # `def` y no `async def`: parsear el `.xlsx` y consultar cada código son
        # sincrónicos (lectura del archivo subido + varias consultas a la base);
        # con un solo proceso de uvicorn frenaría el loop mientras dura. Corre en
        # el threadpool, así que el archivo se lee de su `SpooledTemporaryFile`
        # directo, sin `await`.
        try:
            filas = parsear_planilla(archivo.file.read())
        except PlanillaInvalida as exc:
            raise HTTPException(422, str(exc)) from exc
        with abrir() as conn:
            resultado = am.calcular(conn, filas)
        return _resultado_dict(resultado)

    @router.post("/precios/aplicar")
    def aplicar(archivo: UploadFile = File(...), user: dict = Depends(usuario)):  # noqa: ARG001
        # Recalcula desde la MISMA planilla en vez de aceptar del cliente una
        # lista de precios ya resueltos: así nadie puede mandar un `venta_nueva`
        # que no salga de recalcular el margen, y una edición manual hecha entre
        # la vista previa y este click se lee fresca (no la pisa un número viejo).
        try:
            filas = parsear_planilla(archivo.file.read())
        except PlanillaInvalida as exc:
            raise HTTPException(422, str(exc)) from exc
        with abrir() as conn:
            resultado = am.calcular(conn, filas)
            am.aplicar(conn, resultado.actualizaciones)
        return _resultado_dict(resultado)

    return router
