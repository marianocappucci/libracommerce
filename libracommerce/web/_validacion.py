"""Validaciones de entrada que comparten los routers (ADR-026, ADR-027; reexportadas de libracore desde ADR-030).

**La canónica vive en `libracore.validacion`** (libracore v1.125.0, ADR-013 de libracore): `sin_booleanos(*campos)` para los campos tipados y `rechazar_booleanos(valor, campos, donde, *, esperado)`
para los dicts sin tipar. ADR-029 de este repo la deja de copiar (regla del wiki: «toda lógica de fondo se escribe y se arregla en libracore»); este módulo sólo la **reexporta**, con la misma
identidad (`libracommerce.web._validacion.sin_booleanos is libracore.validacion.sin_booleanos`), el mismo código y el mismo mensaje (422): «<campo> tiene que ser un número, no un booleano».

Se reexporta, y no se pide a cada router y a cada producto que importen de libracore, porque los siete routers que la usan (`catalogo`, `compras`, `listas`, `promociones`, `reposicion`,
`vencimientos`, `ventas`) y los productos ya importan `from ._validacion import sin_booleanos` / `from libracommerce.web._validacion import sin_booleanos`: ese camino sigue andando sin tocarlos.

Qué hace (ADR-026, ADR-027): los campos `int`/`float` de pydantic convierten `true` en `1` y `false` en `0` (en modo laxo, que es el de FastAPI) antes de que el motor, que en varios lugares
rechaza el booleano a propósito (`isinstance(valor, bool)`), llegue a verlo: el cuerpo `{"proveedor_id": true}` entraba como el proveedor 1. Los campos `Decimal` ya rechazan el booleano solos, y los
`bool` de verdad y los `StrictInt` no lo necesitan: **sólo** hay que aplicarlo a un campo `int`/`float` (o a una lista o un diccionario de ellos) donde un `1` o un `0` cambian algo del negocio. Lo
que quede sin aplicar lo encuentra `libracommerce.testing.campos_numericos_que_aceptan_booleano(app)` (ADR-028), que cada producto corre sobre su app completa.

Esto es parte de la capa `web/`: pide `libracommerce[web]`, que trae `libracore>=1.125` (el núcleo del motor, sin `web`, sigue sin importar libracore). Sin libracore, o con uno anterior a v1.125.0,
importar este módulo levanta `SinLibracore` con el extra que falta, en vez de un `ModuleNotFoundError` suelto.
"""

from __future__ import annotations

from . import SinLibracore

try:
    from libracore.validacion import rechazar_booleanos, sin_booleanos  # noqa: F401
except ImportError as e:
    raise SinLibracore() from e

__all__ = ["rechazar_booleanos", "sin_booleanos"]
