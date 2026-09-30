"""Neutralización de fórmulas en los CSV que exporta el motor (CSV injection).

Una planilla (Excel, Calc, Sheets) interpreta como **fórmula** una celda de texto que empieza con `=`, `+`, `-`, `@`,
tab, retorno de carro o salto de línea. Un nombre de producto, un código de lote o una nota cargados por el personal llegan a los
exports (margen, reposición, vencimientos) y, con `=HYPERLINK(...)` o un DDE, ejecutan algo en la máquina de quien
abre el archivo. La defensa estándar es prefijar la celda con `'`: la planilla la muestra como texto.

Sólo se tocan los **textos**: un número (`-2`, `-3.5`) o `None` se escriben como siempre, porque un negativo es un
número y no una fórmula. Es el único punto por el que pasa todo CSV del motor (`margen_router._csv`).
"""

from __future__ import annotations

from typing import Any

#: Los caracteres con los que una planilla arranca una fórmula (OWASP, CSV injection).
PREFIJOS_PELIGROSOS = ("=", "+", "-", "@", "\t", "\r", "\n")


def celda_segura(valor: Any) -> Any:
    """`valor` tal cual, salvo un texto que empieza con un carácter de fórmula: ese sale con un `'` adelante."""
    if isinstance(valor, str) and valor.startswith(PREFIJOS_PELIGROSOS):
        return "'" + valor
    return valor
