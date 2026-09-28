"""La capa HTTP de LibraCommerce: factories de router, detrás del extra `[web]`.

Mismo patrón que `libracore.facturas_router`: cada módulo expone una
`build_<modulo>_router(...)` que recibe lo que el producto decide —la
dependencia que abre la conexión, los gates de rol, los `Hooks`— y devuelve un
`APIRouter` que el producto monta con `include_router`. El motor no monta nada
solo ni conoce la app del producto.

FastAPI no es dependencia del motor (`dependencies = []`): viene con el extra
`[web]`. Los módulos de esta capa lo importan a través de `fastapi()` para que
la ausencia del extra se lea como lo que es y no como un `ModuleNotFoundError`
en medio del arranque de un producto. Mismo criterio para `openpyxl`, que trae
el extra aparte `[planillas]` (sólo lo necesita `planillas_router`, no el resto
de esta capa).

M0 deja el paquete y la guarda; M1 trae la primera factory (catálogo y stock).
"""

from __future__ import annotations


class SinFastAPI(RuntimeError):
    """Falta el extra `[web]`, que es quien trae FastAPI."""


class SinOpenpyxl(RuntimeError):
    """Falta el extra `[planillas]`, que es quien trae openpyxl."""


def fastapi():
    """El módulo `fastapi`, o un error que dice qué extra falta."""
    try:
        import fastapi as _fastapi
    except ModuleNotFoundError as e:
        raise SinFastAPI(
            "Falta fastapi: LibraCommerce no lo declara como dependencia, viene "
            "en el extra `[web]`. Instalá `libracommerce[web]` en la imagen del "
            "producto antes de montar una factory de router de este motor."
        ) from e
    return _fastapi


def openpyxl():
    """El módulo `openpyxl`, o un error que dice qué extra falta."""
    try:
        import openpyxl as _openpyxl
    except ModuleNotFoundError as e:
        raise SinOpenpyxl(
            "Falta openpyxl: hace falta para leer la planilla de la actualización "
            "masiva de precios, viene en el extra `[planillas]`. Instalá "
            "`libracommerce[planillas]` en la imagen del producto antes de montar "
            "`build_actualizacion_precios_router`."
        ) from e
    return _openpyxl


__all__ = ["SinFastAPI", "SinOpenpyxl", "fastapi", "openpyxl"]
