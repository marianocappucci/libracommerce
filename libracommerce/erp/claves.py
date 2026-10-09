"""La `clave_operacion`: lo que hace idempotente a una escritura que un cliente puede reintentar (doble clic, timeout de red).

Lo comparten `vencimientos` (asignar y dar de baja), `reposicion_ordenes` (generar órdenes) y `ventas` (devolver): cada uno valida la
clave, la estampa como `[op:<clave>]` en la nota o la referencia de lo que escribe, y antes de escribir busca esa marca. Con la
marca y los MISMOS datos devuelve el resultado anterior (`repetida: True`); con la marca y OTROS datos levanta `ClaveReusada`, que el
router contesta 409. Acá vive sólo lo común (la validación, la marca y la excepción); qué se busca y cómo se compara es de cada
operación, porque cada una escribe en una tabla distinta."""

from __future__ import annotations

#: Largo máximo de la `clave_operacion` (un UUID en texto son 36).
MAX_LARGO_CLAVE = 64


class ClaveReusada(ValueError):
    """La `clave_operacion` ya se aplicó con OTROS datos: es un pedido distinto y la clave identifica uno solo. El router la contesta 409."""


def normalizar_clave(clave) -> str:
    """La `clave_operacion` recortada: un texto imprimible, no vacío, de hasta `MAX_LARGO_CLAVE` caracteres y sin
    corchetes (delimitan la marca). `ValueError` si no."""
    if not isinstance(clave, str):
        raise ValueError(f"clave_operacion tiene que ser un texto (p. ej. un UUID): {clave!r}")
    clave = clave.strip()
    if not clave:
        raise ValueError("clave_operacion no puede estar vacía: es obligatoria para poder reintentar sin duplicar")
    if len(clave) > MAX_LARGO_CLAVE:
        raise ValueError(f"clave_operacion no puede pasar de {MAX_LARGO_CLAVE} caracteres")
    if "[" in clave or "]" in clave or not clave.isprintable():
        raise ValueError("clave_operacion no puede tener corchetes ni caracteres no imprimibles")
    return clave


def marca(clave: str) -> str:
    """El marcador `[op:<clave>]` que se estampa en la nota o la referencia de lo que escribe la operación."""
    return f"[op:{clave}]"
