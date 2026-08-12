"""`libracommerce.__version__` contra la metadata del paquete instalado.

El test que importa es el primero: si `__version__` vuelve a ser un literal, la
igualdad se rompe. Es la regresion que ya pagamos una vez -- el tag `v0.5.0`
publico un paquete que se presentaba como `0.4.0`, porque la version era una
linea que habia que acordarse de mover. No lo agarro este repo sino el guard de
CI de Contalibra y Restolibra, comparando su pin con lo que recibian.

Corre tambien en el build del tag (`push: tags: ["v*"]` en ci.yml), que es
justo el momento en que un literal desfasado se convierte en una release mala.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as metadata_version

import pytest

import libracommerce

try:
    VERSION_INSTALADA: str | None = metadata_version("libracommerce")
except PackageNotFoundError:
    VERSION_INSTALADA = None


@pytest.mark.skipif(
    VERSION_INSTALADA is None,
    reason="El paquete no esta instalado (checkout de desarrollo): no hay metadata contra que comparar.",
)
def test_version_no_diverge_de_la_metadata_instalada():
    assert libracommerce.__version__ == VERSION_INSTALADA


def test_version_es_una_cadena_no_vacia():
    assert isinstance(libracommerce.__version__, str)
    assert libracommerce.__version__ != ""
