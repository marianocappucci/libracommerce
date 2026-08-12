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

from datetime import datetime
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
    permitir_negativo: bool = False,
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

    `permitir_negativo` existe para el ajuste de un inventario que ya estaba
    mal cargado, donde la realidad fisica manda sobre la proyeccion. No es el
    camino normal y por eso hay que pedirlo.
    """
    if quantity <= 0:
        raise ValueError(f"La cantidad a transferir tiene que ser positiva (recibido: {quantity}).")
    if from_location_id == to_location_id:
        raise ValueError(
            f"El origen y el destino son el mismo deposito ({from_location_id}): "
            "la transferencia no moveria nada."
        )

    with repo.transaction():
        if not permitir_negativo:
            verificar_disponibilidad(
                repo, item_id, from_location_id, quantity, variant_id=variant_id
            )

        salida = repo.append_stock_movement(
            StockMovement(
                id=None,
                item_id=item_id,
                variant_id=variant_id,
                location_id=from_location_id,
                movement_type=StockMovementType.TRANSFER_OUT,
                quantity_delta=-quantity,
                occurred_at=occurred_at,
                source_type="transfer",
                source_id=None,
                note=note,
                created_by=created_by,
            )
        )
        entrada = repo.append_stock_movement(
            StockMovement(
                id=None,
                item_id=item_id,
                variant_id=variant_id,
                location_id=to_location_id,
                movement_type=StockMovementType.TRANSFER_IN,
                quantity_delta=quantity,
                occurred_at=occurred_at,
                source_type="transfer",
                source_id=salida.id,
                note=note,
                created_by=created_by,
            )
        )

    return salida, entrada
