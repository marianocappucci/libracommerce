"""El rubro de la instancia, guardado en `commerce_settings`.

El rubro es una propiedad del comercio, no de cada item: una peluqueria es una
peluqueria para todo su catalogo. Por eso vive en `commerce_settings` y no en
una columna de `catalog_items`.

Los ejes que se derivan de el estan en `domain/presets.py`, que no toca la
base.
"""

from libracommerce.domain.presets import (
    RUBRO_POR_DEFECTO,
    EjeDeVariante,
    ejes_visibles,
    preset_de,
)
from libracommerce.ports.persistence import CommerceRepository

#: Prefijo `catalog.` para no chocar con las claves de balanza y ticket que ya
#: viven en la misma tabla.
CLAVE_RUBRO = "catalog.rubro"


class RubroDesconocidoError(ValueError):
    """El codigo de rubro no esta en `PRESETS`."""


def leer_rubro(repo: CommerceRepository) -> str:
    """El rubro configurado, o el generico si nadie lo eligio todavia.

    Nunca falla: una instancia sin rubro configurado es el estado normal de
    las que ya existen, y tienen que seguir funcionando.
    """
    return repo.get_setting(CLAVE_RUBRO) or RUBRO_POR_DEFECTO


def fijar_rubro(repo: CommerceRepository, codigo: str) -> None:
    """Configura el rubro, rechazando un codigo que no existe.

    Aca si se valida --al reves que los atributos, que son libres-- porque un
    rubro mal escrito no rompe nada de forma visible: la pantalla deja de
    sugerir ejes y eso se ve igual que "todavia no lo configuraron". El error
    aparecerria semanas despues como "las sugerencias no andan".
    """
    if preset_de(codigo) is None:
        raise RubroDesconocidoError(
            f"El rubro '{codigo}' no existe. Ver PRESETS en domain/presets.py."
        )
    repo.set_setting(CLAVE_RUBRO, codigo)


def ejes_para(
    repo: CommerceRepository, atributos: dict[str, str] | None = None
) -> tuple[EjeDeVariante, ...]:
    """Los ejes a mostrar para un item de esta instancia.

    Atajo de `ejes_visibles(leer_rubro(repo), atributos)`, que es la
    combinacion que necesita cualquier pantalla de variantes.
    """
    return ejes_visibles(leer_rubro(repo), atributos)
