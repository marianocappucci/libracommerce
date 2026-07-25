"""Translate LibraCommerce sales into LibraEdge operations."""

from libracommerce.domain.sales import Sale, SaleStatus


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
