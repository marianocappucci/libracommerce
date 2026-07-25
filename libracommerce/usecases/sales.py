"""Orchestrate what a confirmed sale means beyond persisting the sale row.

Cash/register movements are deliberately out of scope here: caja lives in
LibraCore's bounded context (see arquitectura-familia-libra-alcance.md),
not LibraCommerce's. This use case only owns the stock side.
"""

from dataclasses import replace
from datetime import datetime

from libracommerce.domain.catalog import CatalogItemType
from libracommerce.domain.inventory import StockMovement, StockMovementType
from libracommerce.domain.sales import Sale, SaleStatus
from libracommerce.ports.persistence import CommerceRepository


def confirm_sale(
    repo: CommerceRepository, sale: Sale, location_id: int, occurred_at: datetime
) -> Sale:
    """Confirm a draft sale and append an outbound stock movement per product line.

    Service lines never move stock. Persists the sale first so stock
    movements can reference its id as source_id, whether the sale already
    existed or is being confirmed on first save.
    """
    if sale.status != SaleStatus.DRAFT:
        raise ValueError(
            f"Solo se puede confirmar una venta en estado draft (actual: {sale.status})"
        )
    saved = repo.save_sale(replace(sale, status=SaleStatus.CONFIRMED, confirmed_at=occurred_at))
    for line in saved.items:
        if line.kind != CatalogItemType.PRODUCT:
            continue
        repo.append_stock_movement(
            StockMovement(
                id=None,
                item_id=line.item_id,
                location_id=location_id,
                movement_type=StockMovementType.SALE,
                quantity_delta=-line.quantity,
                occurred_at=occurred_at,
                source_type="sale",
                source_id=saved.id,
            )
        )
    return saved
