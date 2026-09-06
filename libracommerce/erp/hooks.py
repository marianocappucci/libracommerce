"""Los puntos de extensión de la capa ERP: lo que un producto engancha, tipado.

P9 (2026-09-06) mueve a este motor la orquestación comercial que Contalibra y
Restolibra tenían duplicada. La regla que gobierna esa mudanza es **sin
`if producto`**: el motor no sabe qué vertical lo corre. Todo lo que hoy
distingue a un producto del otro en el núcleo comercial entra por uno de estos
ganchos, con un default que es el comportamiento de Contalibra (el producto
de referencia, con cliente real).

Cada uno nombra el caso real que lo motiva:

| Gancho | Quién lo necesita hoy | Para qué |
|---|---|---|
| `resolver_receta` | Restolibra | descontar los insumos de la receta en vez del plato (`erp.stock.descontar_stock_venta`) |
| `al_confirmar_venta` / `al_anular_venta` | Restolibra, Contalibra | marcar el pedido cobrado y liberar la mesa; `venta_links`, integraciones, outbox de LibraEdge |
| `lista_de_precio_para` | Contalibra | la lista mayorista asignada al cliente (`cliente_lista_precio`) |
| `canales` | Restolibra | mostrador y delivery como canales del reporte de ventas |

🔴 **Los ganchos de venta reciben la MISMA conexión** con la que el caso de uso
está escribiendo la venta, y corren adentro de esa transacción. Es lo que P7
fijó como no negociable: si algo falla, el rollback revierte venta, líneas,
stock, pagos, caja, turno **y lo que el producto enganchó**, juntos. Un gancho
que abriera su propia conexión rompería eso sin que ningún test lo viera.

`venta` es el dict de `erp.ventas.obtener_venta` (id, numero, estado, items,
pagos, turno_id, ...), leído con la misma conexión. `al_confirmar_venta` corre
cuando la venta llega a `cobrada` —al nacer cobrada, o al acreditarse el QR—;
`al_anular_venta`, después de reponer stock y revertir la caja.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol


@dataclass(frozen=True)
class Insumo:
    """Una línea de receta resuelta: qué ítem del catálogo se descuenta y cuánto
    por unidad vendida (con los modificadores del pedido ya aplicados)."""

    item_id: int
    cantidad: Decimal


class ResolverReceta(Protocol):
    """Dado un ítem vendido —su id y la línea de venta entera, que en Restolibra
    trae los `modificadores` del pedido—, sus insumos; o `None` si el ítem no
    tiene receta y se descuenta él mismo. El default devuelve `None` siempre.

    M1 (2026-09-06) le agregó la línea al contrato: el gancho de M0 recibía
    sólo el id y no había forma de aplicar "sin cheddar" / "doble medallón"."""

    def __call__(self, item_id: int, item: Mapping[str, Any]) -> Sequence[Insumo] | None: ...


class GanchoDeVenta(Protocol):
    """Corre dentro de la transacción de la venta, con la misma conexión."""

    def __call__(self, conn: Any, venta: Any) -> None: ...


class ListaDePrecioPara(Protocol):
    """La lista de precio que aplica a un cliente, o `None` para la default."""

    def __call__(self, conn: Any, cliente_id: int | None) -> int | None: ...


def _sin_receta(item_id: int, item: Mapping[str, Any]) -> None:
    return None


def _nada(conn: Any, venta: Any) -> None:
    return None


def _lista_default(conn: Any, cliente_id: int | None) -> None:
    return None


@dataclass(frozen=True)
class Hooks:
    """El conjunto de ganchos de un producto. Inmutable: se arma una vez en el
    arranque y se pasa a las factories de router y a los casos de uso."""

    resolver_receta: ResolverReceta = _sin_receta
    al_confirmar_venta: GanchoDeVenta = _nada
    al_anular_venta: GanchoDeVenta = _nada
    lista_de_precio_para: ListaDePrecioPara = _lista_default
    #: Canales de venta que el producto agrega al reporte, además de los del
    #: motor. Restolibra: ("mostrador", "delivery").
    canales: tuple[str, ...] = ()


#: El comportamiento de Contalibra: ningún gancho enganchado.
SIN_GANCHOS = Hooks()
