from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class SaleConfirmedEvent:
    sale_id: int
    total: Decimal
    payment_reference: str | None = None


class CommerceEventPublisher(Protocol):
    def publish_sale_confirmed(self, event: SaleConfirmedEvent) -> None: ...
