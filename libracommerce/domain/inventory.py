from dataclasses import dataclass
from datetime import datetime
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
class Location:
    id: int | None
    name: str
    branch_id: int | None = None
    location_type: str = "warehouse"
    active: bool = True
    description: str = ""
    is_default: bool = False


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
    expires_at: datetime | None = None
    variant_id: int | None = None
    note: str = ""
    created_by: int | None = None
    reason_code: str | None = None

    def is_inbound(self) -> bool:
        return self.quantity_delta > 0

    def is_outbound(self) -> bool:
        return self.quantity_delta < 0
