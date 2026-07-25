"""Offline sync now delegates persistence/transport/reception to LibraEdge.

These tests only exercise the LibraCommerce side of the integration
(translation + central-side handler); LibraEdge's own outbox/worker/receiver
behavior is tested in its own repository. All of them require the optional
`libraedge` dependency and are skipped cleanly when it isn't installed.
"""

import sqlite3
from decimal import Decimal

import pytest

from libracommerce.db.repository import SqliteCommerceRepository
from libracommerce.db.schema import init_schema
from libracommerce.domain.catalog import CatalogItemType
from libracommerce.domain.sales import Sale, SaleItem, SaleStatus
from libracommerce.integrations.libraedge import (
    apply_confirmed_sale_operation,
    sale_to_edge_operation,
)

libraedge = pytest.importorskip("libraedge")


def _node_repo():
    from libraedge.db.repository import SqliteNodeRepository
    from libraedge.db.schema import init_schema as init_edge_schema

    conn = sqlite3.connect(":memory:")
    init_edge_schema(conn)
    return SqliteNodeRepository(conn)


def test_libraedge_adapter_translates_confirmed_sale():
    sale = Sale(
        None, "V-EDGE",
        (SaleItem(CatalogItemType.SERVICE, "Consulta", Decimal("1"), Decimal("300")),),
        status=SaleStatus.CONFIRMED, total=Decimal("300"),
    )
    event = sale_to_edge_operation(sale, "node-1", 7, "2026-07-25T19:00:00Z")
    assert event.operation_id == "node-1:7"
    assert event.payload["items"][0]["description_snapshot"] == "Consulta"


def test_confirmed_sale_enqueues_into_libraedge_and_appears_pending():
    local_conn = sqlite3.connect(":memory:")
    init_schema(local_conn)
    local = SqliteCommerceRepository(local_conn)
    sale = Sale(
        None, "OFF-0001",
        (SaleItem(CatalogItemType.SERVICE, "Servicio", Decimal("1"), Decimal("100")),),
        status=SaleStatus.CONFIRMED, total=Decimal("100"),
    )
    saved = local.save_sale(sale)

    edge = _node_repo()
    sequence = edge.next_sequence()
    operation = sale_to_edge_operation(saved, "node-1", sequence, "2026-07-25T18:30:00Z")
    enqueued = edge.enqueue_operation(operation)

    assert enqueued.operation_id == "node-1:1"
    assert len(edge.list_pending_operations()) == 1


def test_receiver_with_libracommerce_handler_applies_confirmed_sale_once():
    local_conn = sqlite3.connect(":memory:")
    init_schema(local_conn)
    local = SqliteCommerceRepository(local_conn)
    sale = Sale(
        None, "OFF-APPLY",
        (SaleItem(CatalogItemType.SERVICE, "Consulta", Decimal("1"), Decimal("250")),),
        status=SaleStatus.CONFIRMED, total=Decimal("250"),
    )
    saved = local.save_sale(sale)
    operation = sale_to_edge_operation(saved, "node-apply", 1, "2026-07-25T18:30:00Z")

    from libraedge.db.schema import init_schema as init_edge_schema
    from libraedge.sync.receiver import SyncReceiver

    central_conn = sqlite3.connect(":memory:")
    init_schema(central_conn)
    init_edge_schema(central_conn)
    central = SqliteCommerceRepository(central_conn)
    receiver = SyncReceiver(
        central_conn, operation_handler=lambda op: apply_confirmed_sale_operation(central_conn, op)
    )

    assert receiver.accept(operation).result == "accepted"
    central_id = central_conn.execute(
        "SELECT id FROM sales WHERE number = 'OFF-APPLY'"
    ).fetchone()[0]
    central_sale = central.get_sale(central_id)
    assert central_sale.source_type == "offline:node-apply"
    assert central_sale.items[0].description_snapshot == "Consulta"

    assert receiver.accept(operation).result == "duplicate"
    assert central_conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 1
