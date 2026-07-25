"""Translate between LibraCommerce sales and LibraEdge sync operations."""

import sqlite3
from decimal import Decimal

from libracommerce.domain.catalog import CatalogItemType
from libracommerce.domain.sales import Sale, SaleItem, SaleStatus


def sale_to_edge_operation(sale: Sale, node_id: str, sequence: int, occurred_at: str):
    """Build a LibraEdge operation without making LibraEdge know commerce rules."""
    if sale.status != SaleStatus.CONFIRMED:
        raise ValueError("solo se pueden sincronizar ventas confirmadas")
    try:
        from libraedge.domain.sync import OutboxOperation
    except ImportError as exc:
        raise RuntimeError(
            "Instalar la dependencia opcional libraedge para usar este adaptador"
        ) from exc
    payload = {
        "sale_id": sale.id, "number": sale.number, "total": str(sale.total),
        "status": str(sale.status), "branch_id": sale.branch_id,
        "register_id": sale.register_id,
        "items": [
            {
                "kind": str(line.kind), "item_id": line.item_id,
                "description_snapshot": line.description_snapshot,
                "quantity": str(line.quantity), "unit_price": str(line.unit_price),
                "discount_amount": str(line.discount_amount),
                "tax_rate": str(line.tax_rate), "tax_amount": str(line.tax_amount),
                "unit_cost_snapshot": (
                    str(line.unit_cost_snapshot)
                    if line.unit_cost_snapshot is not None else None
                ),
            }
            for line in sale.items
        ],
    }
    return OutboxOperation(
        operation_id=f"{node_id}:{sequence}", node_id=node_id, sequence=sequence,
        operation_type="sale.confirmed", aggregate_type="sale",
        aggregate_id=f"{node_id}:sale:{sale.id}", occurred_at=occurred_at,
        schema_version=1, payload=payload,
    )


def apply_confirmed_sale_operation(conn: sqlite3.Connection, operation) -> None:
    """Central-side handler: materialize a `sale.confirmed` LibraEdge operation.

    Meant to be passed as `operation_handler` to `libraedge.sync.receiver.SyncReceiver`.
    LibraEdge only knows it received a generic operation; only LibraCommerce knows
    how to turn a `sale.confirmed` payload back into a real `Sale`.
    """
    if operation.operation_type != "sale.confirmed":
        return
    data = operation.payload
    if "items" not in data:
        return
    from libracommerce.db.repository import SqliteCommerceRepository

    items = tuple(
        SaleItem(
            kind=CatalogItemType(item["kind"]), item_id=item["item_id"],
            description_snapshot=item["description_snapshot"],
            quantity=Decimal(item["quantity"]),
            unit_price=Decimal(item["unit_price"]),
            discount_amount=Decimal(item["discount_amount"]),
            tax_rate=Decimal(item["tax_rate"]),
            tax_amount=Decimal(item["tax_amount"]),
            unit_cost_snapshot=(
                Decimal(item["unit_cost_snapshot"])
                if item["unit_cost_snapshot"] is not None else None
            ),
        )
        for item in data["items"]
    )
    sale = Sale(
        id=None, number=data["number"], items=items, status=SaleStatus.CONFIRMED,
        branch_id=data.get("branch_id"), register_id=data.get("register_id"),
        source_type=f"offline:{operation.node_id}", source_id=data["sale_id"],
        total=Decimal(data["total"]),
    )
    SqliteCommerceRepository(conn).save_sale(sale)
