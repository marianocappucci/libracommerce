from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class PurchaseOrderStatus(StrEnum):
    DRAFT = "draft"
    SENT = "sent"
    PARTIAL = "partial"
    RECEIVED = "received"
    CANCELLED = "cancelled"


class PurchaseReceiptStatus(StrEnum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class PurchaseOrderItem:
    item_id: int
    quantity_ordered: Decimal
    unit_cost: Decimal
    quantity_received: Decimal = Decimal("0")
    tax_rate: Decimal = Decimal("0")

    @property
    def subtotal(self) -> Decimal:
        return self.quantity_ordered * self.unit_cost

    @property
    def pending_quantity(self) -> Decimal:
        return self.quantity_ordered - self.quantity_received


@dataclass(frozen=True)
class PurchaseOrder:
    id: int | None
    number: str
    supplier_party_id: int
    items: tuple[PurchaseOrderItem, ...]
    status: PurchaseOrderStatus = PurchaseOrderStatus.DRAFT
    branch_id: int | None = None
    ordered_at: datetime | None = None
    expected_at: datetime | None = None
    notes: str = ""
    created_by: int | None = None

    def is_fully_received(self) -> bool:
        return all(item.pending_quantity <= 0 for item in self.items)


@dataclass(frozen=True)
class PurchaseReceiptItem:
    item_id: int
    quantity: Decimal
    unit_cost: Decimal
    lot_code: str | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True)
class PurchaseReceipt:
    id: int | None
    supplier_party_id: int
    items: tuple[PurchaseReceiptItem, ...]
    purchase_order_id: int | None = None
    status: PurchaseReceiptStatus = PurchaseReceiptStatus.DRAFT
    received_at: datetime | None = None
    document_reference: str | None = None
    created_by: int | None = None
