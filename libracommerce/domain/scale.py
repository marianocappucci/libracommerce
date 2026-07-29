"""Etiquetas de balanza de mostrador.

Una balanza de fiambrería o verdulería imprime un EAN-13 que no identifica
un producto único: identifica un producto **y** cuánto se pesó de él. La
forma típica en Argentina es

    P P C C C C C V V V V V D
    └─┘ └───────┘ └───────┘ └┘
    pref  código    valor   checksum

donde `valor` es el peso en gramos o el importe ya calculado por la balanza,
según cómo esté configurado el equipo. Los largos y el prefijo varían por
marca y por configuración del comercio, así que nada de esto va hardcodeado:
se describe con un `ScaleFormat`.

Este módulo sólo interpreta el código. Resolver a qué producto corresponde,
y qué precio aplicar, es del consumidor.
"""
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class ScaleValueKind(StrEnum):
    """Qué representan los dígitos de valor de la etiqueta."""

    #: Peso pesado por la balanza. El importe lo calcula el sistema con el
    #: precio por kilo vigente -- si el precio cambió después de pesar, se
    #: cobra el actual.
    WEIGHT = "weight"
    #: Importe ya calculado por la balanza. Se cobra tal cual, así coincide
    #: con la etiqueta pegada al producto aunque el precio haya cambiado.
    AMOUNT = "amount"


@dataclass(frozen=True)
class ScaleFormat:
    """Cómo leer las etiquetas de la balanza de un comercio.

    `divisor` lleva los dígitos crudos a la unidad real: 1000 para gramos a
    kilos, 100 para centavos a pesos.
    """

    prefix: str = "20"
    code_digits: int = 5
    value_digits: int = 5
    value_kind: ScaleValueKind = ScaleValueKind.WEIGHT
    divisor: int = 1000
    #: Largo total del código impreso. EAN-13 por defecto; algunas balanzas
    #: emiten EAN-8 o códigos sin checksum.
    total_digits: int = 13

    def __post_init__(self):
        if not self.prefix or not self.prefix.isdigit():
            raise ValueError("El prefijo de balanza debe ser una cadena de dígitos.")
        if self.code_digits <= 0 or self.value_digits <= 0:
            raise ValueError("Los largos de código y valor deben ser mayores que cero.")
        if self.divisor <= 0:
            raise ValueError("El divisor debe ser mayor que cero.")
        minimo = len(self.prefix) + self.code_digits + self.value_digits
        if self.total_digits < minimo:
            raise ValueError(
                f"El código no entra en {self.total_digits} dígitos: el prefijo, el "
                f"código y el valor ya suman {minimo}."
            )


@dataclass(frozen=True)
class ScaleReading:
    """Lo que dice una etiqueta de balanza, ya interpretada."""

    #: Código del producto tal como está cargado en la balanza, sin los ceros
    #: de relleno del EAN.
    item_code: str
    kind: ScaleValueKind
    #: Kilos si `kind` es WEIGHT, pesos si es AMOUNT.
    value: Decimal


def parse_scale_barcode(code: str, fmt: ScaleFormat) -> ScaleReading | None:
    """Interpreta `code` como etiqueta de balanza, o devuelve None si no lo es.

    Devolver None no es un error: la enorme mayoría de lo que se escanea en
    el mostrador son códigos de barra comunes, y el caller sigue con su
    búsqueda normal. Sólo se interpreta lo que coincide con el formato del
    comercio.
    """
    limpio = code.strip()
    if not limpio.isdigit() or len(limpio) != fmt.total_digits:
        return None
    if not limpio.startswith(fmt.prefix):
        return None

    inicio_codigo = len(fmt.prefix)
    fin_codigo = inicio_codigo + fmt.code_digits
    fin_valor = fin_codigo + fmt.value_digits

    crudo_codigo = limpio[inicio_codigo:fin_codigo]
    crudo_valor = limpio[fin_codigo:fin_valor]

    # Los ceros a la izquierda son relleno del EAN, no parte del código que
    # el comercio cargó en la balanza.
    item_code = crudo_codigo.lstrip("0")
    if not item_code:
        return None

    valor = Decimal(crudo_valor) / Decimal(fmt.divisor)
    if valor <= 0:
        # Una etiqueta con peso o importe cero no se puede cobrar; es una
        # etiqueta mal impresa, no una venta de cero.
        return None

    return ScaleReading(item_code=item_code, kind=fmt.value_kind, value=valor)
