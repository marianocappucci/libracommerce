from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from libracommerce.domain.catalog import CatalogItemType


class SaleStatus(StrEnum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    PARTIALLY_RETURNED = "partially_returned"
    RETURNED = "returned"


@dataclass(frozen=True)
class SaleItem:
    """A sale line. Products must reference a registered CatalogItem;
    services may reference one (pre-loaded, priced) or be entirely
    ad-hoc (item_id=None, free-text description_snapshot only) — a
    professional invoicing a one-off service that was never catalogued.
    """

    kind: CatalogItemType
    description_snapshot: str
    quantity: Decimal
    unit_price: Decimal
    item_id: int | None = None
    variant_id: int | None = None
    discount_amount: Decimal = Decimal("0")
    tax_rate: Decimal = Decimal("0")
    tax_amount: Decimal = Decimal("0")
    unit_cost_snapshot: Decimal | None = None

    def __post_init__(self):
        if self.kind == CatalogItemType.PRODUCT and self.item_id is None:
            raise ValueError(
                "Una línea de producto requiere item_id: a diferencia de los "
                "servicios, las ventas de productos siempre deben referenciar "
                "un CatalogItem registrado."
            )
        if self.variant_id is not None and self.item_id is None:
            raise ValueError(
                "Una línea con variant_id requiere item_id: una variante "
                "siempre pertenece a un item del catálogo."
            )

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
