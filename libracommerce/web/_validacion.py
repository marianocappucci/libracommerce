"""Validaciones de entrada que comparten los routers (ADR-026, ADR-027).

Hoy una sola: `sin_booleanos`. Los campos `int`/`float` de pydantic convierten `true` en `1` y `false` en `0` (en modo laxo, que es el de FastAPI) antes de que el
motor, que en varios lugares rechaza el booleano a propósito (`isinstance(valor, bool)`), llegue a verlo: el cuerpo `{"proveedor_id": true}` entraba como el
proveedor 1. Los campos `Decimal` ya rechazan el booleano solos, y los `bool` de verdad y los `StrictInt` no lo necesitan: **sólo** hay que aplicarlo a un campo
`int`/`float` (o a una lista o un diccionario de ellos) donde un `1` o un `0` cambian algo del negocio.
"""

from __future__ import annotations

from . import fastapi as _fastapi

_fastapi()
from pydantic import field_validator  # noqa: E402


def sin_booleanos(*campos: str):
    """Un `field_validator(..., mode="before")` que rechaza `true`/`false` en los `campos` numéricos (ADR-026). Sin esto, pydantic convierte el booleano en `1`/`0` (o
    `1.0`/`0.0`) antes de que el motor, que sí los rechaza, llegue a verlo. Mira también dentro de una lista y de un diccionario (`producto_ids`, `topes`, `precios`). Un `0` numérico,
    un entero o un texto numérico pasan igual que antes: la conversión que sigue es la de siempre. El mensaje (422): «<campo> tiene que ser un número, no un booleano».

    Uso, dentro del modelo: `_no_son_booleanos = sin_booleanos("cantidad", "producto_id")`. Un `true` en un campo que lo admite (un `bool` de verdad) no pasa por acá."""
    def _validar(cls, valor, info):
        if isinstance(valor, dict):
            adentro = [*valor, *valor.values()]
        elif isinstance(valor, (list, tuple)):
            adentro = valor
        else:
            adentro = [valor]
        if any(isinstance(v, bool) for v in adentro):
            raise ValueError(f"{info.field_name} tiene que ser un número, no un booleano")
        return valor
    return field_validator(*campos, mode="before")(classmethod(_validar))
