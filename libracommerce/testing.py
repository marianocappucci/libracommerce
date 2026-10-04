"""Ayudas de prueba para quien monta routers de este motor (ADR-028; reexportadas de libracore desde ADR-030). Se importan desde la suite de un producto, no desde la app.

**La canónica vive en `libracore.testing`** (libracore v1.125.0, `libracore/testing/booleanos.py`, ADR-013 de libracore): `campos_numericos_que_aceptan_booleano(app, *, ignorar)`, la guardia que cierra
lo que ADR-026 y ADR-027 arreglaron a mano. ADR-030 de este repo la deja de copiar (regla del wiki: «toda lógica de fondo se escribe y se arregla en libracore»); este módulo sólo la **reexporta**, con
la misma identidad (`libracommerce.testing.campos_numericos_que_aceptan_booleano is libracore.testing.campos_numericos_que_aceptan_booleano`), el mismo contrato y el mismo informe. Se reexporta
para que la suite de cada producto, que ya hace `from libracommerce.testing import campos_numericos_que_aceptan_booleano`, siga igual.

Qué mide (ADR-028): pydantic convierte `true` en `1` y `false` en `0` en un campo `int`/`float` (en el modo laxo que usa FastAPI) antes de que el motor, que en varios lugares rechaza el booleano a
propósito, llegue a verlo. `libracommerce.web._validacion.sin_booleanos` lo evita en cada campo, pero nada avisaba de un campo nuevo, de un router propio del producto o de un payload heredado al que se
le sumó un número y no se le puso. La función lo mide: recorre las rutas reales de la app, instancia el modelo real (con sus validadores) con `True`/`False` en cada hoja numérica y devuelve las que lo aceptan.

Necesita el extra `[web]`, que trae `libracore>=1.125` (FastAPI y pydantic son dependencias de libracore). Sin libracore, o con uno anterior a v1.125.0, importar este módulo levanta `SinLibracore`
con el extra que falta, en vez de un `ModuleNotFoundError` suelto.
"""

from __future__ import annotations

from .web import SinLibracore

try:
    from libracore.testing import campos_numericos_que_aceptan_booleano  # noqa: F401
except ImportError as e:
    raise SinLibracore() from e

__all__ = ["campos_numericos_que_aceptan_booleano"]
