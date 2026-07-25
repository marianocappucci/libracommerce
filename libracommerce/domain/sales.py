from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class SaleStatus(StrEnum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    PARTIALLY_RETURNED = "partially_returned"
    RETURNED = "returned"


@dataclass(frozen=True)
class SaleItem:
    item_id: int
    description_snapshot: str
    quantity: Decimal
    unit_price: Decimal
    discount_amount: Decimal = Decimal("0")
    tax_rate: Decimal = Decimal("0")
    tax_amount: Decimal = Decimal("0")
    unit_cost_snapshot: Decimal | None = None

    @property
    def line_total(self) -> Decimal:
        return self.quantity * self.unit_price - self.discount_amount + self.tax_amount


@dataclass(frozen=True)
class Sale:
    id: int | None
    number: str
    items: tuple[SaleItem, ...]
    status: SaleStatus = SaleStatus.DRAFT
    customer_party_id: int | None = None
    branch_id: int | None = None
    register_id: int | None = None
    source_type: str = "pos"
    source_id: int | None = None
    subtotal: Decimal = Decimal("0")
    discount_total: Decimal = Decimal("0")
    tax_total: Decimal = Decimal("0")
    total: Decimal = Decimal("0")
    confirmed_at: datetime | None = None

    def calculated_total(self) -> Decimal:
        return sum((item.line_total for item in self.items), Decimal("0"))
