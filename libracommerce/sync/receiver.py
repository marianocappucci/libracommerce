"""Central-side idempotent receiver for synchronization operations."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from decimal import Decimal

from libracommerce.domain.catalog import CatalogItemType
from libracommerce.domain.sales import Sale, SaleItem, SaleStatus
from libracommerce.domain.sync import OutboxOperation
from libracommerce.db.repository import SqliteCommerceRepository
from libracommerce.sync.worker import PushResult


@dataclass(frozen=True)
class SyncReceiver:
    conn: sqlite3.Connection
    supported_schema_version: int = 1

    def _apply(self, operation: OutboxOperation) -> None:
        if operation.operation_type != "sale.confirmed":
            return
        data = operation.payload
        if "items" not in data:
            return
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
        SqliteCommerceRepository(self.conn).save_sale(sale)

    def accept(self, operation: OutboxOperation) -> PushResult:
        if operation.schema_version != self.supported_schema_version:
            return PushResult("rejected", "schema incompatible")
        existing = self.conn.execute(
            "SELECT status FROM sync_inbox WHERE operation_id = ?",
            (operation.operation_id,),
        ).fetchone()
        if existing is not None:
            return PushResult("duplicate")
        try:
            self._apply(operation)
            self.conn.execute(
                """INSERT INTO sync_inbox (operation_id, applied_at, status)
                   VALUES (?, ?, 'applied')""",
                (operation.operation_id, datetime.now(timezone.utc).isoformat()),
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            self.conn.rollback()
            return PushResult("duplicate")
        except (KeyError, TypeError, ValueError) as exc:
            self.conn.rollback()
            return PushResult("rejected", str(exc))
        return PushResult("accepted")
