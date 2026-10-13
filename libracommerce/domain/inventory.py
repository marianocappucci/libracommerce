from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum


class StockMovementType(StrEnum):
    PURCHASE = "purchase"
    SALE = "sale"
    ADJUSTMENT = "adjustment"
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"
    RETURN = "return"
    WASTE = "waste"


@dataclass(frozen=True)
class Branch:
    id: int | None
    name: str
    code: str = ""
    address: str = ""
    active: bool = True
    is_default: bool = False


@dataclass(frozen=True)
class Location:
    id: int | None
    name: str
    branch_id: int | None = None
    location_type: str = "warehouse"
    active: bool = True
    description: str = ""
    is_default: bool = False


class DepositoInexistente(ValueError):
    """Un `deposito_id` no existe o no está activo en `locations`.

    La regla es una sola, la de `exigir_deposito_activo`, y la comparten los tres caminos que escriben stock (ADR-051): la capa ERP
    (`erp.catalogo.validar_deposito`, `erp.stock.add_movimiento_stock`), los casos de uso del dominio (`usecases/`) y quien llame al
    repositorio. Un depósito inventado no es un conflicto con otra operación simultánea, es un dato del pedido que nunca iba a dejar
    de fallar. Hereda de `ValueError` para que el 422 de los routers sea el mismo mecanismo con el que ya rebotan
    `delete_deposito`/`update_deposito`."""


class DepositoPorDefectoInactivo(ValueError):
    """Se intentó guardar el depósito POR DEFECTO como inactivo (ADR-051).

    Un movimiento sin `deposito_id` cae en el por defecto: si estuviera dado de baja le seguiría cargando stock a un depósito que la
    pantalla muestra apagado. Es un `ValueError` por la misma razón que `DepositoInexistente`."""


#: El texto que ve quien intenta desactivar el depósito por defecto, venga por la ERP, por la web o por el repositorio.
MENSAJE_DEFAULT_INACTIVO = (
    "No se puede desactivar el depósito por defecto: primero hay que marcar otro depósito como default."
)


def exigir_default_activo(location: Location) -> None:
    """Levanta `DepositoPorDefectoInactivo` si `location` es el depósito por defecto y no está activo. Se aplica a lo que se va a
    GUARDAR (no a lo que se lee: una base con un default inactivo de antes de esta regla tiene que seguir pudiendo leerse)."""
    if location.is_default and not location.active:
        raise DepositoPorDefectoInactivo(MENSAJE_DEFAULT_INACTIVO)


def exigir_deposito_activo(location: Location | None, location_id: int) -> None:
    """Levanta `DepositoInexistente` si `location` (lo que resolvió `location_id`) no existe o no está activo."""
    if location is None or not location.active:
        raise DepositoInexistente(f"El depósito {location_id} no existe o no está activo.")


@dataclass(frozen=True)
class StockMovement:
    id: int | None
    item_id: int
    location_id: int
    movement_type: StockMovementType
    quantity_delta: Decimal
    occurred_at: datetime
    source_type: str | None = None
    source_id: int | None = None
    unit_cost: Decimal | None = None
    lot_code: str | None = None
    #: `datetime` (la recepcion de compra) o `date` (el lote de una transferencia, A-4 PR-3): el repositorio escribe
    #: `.isoformat()`, asi que un `date` queda como `AAAA-MM-DD`, igual que las filas de venta y ajuste.
    expires_at: date | datetime | None = None
    variant_id: int | None = None
    note: str = ""
    created_by: int | None = None
    reason_code: str | None = None

    def is_inbound(self) -> bool:
        return self.quantity_delta > 0

    def is_outbound(self) -> bool:
        return self.quantity_delta < 0
