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
class SalePayment:
    """Un cobro de la venta. Son varios cuando el cliente paga mixto — parte
    en efectivo, parte con tarjeta.

    `received_amount` es cuánto entregó el cliente cuando paga en efectivo y
    hay que darle vuelto. Se guarda para que un arqueo con diferencia se
    pueda reconstruir después; en los demás medios va None, porque no existe
    el concepto de entregar de más.
    """

    method: str
    amount: Decimal
    received_amount: Decimal | None = None
    reference: str = ""

    def __post_init__(self):
        if self.amount <= 0:
            raise ValueError("El monto de un pago debe ser mayor que cero.")
        if self.received_amount is not None and self.received_amount < self.amount:
            raise ValueError(
                "Lo recibido no puede ser menor que el monto del pago: "
                f"recibido={self.received_amount}, monto={self.amount}."
            )

    @property
    def change(self) -> Decimal:
        """Vuelto de este pago. Cero cuando no se registró lo recibido, que
        es el caso de todo medio que no sea efectivo."""
        if self.received_amount is None:
            return Decimal("0")
        return self.received_amount - self.amount


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
    occurred_on: str | None = None
    customer_name_snapshot: str = ""
    created_by: int | None = None
    notes: str = ""
    status_detail: str | None = None
    # Va al final a proposito: agregarlo entre los campos existentes correria
    # el orden posicional del dataclass y le cambiaria el significado a
    # cualquier consumidor que construya Sale() sin keywords.
    payments: tuple[SalePayment, ...] = ()

    def calculated_total(self) -> Decimal:
        return sum((item.line_total for item in self.items), Decimal("0"))

    def paid_total(self) -> Decimal:
        """Suma de los cobros registrados. No incluye el vuelto: lo que el
        cliente entregó de más no es plata de la venta."""
        return sum((payment.amount for payment in self.payments), Decimal("0"))

    def change_due(self) -> Decimal:
        """Vuelto total a devolver."""
        return sum((payment.change for payment in self.payments), Decimal("0"))

    def is_fully_paid(self) -> bool:
        return self.payments != () and self.paid_total() >= self.total
