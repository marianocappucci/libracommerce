"""Orchestrate what confirming a purchase receipt means.

"La recepcion es la operacion que genera inventario y actualiza el costo,
no la orden de compra" (arquitectura-familia-libra-alcance.md). This use
case is the one place that turns a confirmed receipt into stock movements,
a catalog cost update, and progress on the linked purchase order, if any.
"""

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from libracommerce.domain.inventory import StockMovement, StockMovementType
from libracommerce.domain.purchasing import PurchaseOrderStatus, PurchaseReceipt, PurchaseReceiptStatus
from libracommerce.ports.persistence import CommerceRepository


def confirm_purchase_receipt(
    repo: CommerceRepository, receipt: PurchaseReceipt, location_id: int, occurred_at: datetime
) -> PurchaseReceipt:
    """Confirm a draft receipt: inbound stock movements, last-cost update per
    item, and (if linked to a purchase order) received-quantity/status sync.

    Cost update uses the received unit_cost as the new default_cost — a
    last-cost strategy, not a weighted average; nothing in the
    architecture spec calls for the latter.
    """
    if receipt.status != PurchaseReceiptStatus.DRAFT:
        raise ValueError(
            f"Solo se puede confirmar una recepcion en estado draft (actual: {receipt.status})"
        )
    saved = repo.save_purchase_receipt(
        replace(receipt, status=PurchaseReceiptStatus.CONFIRMED, received_at=occurred_at)
    )

    for line in saved.items:
        repo.append_stock_movement(
            StockMovement(
                id=None,
                item_id=line.item_id,
                location_id=location_id,
                movement_type=StockMovementType.PURCHASE,
                quantity_delta=line.quantity,
                occurred_at=occurred_at,
                source_type="purchase_receipt",
                source_id=saved.id,
                unit_cost=line.unit_cost,
                lot_code=line.lot_code,
                expires_at=line.expires_at,
            )
        )
        item = repo.get_catalog_item(line.item_id)
        if item is not None:
            repo.save_catalog_item(replace(item, default_cost=line.unit_cost))

    if saved.purchase_order_id is not None:
        order = repo.get_purchase_order(saved.purchase_order_id)
        if order is not None:
            received_by_item: dict[int, Decimal] = {}
            for line in saved.items:
                received_by_item[line.item_id] = (
                    received_by_item.get(line.item_id, Decimal("0")) + line.quantity
                )
            updated_items = tuple(
                replace(
                    order_item,
                    quantity_received=order_item.quantity_received
                    + received_by_item.get(order_item.item_id, Decimal("0")),
                )
                for order_item in order.items
            )
            updated_order = replace(order, items=updated_items)
            updated_order = replace(
                updated_order,
                status=(
                    PurchaseOrderStatus.RECEIVED
                    if updated_order.is_fully_received()
                    else PurchaseOrderStatus.PARTIAL
                ),
            )
            repo.save_purchase_order(updated_order)

    return saved
