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
| `numerador` | VentaLibra | numeración propia (`POS-000001` contra su tabla `sequences`) en vez de `V-00001` |
| `turno_para` | VentaLibra | el turno de caja del usuario — desde el 2026-09-16 usa el mismo criterio que el default (`get_turno_activo`), ya no un turno compartido |
| `cliente_cc_de` | VentaLibra | traducir el `party_id` de una venta al `clients.id` de LibraCore (`external_ref = party-<id>`) para la cuenta corriente |
| `validar_deposito` | VentaLibra | rechazar un `deposito_id` que existe pero no es el de la sucursal del turno de caja abierto |

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


class Numerador(Protocol):
    """El próximo número de venta. El default es `V-00001` (`erp.ventas.siguiente_numero`);
    VentaLibra lo cambia por `POS-000001` contra su propia tabla `sequences`.

    Recibe la misma conexión que la transacción de la venta —igual que
    `siguiente_numero`, calcula el número con el write-lock ya tomado, para no
    chocar con otro cobro concurrente."""

    def __call__(self, conn: Any) -> str: ...


class TurnoPara(Protocol):
    """El turno de caja abierto para este usuario, o `None` si no hay ninguno
    (o no hay usuario). El default es el turno **por cajero**
    (`libracore.db.turnos.get_turno_activo`); VentaLibra lo reemplaza acá con
    el mismo criterio (ver `services/cuenta_corriente.py` de ese repo).

    > Hasta el 2026-09-16 esta línea decía que VentaLibra tenía un turno
    > **compartido** (`get_turno_activo_any`) en vez de uno por cajero. Quedó
    > vencido cuando VentaLibra pasó al turno por usuario — corregido acá."""

    def __call__(self, conn: Any, usuario_id: int | None) -> Any | None: ...


class ClienteCcDe(Protocol):
    """El `clients.id` de LibraCore al que se le acredita o debita la cuenta
    corriente de esta venta, o `None` si la venta no tiene cliente. El default
    es `venta["cliente_id"]` (`customer_party_id`): en Contalibra el id de
    `parties` y el de `clients` coinciden. VentaLibra traduce por
    `external_ref = party-<id>` (ver `services/cuenta_corriente.py::_cliente_cc`
    de ese repo)."""

    def __call__(self, conn: Any, venta: Any) -> int | None: ...


class ValidarDeposito(Protocol):
    """Corre dentro de la transacción de la venta o la devolución, con la
    misma conexión que el caso de uso, ANTES de escribir nada. Levanta
    `DepositoNoPermitido` (`erp.ventas`) para rechazar el `deposito_id`
    recibido; el default no valida nada.

    Caso real: VentaLibra multisucursal, donde la venta y la devolución
    tienen que salir del depósito de la sucursal de la caja del turno
    abierto, no de cualquier depósito activo (eso ya lo garantiza
    `catalogo.validar_deposito`, que sólo mira si el depósito existe y está
    activo, sin importar la sucursal).

    `operacion` distingue `"venta"` de `"devolucion"` —el mismo gancho sirve
    para las dos, por si el criterio llegara a diferir—. `turno` es el que
    resolvió `hooks.turno_para` para este mismo llamado (puede ser `None` si
    no hay turno abierto). `deposito_id` llega tal cual lo recibió el caso de
    uso, incluido `None`."""

    def __call__(self, conn: Any, *, operacion: str, turno: Any | None,
                deposito_id: int | None) -> None: ...


def _sin_receta(item_id: int, item: Mapping[str, Any]) -> None:
    return None


def _nada(conn: Any, venta: Any) -> None:
    return None


def _lista_default(conn: Any, cliente_id: int | None) -> None:
    return None


def _numerador_default(conn: Any) -> str:
    # Import diferido: `.ventas` importa `.hooks` a nivel de módulo, así que
    # traerlo acá arriba cerraría el ciclo. También es lo que hace que un
    # monkeypatch de `ventas.siguiente_numero` (como en los tests de reintento)
    # se vea reflejado: el nombre se resuelve recién al llamar, no al importar.
    from .ventas import siguiente_numero

    return siguiente_numero(conn)


def _turno_default(conn: Any, usuario_id: int | None) -> Any | None:
    if not usuario_id:
        return None
    from libracore.db.turnos import get_turno_activo

    return get_turno_activo(usuario_id, conn=conn)


def _cliente_cc_default(conn: Any, venta: Any) -> int | None:
    return venta.get("cliente_id")


def _deposito_libre(conn: Any, *, operacion: str, turno: Any | None,
                    deposito_id: int | None) -> None:
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
    numerador: Numerador = _numerador_default
    turno_para: TurnoPara = _turno_default
    cliente_cc_de: ClienteCcDe = _cliente_cc_default
    validar_deposito: ValidarDeposito = _deposito_libre


#: El comportamiento de Contalibra: ningún gancho enganchado.
SIN_GANCHOS = Hooks()
