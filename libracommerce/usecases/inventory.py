"""Operaciones de inventario que son una sola cosa, no dos asientos sueltos.

Hasta ahora el motor sabia *appendear* un movimiento y proyectar
`current_stock`, pero no orquestaba ninguna operacion de inventario: mover
mercaderia de un deposito a otro y no vender lo que no hay quedaron en cada
consumidor. Hoy solo Contalibra los tiene, en `app/db_productos.py`, con SQL
crudo contra `stock_movements`.

Este modulo los sube al motor, y de paso corrige dos defectos que esa version
arrastra:

1. **No es atomica.** Llama dos veces a `add_movimiento_stock` y cada llamada
   abre su propia conexion, asi que si la segunda falla la mercaderia ya salio
   del origen y no llego al destino. Perdida silenciosa: no hay error visible
   ni fila que delate el hueco.
2. **El chequeo de disponibilidad corre en otra conexion, antes de escribir.**
   Entre el `SELECT` y los `INSERT` entra otra transferencia y las dos pasan la
   validacion sobre el mismo stock.

Aca las dos escrituras y la lectura que las autoriza viven en el mismo
`repo.transaction()`.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from libracommerce.domain.inventory import StockMovement, StockMovementType
from libracommerce.ports.persistence import CommerceRepository


class StockInsuficienteError(ValueError):
    """No hay existencias para sacar lo que se pide del origen.

    Hereda de `ValueError` a proposito: los consumidores que hoy atrapan el
    `ValueError` de `transferir_stock` de Contalibra siguen andando sin
    cambios cuando migren a este caso de uso.
    """

    def __init__(self, item_id: int, location_id: int, pedido: Decimal, disponible: Decimal):
        self.item_id = item_id
        self.location_id = location_id
        self.pedido = pedido
        self.disponible = disponible
        super().__init__(
            f"Stock insuficiente en el deposito {location_id} para el item {item_id}: "
            f"se piden {pedido} y hay {disponible}."
        )


@dataclass(frozen=True)
class TramoDeTransferencia:
    """Un tramo de una transferencia por lote (ADR-018, A-4 PR-3): `cantidad` (positiva) del bucket `(lot_code,
    expires_at)` del origen. Cada tramo escribe su propio par salida/entrada, y la entrada COPIA el lote y el
    vencimiento de la salida. `lot_code` y `expires_at` en `None` es el tramo «sin lote»."""

    cantidad: Decimal
    lot_code: str | None = None
    expires_at: date | None = None


#: Diferencia relativa por debajo de la cual la suma de los tramos cuenta como igual a la cantidad pedida (el ruido de
#: `float` que `erp.lotes` ya descarta al planificar).
_UMBRAL_DE_RUIDO = Decimal("1e-12")


def verificar_disponibilidad(
    repo: CommerceRepository,
    item_id: int,
    location_id: int,
    cantidad: Decimal,
    *,
    variant_id: int | None = None,
) -> Decimal:
    """Levanta `StockInsuficienteError` si no alcanza. Devuelve lo disponible.

    Es una funcion aparte y no un `if` adentro de la transferencia porque la
    necesitan tambien las salidas que no son transferencia (una venta que
    quiera validar, el consumo de materiales de un ticket). **Llamarla dentro
    de la misma transaccion que la escritura**: sola, no cierra la ventana
    entre leer y escribir.
    """
    disponible = repo.current_stock(item_id, location_id, variant_id=variant_id)
    if cantidad > disponible:
        raise StockInsuficienteError(item_id, location_id, cantidad, disponible)
    return disponible


def transfer_stock(
    repo: CommerceRepository,
    *,
    item_id: int,
    from_location_id: int,
    to_location_id: int,
    quantity: Decimal,
    occurred_at: datetime,
    variant_id: int | None = None,
    note: str = "",
    created_by: int | None = None,
    reason_code_salida: str | None = None,
    reason_code_entrada: str | None = None,
    permitir_negativo: bool = False,
    tramos: Sequence[TramoDeTransferencia] | None = None,
) -> tuple[StockMovement, StockMovement]:
    """Mueve `quantity` de un deposito a otro como una sola operacion.

    Devuelve el par (salida, entrada). Las dos filas y la lectura que las
    autoriza van en la misma transaccion: o quedan las dos o no queda ninguna.

    **Como se reconoce el par despues.** No hay tabla de transferencias, asi
    que la entrada apunta a la salida con `source_type="transfer"` y
    `source_id` = id de la salida. Con eso
    `list_stock_movements_by_source("transfer", salida.id)` devuelve la
    contraparte. La salida no puede apuntar a la entrada porque se escribe
    primero y los movimientos son inmutables -- que es justamente la propiedad
    que hace confiable a `current_stock`.

    **Los `reason_code` son dos y no uno** porque cada pata de la
    transferencia es un evento distinto para quien lee el ledger. Existen para
    que un consumidor conserve su propio vocabulario: Contalibra escribe
    `transferencia_salida`/`transferencia_entrada` ahi y su pantalla de
    actividad los muestra tal cual, con un `COALESCE(reason_code,
    movement_type)` sin mapa. Sin este parametro, adoptar este caso de uso le
    degradaria esa pantalla a `transfer_out` sin que ningun test lo note.

    `permitir_negativo` existe para el ajuste de un inventario que ya estaba
    mal cargado, donde la realidad fisica manda sobre la proyeccion. No es el
    camino normal y por eso hay que pedirlo.

    **Por lote (A-4 PR-3, ADR-018).** `tramos=None` (el default, y lo unico que
    mandan los tres productos hoy) es EL CAMINO DE SIEMPRE: una salida y una
    entrada con lote y vencimiento en NULL. Con `tramos` (el plan FEFO de un
    producto marcado, que arma `erp.catalogo.transferir_stock`) se escribe **un
    par por tramo**, en la misma transaccion: la salida lleva el lote y el
    vencimiento del bucket de origen y la entrada los copia; cada entrada apunta
    a SU salida con `source_id`, asi que el par sigue siendo 1:1 (una
    transferencia de N lotes son N pares, y `get_transferencias` muestra N
    filas). La suma de los tramos tiene que ser `quantity`. La guarda de
    disponibilidad sigue siendo sobre el total del origen. Con `tramos` devuelve
    el par del PRIMER tramo; los demas se leen del ledger.
    """
    if quantity <= 0:
        raise ValueError(f"La cantidad a transferir tiene que ser positiva (recibido: {quantity}).")
    if from_location_id == to_location_id:
        raise ValueError(
            f"El origen y el destino son el mismo deposito ({from_location_id}): "
            "la transferencia no moveria nada."
        )

    if tramos is not None:
        if not tramos or any(tr.cantidad <= 0 for tr in tramos):
            raise ValueError("Los tramos de la transferencia tienen que ser positivos y no estar vacios.")
        if abs(sum((tr.cantidad for tr in tramos), Decimal(0)) - quantity) > _UMBRAL_DE_RUIDO * max(Decimal(1), quantity):
            raise ValueError(
                f"Los tramos de la transferencia ({sum((tr.cantidad for tr in tramos), Decimal(0))}) no suman la "
                f"cantidad ({quantity})."
            )
    # Sin tramos: un unico par por la cantidad entera, sin lote (lo de siempre).
    a_escribir = tramos if tramos is not None else [TramoDeTransferencia(quantity)]

    with repo.transaction():
        if not permitir_negativo:
            verificar_disponibilidad(
                repo, item_id, from_location_id, quantity, variant_id=variant_id
            )

        pares = []
        for tr in a_escribir:
            salida = repo.append_stock_movement(
                StockMovement(
                    id=None,
                    item_id=item_id,
                    variant_id=variant_id,
                    location_id=from_location_id,
                    movement_type=StockMovementType.TRANSFER_OUT,
                    quantity_delta=-tr.cantidad,
                    occurred_at=occurred_at,
                    source_type="transfer",
                    source_id=None,
                    lot_code=tr.lot_code,
                    expires_at=tr.expires_at,
                    note=note,
                    created_by=created_by,
                    reason_code=reason_code_salida,
                )
            )
            entrada = repo.append_stock_movement(
                StockMovement(
                    id=None,
                    item_id=item_id,
                    variant_id=variant_id,
                    location_id=to_location_id,
                    movement_type=StockMovementType.TRANSFER_IN,
                    quantity_delta=tr.cantidad,
                    occurred_at=occurred_at,
                    source_type="transfer",
                    source_id=salida.id,
                    lot_code=tr.lot_code,
                    expires_at=tr.expires_at,
                    note=note,
                    created_by=created_by,
                    reason_code=reason_code_entrada,
                )
            )
            pares.append((salida, entrada))

    return pares[0]
