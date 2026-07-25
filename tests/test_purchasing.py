from decimal import Decimal

from libracommerce.domain.purchasing import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
    PurchaseReceipt,
    PurchaseReceiptItem,
    PurchaseReceiptStatus,
)


def test_purchase_order_defaults_to_draft():
    order = PurchaseOrder(None, "OC-1", supplier_party_id=1, items=())
    assert order.status == PurchaseOrderStatus.DRAFT
    assert order.is_fully_received()


def test_purchase_order_item_tracks_pending_quantity():
    line = PurchaseOrderItem(item_id=1, quantity_ordered=Decimal("10"), unit_cost=Decimal("100"))
    assert line.subtotal == Decimal("1000")
    assert line.pending_quantity == Decimal("10")

    partially_received = PurchaseOrderItem(
        item_id=1, quantity_ordered=Decimal("10"), unit_cost=Decimal("100"), quantity_received=Decimal("4")
    )
    assert partially_received.pending_quantity == Decimal("6")


def test_purchase_order_is_fully_received_only_when_all_lines_are_closed():
    fully = PurchaseOrderItem(
        item_id=1, quantity_ordered=Decimal("10"), unit_cost=Decimal("100"), quantity_received=Decimal("10")
    )
    pending = PurchaseOrderItem(item_id=2, quantity_ordered=Decimal("5"), unit_cost=Decimal("50"))

    order = PurchaseOrder(None, "OC-1", supplier_party_id=1, items=(fully,))
    assert order.is_fully_received()

    order_with_pending = PurchaseOrder(None, "OC-2", supplier_party_id=1, items=(fully, pending))
    assert not order_with_pending.is_fully_received()


def test_purchase_receipt_defaults_to_draft_and_carries_lot_data():
    line = PurchaseReceiptItem(item_id=1, quantity=Decimal("10"), unit_cost=Decimal("95"), lot_code="L-2026-01")
    receipt = PurchaseReceipt(None, supplier_party_id=1, items=(line,))
    assert receipt.status == PurchaseReceiptStatus.DRAFT
    assert receipt.items[0].lot_code == "L-2026-01"
